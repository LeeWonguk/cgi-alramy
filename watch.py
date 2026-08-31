#!/usr/bin/env python3
"""CGV 예매 가능 날짜 추가 알림기 — CGV 접근과 비교 로직.

DB(store)에 등록된 영화×극장 조합의 예매 가능 날짜를 확인해, 이전 확인
때보다 날짜가 늘어났으면 웹훅(Slack·Discord)으로 알린다.

CGV는 Cloudflare가 TLS 지문 단위로 봇을 막기 때문에 requests/curl로는
헤더를 완벽히 맞춰도 403이 떨어진다. 그래서 Chromium을 실제로 띄우고
페이지 컨텍스트 안에서 same-origin fetch로 내부 API를 호출한다.

이 모듈은 CLI로도 쓰지만(`--once`), 상시 동작은 web/poller.py가 브라우저
세션을 상주시키며 check_all()을 반복 호출하는 쪽이다.
"""

from __future__ import annotations

from collections import OrderedDict
import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import store
from envfile import load_env

# ── CGV 내부 API ────────────────────────────────────────────────────────────
# 비공개 API라 사이트 개편 시 바뀔 수 있다. 변경 지점을 여기 한 곳에 모아둔다.
CO_CD = "A420"  # CGV 고정값
BASE_URL = "https://cgv.co.kr"
BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/movie"

EP_MOVIES = f"/api/v1/booking/searchAtktTopPostrList?coCd={CO_CD}&movNm=&div=&attrCd="
EP_SITES = f"/api/v1/content/site/searchAllRegionAndSite?coCd={CO_CD}"
EP_DATES = (
    "/api/v1/booking/searchSiteScnscYmdListByMov"
    f"?coCd={CO_CD}&siteNo={{site_no}}&movNo={{mov_no}}"
)
EP_SCHEDULE = (
    "/api/v1/booking/searchSchByMov"
    f"?coCd={CO_CD}&siteNo={{site_no}}&scnYmd={{ymd}}&movNo={{mov_no}}&rtctlScopCd=08"
)
# 좌석 배치도. CGV 웹은 api.cgv.co.kr/cnm/atkt/searchIfSeatData를 부르지만,
# 페이지의 URL 매퍼가 /cnm/atkt → /api/v1/booking으로 바꿔 같은 오리진(cgv.co.kr)
# BFF로 보낸다. 그래서 우리도 같은 오리진 경로로 부르며, 로그인 쿠키(accessToken)를
# 자동으로 실어 보낸다. 로그인 안 된 상태면 401이 떨어진다.
EP_SEAT = (
    "/api/v1/booking/searchIfSeatData"
    f"?coCd={CO_CD}&siteNo={{site_no}}&scnsNo={{scns_no}}"
    "&scnYmd={ymd}&scnSseq={scn_sseq}"
)

# ── CGV 로그인 (계정 세션) ──────────────────────────────────────────────────
# 로그인은 cgv.co.kr/mem/login에서 이뤄진다. 비밀번호 암호화·바디 구성은 페이지의
# 자체 JS가 하므로, 우리는 폼을 채우고 제출만 한다. 화면의 숫자 캡차는 canvas에
# 클라이언트가 fillText로 그리고 클라이언트가 검증하며(서버 요청에 캡차 필드가
# 없다), 우리는 fillText를 후킹해 그려지는 숫자를 그대로 읽어 입력한다.
LOGIN_PAGE_URL = f"{BASE_URL}/mem/login"
REFRESH_URL = "https://oidc.cgv.co.kr/common/auth/refreshtoken"
# 로그인 성공 시 발급돼 세션을 이루는 쿠키들. 저장·복원할 때 이만큼을 다룬다.
SESSION_COOKIES = ("accessToken", "refresh_token", "cjssoq",
                   "CJONE_SSO", "CJONE_SSO_SYS")
# canvas의 fillText를 후킹해 캡차 숫자를 모으는 스크립트. 페이지 JS보다 먼저
# 심어야 하므로 add_init_script로 넣는다.
_CAPTCHA_HOOK = """
window.__cap = [];
const __origFillText = CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText = function (t) {
    try {
        if (typeof t === 'string' && t.length === 1 && /[0-9]/.test(t))
            window.__cap.push(t);
    } catch (e) {}
    return __origFillText.apply(this, arguments);
};
"""

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 웹훅 전송용 User-Agent. Discord 앞단 Cloudflare가 파이썬 기본 UA
# (Python-urllib/x.y)를 봇으로 보고 403 error code 1010으로 막으므로, 우리를
# 식별하는 UA를 명시한다. Slack도 이 헤더가 있어 문제되지 않는다.
WEBHOOK_UA = "cgv-watch/1.0 (+https://github.com/LeeWonguk/cgi-alramy)"

# ── 경로 ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / ".watch.lock"
LOG_PATH = ROOT / "logs" / "watch.log"

FAIL_ALERT_THRESHOLD = 3  # 연속 실패 이 횟수부터 웹훅 경고

# 예매 화면을 미리 띄워 둘 탭의 최대 개수.
#
# **모자라면 조용히 손해가 난다.** 감시하는 (영화·극장·날짜) 조합이 이 수를 넘으면
# LRU가 매 사이클 탭을 갈아치우고, 그때마다 딥링크를 다시 연다 — 실측으로 6개
# 조합에 상한이 4일 때 사이클마다 5번을 다시 열어 4.2초를 썼다. 프리워밍은
# "이미 열려 있으면 0초"라서 하는 것인데, 정확히 그 이득이 사라진다.
#
# 그래서 넉넉히 잡고, 그래도 넘치면 로그로 알린다(_evict_booking_page).
# 날짜를 2주치 걸어도 덮을 만한 수다.
BOOKING_PAGE_LIMIT = 12

# 동시에 살려 둘 소유자 공간(BrowserContext)의 최대 개수. 컨텍스트는 브라우저
# 프로세스를 공유하므로 탭 몇 개보다 조금 무거운 정도지만, 사용자가 늘어도
# 무한정 쌓이면 안 된다. 넘치면 가장 오래 안 쓴 쪽을 닫고, 그 소유자는 다음
# 차례에 다시 만들어 로그인한다(= 지금과 같은 비용으로 되돌아갈 뿐이다).
OWNER_SPACE_LIMIT = 4

# 좌석맵을 한 번에 묶어 받을 개수. 두 가지를 함께 막는다:
#   · 한 프로토콜 메시지가 커지는 것 — 624석 IMAX 한 건이 깎고도 214KB다
#   · CGV로 나가는 요청이 한꺼번에 터지는 것 — 부하는 계속 신경 써 왔다
# 한 번에 묶어 보낼 개수.
#
# 16까지 올렸다가 CGV에서 429(Too Many Requests)를 받고 되돌렸다. 사이클을
# 11.8초에서 2.2초로 줄이면서 폴링 3초와 겹쳐, 요청이 초당 3.4건에서 13.7건으로
# 뛴 상태였다 — 거기에 16건이 한꺼번에 나갔다. **빨라진 만큼 상대에게 부담이
# 간다**는 걸 이 숫자로 배웠다.
SEAT_MAP_BATCH = 6

# 429를 받으면 이만큼 쉰다. CGV가 그만하라고 한 것이므로 재시도로 맞서지 않는다.
THROTTLE_BACKOFF_SECONDS = 60
WEEKDAYS = "월화수목금토일"

log = logging.getLogger("cgv-watch")


# ── 설정 / 상태 ─────────────────────────────────────────────────────────────
def setup_logging(verbose: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)

    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers.clear()  # 진입점이 여러 개라 두 번 불릴 수 있다
    log.addHandler(file_handler)
    log.addHandler(stream)
    # 루트로 올려보내지 않는다. 의존 패키지 중 하나가 logging.basicConfig()를
    # 부르면 루트에 핸들러가 붙어 같은 줄이 두 번 찍힌다.
    log.propagate = False


@contextmanager
def single_instance():
    """확인이 겹치지 않게 막는다.

    상시 동작하는 서버와 수동으로 띄운 `--once`가 같은 순간에 돌면, 늦게 끝난
    쪽이 앞의 결과를 덮어써 그 사이에 열린 날짜를 영영 놓친다. 서버는 락을
    사이클 단위로만 잡으므로 CLI도 그대로 쓸 수 있다.
    """
    fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ── 포맷 헬퍼 ───────────────────────────────────────────────────────────────
def fmt_date(ymd: str) -> str:
    """'20260812' -> '8/12(수)'"""
    try:
        d = datetime.strptime(ymd, "%Y%m%d")
    except ValueError:
        return ymd
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def fmt_time(hhmm: str) -> str:
    """'1400' -> '14:00'. CGV는 심야를 '2525'(=다음날 01:25)로 주는데 그대로 표기한다."""
    return f"{hhmm[:2]}:{hhmm[2:]}" if hhmm and len(hhmm) == 4 else (hhmm or "")


def normalize(name: str) -> str:
    """이름 비교용 정규화 — 공백/대소문자/CGV 접두 무시."""
    stripped = "".join(name.split())  # 일반/비분리 공백 모두 제거
    return stripped.lower().removeprefix("cgv")


# ── CGV 세션 ────────────────────────────────────────────────────────────────
class Throttled(RuntimeError):
    """CGV가 429로 요청을 거절했다 — 잠시 물러나야 한다.

    재시도로 맞서면 안 된다. 예전에는 get_json이 2초·4초 백오프로 세 번을 더
    보냈는데, 그건 그만하라는 쪽에 세 배로 때리는 짓이다.
    """


class AuthRequired(RuntimeError):
    """CGV가 401을 냈다 — 로그인이 끊겼으니 세션을 되살려야 한다.

    RuntimeError를 상속하므로 기존의 `except RuntimeError` 처리는 그대로
    동작한다. 되살릴 수 있는 호출자만 이 타입을 따로 잡으면 된다.
    """


class _OwnerSpace:
    """소유자 한 명의 작업 공간 — BrowserContext 하나와 그 안의 탭들.

    **왜 소유자마다 컨텍스트를 따로 두는가.** 예전에는 컨텍스트가 하나뿐이라,
    사용자가 둘이면 사이클마다 쿠키를 비우고 다시 로그인하기를 반복했다. 값이
    비싼 건 로그인만이 아니다 — 주인이 바뀔 때마다 **미리 띄워 둔 예매 탭을 전부
    닫아야 했다**(앞사람 화면으로 선점하면 안 되므로). 그래서 사용자가 둘 이상이면
    프리워밍이 한 번도 살아남지 못하고, 선점마다 딥링크 6.2초를 다시 물었다.

    컨텍스트는 쿠키 저장소가 서로 분리돼 있다. 그래서 공간을 나누면 로그인이
    각자 유지되고, 탭도 각자 살아 있고, **남의 계정으로 선점할 여지 자체가
    구조적으로 없어진다** — 규율이 아니라 격리로 막는다.
    """

    def __init__(self, context, page):
        self.context = context
        self.page = page
        # 이 공간이 누구의 CGV 계정으로 로그인돼 있는지. 값을 넣는 곳은
        # cgv_login 하나뿐이다(mark_logged_in).
        self.logged_in_owner: int | None = None
        # 예매 화면을 미리 띄워 둔 탭들 (키: 영화·극장·날짜).
        self.booking_pages: "OrderedDict[str, object]" = OrderedDict()

    def close(self) -> None:
        try:
            self.context.close()        # 안의 탭들도 함께 닫힌다
        except Exception:  # noqa: BLE001 - 정리 중 실패는 무시
            pass
        self.booking_pages.clear()


class CgvSession:
    """Chromium을 띄워 CGV 내부 API를 호출하는 세션.

    Cloudflare 쿠키를 얻기 위해 먼저 홈페이지를 방문하고, 이후 요청은
    페이지 컨텍스트 안에서 same-origin fetch로 보낸다.

    브라우저는 하나지만 **작업 공간은 소유자마다 따로** 둔다(_OwnerSpace).
    use(owner_id)로 공간을 고르면 그 뒤의 모든 호출이 그 공간에서 일어난다.
    """

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._pw = None
        self._browser = None
        # 소유자별 작업 공간. 키는 owner_id(로그인이 필요 없는 일은 None).
        # 가장 오래 안 쓴 것부터 닫으려고 순서를 지키는 dict를 쓴다.
        self._spaces: "OrderedDict[int | None, _OwnerSpace]" = OrderedDict()
        self._current: int | None = None
        self.requests = 0  # 사이클당 CGV 요청 수 — 대시보드에서 부하를 본다
        self.opened_at: datetime | None = None
        # 429를 받으면 이 시각까지 요청을 보내지 않는다. 브라우저 하나를
        # 공유하므로 소유자와 무관하게 세션 전체가 함께 쉰다 — 상대 쪽에서
        # 보면 우리는 한 곳이다.
        self._throttled_until = 0.0

    def __enter__(self) -> "CgvSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()

        # Playwright의 headless=True는 chrome-headless-shell 바이너리를 찾는데
        # 그게 없는 환경이 많다. 정식 Chromium을 신형 headless 모드로 띄운다.
        args = ["--disable-gpu"]
        if self._headless:
            args.insert(0, "--headless=new")

        self._browser = self._pw.chromium.launch(headless=False, args=args)
        try:
            self._spaces[None] = self._new_space()
        except Exception as exc:  # noqa: BLE001
            self.__exit__(None, None, None)
            raise RuntimeError(f"CGV 접속 실패: {exc}") from exc
        self._current = None
        self.opened_at = datetime.now().astimezone()
        return self

    def _new_space(self) -> "_OwnerSpace":
        """컨텍스트를 새로 만들고 CGV 홈을 한 번 방문한다.

        **홈 방문을 건너뛸 수 없다.** 컨텍스트는 쿠키가 비어 있어 Cloudflare
        통과 흔적이 없고, 그 상태로 API를 부르면 403이 떨어진다. 공간을 만들
        때 딱 한 번 무는 비용이다.
        """
        if self._browser is None:
            raise RuntimeError("세션이 열려 있지 않습니다")
        context = self._browser.new_context(locale="ko-KR", user_agent=CHROME_UA)
        page = context.new_page()

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = page.goto(BASE_URL, wait_until="domcontentloaded",
                                 timeout=45_000)
                if resp and resp.status == 200:
                    log.debug("CGV 홈 접속 성공 (%d번째 시도)", attempt + 1)
                    return _OwnerSpace(context, page)
                last_exc = RuntimeError(
                    f"CGV 홈 응답 코드 {resp.status if resp else 'None'}"
                )
            except Exception as exc:  # noqa: BLE001 - playwright 예외 종류가 다양
                last_exc = exc
            log.warning("CGV 홈 접속 실패 (%d/3): %s", attempt + 1, last_exc)
            time.sleep(2 * (attempt + 1))

        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"{last_exc}")

    # ── 소유자별 공간 ────────────────────────────────────────────────────
    def use(self, owner_id: int | None) -> None:
        """이 뒤의 호출을 그 소유자의 공간에서 한다. 없으면 만든다.

        이미 그 공간이면 아무 일도 하지 않는다 — 사이클마다 불리는 자리라
        '바꾸지 않는 전환'이 공짜여야 한다.
        """
        if self._current == owner_id and owner_id in self._spaces:
            return
        space = self._spaces.get(owner_id)
        if space is not None:
            try:
                space.page.evaluate("() => 1")      # 살아 있는지 확인
                self._spaces.move_to_end(owner_id)
                self._current = owner_id
                return
            except Exception:  # noqa: BLE001 - 죽었으면 새로 만든다
                space.close()
                self._spaces.pop(owner_id, None)

        # 넘치면 가장 오래 안 쓴 것부터 닫는다. 기본 공간(None)은 로그인 없는
        # 일들이 쓰므로 남긴다 — 이걸 닫으면 날짜 감시가 매번 홈부터 다시 연다.
        while len(self._spaces) >= OWNER_SPACE_LIMIT:
            victim = next((k for k in self._spaces if k is not None), None)
            if victim is None:
                break
            log.info("소유자 공간이 가득 차 owner %s의 것을 닫습니다", victim)
            self._spaces.pop(victim).close()

        self._spaces[owner_id] = self._new_space()
        self._current = owner_id

    @property
    def _space(self) -> "_OwnerSpace":
        space = self._spaces.get(self._current)
        if space is None:
            raise RuntimeError("세션이 열려 있지 않습니다")
        return space

    @property
    def _page(self):
        """지금 공간의 기본 페이지. 예전 코드가 그대로 쓰도록 이름을 지킨다."""
        return self._space.page

    @property
    def logged_in_owner(self) -> int | None:
        return self._space.logged_in_owner

    @logged_in_owner.setter
    def logged_in_owner(self, value: int | None) -> None:
        self._space.logged_in_owner = value

    def __exit__(self, *_exc) -> None:
        for space in self._spaces.values():
            space.close()
        self._spaces.clear()
        self._current = None
        for closer in (self._browser, self._pw):
            if closer is None:
                continue
            try:
                closer.close() if closer is self._browser else closer.stop()
            except Exception:  # noqa: BLE001 - 정리 중 실패는 무시
                pass
        self._browser = self._pw = None

    @property
    def page(self):
        """살아 있는 Playwright 페이지. UI를 직접 몰아야 하는 쪽(booking)이 쓴다."""
        if self._page is None:
            raise RuntimeError("세션이 열려 있지 않습니다")
        return self._page

    # ── 예매 화면을 미리 띄워 두는 탭 ────────────────────────────────────────
    def booking_page(self, key: str):
        """그 조합(영화·극장·날짜)의 예매 화면 전용 탭. 없으면 새로 연다.

        예매 화면을 딥링크로 **새로 여는 데만 6.2초**가 든다(회차 목록이 그려질
        때까지). 좌석이 난 순간 그 6.2초를 쓰면 경쟁에서 밀리므로, 자동 예매를 켠
        감시의 화면을 미리 띄워 두고 그 탭을 계속 쓴다 — 이미 그 화면이면 0초다.

        좌석 확인은 이 탭이 아니라 기본 페이지에서 fetch로 하므로(get_json), 탭을
        띄워 둬도 감시에는 영향이 없다.
        """
        space = self._space
        tabs = space.booking_pages
        page = tabs.get(key)
        if page is not None:
            try:
                page.evaluate("() => 1")           # 살아 있는지 확인
                tabs.move_to_end(key)
                return page
            except Exception:  # noqa: BLE001 - 닫혔으면 새로 연다
                tabs.pop(key, None)
        # 탭이 무한정 늘지 않게 오래된 것부터 닫는다. 이 한도는 **소유자마다** 따로다.
        while len(tabs) >= BOOKING_PAGE_LIMIT:
            victim, old = tabs.popitem(last=False)
            # 조용히 넘어가면 안 된다. 여기 걸린다는 건 매 사이클 탭을 갈아치우고
            # 있다는 뜻이고, 그러면 프리워밍이 통째로 무의미해진다.
            log.warning("예매 화면 탭이 가득 차 %s를 닫습니다 — 감시하는 조합이 "
                        "%d개를 넘었습니다. 매 사이클 화면을 다시 열게 되니 "
                        "BOOKING_PAGE_LIMIT을 올리는 편이 좋습니다.",
                        victim, BOOKING_PAGE_LIMIT)
            try:
                old.close()
            except Exception:  # noqa: BLE001 - 이미 닫혔을 수 있다
                pass
        page = space.context.new_page()
        tabs[key] = page
        return page

    def close_booking_pages(self) -> None:
        """지금 공간에 미리 띄워 둔 예매 탭을 모두 닫는다.

        예전에는 **로그인 주인이 바뀔 때마다** 이걸 불러야 했다 — 컨텍스트가
        하나뿐이라 그 탭들이 앞사람의 화면이었기 때문이다. 지금은 공간이 소유자
        단위로 갈려 있어 그럴 일이 없고, 이 함수는 한 소유자의 화면을 일부러
        버릴 때만 쓴다.
        """
        tabs = self._space.booking_pages
        for page in tabs.values():
            try:
                page.close()
            except Exception:  # noqa: BLE001 - 정리 중 실패는 무시
                pass
        tabs.clear()

    def is_alive(self) -> bool:
        """페이지가 아직 살아 있는지. 브라우저 상주 중 크래시를 감지하는 데 쓴다."""
        if self._page is None:
            return False
        try:
            return self._page.evaluate("() => 1") == 1
        except Exception:  # noqa: BLE001 - 죽었는지 보는 게 목적이다
            return False

    def throttled_for(self) -> float:
        """지금 쉬어야 하는 남은 초. 0이면 요청해도 된다 (호출자용)."""
        return self._throttle_left()

    def _throttle_left(self) -> float:
        """지금 쉬어야 하는 남은 초. 0이면 보내도 된다."""
        return max(0.0, self._throttled_until - time.monotonic())

    def _start_throttle(self, path: str) -> None:
        if self._throttle_left() <= 0:
            log.warning("CGV가 요청을 거절했습니다 (HTTP 429) — %d초 쉽니다. "
                        "확인 간격을 늘리거나 감시 회차를 줄이는 편이 좋습니다 "
                        "(%s)", THROTTLE_BACKOFF_SECONDS, path.split("?")[0])
        self._throttled_until = time.monotonic() + THROTTLE_BACKOFF_SECONDS

    def get_json(self, path: str, retries: int = 2) -> dict:
        """API를 호출해 JSON을 반환. 실패하면 지수 백오프로 재시도."""
        script = """async (path) => {
            const res = await fetch(path, {headers: {'accept': 'application/json'}});
            const text = await res.text();
            return {status: res.status, text: text};
        }"""

        left = self._throttle_left()
        if left > 0:
            raise Throttled(f"{path.split('?')[0]}: CGV가 요청을 거절해 "
                            f"{left:.0f}초 더 쉬는 중입니다")

        last_error = ""
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(2**attempt)
            self.requests += 1
            try:
                out = self._page.evaluate(script, path)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            if out["status"] == 401:
                # 로그인이 끊긴 것이다. 재시도해 봐야 같은 답이 오므로 바로 올려
                # 호출자가 세션을 되살릴 기회를 준다 (cgv_login.recover_session).
                raise AuthRequired(
                    f"{path.split('?')[0]}: 로그인이 필요합니다 (HTTP 401)")
            if out["status"] == 429:
                # 그만하라는 답이다. 재시도로 맞서면 세 배로 때리는 셈이다.
                self._start_throttle(path)
                raise Throttled(f"{path.split('?')[0]}: CGV가 요청을 거절했습니다 "
                                f"(HTTP 429)")
            if out["status"] != 200:
                last_error = f"HTTP {out['status']}"
                continue
            try:
                payload = json.loads(out["text"])
            except json.JSONDecodeError:
                last_error = f"JSON 파싱 실패: {out['text'][:120]}"
                continue
            if payload.get("statusCode") not in (0, "0", None):
                last_error = f"API 오류: {payload.get('statusMessage')}"
                continue
            return payload

        raise RuntimeError(f"{path.split('?')[0]} 조회 실패 — {last_error}")

    def get_json_many(self, paths: list[str], *,
                      seat_fields: list[str] | None = None) -> list[dict | None]:
        """여러 API를 **한꺼번에** 부른다. 길이가 같은 결과 리스트를 돌려준다.

        실패한 항목은 그 자리에 None이 들어간다 — 예외를 내지 않는다. 호출자가
        None인 것만 개별 경로(get_json)로 다시 받으면, 401 복구나 회차별 실패
        처리 같은 기존 로직을 배치가 새로 떠안지 않아도 된다.

        **왜 한꺼번에인가.** 회차마다 따로 부르면 왕복을 줄줄이 기다린다. 배포
        실측으로 좌석맵 32건에 6.2초였고, 그게 사이클 11.8초의 절반이었다.
        브라우저 안에서 Promise.all로 묶으면 서로 다른 URL 8건이 642ms → 65ms다
        (실측 9.8배).

        **같은 URL을 여러 번 넣으면 이득이 없다.** 브라우저가 동일 URL 동시
        요청을 캐시 락으로 직렬화하기 때문이다 — 실측에서 8건이 1.0배로 나왔다.
        좌석맵은 회차마다 URL이 달라서 해당되지 않지만, 다른 데 쓸 때는
        알고 있어야 한다.

        seat_fields를 주면 **브라우저에서 좌석 필드를 깎아** 보낸다. 좌석맵
        원본은 좌석당 39개 필드인데 seats.parse_seats가 읽는 건 17개뿐이라,
        안 깎으면 32건이 17.6MB로 한 번에 넘어와 병렬화 이득을 도로 까먹는다
        (깎으면 6.7MB). **필드 이름은 그대로 두므로 parse_seats 출력은 바뀌지
        않는다.**
        """
        if not paths:
            return []
        if self._throttle_left() > 0:
            # 쉬는 중이면 아예 보내지 않는다. 전부 None이라 호출자는 "못 받았다"로
            # 다루고, 개별 폴백도 같은 이유로 즉시 Throttled를 낸다.
            return [None] * len(paths)

        # eval을 쓰지 않는다 — CGV 페이지의 CSP가 막을 수 있고, 막히면 배치가
        # 통째로 실패한다. 깎는 규칙은 필드 목록으로 넘긴다.
        script = """async (arg) => {
            const keep = arg.seatFields;
            // 좌석을 **값 배열로** 보낸다. 키 이름이 좌석마다(624석 × 회차 수)
            // 반복되는 게 전송 비용의 대부분이었다 — 실측 6.6MB → 1.9MB,
            // 전송 1760ms → 829ms. 파이썬 쪽에서 같은 순서로 되돌린다.
            const slim = (data) => {
                if (!keep || !data || !data.data) return data;
                const items = data.data.items;
                if (!Array.isArray(items)) return data;
                for (const item of items) {
                    if (!Array.isArray(item.seats)) continue;
                    item.seats = item.seats.map(
                        (s) => keep.map((k) => (k in s ? s[k] : null)));
                }
                return data;
            };
            return Promise.all(arg.paths.map(async (p) => {
                try {
                    const res = await fetch(p, {headers: {'accept': 'application/json'}});
                    const text = await res.text();
                    if (res.status !== 200) return {status: res.status};
                    let data;
                    try { data = JSON.parse(text); }
                    catch (e) { return {status: 200, bad: text.slice(0, 120)}; }
                    return {status: 200, json: slim(data)};
                } catch (e) {
                    return {status: 0, error: String(e)};
                }
            }));
        }"""

        out: list[dict | None] = []
        for start in range(0, len(paths), SEAT_MAP_BATCH):
            chunk = paths[start:start + SEAT_MAP_BATCH]
            self.requests += len(chunk)
            try:
                results = self._page.evaluate(
                    script, {"paths": chunk, "seatFields": seat_fields})
            except Exception as exc:  # noqa: BLE001 - 통째로 실패하면 전부 개별로
                log.warning("좌석 데이터를 묶어 받지 못했습니다 (%s) — "
                            "하나씩 받습니다", exc)
                out.extend([None] * len(chunk))
                continue
            throttled = False
            for path, item in zip(chunk, results):
                if item and item.get("status") == 429:
                    self._start_throttle(path)
                    throttled = True
                payload = self._one_of_many(path, item)
                if payload is not None and seat_fields:
                    payload = self._inflate_seats(payload, seat_fields)
                out.append(payload)
            if throttled:
                # 남은 묶음은 보내지 않는다. 거절당한 뒤에도 계속 보내면
                # 그만하라는 쪽을 더 때리는 셈이다.
                out.extend([None] * (len(paths) - len(out)))
                break
        return out

    @staticmethod
    def _inflate_seats(payload: dict, fields: list[str]) -> dict:
        """값 배열로 온 좌석을 원래 모양(dict)으로 되돌린다.

        브라우저가 키 이름을 떼고 보냈으므로 같은 순서로 다시 붙인다. 되돌린
        결과는 원본에서 그 필드만 남긴 것과 **똑같다** — seats.parse_seats가
        그대로 읽는다.
        """
        for item in (payload.get("data") or {}).get("items") or []:
            rows = item.get("seats")
            if not isinstance(rows, list):
                continue
            item["seats"] = [
                dict(zip(fields, row)) if isinstance(row, list) else row
                for row in rows
            ]
        return payload

    @staticmethod
    def _one_of_many(path: str, item: dict | None) -> dict | None:
        """묶음 결과 하나를 get_json과 같은 기준으로 판정한다. 실패하면 None.

        401도 None으로 돌려준다 — 개별 경로가 다시 받으면서 AuthRequired를 내고,
        복구는 지금처럼 거기서 한다.
        """
        if not item:
            return None
        if item.get("status") != 200 or "json" not in item:
            return None
        payload = item["json"]
        if not isinstance(payload, dict):
            return None
        if payload.get("statusCode") not in (0, "0", None):
            log.debug("%s: API 오류 %s", path.split("?")[0],
                      payload.get("statusMessage"))
            return None
        return payload

    # ── 개별 조회 ──
    def bookable_movies(self) -> list[dict]:
        """현재 예매가 열린 영화 목록. 아직 오픈 전이면 여기에 없다."""
        return self.get_json(EP_MOVIES).get("data") or []

    def sites(self) -> tuple[list[dict], dict[str, str]]:
        """(극장 목록, 지역코드 -> 지역명)"""
        data = self.get_json(EP_SITES).get("data") or {}
        regions = {
            r["comCdval"]: r["comCdvalNm"] for r in (data.get("regionInfo") or [])
        }
        return (data.get("siteInfo") or []), regions

    def bookable_dates(self, site_no: str, mov_no: str) -> list[str]:
        payload = self.get_json(EP_DATES.format(site_no=site_no, mov_no=mov_no))
        return sorted(
            row["scnYmd"] for row in (payload.get("data") or []) if row.get("scnYmd")
        )

    def showtimes(self, site_no: str, mov_no: str, ymd: str) -> list[dict]:
        payload = self.get_json(
            EP_SCHEDULE.format(site_no=site_no, mov_no=mov_no, ymd=ymd), retries=1
        )
        return payload.get("data") or []

    def seat_map(self, site_no: str, scns_no: str, ymd: str,
                 scn_sseq: str) -> dict:
        """한 회차의 좌석 배치도. **로그인된 세션에서만** 열린다(아니면 401).

        같은 오리진 BFF라 get_json이 로그인 쿠키를 자동으로 실어 보낸다.
        반환은 CGV 원본 data — seats.parse_seats로 좌석 목록을 뽑는다.
        """
        payload = self.get_json(
            EP_SEAT.format(site_no=site_no, scns_no=scns_no, ymd=ymd,
                           scn_sseq=scn_sseq),
            retries=1,
        )
        return payload.get("data") or {}

    # ── 계정 로그인 / 세션 ──
    def logged_in(self) -> bool:
        """accessToken 쿠키가 있으면 로그인된 것으로 본다.

        **누구로** 로그인됐는지는 알려주지 않는다. 사용자가 여럿인 경로에서는
        이 값만 보고 통과시키면 안 되고 `logged_in_as`를 써야 한다.
        """
        return any(c["name"] == "accessToken" and c.get("value")
                   for c in self._page.context.cookies())

    def logged_in_as(self, owner_id: int) -> bool:
        """이 세션이 **그 소유자의** 계정으로 로그인돼 있는지."""
        return self.logged_in_owner == owner_id and self.logged_in()

    def mark_logged_in(self, owner_id: int) -> None:
        """지금 공간에 얹힌 로그인 쿠키의 주인을 기록한다 (cgv_login 전용).

        예전에는 여기서 주인이 바뀌면 예매 탭을 전부 닫았다. 컨텍스트가 하나라
        그 탭들이 앞사람 화면이었기 때문인데, 그 바람에 사용자가 둘 이상이면
        프리워밍이 한 번도 살아남지 못했다. 지금은 공간이 소유자마다 따로라
        **남의 화면이 섞일 수 없어** 그럴 필요가 없다.

        공간을 잘못 고른 채 부르는 것만은 막는다 — 그건 격리가 깨졌다는 뜻이다.
        """
        current = self._space.logged_in_owner
        if current is not None and current != owner_id:
            raise RuntimeError(
                f"owner {current}의 공간에 owner {owner_id}로 로그인하려 합니다 "
                f"— use({owner_id})를 먼저 불러야 합니다")
        self._space.logged_in_owner = owner_id

    def session_tokens(self) -> dict[str, str]:
        """현재 세션 쿠키 중 저장해 둘 값들(accessToken·refresh_token 등)."""
        jar = {c["name"]: c["value"] for c in self._page.context.cookies()}
        return {name: jar[name] for name in SESSION_COOKIES if jar.get(name)}

    def restore_tokens(self, tokens: dict[str, str]) -> None:
        """저장해 둔 세션 쿠키를 되살린다 — 캡차 없이 다시 로그인 상태가 된다."""
        cookies = [
            {"name": name, "value": value, "domain": "cgv.co.kr", "path": "/"}
            for name, value in tokens.items() if value
        ]
        if cookies:
            self._page.context.add_cookies(cookies)

    def clear_session_cookies(self) -> None:
        """브라우저의 로그인 쿠키를 버린다.

        만료된 accessToken이 남아 있으면 `logged_in()`이 계속 True를 내서 재로그인
        경로로 못 간다. 계정을 바꿔 다는 경로도 여기를 지나야 한다 — 앞사람의
        쿠키가 남아 있으면 로그인 페이지가 그대로 되돌아가 버린다.

        **로그인 쿠키만** 골라 지운다. 통째로 지우면 Cloudflare 봇 차단을 통과한
        흔적까지 날아가 다음 요청이 403을 맞을 수 있다 — 좌석과 무관한 날짜
        확인까지 같이 멎는다. 이름 지정 삭제를 못 받는 버전에서만 통째로 지운다.
        """
        self.logged_in_owner = None
        try:
            for name in SESSION_COOKIES:
                self._page.context.clear_cookies(name=name)
            return
        except TypeError:
            pass  # 이름 인자를 못 받는 구버전 — 아래에서 통째로 지운다
        except Exception as exc:  # noqa: BLE001 - 못 지워도 재로그인은 시도한다
            log.warning("세션 쿠키를 지우지 못했습니다: %s", exc)
            return
        try:
            self._page.context.clear_cookies()
        except Exception as exc:  # noqa: BLE001
            log.warning("세션 쿠키를 지우지 못했습니다: %s", exc)

    def refresh_session(self) -> bool:
        """refresh_token으로 accessToken을 갱신한다. 성공하면 True.

        refresh 토큰이 만료됐으면(401) False — 캡차를 다시 풀어 로그인해야 한다.

        성공 판정은 **accessToken이 실제로 바뀌었는지**로 한다. 쿠키의 존재만
        보면(logged_in) 만료된 토큰이 그대로 남아 있어도 True가 나와서, 되살렸다고
        착각한 채 같은 401을 계속 맞는다.
        """
        def token() -> str:
            return next((c["value"] for c in self._page.context.cookies()
                         if c["name"] == "accessToken"), "")

        access = token()
        try:
            out = self._page.evaluate(
                """async ([url, body]) => {
                    const r = await fetch(url, {method: 'POST', credentials: 'include',
                        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
                    return {status: r.status};
                }""",
                [REFRESH_URL, {"accessToken": access}],
            )
        except Exception as exc:  # noqa: BLE001 - 실패하면 재로그인으로 폴백한다
            log.debug("refresh 호출 실패 (재로그인으로 폴백): %s", exc)
            return False
        fresh = token()
        ok = out["status"] == 200 and bool(fresh) and fresh != access
        if ok:
            log.info("CGV accessToken을 refresh로 갱신했습니다")
        return ok

    def login_cgv(self, user_id: str, password: str, *,
                  timeout_ms: int = 20_000) -> dict[str, str]:
        """cgv.co.kr/mem/login에서 로그인한다. 성공하면 세션 쿠키를 돌려준다.

        비밀번호 암호화·요청 바디 구성은 페이지 JS가 처리한다. 숫자 캡차는
        fillText 후킹으로 읽어 입력한다. 실패하면 LoginError를 올린다.
        """
        page = self._page
        page.add_init_script(_CAPTCHA_HOOK)
        page.goto(LOGIN_PAGE_URL + "?returnUrl=%2F",
                  wait_until="networkidle", timeout=timeout_ms + 25_000)
        page.wait_for_timeout(2500)

        captcha = "".join((page.evaluate("() => (window.__cap || []).slice()"))[-6:])
        if len(captcha) != 6:
            raise LoginError("캡차 숫자를 읽지 못했습니다")

        # 입력칸: userId / password / captcha (id는 loginInput1~3)
        page.fill("#loginInput1", user_id)
        page.fill("#loginInput2", password)
        page.fill("#loginInput3", captcha)

        # 폼 onSubmit이 암호화·POST를 한다 — 헤더의 '로그인' 링크와 헷갈리지 않게
        # 캡차 입력칸이 속한 form을 직접 제출한다.
        page.evaluate("""() => {
            const inp = document.querySelector('#loginInput3');
            const f = inp && inp.closest('form');
            if (f) { f.requestSubmit ? f.requestSubmit() : f.submit(); }
        }""")

        # 로그인 성공은 accessToken 쿠키로 판별한다 — 리다이렉트를 기다린다.
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.logged_in():
                log.info("CGV 로그인 성공: %s", user_id)
                return self.session_tokens()
            page.wait_for_timeout(500)

        raise LoginError("로그인에 실패했습니다 (아이디·비밀번호·캡차를 확인하세요)")


class LoginError(RuntimeError):
    """CGV 로그인 실패 — 사용자에게 보여줄 수 있는 오류."""


# ── 이름 -> 코드 해석 ───────────────────────────────────────────────────────
def resolve(query: str, items: list[dict], name_key: str) -> tuple[dict | None, str]:
    """이름으로 항목을 찾는다. 완전일치 우선, 그다음 부분일치.

    Returns: (찾은 항목 또는 None, 문제 설명)
    """
    q = normalize(query)
    exact = [it for it in items if normalize(it.get(name_key, "")) == q]
    if len(exact) == 1:
        return exact[0], ""

    partial = [it for it in items if q and q in normalize(it.get(name_key, ""))]
    if len(partial) == 1:
        return partial[0], ""
    if not partial:
        return None, "일치하는 항목이 없습니다"

    names = ", ".join(it.get(name_key, "?") for it in partial[:6])
    return None, f"여러 항목에 걸립니다 ({names}) — 더 구체적으로 적어주세요"


# ── 웹훅 (Slack · Discord) ──────────────────────────────────────────────────
# 알림 문구는 Slack mrkdwn 한 가지로만 만들고, 전송 직전에 서비스 문법으로 옮긴다.
# 두 벌의 포맷 함수를 들고 있으면 문구를 고칠 때마다 한쪽을 잊는다.
WEBHOOK_LABELS = {"slack": "Slack", "discord": "Discord"}
DISCORD_LIMIT = 2000    # content 최대 길이 — 넘기면 400이 떨어진다
SLACK_LIMIT = 39000     # text 최대 길이 (문서상 40000)

# <url|라벨> · <url> — Slack의 링크 표기
LINK_LABELED_RE = re.compile(r"<(https?://[^|>\s]+)\|([^>]+)>")
LINK_BARE_RE = re.compile(r"<(https?://[^>\s]+)>")
# *굵게* — 앞뒤에 별이 더 붙지 않은 한 쌍만
BOLD_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def to_discord_markdown(text: str) -> str:
    """Slack mrkdwn 문구를 Discord 마크다운으로 옮긴다.

    링크는 <url|라벨> → [라벨](url), 굵게는 *한 개* → **두 개**다.
    Discord에서 별 하나는 기울임이라 그대로 보내면 강조가 어긋난다.
    """
    text = LINK_LABELED_RE.sub(r"[\2](\1)", text)
    text = LINK_BARE_RE.sub(r"\1", text)
    return BOLD_RE.sub(r"**\1**", text)


def webhook_payload(text: str, kind: str) -> dict:
    """서비스별 요청 본문. 길이 제한을 넘으면 잘라 보낸다 (400보다 낫다)."""
    if kind == "discord":
        return {"content": to_discord_markdown(text)[:DISCORD_LIMIT]}
    return {"text": text[:SLACK_LIMIT]}


def resolve_webhook(webhook_url: str | None = None,
                    kind: str | None = None) -> tuple[str, str]:
    """보낼 곳과 종류를 정한다 — 사용자 설정이 없으면 .env의 전역 웹훅.

    종류를 주지 않았거나 주소와 어긋나면 주소에서 알아낸 값을 쓴다.
    """
    url = (webhook_url or "").strip()
    if not url:
        url = (os.environ.get("SLACK_WEBHOOK_URL", "").strip()
               or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
        kind = None  # 전역 웹훅에는 사용자가 고른 종류를 적용하지 않는다
    detected = store.detect_webhook_kind(url)
    return url, (detected or kind or "slack")


def send_webhook(text: str, dry_run: bool = False,
                 webhook_url: str | None = None,
                 kind: str | None = None) -> bool:
    """웹훅으로 보낸다. webhook_url을 주면 그쪽으로, 없으면 .env의 전역 웹훅으로."""
    if dry_run:
        print("\n--- [dry-run] 웹훅 전송 안 함 ---")
        print(text)
        print("-" * 34)
        return True

    url, kind = resolve_webhook(webhook_url, kind)
    if not url:
        log.error("보낼 웹훅이 없습니다 (웹의 '설정' 탭 또는 .env의 "
                  "SLACK_WEBHOOK_URL·DISCORD_WEBHOOK_URL을 확인하세요)")
        return False

    label = WEBHOOK_LABELS.get(kind, kind)
    body = json.dumps(webhook_payload(text, kind)).encode("utf-8")
    # User-Agent를 반드시 붙인다. Discord 앞단 Cloudflare는 파이썬 기본 UA
    # (Python-urllib/x.y)를 봇으로 보고 403 error code 1010으로 막는다.
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": WEBHOOK_UA},
    )
    for attempt in range(3):
        if attempt:
            time.sleep(2**attempt)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                # Slack은 200 + "ok", Discord는 204 + 빈 본문으로 답한다.
                if 200 <= resp.status < 300:
                    return True
                log.warning("%s 응답 코드 %d", label, resp.status)
        except urllib.error.HTTPError as exc:
            # 400·404는 주소나 본문이 틀린 것이다 — 재시도해도 같은 답이 온다.
            detail = exc.read(500).decode("utf-8", "replace").strip()
            log.error("%s 전송 실패 (HTTP %d): %s", label, exc.code, detail)
            if exc.code < 500:
                return False
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("%s 전송 실패 (%d/3): %s", label, attempt + 1, exc)
    log.error("%s 전송을 포기했습니다", label)
    return False


def deliver_alert(kind: str, body: str, *, dry_run: bool = False,
                  target_id: int | None = None, owner_id: int | None = None,
                  webhook_url: str | None = None,
                  webhook_kind: str | None = None, mov_nm: str | None = None,
                  site_nm: str | None = None, dates: list[str] | None = None,
                  seat_watch_id: int | None = None) -> bool:
    """알림을 이력에 남기고 그 소유자의 웹훅으로 보낸다. 전송 성공 여부를 반환.

    먼저 delivered=false로 적어 두므로, 전송이 실패해도 "무엇을 못 보냈는지"가
    화면에 남는다. dry-run은 이력도 남기지 않는다 — 아무것도 바꾸지 않는 게 뜻이다.
    """
    if dry_run:
        return send_webhook(body, dry_run=True)

    alert_id = store.record_alert(
        kind, body, target_id=target_id, owner_id=owner_id, mov_nm=mov_nm,
        site_nm=site_nm, dates=dates or [], seat_watch_id=seat_watch_id,
    )
    if send_webhook(body, webhook_url=webhook_url, kind=webhook_kind):
        store.mark_alert_delivered(alert_id)
        return True
    return False


def screen_label(row: dict) -> str:
    """상영 한 건의 상영관 표기 — 'IMAX LASER 2D IMAX관' 같은 형태."""
    kind = row.get("movkndDsplNm") or ""
    screen = row.get("expoScnsNm") or row.get("scnsNm") or ""
    return " ".join(part for part in (kind, screen) if part)


def matches_screen_types(row: dict, wanted: list[str]) -> bool:
    """상영 한 건이 원하는 상영관 종류인지.

    CGV는 IMAX를 movkndDsplNm='IMAX LASER 2D', expoScnsNm='IMAX관'처럼 주고
    4DX는 '4DX 2D'/'4DX관', SCREENX는 'SCREENX 2D'/'4관[SCREENX]'로 준다.
    두 필드를 합쳐 부분 문자열로 보면 'IMAX' 한 단어로 충분히 걸린다.
    """
    if not wanted:
        return True
    haystack = normalize(screen_label(row))
    return any(normalize(w) in haystack for w in wanted)


def group_showtimes(rows: list[dict]) -> list[dict]:
    """상영 목록을 상영관별로 묶는다 — [{label, times}, ...].

    웹훅 알림과 웹 화면이 같은 묶음을 쓰도록 포맷과 분리해 둔다.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        key = (row.get("movkndDsplNm") or "",
               row.get("expoScnsNm") or row.get("scnsNm") or "")
        grouped.setdefault(key, []).append(row.get("scnsrtTm") or "")

    groups = []
    for (kind, screen), times in grouped.items():
        groups.append({
            "label": " ".join(part for part in (kind, screen) if part),
            "times": [fmt_time(t) for t in sorted(times)],
        })
    return groups


def summarize_showtimes(rows: list[dict]) -> list[str]:
    """상영 목록을 '2D 1관 (Laser): 14:00, 17:00' 형태의 줄들로 요약."""
    return [f"     • {g['label']}: {', '.join(g['times'])}"
            for g in group_showtimes(rows)]


def build_new_dates_message(
    movie_name: str,
    site_name: str,
    new_dates: list[str],
    showtimes: dict[str, list[dict]],
    screen_types: list[str] | None = None,
) -> str:
    what = f"{'/'.join(screen_types)} " if screen_types else ""
    lines = [
        f"🎟 *새 {what}예매 날짜 오픈*",
        f"*{movie_name}* · CGV {site_name}",
        f"➕ {', '.join(fmt_date(d) for d in new_dates)}",
    ]
    for ymd in new_dates:
        rows = showtimes.get(ymd)
        if not rows:
            continue
        lines.append(f"  {fmt_date(ymd)}")
        lines.extend(summarize_showtimes(rows))
    lines.append(f"<{BOOKING_URL}|▶ 예매하러 가기>")
    return "\n".join(lines)


def build_open_message(
    movie_name: str,
    site_name: str,
    dates: list[str],
    screen_types: list[str] | None = None,
) -> str:
    span = (
        f"{fmt_date(dates[0])} ~ {fmt_date(dates[-1])}"
        if len(dates) > 1
        else (fmt_date(dates[0]) if dates else "날짜 정보 없음")
    )
    what = f"{'/'.join(screen_types)} " if screen_types else ""
    return "\n".join(
        [
            f"🎬 *{what}예매 오픈!*",
            f"*{movie_name}* · CGV {site_name}",
            f"예매 가능 날짜: {span} (총 {len(dates)}일)",
            f"<{BOOKING_URL}|▶ 예매하러 가기>",
        ]
    )


# ── 메인 확인 로직 ──────────────────────────────────────────────────────────
def within_lookahead(ymd: str, lookahead_days: int) -> bool:
    if lookahead_days <= 0:
        return True
    try:
        target = datetime.strptime(ymd, "%Y%m%d").date()
    except ValueError:
        return True
    return target <= date.today() + timedelta(days=lookahead_days)


def diff_dates(candidates: list[str], known: set[str],
               lookahead_days: int) -> tuple[list[str], list[str]]:
    """(기억할 날짜, 새로 알릴 날짜)를 가른다.

    **둘이 같은 필터를 지나야 한다.** lookahead 범위 밖 날짜를 기준선에 넣어
    버리면 나중에 범위 안으로 들어와도 "이미 아는 날짜"가 되어 그 날짜는 영구히
    알림이 가지 않는다. 판정하는 집합과 기억하는 집합을 갈라 놓으면 이 구멍이
    생기므로 한 함수에서 같이 만든다.
    """
    tracked = [d for d in candidates if within_lookahead(d, lookahead_days)]
    return tracked, [d for d in tracked if d not in known]


# 영화·극장 목록을 DB 캐시로 때울 수 있는 기간. 이보다 낡으면 새로 받는다.
# 폴러가 하루에 한 번 갱신하므로(web/poller.py CATALOG_MAX_AGE_HOURS) 그보다
# 넉넉히 잡아 둔다 — 갱신이 잠깐 밀렸다고 매 바퀴 두 번씩 다시 받을 이유는 없다.
CATALOG_CACHE_MAX_AGE_HOURS = 36


class Catalog:
    """예매 영화·극장 목록을 처음 필요할 때 한 번만 받아오는 지연 로더.

    DB에 movNo·siteNo가 남아 있는 감시 대상은 이름을 다시 해석할 필요가 없다.
    모든 대상이 캐시에 걸리면 이 두 API는 아예 호출되지 않아 사이클당 요청이
    절반으로 줄고, 그만큼 폴링 간격을 좁힐 여유가 생긴다.

    목록을 실제로 받아오면 DB 캐시(catalog_movies·catalog_sites)도 갱신한다 —
    웹의 감시 대상 편집 화면이 그 캐시로 영화·극장 선택지를 만든다.
    """

    def __init__(self, cgv: CgvSession, persist: bool = True):
        self._cgv = cgv
        self._persist = persist
        self._movies: list[dict] | None = None
        self._sites: list[dict] | None = None
        self._from_db = False   # 지금 들고 있는 게 DB 캐시인지

    @property
    def movies(self) -> list[dict]:
        self._load()
        return self._movies  # type: ignore[return-value]

    @property
    def sites(self) -> list[dict]:
        self._load()
        return self._sites  # type: ignore[return-value]

    def resolve_movie(self, query: str) -> tuple[dict | None, str]:
        """영화 이름을 코드로. DB 캐시를 먼저 보고, 못 찾으면 새로 받아 다시 본다.

        **못 찾았을 때 반드시 새로 받는 게 핵심이다.** 영화 목록에는 예매가 열린
        영화만 들어 있어서, 목록에 나타나는 순간이 곧 오픈이다. 캐시에 없다고
        "아직 안 열렸다"고 답해 버리면 오픈을 영영 감지하지 못한다.

        반대로 **찾았다면 캐시로 충분하다.** 이미 열린 영화가 목록에서 사라질 일은
        상영 종료뿐이고, 그건 몇 초를 다툴 일이 아니다.
        """
        return self._resolve_cached(query, "movie")

    def resolve_site(self, query: str) -> tuple[dict | None, str]:
        """극장 이름을 코드로. 영화와 같은 방식이다."""
        return self._resolve_cached(query, "site")

    def _resolve_cached(self, query: str, kind: str) -> tuple[dict | None, str]:
        name_key = "movNm" if kind == "movie" else "siteNm"
        if not self._loaded():
            cached = self._db_cache(kind)
            if cached is not None:
                found, problem = resolve(query, cached, name_key)
                if found is not None:
                    return found, problem
                # 못 찾았다 — 캐시가 낡아서일 수 있으므로 실물을 보고 판단한다.
        items = self.movies if kind == "movie" else self.sites
        return resolve(query, items, name_key)

    def _loaded(self) -> bool:
        return self._movies is not None

    @staticmethod
    def _db_cache(kind: str) -> list[dict] | None:
        """DB에 담아 둔 목록을 API와 같은 모양으로. 없거나 낡았으면 None.

        저장은 스네이크 케이스(mov_no)인데 resolve는 API 모양(movNo)을 보므로
        여기서 맞춰 준다.
        """
        try:
            refreshed = store.catalog_refreshed_at()
            if refreshed is None:
                return None
            age = datetime.now().astimezone() - refreshed
            if age > timedelta(hours=CATALOG_CACHE_MAX_AGE_HOURS):
                return None
            if kind == "movie":
                return [{"movNo": r["mov_no"], "movNm": r["mov_nm"]}
                        for r in store.catalog_movies()]
            return [{"siteNo": r["site_no"], "siteNm": r["site_nm"]}
                    for r in store.catalog_sites()]
        except Exception as exc:  # noqa: BLE001 - 캐시를 못 읽으면 그냥 새로 받는다
            log.debug("영화·극장 캐시를 읽지 못했습니다: %s", exc)
            return None

    def _load(self) -> None:
        if self._movies is not None:
            return
        self._movies = self._cgv.bookable_movies()
        self._sites, regions = self._cgv.sites()
        log.info("예매 가능 영화 %d편 / 극장 %d곳 조회됨",
                 len(self._movies), len(self._sites))
        if self._persist:
            try:
                store.replace_catalog_movies(self._movies)
                store.replace_catalog_sites(self._sites, regions)
            except Exception as exc:  # noqa: BLE001 - 캐시 갱신 실패로 확인을 멈추지 않는다
                log.warning("영화·극장 목록 캐시 갱신 실패: %s", exc)


def cached_ids(prev: dict) -> tuple[str, str, str, str] | None:
    """이전 관측에 남은 (movNo, siteNo, movNm, siteNm)을 재사용할 수 있으면 반환.

    아직 예매가 열리지 않은 항목은 영화 목록에 등장하는 순간이 곧 오픈이므로
    캐시를 쓰면 안 된다 — 목록을 봐야만 오픈을 감지할 수 있다.
    """
    if not prev or prev.get("status") == "not_open":
        return None
    ids = (prev.get("movNo"), prev.get("siteNo"), prev.get("movNm"),
           prev.get("siteNm"))
    if not all(ids):
        return None
    return ids  # type: ignore[return-value]


def check_all(cgv: CgvSession | None = None, *, dry_run: bool = False) -> dict:
    """감시 대상을 한 바퀴 확인한다. cgv를 넘기면 그 세션을 재사용한다.

    돌려주는 요약(targets_checked·requests·new_dates·alerts_sent)은 호출한 쪽이
    poll_cycles에 기록한다 — 사이클을 누가 돌렸는지(스케줄·수동·CLI)는 여기서
    알 필요가 없다.
    """
    if cgv is None:
        headless = bool(store.get_setting("headless", True))
        with CgvSession(headless=headless) as session:
            return check_all(session, dry_run=dry_run)

    # 소유자가 아직 없는 대상(로그인을 붙이기 전에 만든 것)에 쓸 기본값.
    seed = store.settings_all()

    rows = store.targets(enabled_only=True)
    requests_before = cgv.requests
    summary = {"targets_checked": 0, "requests": 0, "new_dates": 0, "alerts_sent": 0}

    if not rows:
        log.warning("감시 대상이 없습니다 — 웹에서 추가하거나 --migrate를 실행하세요")
        return summary

    messages: list[dict] = []  # 운영 알림 — 상태 진행과 무관
    deferred: list[dict] = []  # 전송에 성공해야 상태를 미는 날짜 알림
    config_errors: list[tuple[int | None, str]] = []  # (소유자, 문제)
    # 설정 오류를 보낼 곳 — 소유자별 (웹훅 주소, 종류)
    owner_webhooks: dict[int | None, tuple[str | None, str | None]] = {}

    def add_config_error(owner_id: int | None, problem: str) -> None:
        # 같은 오타가 대상 수만큼 중복되지 않게 한 번만 담는다.
        if (owner_id, problem) not in config_errors:
            config_errors.append((owner_id, problem))

    catalog = Catalog(cgv)

    for row in rows:
        target_id = row["id"]
        movie_query, site_query = row["movie_query"], row["site_query"]
        prev = store.prev_state(row)
        wanted = store.normalize_screen_types(row["screen_types"])

        # 확인 조건과 알림 수신처는 그 대상의 **소유자** 것을 쓴다.
        owner_id = row["owner_id"]
        webhook = row["owner_webhook_url"] if owner_id else None
        webhook_kind = row["owner_webhook_kind"] if owner_id else None
        owner_webhooks[owner_id] = (webhook, webhook_kind)
        lookahead = int((row["owner_lookahead_days"] if owner_id
                         else seed["lookahead_days"]) or 0)
        want_showtimes = bool(row["owner_include_showtimes"] if owner_id
                              else seed["include_showtimes"])

        # ── 코드 확보: 이전 관측 캐시를 먼저 쓰고, 안 되면 목록을 받는다 ──
        ids = cached_ids(prev)
        dates: list[str] | None = None
        mov_no = site_no = mov_nm = site_nm = ""

        if ids is not None:
            mov_no, site_no, mov_nm, site_nm = ids
            try:
                dates = cgv.bookable_dates(site_no, mov_no)
            except RuntimeError as exc:
                log.debug("캐시된 코드로 조회 실패 — 목록을 다시 받습니다: %s", exc)
                dates = None
            if not dates:
                # 종영·재편성으로 코드가 낡았을 수 있다. 목록으로 확인한다.
                ids = None

        if ids is None:
            movie, movie_problem = resolve(movie_query, catalog.movies, "movNm")

            # 이름이 여러 영화에 걸리는 건 설정 오류다 — 확인을 진행할 수 없다.
            if movie is None and movie_problem != "일치하는 항목이 없습니다":
                add_config_error(owner_id, f"영화 '{movie_query}': {movie_problem}")
                continue

            site, site_problem = resolve(site_query, catalog.sites, "siteNm")
            if site is None:
                add_config_error(owner_id, f"극장 '{site_query}': {site_problem}")
                continue
            site_nm = site["siteNm"]

            # 아직 예매가 열리지 않은 영화 — 목록에 등장하는 순간이 곧 티켓 오픈.
            if movie is None:
                log.info("%s · %s — 아직 예매 오픈 전", movie_query, site_nm)
                if not dry_run:
                    store.mark_not_open(target_id, wanted)
                summary["targets_checked"] += 1
                continue

            mov_no, site_no = movie["movNo"], site["siteNo"]
            mov_nm = movie["movNm"]
            try:
                dates = cgv.bookable_dates(site_no, mov_no)
            except RuntimeError as exc:
                # 날짜는 갱신하지 않는다 — 다음 성공 때 그 사이 열린 날짜를 잡는다.
                fails = (int(prev.get("fail_count", 0)) + 1 if dry_run
                         else store.record_fail(target_id, str(exc)))
                log.error("%s · %s 조회 실패 (%d회 연속): %s",
                          mov_nm, site_nm, fails, exc)
                if fails == FAIL_ALERT_THRESHOLD:
                    messages.append({
                        "kind": "fetch_error",
                        "owner_id": owner_id,
                        "webhook": webhook,
                        "webhook_kind": webhook_kind,
                        "body": "⚠️ *CGV 알림기 조회 실패*\n"
                                f"*{mov_nm}* · CGV {site_nm} — "
                                f"{fails}회 연속 실패\n`{exc}`",
                    })
                continue

        was_not_open = prev.get("status") == "not_open"

        # 필터를 바꾸면 이전에 쌓은 날짜 집합은 의미가 달라진다.
        # 기준선을 다시 잡아 엉뚱한 알림이 쏟아지지 않게 한다.
        # (순서만 다른 건 같은 필터로 본다 — 체크박스 순서로 기준선이 날아가면 안 된다.)
        if prev and sorted(prev.get("screen_types", [])) != sorted(wanted):
            log.info("%s · %s — 상영관 필터가 바뀌어 기준선을 다시 잡습니다",
                     mov_nm, site_nm)
            prev = {}

        showtimes: dict[str, list[dict]] = {}

        if wanted:
            # IMAX 등 특정 상영관만 감시. 상영관 정보는 날짜 목록 API에 없고
            # 시간표 API에만 있으므로 날짜별로 확인해야 한다.
            # 이미 해당 상영관이 확인된 날짜는 다시 볼 필요가 없다.
            known = set(prev.get("matched_dates", []))
            matched: list[str] = []
            for ymd in dates:
                if ymd in known:
                    matched.append(ymd)
                    continue
                if not within_lookahead(ymd, lookahead):
                    # 범위 밖 날짜는 알리지도, 기준선에 넣지도 않는다.
                    # 그러면 시간표를 받아 볼 이유도 없다 — 요청을 아낀다.
                    continue
                try:
                    schedule = cgv.showtimes(site_no, mov_no, ymd)
                except RuntimeError as exc:
                    # 확인 못 한 날짜는 미확정으로 남긴다 — 다음 확인에서 재시도.
                    log.warning("%s %s 시간표 조회 실패: %s", site_nm, ymd, exc)
                    continue
                hits = [r for r in schedule if matches_screen_types(r, wanted)]
                if hits:
                    matched.append(ymd)
                    showtimes[ymd] = hits

            candidates = matched
        else:
            known = set(prev.get("dates", []))
            candidates = dates

        tracked, new_dates = diff_dates(candidates, known, lookahead)

        alert: str | None = None
        kind = ""
        label = "/".join(wanted) if wanted else "전체 상영관"

        if was_not_open and tracked:
            # 예매가 새로 열렸다.
            log.info("%s · %s — 예매 오픈 감지 (%s, %d일)",
                     mov_nm, site_nm, label, len(tracked))
            alert, kind = build_open_message(mov_nm, site_nm, tracked, wanted), "open"
        elif was_not_open:
            log.info("%s · %s — 예매는 열렸지만 %s 상영이 아직 없습니다",
                     mov_nm, site_nm, label)
        elif not prev:
            # 첫 관측: 기준선만 저장하고 알리지 않는다.
            log.info("%s · %s — 기준선 저장 (%s, %d일: %s)",
                     mov_nm, site_nm, label, len(tracked),
                     ", ".join(fmt_date(d) for d in tracked) or "해당 없음")
        elif new_dates:
            log.info("%s · %s — 새 %s 날짜 %s", mov_nm, site_nm, label,
                     ", ".join(fmt_date(d) for d in new_dates))
            if want_showtimes and not wanted:
                # 필터가 없으면 시간표를 아직 안 받았다 — 알림용으로 받아온다.
                for ymd in new_dates:
                    try:
                        showtimes[ymd] = cgv.showtimes(site_no, mov_no, ymd)
                    except RuntimeError as exc:
                        # 시간표는 부가 정보다 — 실패해도 알림은 보낸다.
                        log.warning("%s 시간표 조회 실패: %s", ymd, exc)
            alert = build_new_dates_message(
                mov_nm, site_nm, new_dates,
                showtimes if want_showtimes else {}, wanted
            )
            kind = "new_dates"
        else:
            log.info("%s · %s — 변화 없음 (%s, %d일)",
                     mov_nm, site_nm, label, len(tracked))

        fresh = {
            "mov_no": mov_no,
            "site_no": site_no,
            "mov_nm": mov_nm,
            "site_nm": site_nm,
            # 비교 기준은 필터가 있으면 matched_dates, 없으면 dates다.
            # 어느 쪽이든 판정한 집합(tracked)을 그대로 넣어야 어긋나지 않는다.
            "dates": dates if wanted else tracked,
            "matched_dates": tracked if wanted else [],
            "screen_types": wanted,
        }

        if not dry_run:
            # 받아온 시간표는 캐시에 남겨 화면이 CGV를 다시 부르지 않게 한다.
            for ymd, hits in showtimes.items():
                store.save_showtimes(target_id, ymd, hits)
            store.prune_showtimes(target_id, dates)

        summary["targets_checked"] += 1
        if prev:
            # 첫 관측은 비교 대상이 없다 — 열린 날짜 전부를 "새 날짜"로 세면
            # 사이클 이력에 알림 없는 16일 같은 값이 남아 읽는 사람을 속인다.
            summary["new_dates"] += len(new_dates)

        if alert:
            # 알림 전송이 성공한 뒤에만 상태를 갱신한다. 지금 반영해 버리면
            # 웹훅 전송이 실패했을 때 그 날짜를 두 번 다시 알릴 수 없다.
            deferred.append({"target_id": target_id, "owner_id": owner_id,
                             "webhook": webhook, "webhook_kind": webhook_kind,
                             "kind": kind, "body": alert,
                             "fresh": fresh, "dates": new_dates or tracked})
        elif not dry_run:
            store.save_state(target_id, **fresh)

    # 설정 오류는 한 번만 알린다 — 같은 오타로 30초마다 알림이 오면 안 된다.
    if config_errors:
        for _, problem in config_errors:
            log.error("설정 오류: %s", problem)
        signature = "\n".join(sorted(f"{o}:{p}" for o, p in config_errors))
        if store.config_error_signature() != signature:
            # 소유자별로 묶어 자기 오류만 받게 한다.
            by_owner: dict[int | None, list[str]] = {}
            for problem_owner, problem in config_errors:
                by_owner.setdefault(problem_owner, []).append(problem)
            for problem_owner, problems in by_owner.items():
                hook, hook_kind = owner_webhooks.get(problem_owner, (None, None))
                messages.append({
                    "kind": "config_error",
                    "owner_id": problem_owner,
                    "webhook": hook,
                    "webhook_kind": hook_kind,
                    "body": "⚠️ *CGV 알림기 설정 오류*\n"
                            + "\n".join(f"• {p}" for p in problems)
                            + "\n웹의 감시 대상 화면에서 정확한 영화·극장을 "
                              "골라 주세요.",
                })
            if not dry_run:
                store.set_config_error_signature(signature)
    elif not dry_run and store.config_error_signature() is not None:
        store.set_config_error_signature(None)

    # 여기까지 왔으면 사이트 접속은 성공했다 — 전역 실패 카운터를 되돌린다.
    if not dry_run:
        store.clear_global_fail()

    # 날짜 알림: 전송에 성공한 대상만 상태를 앞으로 밀어준다.
    # 실패하면 상태를 그대로 남겨 다음 확인에서 같은 날짜를 다시 알린다.
    for item in deferred:
        fresh = item["fresh"]
        sent = deliver_alert(
            item["kind"], item["body"], dry_run=dry_run,
            target_id=item["target_id"], owner_id=item["owner_id"],
            webhook_url=item["webhook"], webhook_kind=item["webhook_kind"],
            mov_nm=fresh["mov_nm"], site_nm=fresh["site_nm"], dates=item["dates"],
        )
        if sent:
            summary["alerts_sent"] += 1
            if not dry_run:
                store.save_state(item["target_id"], **fresh)
        else:
            log.error("%s · %s 알림 전송 실패 — 다음 확인에서 다시 시도합니다",
                      fresh["mov_nm"], fresh["site_nm"])

    # 운영 알림은 상태 진행과 무관하므로 결과를 따지지 않는다.
    for message in messages:
        deliver_alert(message["kind"], message["body"], dry_run=dry_run,
                      owner_id=message["owner_id"],
                      webhook_url=message["webhook"],
                      webhook_kind=message["webhook_kind"])

    summary["requests"] = cgv.requests - requests_before
    if dry_run:
        log.info("dry-run: DB를 변경하지 않았습니다")
    return summary


def refresh_catalog(cgv: CgvSession) -> dict:
    """영화·극장 목록을 받아 DB 캐시를 갱신한다.

    감시 대상이 모두 코드 캐시에 걸리면 확인 사이클은 이 두 API를 아예 부르지
    않는다. 그래서 웹의 편집 화면이 필요할 때 이 함수로 따로 갱신한다.
    """
    catalog = Catalog(cgv)
    return {"movies": len(catalog.movies), "sites": len(catalog.sites)}


# ── CLI 서브 동작 ───────────────────────────────────────────────────────────
def _headless() -> bool:
    return bool(store.get_setting("headless", True))


def cmd_list_movies() -> None:
    with CgvSession(headless=_headless()) as cgv:
        movies = cgv.bookable_movies()
    print(f"예매 가능 영화 {len(movies)}편:\n")
    for m in movies:
        print(f"  {m['movNo']}  {m['movNm']}  (예매율 {m.get('atktRate', '-')}%)")


def cmd_list_sites(region_query: str | None) -> None:
    with CgvSession(headless=_headless()) as cgv:
        sites, regions = cgv.sites()

    if region_query:
        q = normalize(region_query)
        matched = [code for code, name in regions.items() if q in normalize(name)]
        if not matched:
            print(f"'{region_query}'에 맞는 지역이 없습니다. 가능한 지역: "
                  + ", ".join(regions.values()))
            return
        sites = [s for s in sites if s.get("regnGrpCd") in matched]

    print(f"극장 {len(sites)}곳:\n")
    for s in sites:
        region = regions.get(s.get("regnGrpCd", ""), "?")
        print(f"  {s['siteNo']}  [{region}] {s['siteNm']}")


def cmd_reset() -> None:
    """모든 감시 대상의 기준선을 지운다. 대상 자체는 지우지 않는다."""
    rows = store.targets()
    for row in rows:
        store.reset_state(row["id"])
    print(f"기준선을 지웠습니다 ({len(rows)}개 대상).")
    print("다음 확인은 현재 상태만 저장하고 알림을 보내지 않습니다.")


def cmd_migrate() -> None:
    result = store.migrate_legacy()
    print(f"감시 대상 {result['targets_added']}개 추가, "
          f"관측 상태 {result['states_imported']}개 이관")
    for problem in result["skipped"]:
        print(f"  건너뜀: {problem}")
    if store.LEGACY_STATE_PATH.exists():
        backup = store.LEGACY_STATE_PATH.with_suffix(".json.bak")
        store.LEGACY_STATE_PATH.replace(backup)
        print(f"state.json을 {backup.name}으로 옮겼습니다 (이제 DB가 기준입니다).")


def run_once(dry_run: bool) -> int:
    """CLI 1회 확인. 결과를 poll_cycles에도 남긴다."""
    with single_instance() as acquired:
        if not acquired:
            log.info("다른 확인이 진행 중이라 이번 차례는 건너뜁니다")
            return 0

        cycle_id = None if dry_run else store.start_cycle("cli")
        try:
            summary = check_all(dry_run=dry_run)
        except RuntimeError as exc:
            if cycle_id is not None:
                store.finish_cycle(cycle_id, ok=False, error=str(exc))
            raise
        if cycle_id is not None:
            store.finish_cycle(cycle_id, ok=True, **summary_fields(summary))
        log.info("확인 완료 — 대상 %d개 / 요청 %d건 / 새 날짜 %d개",
                 summary["targets_checked"], summary["requests"],
                 summary["new_dates"])
    return 0


def cmd_check_seats(dry_run: bool) -> int:
    """좌석 감시를 1회 확인한다. 세션 하나를 띄워 로그인·좌석 조회를 처리한다."""
    import seats

    watches = store.seat_watches(enabled_only=True)
    if not watches:
        log.info("좌석 감시 대상이 없습니다 — 웹에서 추가하세요")
        return 0

    with single_instance() as acquired:
        if not acquired:
            log.info("다른 확인이 진행 중이라 이번 차례는 건너뜁니다")
            return 0
        with CgvSession(headless=_headless()) as session:
            result = seats.check_seat_watches(session, dry_run=dry_run)
    log.info("좌석 감시 완료 — 확인 %d건 / 알림 %d건",
             result["watches_checked"], result["alerts_sent"])
    return 0


def summary_fields(summary: dict) -> dict:
    """check_all의 요약을 store.finish_cycle 인자로 옮긴다."""
    return {
        "targets_checked": summary["targets_checked"],
        "requests": summary["requests"],
        "new_dates": summary["new_dates"],
    }


def report_connect_failure(exc: Exception, dry_run: bool) -> None:
    """브라우저 기동·접속 실패. 연속 3회일 때만 웹훅으로 알린다."""
    log.error("%s", exc)
    if dry_run:
        return
    fails = store.bump_global_fail()
    if fails == FAIL_ALERT_THRESHOLD:
        deliver_alert(
            "connect_error",
            f"⚠️ *CGV 알림기가 사이트에 접속하지 못했습니다* ({fails}회 연속)\n"
            f"`{exc}`",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CGV 예매 가능 날짜가 추가되면 Slack·Discord로 알립니다. "
                    "상시 동작은 `python3 -m web.app`(웹 서버)이 담당합니다."
    )
    parser.add_argument("--once", action="store_true",
                        help="1회 확인 (기본 동작)")
    parser.add_argument("--dry-run", action="store_true",
                        help="웹훅 전송·DB 갱신 없이 감지 결과만 출력")
    parser.add_argument("--list-movies", action="store_true",
                        help="예매 가능 영화 목록 출력 (정확한 이름·코드 확인)")
    parser.add_argument("--list-sites", nargs="?", const="", metavar="지역",
                        help="극장 목록 출력. 지역명을 주면 그 지역만")
    parser.add_argument("--reset", action="store_true",
                        help="기준선을 지웁니다 (감시 대상은 유지)")
    parser.add_argument("--migrate", action="store_true",
                        help="config.toml·state.json을 DB로 이관 (1회용)")
    parser.add_argument("--test-notify", action="store_true",
                        help="웹훅 연동만 테스트 (.env의 전역 웹훅)")
    parser.add_argument("--check-seats", action="store_true",
                        help="좌석 감시를 1회 확인 (로그인 필요)")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그")
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_env()

    if args.test_notify:
        ok = send_webhook(
            "✅ *CGV 알림기 연결 테스트*\n"
            "이 메시지가 보이면 알림이 정상 동작합니다.",
            dry_run=args.dry_run,
        )
        return 0 if ok else 1

    try:
        store.init_db()
    except Exception as exc:  # noqa: BLE001 - 접속 실패 종류가 여러 가지다
        log.error("DB에 접속할 수 없습니다: %s", exc)
        log.error("DATABASE_URL을 확인하세요 (현재: %s)", store.safe_dsn())
        return 1

    if args.migrate:
        cmd_migrate()
        return 0

    if args.reset:
        cmd_reset()
        return 0

    try:
        if args.list_movies:
            cmd_list_movies()
            return 0
        if args.list_sites is not None:
            cmd_list_sites(args.list_sites or None)
            return 0
        if args.check_seats:
            return cmd_check_seats(dry_run=args.dry_run)
        return run_once(dry_run=args.dry_run)
    except RuntimeError as exc:
        report_connect_failure(exc, args.dry_run)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
