#!/usr/bin/env python3
"""자동 예매 — 좌석 선점(auto_book)과 카카오페이 결제 요청(auto_pay).

좌석 감시에서 빈자리(또는 연속 블록)가 감지되고 그 감시의 auto_book이 켜져 있으면,
인원수만큼 좌석을 골라 CGV에 **임시 선점(seatTempPrmp)** 을 건다. auto_pay까지
켜져 있으면 이어서 결제 화면에서 **카카오페이를 고르고 약관에 동의해 결제를
요청**하는 데까지 간다.

**마지막 승인은 사람이 한다.** 카카오페이는 카카오톡 인증이나 결제 비밀번호를
사용자 기기에서 받아야 끝나고, 그건 시스템이 대신할 수 없다. 그래서 결제 요청까지
가서 **휴대폰으로 열면 바로 결제되는 링크**를 받아 알림에 실어 보낸다 — 사람이
CGV 앱을 다시 열어 결제수단부터 고르는 대신, 링크 하나만 눌러 인증하면 된다.

세 층으로 나뉜다:
  - try_auto_book(...)  — 순수 오케스트레이션(좌석 선택·중복 방지·이력 기록·감시 비활성).
    hold_fn·pay_fn을 주입할 수 있어 브라우저 없이 단위 테스트가 된다.
  - hold_block(...)     — 실제 CGV 예매 UI를 몰아 선점을 거는 라이브 구동. 사이트의
    자체 JS가 요청 바디(custNo 등)를 채우므로 우리가 재구성하지 않는다. seatTempPrmp
    응답에서 예매번호·만료시각을 읽고 **결제 화면 앞에서 멈춘다.**
  - pay_block(...)      — 그 뒤를 이어 결제 화면을 몰아 카카오페이 결제창을 띄우고
    결제 링크를 받아 온다. **카카오페이 인증은 건드리지 않는다.**

라이브 구동은 브라우저 워커 스레드에서(세션을 소유한 스레드에서) 실행돼야 한다.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import seats as seats_mod
import store

log = logging.getLogger("cgv-watch.booking")

SEAT_HOLD_URL_MARK = "seatTemp/seatTempPrmp"

BOOKING_PAGE = "https://cgv.co.kr/cnm/movieBook/movie"
# 회차를 고르면 넘어가는 인원 선택 화면. 여기 닿았는지로 진행을 판정한다.
VISITOR_PAGE_MARK = "/cnm/selectVisitorCnt"

# CGV는 접속이 몰리면 가상 대기열을 세운다("대기인원 67명 · 예상 대기시간 3초").
# 그동안 화면은 예매 목록 그대로라, 고정 대기로 넘겨짚으면 다음 단계가 통째로
# 실패한다 — 인원 선택 버튼을 못 찾는 모양으로 나타난다.
QUEUE_MARKS = ("대기중입니다", "대기인원", "예상 대기시간")
QUEUE_WAIT_MS = 90_000
# 대기열이 아닐 때(= 그냥 화면이 넘어가는 중일 때) 얼마나 촘촘히 볼지. 1초씩
# 자면 이미 넘어간 화면을 최대 1초 늦게 알아채고, 그만큼 좌석 경쟁에서 밀린다.
QUEUE_POLL_MS = 200

# 좌석 선택 뒤의 '결제하기'가 **선점까지만** 한다는 전제 위에 선점 단계의 안전성이
# 서 있다. CGV가 그 버튼의 뜻을 바꾸면 우리는 아무것도 눈치채지 못한 채 돈을 쓰게
# 된다 — 코드가 스스로 알아챌 수 있는 유일한 지점이 그때 나가는 요청이므로,
# **승인**(돈이 실제로 빠지는 단계) 계열 경로가 보이면 크게 남기고 실패로 끊는다.
#
# 예전에는 여기 '/pay/'가 있었는데, 그건 결제 화면을 **그리기만 하는** 조회
# (`/api/v1/payment/pay/searchCrdCocdList`, `searchGroupedPaymdList` 등)에도 걸린다.
# 실측해 보니 그 조회들은 선점 +9초쯤에 나가고 hold_block의 감시 구간은 +5초라,
# 지금까지 걸리지 않은 건 타이밍 운이었다 — CGV가 화면을 조금만 빨리 그렸으면
# 성공한 선점이 "결제 계열 요청 감지"로 되돌려졌다. 조회는 빼고, 승인만 남긴다.
# 선점 응답(seatTempPrmp)은 이 목록과 겹치지 않아야 한다.
PAYMENT_URL_MARKS = (
    "payApprov", "payApprv", "atktPay", "movAtktPay",
    "settle", "approvePayment", "paymentComplete",
    # CGV의 PG 게이트웨이. 카카오페이 인증이 끝나면 여기 승인 주소로 돌아온다.
    "onepg.cjsystems.co.kr",
)

# CGV가 주는 시각은 전부 한국 시간이고 시간대 표시가 붙어 있지 않다. 서버가
# 어디서 돌든 같은 뜻이어야 하므로 여기 한 곳에 못박는다 — 컨테이너는 보통
# UTC라, 로컬 시간대로 해석하면 선점 만료가 9시간 뒤로 기록된다. 그러면
# store.active_hold()가 이미 끝난 선점을 유효하다고 보고 그 감시는 영영
# 다시 잡지 않는다.
KST = ZoneInfo("Asia/Seoul")

# 회차(상영 시작 시각) 버튼을 찾는 방법을 확실한 순서로 늘어놓는다.
#
# 해시가 붙은 클래스명(`cinemaSchedule_startTime__ZE5Zp`)은 CSS 모듈이 만든
# 것이라 CGV가 프론트를 **다시 빌드하기만 해도** 사라진다. 해시를 뺀 부분일치를
# 먼저 쓰고, 그것도 빗나가면 더 느슨한 쪽으로 내려간다.
#
# **끝나는 시각이 아니라 시작 시각 요소만 본다.** 버튼 전체 텍스트는
# "15:10-18:00"처럼 끝 시각을 품고 있어서, has_text로 "18:00"을 찾으면 15:10
# 회차가 걸린다 — 엉뚱한 회차를 선점하게 된다.
SHOWTIME_SELECTORS = (
    'span[class*="cinemaSchedule_startTime"]',   # 2026-08 기준 실제 구조
    'span[class*="startTime"]',
    'span[class*="_start"]',
)

# 상영관 블록의 클래스 조각. 같은 시각의 회차가 여러 상영관에 있을 때 어느 쪽인지
# 가리는 데 쓴다.
#
# **안쪽부터 적는다.** 바깥 컨테이너(screenInfoStore_container)는 그 날의 상영관을
# 전부 품고 있어서 어느 후보로 물어도 이름이 걸린다 — 그걸 먼저 보면 아무것도
# 가려지지 않는다. 실제로 '17관[PREMIUM] (Laser)'(12석)와 '17관 (Laser)'(156석)가
# 같은 10:25에 있을 때 둘 다 통과해 버려서, 안전장치가 "가리지 못했습니다"로
# 멈추는 바람에 정작 잡을 수 있는 좌석을 놓쳤다.
SCREEN_CONTAINER_CLASSES = (
    "screenInfo_timeItem",        # 회차 하나 — 이 층에 상영관 이름이 붙는 배치도 있다
    "screenInfo_contentWrap",     # 상영관 하나
    "screenInfoStore_container",  # 그 날 전체 (마지막 수단)
)

# 날짜 스트립. 버튼 하나가 하루이고, 안의 number 스팬이 '31' 또는 '9.1'을 담는다.
# 스와이퍼가 같은 버튼을 숨김 사본으로 한 벌 더 만들어 두므로 **보이는 것만** 쓴다.
DATE_BUTTON_SELECTORS = (
    'button[class*="dayScroll_scrollItem"]',
    '[class*="dayScroll"] button',
)
DATE_NUMBER_SELECTOR = '[class*="dayScroll_number"]'
DATE_ACTIVE_MARK = "itemActive"

# 실패했을 때 화면을 남겨 둘 곳. 셀렉터가 깨졌는지 좌석이 없어진 건지는 스크린샷
# 없이는 사후에 가릴 수 없다.
SHOT_DIR = Path(__file__).resolve().parent / "logs" / "booking"
# 성공한 선점 요청의 형태를 남겨 두는 곳. 지금은 **기록만** 한다 — 언젠가 UI를
# 몰지 않고 seatTempPrmp를 직접 부르려면 그 요청이 어떻게 생겼는지 알아야 하는데,
# 지금 우리는 응답만 가로챌 뿐 요청은 CGV의 JS가 만들어 보내고 있어 형태를 모른다.
HOLD_SPEC_DIR = Path(__file__).resolve().parent / "logs" / "holdspec"

# 기록에서 가려야 할 값. 이름에 이 조각이 들어간 헤더·필드는 값을 지운다 —
# 선점 요청에 로그인 토큰이 실려 나가므로 그대로 남기면 파일이 곧 자격증명이다.
SECRET_HINTS = ("token", "cookie", "authorization", "auth", "secret",
                "password", "pwd", "session", "csrf", "custno")

# ── 결제 화면 (auto_pay) ────────────────────────────────────────────────────
# 2026-08 실측 구조.
#
# 좌석을 고르고 나면 '결제 전 확인해 주세요' 바텀시트가 뜨는데, 거기 [결제하기]는
# **선점만** 건다(seatTempPrmp). 결제 화면으로 넘어가려면 그 버튼을 한 번 더
# 눌러야 한다 — 이 두 클릭이 같은 이름이라 하나로 착각하기 쉽다.
#
# 결제 화면에 닿았는지는 결제수단 목록이 있는지로 판정한다. 주소로는 못 가린다:
# CGV는 결제 화면에서도 주소를 `/cnm/selectVisitorCnt` 그대로 두다가 뒤늦게
# `/mpy/main`으로 바꾼다.
PAY_LIST_SELECTOR = 'ul[class*="basicPaymentList"]'
# 고른 수단의 <li>에 붙는 클래스 조각. 눌렀는데 안 붙었으면 못 고른 것이다.
PAY_ACTIVE_MARK = "basicPaymentList_active"

# 결제수단 칸은 대부분 **로고 이미지**라 글자로는 잡히지 않는다 — img[alt]로 가른다.
# 지금 쓰는 건 카카오페이 하나지만, 늘어날 수 있으니 표로 둔다(seat_watches.pay_method).
PAY_METHODS = {"kakaopay": "카카오페이"}
DEFAULT_PAY_METHOD = "kakaopay"

# 약관. 개별 체크(chk1·chckAgreeList)는 '전체 동의' 하나로 함께 켜진다.
TERMS_ALL_LABEL = 'label[for="chkAll"].chck-icon'
TERMS_ALL_INPUT = "#chkAll"

# 최종 결제 금액. 화면 아래 '최종결제금액' 블록의 강조 숫자다.
FINAL_AMOUNT_SELECTOR = '[class*="mpy_lastPayment"] strong'

# 카카오페이 PC 결제창은 **iframe**으로 뜬다(새 창이 아니다).
KAKAO_BRIDGE_MARK = "online-payment.kakaopay.com"
# 그 iframe이 부르는 내부 API. 응답에 휴대폰용 결제 주소와 만료 시각이 들어 있다.
KAKAO_BRIDGE_API_MARK = "/pc/bridge"
# 휴대폰에서 열면 바로 결제로 이어지는 주소 — **화면의 QR에 담긴 것과 같은 주소**다.
#
# 이 형태는 QR을 직접 디코드해서 확인했다. 예전에는 브릿지 응답의
# ios_app_url 안에 있는 `url=` 파라미터를 썼는데, 그건 다른 주소이고
# (online-pay.kakaopay.com/pay/r1/...) 열면 "인증정보를 찾을 수 없습니다"가 뜬다.
# 게다가 그 주소의 해시는 응답의 `hash` 필드보다 **한 글자 짧다** — 만료 문제로
# 착각하기 딱 좋아서, 갓 만든 링크를 4초 만에 열어 보고서야 형태 문제임을 알았다.
KAKAO_PAY_LINK = ("https://online-payment.kakaopay.com"
                  "/bridge/mobile-pc/reseller/one-time/payment/{hash}")

# 결제 화면으로 넘어갈 때까지 '결제하기'를 다시 눌러 보는 횟수와 한 바퀴의 상한.
PAY_PAGE_ROUNDS = 6
PAY_PAGE_ROUND_MS = 2500
# 결제수단을 누른 뒤 그게 켜졌는지, 약관 동의가 켜졌는지 기다리는 상한.
# 값은 예전 고정 대기와 같다 — 상한만 같고 실제로는 반영되는 즉시 넘어간다.
PAY_METHOD_ACTIVE_MS = 1500
TERMS_CHECK_MS = 800
# 결제 요청 뒤 카카오페이 iframe이 뜨기를 기다리는 시간.
PAY_BRIDGE_WAIT_MS = 25_000
# iframe이 뜬 뒤 브릿지 응답이 늦게 올 때 더 기다리는 상한.
PAY_BRIDGE_BODY_MS = 5000

# ── 화면 전환 기다리기 ───────────────────────────────────────────────────────
# 예전에는 단계마다 고정 시간을 잤다(인원 1.5초 · 좌석맵 1.5초 · 선택완료 2.5초 ·
# 결제하기 5초). 딥링크가 성공하는 정상 경로에서 그 합이 **10.5초**로, 선점 전체
# (실측 18초)의 절반을 넘었다. 좌석 경쟁은 초 단위라 이 시간이 곧 "그 사이 팔린 것
# 같습니다"가 된다 — 화면이 준비되면 **바로** 넘어가도록 짧게 폴링한다.
STEP_POLL_MS = 100
# 날짜를 누른 뒤 그 날짜가 활성으로 바뀌기를 기다리는 상한.
DATE_SELECT_MS = 2500
SEATMAP_READY_MS = 5000      # 좌석맵 모달이 열려 좌석이 눌릴 수 있게 될 때까지
PAY_BUTTON_READY_MS = 6000   # 좌석 선택완료 → '결제하기'가 뜰 때까지
HOLD_RESPONSE_MS = 12000     # '결제하기' → seatTempPrmp 응답이 올 때까지

# 결제창이 뜬 탭을 얼마나 지켜 줄지 (_keep_paying_page).
#
# 기준은 카카오가 준 결제 만료 시각이다. 거기에 여유를 더하는 이유는, 사람이 시한
# 직전에 승인을 눌러도 CGV가 그 결과를 받아 예매를 확정할 틈이 있어야 하기 때문이다.
PAY_PAGE_GRACE_SECONDS = 120
# 만료 시각을 못 읽었을 때. 결제창 수명은 실측 15분이었다.
PAY_PAGE_KEEP_SECONDS = 20 * 60
# 만료가 이미 지났다고 나와도 이만큼은 지킨다 — 시계가 조금 어긋났을 뿐인데
# 방금 띄운 결제창을 바로 닫아 버리는 것이 더 나쁘다.
PAY_PAGE_MIN_KEEP_SECONDS = 60

# 좌석맵에 좌석이 그려졌는지 보는 표식.
#
# **미니맵과 헷갈리면 안 된다.** 화면 아래쪽 작은 좌석 그림(`seatMainMap_seatNumber`)은
# 좌석맵을 열기 **전부터** 좌석 수만큼(실측 115개) 깔려 있고 aria-hidden이다. 그걸
# 보고 "좌석맵이 떴다"고 판정하는 바람에 '선택' 버튼을 아예 누르지 않아, 열리지도
# 않은 좌석맵에 대고 좌석을 누르다 3초씩 타임아웃을 냈다(실측 12.3초 손해).
# 실제로 누를 수 있는 좌석은 모달 안의 `seatMap_seatNumber`다 — 이름이 한 글자
# 차이라 더 조심해야 한다('seatMainMap_'에는 'seatMap_'이 들어 있지 않다).
SEAT_MAP_SELECTOR = '[class*="seatMap_seatNumber"]'
# 좌석맵 모달을 여는 '선택' 버튼. 이미 열려 있으면 없으므로 오래 기다리지 않는다.
SEATMAP_OPEN_MS = 2500

# 화면 전체를 덮는 로딩 가림막. 이게 떠 있는 동안은 아무것도 누를 수 없고,
# **사라지면서 안내 팝업이 뜬다** — 그래서 로딩이 끝나기를 기다렸다가 팝업을
# 닫아야 한다. 로딩 중에 팝업을 닫으러 가면 아직 없어서 헛걸음한다.
LOADING_SELECTOR = '[class*="loading_page"]'
LOADING_WAIT_MS = 6000

# 화면을 덮은 팝업과, 그 안에서 찾을 닫기 수단(앞에 오는 것부터 시도).
# '확인'을 먼저 본다 — 안내 팝업은 대개 그 버튼 하나로 닫히고, 그게 사람이
# 누르는 것과 같은 길이다. 아이콘만 있는 팝업은 btn-close로 닫는다.
MODAL_SELECTOR = '.cgv-modal.active, [role="dialog"][aria-modal="true"]'
MODAL_CLOSE_SELECTORS = (
    'button:has-text("확인")',
    'button:has-text("닫기")',
    'button.btn-close',
)
# **닫으면 안 되는 모달.** 예매 흐름 자체가 모달로 되어 있다 — 좌석맵도,
# '결제 전 확인해 주세요' 바텀시트도 role=dialog에 자기 닫기 버튼(✕)을 달고 있다.
# 그걸 광고 팝업과 같이 취급해 닫으면 우리가 눌러야 할 버튼을 스스로 치워 버린다.
# 실제로 결제 바텀시트를 닫았다 뜨기를 반복하다 결제 화면에 닿지 못했다.
# 이 문구가 보이는 모달은 통과해야 할 화면이므로 건드리지 않는다.
MODAL_KEEP_TEXTS = ("결제하기", "선택완료")

# 좌석 고르기 재시도. 한 바퀴가 [배치도 다시 읽기 → 고르기 → 클릭]이라 1초 안쪽이니
# 몇 번을 돌아도 싸다. 다만 여기서 오래 끌 이유는 없다 — 이 시각을 넘길 만큼
# 경쟁이 심하면 어차피 다음 사이클에 다시 본다.
SEAT_PICK_ATTEMPTS = 3
SEAT_PICK_DEADLINE = 10.0  # 초

# 좌석 하나를 누르는 데 줄 시간. 여기 도착했다는 건 _seatmap_ready가 "보이는
# 좌석이 있다"를 이미 확인했다는 뜻이라, 3초씩 걸릴 이유가 없다 — 그만큼 걸렸다면
# 화면을 덮은 무언가에 막힌 것이고, 그건 기다린다고 풀리지 않는다.
SEAT_CLICK_MS = 1500

# 미리 띄워 둔 예매 화면은 **회차 목록이 그때의 스냅샷**이다. 매진이던 회차에
# 취소표가 나도 화면은 '매진'인 채로 남는다(그 버튼은 aria-disabled라 Playwright가
# "element is not enabled"로 6초를 기다렸다 죽는다). 좌석이 났다는 건 우리가 API로
# 방금 확인한 사실이므로, 화면이 매진이라고 하면 화면 쪽이 낡은 것이다.
#
# 실측(2026-08-28): 탭은 세션 재기동(30분)에만 다시 열려서 그 사이 최대 30분 낡는다.
# 다시 받는 값은 날짜를 다른 날로 옮겼다 되돌리면 된다 — 같은 날짜를 다시 누르는
# 것은 무효고(SPA가 요청을 안 보낸다), 페이지 리로드는 6.2초다. 왕복은 1.2초.
SHOWTIME_STALE_MS = 4000     # 다시 받은 목록에서 그 회차가 살아나기를 기다리는 상한
DATE_SWITCH_MS = 4000        # 날짜 하나를 옮기고 활성으로 바뀌기를 기다리는 상한


def _fmt_hhmm(scnsrt: str) -> str:
    """'2210' → '22:10'. 이미 콜론이 있으면 그대로."""
    s = (scnsrt or "").strip()
    if ":" in s or len(s) < 4:
        return s
    return f"{s[:2]}:{s[2:4]}"


def _parse_limit_dt(raw: str):
    """'20260825160018' → datetime(KST, aware). 실패하면 None.

    `.astimezone()`으로 붙이면 안 된다 — 그건 **실행 환경의** 시간대로 읽는
    것이라 UTC 컨테이너에서는 같은 숫자가 9시간 다른 순간을 가리킨다.
    """
    if not raw or len(raw) < 12:
        return None
    try:
        naive = datetime.strptime(raw[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
        return naive.replace(tzinfo=KST)
    except ValueError:
        return None


def try_auto_book(session, watch: dict, row: dict, parsed_seats: list[dict],
                  *, mov_nm: str = "", site_nm: str = "", site_no: str = "",
                  mov_no: str = "", num_from: int = 0, num_to: int = 0,
                  hold_fn=None, pay_fn=None, dry_run: bool = False) -> dict:
    """감시 하나의 한 회차에서 자동 선점을(auto_pay면 결제 요청까지) 시도한다.

    반환: {"action": skip|held|failed|no_seats, ...}. hold_fn(session, ctx)->result 와
    pay_fn(session, ctx, method=...)->result 를 주입하면 라이브 구동 대신 그걸
    쓴다(테스트용). 기본은 hold_block·pay_block.

    여기서 고르는 좌석은 **후보**다. 감지 때 읽은 배치도는 UI를 모는 동안 낡으므로,
    실제로 누를 좌석은 좌석맵에 도착해서 다시 고른다(hold_block → _select_block).
    그래서 이력에 남는 좌석도 hold가 실제로 고른 것으로 덮어쓴다.
    """
    if not watch.get("auto_book"):
        return {"action": "skip", "reason": "auto_book off"}

    watch_id = watch["id"]
    showtime_key = seats_mod.showtime_key(row)
    # 이미 유효한 선점이 있으면 다시 잡지 않는다. 이 감시가 잡은 것뿐 아니라
    # **같은 회차를 보는 다른 감시가 잡은 것도** 막아야 한다 — 열만 다르게 건
    # 감시 둘이 같은 회차에서 각각 선점하면 한 사람이 두 번 결제하게 된다.
    if not dry_run and store.active_hold(
            watch_id, owner_id=watch.get("owner_id"), showtime_key=showtime_key,
            scn_ymd=watch["scn_ymd"], site_nm=site_nm):
        return {"action": "skip", "reason": "already held"}

    party = max(1, int(watch.get("party_size") or 1))
    chosen = seats_mod.pick_block(parsed_seats, party, watch.get("rows"),
                                  num_from, num_to)
    if len(chosen) < party:
        return {"action": "no_seats", "reason": f"{party}석 연속 없음"}

    labels = [s["label"] for s in chosen]
    loc_nos = [s["seat_loc_no"] for s in chosen]
    start_hhmm = _fmt_hhmm(row.get("scnsrtTm"))

    if dry_run:
        return {"action": "held", "dry_run": True, "seats": labels}

    attempt_id = store.create_booking_attempt(
        seat_watch_id=watch_id, owner_id=watch.get("owner_id"),
        showtime_key=showtime_key, mov_nm=mov_nm, site_nm=site_nm,
        scn_ymd=watch["scn_ymd"], start_hhmm=start_hhmm,
        seat_labels=labels, seat_loc_nos=loc_nos)

    ctx = {"mov_nm": mov_nm, "site_nm": site_nm, "scn_ymd": watch["scn_ymd"],
           "start_hhmm": start_hhmm, "seat_labels": labels, "party": party,
           # 좌석맵에서 다시 고를 때 쓴다 — 후보와 같은 조건이어야 한다.
           # 범위가 빠지면 후보는 선호 구역인데 실제로 누르는 좌석은 그 밖이 된다.
           "rows": watch.get("rows"), "site_no": site_no,
           "num_from": num_from, "num_to": num_to,
           # 예매 화면을 딥링크로 바로 열 때 쓴다 (booking_url).
           "mov_no": mov_no,
           # 같은 시각의 회차가 여러 상영관에 있을 때 어느 쪽인지 가리는 데 쓴다.
           "scns_nm": row.get("expoScnsNm") or row.get("scnsNm") or "",
           "row": row}
    fn = hold_fn or hold_block
    try:
        result = fn(session, ctx)
    except Exception as exc:  # noqa: BLE001 - 라이브 구동 실패는 이력에 남기고 넘어간다
        log.warning("auto-book 선점 실패(예외): %s", exc)
        store.finish_booking_attempt(attempt_id, "failed", error=str(exc))
        return {"action": "failed", "error": str(exc), "attempt_id": attempt_id,
                "seats": labels}

    # hold가 좌석맵에서 다시 골랐으면 그쪽이 실제로 누른 좌석이다. 알림과 이력이
    # 후보를 그대로 적으면 사용자가 받은 문구와 실제 잡힌 자리가 어긋난다.
    final = list(result.get("seat_labels") or labels)

    if result.get("ok"):
        # 선점이 됐으면 이어서 결제를 요청한다. **결제가 실패해도 선점은 유효하다**
        # — 여기서 held를 되돌리면 사람이 손으로 마칠 수 있는 예매까지 잃는다.
        pay = _try_pay(session, watch, ctx, pay_fn) if watch.get("auto_pay") else {}
        store.finish_booking_attempt(
            attempt_id, "held", mov_atkt_no=result.get("mov_atkt_no"),
            amount=pay.get("amount") or result.get("amount"), seat_labels=final,
            hold_expires_at=result.get("hold_expires_at"),
            pay_method=pay.get("method"), pay_url=pay.get("pay_url"),
            pay_expires_at=pay.get("pay_expires_at"),
            pay_error=pay.get("error") or None)
        # 선점에 성공하면 그 감시는 꺼서 중복 선점을 막는다.
        store.set_seat_watch(watch_id, enabled=False)
        return {"action": "held", "attempt_id": attempt_id, "seats": final,
                "mov_atkt_no": result.get("mov_atkt_no"),
                "hold_expires_at": result.get("hold_expires_at"),
                "amount": pay.get("amount") or result.get("amount"),
                "pay_url": pay.get("pay_url"),
                "pay_expires_at": pay.get("pay_expires_at"),
                "pay_error": pay.get("error") or None}

    store.finish_booking_attempt(attempt_id, "failed", seat_labels=final,
                                 error=result.get("error") or "선점 실패")
    return {"action": "failed", "error": result.get("error"),
            "attempt_id": attempt_id, "seats": final}


def _try_pay(session, watch: dict, ctx: dict, pay_fn=None) -> dict:
    """선점 직후 결제를 요청한다. 실패해도 예외를 밖으로 내지 않는다.

    결제 요청이 실패하는 것과 선점이 실패하는 것은 무게가 다르다 — 좌석은 이미
    잡혀 있으니, 사람이 CGV 앱에서 손으로 마치면 된다. 그래서 여기서는 사유만
    챙겨 돌려주고 호출자는 held를 유지한다.
    """
    fn = pay_fn or pay_block
    method = (watch.get("pay_method") or DEFAULT_PAY_METHOD).strip()
    try:
        out = fn(session, ctx, method=method)
    except Exception as exc:  # noqa: BLE001
        log.warning("자동 결제 요청 실패(예외): %s", exc)
        return {"method": method, "error": str(exc)}
    if not out.get("ok"):
        log.warning("자동 결제 요청 실패: %s", out.get("error"))
    elif out.get("pay_url"):
        _keep_paying_page(session, ctx, out.get("pay_expires_at"))
    return out


def _keep_paying_page(session, ctx: dict, pay_expires_at) -> None:
    """결제창이 뜬 탭을 워밍 풀에서 빼 둔다. 실패해도 결제는 그대로 둔다.

    **이걸 안 하면 그 탭이 다음 사이클에 예매 화면으로 되돌아간다.** 예매 탭은
    (영화·극장·날짜) 하나당 한 장을 공유하는데, 선점에 성공한 감시는 꺼져도 같은
    날짜를 보는 다른 감시는 살아 있어서 그쪽 프리워밍이 같은 탭을 집어 간다.
    실측(2026-09-03): 결제 링크가 나간 13:36:34의 4초 뒤 13:36:38에 그 탭이
    `예매 화면을 20260904로 바로 열었습니다`로 덮였다. 카카오페이 승인은 그 창이
    받아 CGV에 넘겨야 예매가 확정되므로, 덮이면 **돈은 나가고 좌석은 안 잡힌다.**

    지켜 주는 시간은 카카오가 준 결제 만료 시각까지 + 여유다. 그 시각을 못 읽었을
    때만 고정값을 쓴다.
    """
    page = ctx.get("_page")
    if page is None:
        return                  # 주입된 hold_fn으로 돈 경우 — 지킬 탭이 없다
    keep = PAY_PAGE_KEEP_SECONDS
    if pay_expires_at is not None:
        try:
            # 시간대가 없으면 KST로 본다 — 카카오·CGV가 주는 시각은 전부 한국 시간이다.
            exp = (pay_expires_at if pay_expires_at.tzinfo
                   else pay_expires_at.replace(tzinfo=KST))
            left = (exp - datetime.now(KST)).total_seconds()
            keep = max(PAY_PAGE_MIN_KEEP_SECONDS, left + PAY_PAGE_GRACE_SECONDS)
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass                # 시각을 못 빼면 고정값으로 간다
    try:
        if session.detach_booking_page(warm_key(ctx), keep):
            log.info("결제창이 뜬 탭을 %d초간 지킵니다 — 다른 감시의 프리워밍이 "
                     "덮지 못하게 워밍 풀에서 뺐습니다.", int(keep))
    except Exception as exc:  # noqa: BLE001 - 못 빼도 결제 링크는 이미 나갔다
        log.warning("결제창 탭을 지키지 못했습니다 (%s) — 다른 감시가 그 화면을 "
                    "덮으면 승인이 CGV까지 가지 않습니다.", exc)


def _earliest(*values):
    """주어진 시각 중 가장 이른 것. 전부 비어 있으면 None.

    비교하려면 시간대가 붙어 있어야 한다 — naive가 섞이면 TypeError가 나므로
    시간대 없는 값은 KST로 본다(CGV·카카오가 주는 시각은 전부 한국 시간이다).
    """
    out = []
    for value in values:
        if value is None:
            continue
        try:
            out.append(value if value.tzinfo else value.replace(tzinfo=KST))
        except AttributeError:  # datetime이 아닌 것은 비교하지 않는다
            continue
    return min(out) if out else None


def _fmt_kst_hhmm(value) -> str:
    """알림에 적을 시각. 읽는 사람은 한국에 있다 — 서버가 어디서 돌든 KST로."""
    if value is None:
        return ""
    try:
        return value.astimezone(KST).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return str(value)


def build_hold_alert(mov_nm: str, site_nm: str, scn_ymd: str, start_hhmm: str,
                     seat_labels: list[str], hold_expires_at, amount,
                     pay_url: str | None = None, pay_expires_at=None,
                     pay_error: str | None = None) -> str:
    """선점 성공 알림. 결제 링크가 있으면 그걸 앞세운다.

    자동 결제가 켜져 있으면 카카오페이 결제창까지 띄워 둔 상태이므로, 사람이 할
    일은 **링크를 눌러 인증하는 것 하나**다. 링크를 못 받았으면(자동 결제가
    꺼져 있거나 요청이 실패했으면) 예전처럼 CGV에서 직접 결제하도록 안내한다.
    """
    from watch import BOOKING_URL, fmt_date

    # 마감은 **이른 쪽**이다. 실측에서 선점은 5분 남짓, 카카오페이 링크는 15분을
    # 버텼다 — 링크 만료만 적으면 좌석이 이미 풀린 뒤에도 아직 시간이 있는 줄 알고
    # 결제를 시도하게 된다. 반대로 링크가 먼저 죽는 경우도 있을 수 있으니 둘 중
    # 이른 것을 하나만 적는다.
    when = _fmt_kst_hhmm(_earliest(hold_expires_at, pay_expires_at))
    amt = f"\n💳 예상 금액 {amount:,}원" if amount else ""
    limit = f"\n⏰ *{when}까지 결제*해야 좌석이 유지됩니다" if when else ""
    head = "🎫 *좌석 선점 완료 — 결제만 남았습니다*"

    if pay_url:
        head = "🎫 *좌석 선점 완료 — 카카오페이 결제만 누르면 됩니다*"
        tail = f"\n▶ 휴대폰에서 열어 카카오페이로 결제하세요:\n{pay_url}"
    else:
        why = f"\n⚠️ 자동 결제를 마치지 못했습니다: {pay_error}" if pay_error else ""
        tail = (f"{why}\n▶ CGV 앱/웹에서 예매 진행 중인 건으로 결제를 "
                f"완료하세요: {BOOKING_URL}")

    return (f"{head}\n"
            f"*{mov_nm}* · CGV {site_nm}\n"
            f"{fmt_date(scn_ymd)} {start_hhmm} · {', '.join(seats_mod.sort_labels(seat_labels))}"
            f"{amt}{limit}{tail}")


# ── 라이브 구동: CGV 예매 UI를 몰아 선점 (결제 확정 안 함) ────────────────────
def date_labels(scn_ymd: str) -> list[str]:
    """'20260831' → 날짜 스트립에 적힐 법한 표기들 ['31', '31', '8.31'].

    CGV가 같은 날짜를 세 가지로 적는다(2026-08-31 실측, 오늘이 8/31일 때):

        31 · 9.1 · 02 · 03 · 04 …
        ↑     ↑     ↑
        패딩 없음  월.일  **0 패딩**

    달이 바뀌는 첫날만 '9.1'이고, 그 뒤로는 '02'처럼 0이 붙는다. 이걸 놓쳐서
    9/2 이후 딥링크가 전부 "다른 날짜"로 잘못 판정됐다 — 화면은 그 날짜를 제대로
    열어 놓고도 우리가 실패로 보고 클릭 경로로 되돌아갔다.

    어느 표기가 쓰일지는 오늘이 며칠인지에 달려 있으므로 후보를 다 내놓고
    실제로 있는 쪽을 쓴다. 비교가 정확일치라(부분일치가 아니라) 후보를 늘려도
    엉뚱한 날짜에 걸리지 않는다 — 스트립은 오늘부터 열흘 남짓이라 같은 일자가
    두 번 나올 수 없다.
    """
    ymd = "".join(ch for ch in (scn_ymd or "") if ch.isdigit())
    if len(ymd) != 8:
        raise RuntimeError(f"날짜를 이해할 수 없습니다: {scn_ymd!r}")
    month, day = int(ymd[4:6]), int(ymd[6:8])
    out = [str(day), f"{day:02d}", f"{month}.{day}"]
    # 1~9일은 앞의 둘이 다르고 10일부터는 같다 — 중복은 지운다.
    return list(dict.fromkeys(out))


def _visible(nodes) -> list:
    return [n for n in nodes if n.is_visible()]


def _wait_for_any(page, selectors, timeout: int = 9000) -> None:
    """셀렉터 중 하나라도 화면에 나타날 때까지 기다린다.

    아래에서 `locator(...).all()`로 요소를 훑는데, `.all()`은 Playwright의 자동
    대기를 **거치지 않는다** — 아직 안 그려졌으면 그냥 빈 목록이 온다. 그대로 두면
    화면이 조금만 느려도 "버튼을 찾지 못했습니다"로 끝난다.

    끝내 안 나타나도 여기서 예외를 내지는 않는다. 판정은 호출자가 해야 더 쓸모
    있는 문구(어떤 날짜를, 어떤 시각을 찾고 있었는지)를 낼 수 있다.

    **한꺼번에 기다린다.** 예전에는 시간을 셀렉터 수로 쪼개 하나씩 시도했는데,
    그러면 맞는 셀렉터가 제 몫(3초)보다 조금 늦게 뜨기만 해도 포기하고 다음
    후보로 넘어가 처음부터 다시 기다린다. 회차 목록이 딱 그랬다 — 실측 5.3초 중
    3초가 '첫 셀렉터를 기다리다 버린 시간'이었고, 정작 화면에는 그 첫 셀렉터가
    쓰이고 있었다. 콤마로 묶으면 어느 것이든 먼저 뜨는 순간 돌아온다.
    """
    joined = ", ".join(selectors)
    try:
        page.wait_for_selector(joined, timeout=timeout, state="visible")
    except Exception:  # noqa: BLE001 - 판정은 호출자가 한다
        pass


def _click_visible(page, text: str, *, exact: bool, what: str,
                   timeout: int = 8000) -> None:
    """그 문구를 담은 **보이는** 요소를 누른다.

    `get_by_text(...).first`를 그냥 쓰면 안 된다. 이 화면은 스와이퍼와 접힌
    바텀시트가 같은 제목을 숨김 사본으로 한 벌 더 만들어 두는데, `.first`가 하필
    그쪽을 잡으면 Playwright가 "element is not visible"로 타임아웃까지 기다렸다가
    죽는다 — 8/25 자동 예매 실패가 이 모양이었다. `_click_date`가 `_visible()`로
    거르는 것과 같은 이유다.
    """
    try:
        page.wait_for_selector(f"text={text}", timeout=timeout, state="visible")
    except Exception:  # noqa: BLE001 - 아래에서 후보를 세어 더 나은 문구를 낸다
        pass
    nodes = [n for n in page.get_by_text(text, exact=exact).all() if n.is_visible()]
    if not nodes:
        total = page.get_by_text(text, exact=exact).count()
        raise RuntimeError(
            f"{what} '{text}'을(를) 화면에서 찾지 못했습니다 "
            f"(숨겨진 것 {total}개)")
    nodes[0].click(timeout=timeout)


def _click_role(page, name: str, *, what: str, exact: bool = False,
                timeout: int = 5000) -> None:
    """그 이름을 가진 **보이는** 버튼이 나타나면 누른다.

    `_click_visible`과 달리 접근성 이름으로 찾는다. 관람인원 버튼은 숫자와 '선택'이
    서로 다른 요소라 텍스트 매칭으로는 걸리지 않는다.

    **나타날 때까지 기다린다.** `.all()`은 지금 이 순간의 DOM을 그대로 찍어 올 뿐
    Playwright의 자동 대기가 걸리지 않아서, 화면이 아직 그려지는 중이면 "버튼이
    화면에 없습니다"로 즉시 실패한다. 단계 사이의 고정 대기를 걷어내자마자 관람인원
    단계가 이 모양으로 죽었다 — 고정 대기가 우연히 그 시간을 벌어 주고 있었던 것이다.
    """
    found: list = []

    def ready() -> bool:
        found[:] = [b for b in
                    page.get_by_role("button", name=name, exact=exact).all()
                    if b.is_visible()]
        return bool(found)

    if not _wait_until(page, ready, timeout):
        raise RuntimeError(f"{what} '{name}' 버튼이 화면에 없습니다")
    _click_through_modals(page, found[0], what=what, timeout=timeout)


def booking_url(mov_no: str, site_no: str, site_nm: str, scn_ymd: str) -> str:
    """영화·극장·날짜가 이미 골라진 예매 화면 주소.

    예매 페이지가 movNo·siteNo·siteNm·scnYmd를 주소에서 읽는다. 이 주소로 바로
    들어가면 영화 → 극장 → 날짜 클릭 세 단계를 통째로 건너뛴다. 그 구간이
    자동 예매가 가장 자주 죽던 곳이다(숨겨진 요소를 눌러 타임아웃).

    **극장 이름(siteNm)이 빠지면 동작하지 않는다.** siteNo만으로는 화면이 극장을
    고르지 못한다 — 실측으로 확인했다.
    """
    from urllib.parse import urlencode

    return BOOKING_PAGE + "?" + urlencode({
        "movNo": mov_no, "siteNo": site_no, "siteNm": site_nm, "scnYmd": scn_ymd})


# 날짜 스트립의 상태를 **한 번에** 읽는다.
#
# 예전에는 노드마다 왕복했다: locator().all() → is_visible() 24번 →
# get_attribute("class") 12번 → text_content(). 스와이퍼가 숨김 사본을 한 벌 더
# 만들어 두므로 버튼이 24개고, 왕복 40번쯤이 한 번의 판정에 들어갔다. 실제
# CGV 화면은 무거워서 배포에서 이 판정 하나가 783ms였다(프리워밍 6회에 4.7초).
#
# 같은 것을 evaluate 한 번으로 읽으면 로컬 실측 69.6ms → 2.2ms(31배)다.
_DATE_STATE_JS = r"""(arg) => {
  const vis = (e) => e.offsetWidth || e.offsetHeight;
  // 셀렉터를 콤마로 묶어 한 번에 찾는다. querySelectorAll이 노드를 중복 없이
  // 돌려주므로, 예전처럼 후보를 순서대로 훑을 필요가 없다.
  const days = [...document.querySelectorAll(arg.daySel)].filter(vis);
  const num = (b) => {
    const n = b.querySelector(arg.numSel);
    return n ? (n.textContent || '').trim() : '';
  };
  return {
    url: location.href,
    // 회차 목록이 그려졌는지. 화면준비 판정이 이것과 날짜를 함께 본다.
    showtimes: arg.showSel ? document.querySelectorAll(arg.showSel).length : 0,
    days: days.length,
    actives: days.filter((b) => String(b.className || '').includes(arg.activeMark))
                 .map(num),
  };
}"""


def _date_state(page, *, with_showtimes: bool = False) -> dict | None:
    """화면 상태 {url, showtimes, days, actives}. 못 읽으면 None."""
    try:
        return page.evaluate(_DATE_STATE_JS, {
            "daySel": ", ".join(DATE_BUTTON_SELECTORS),
            "numSel": DATE_NUMBER_SELECTOR,
            "activeMark": DATE_ACTIVE_MARK,
            "showSel": ", ".join(SHOWTIME_SELECTORS) if with_showtimes else "",
        })
    except Exception:  # noqa: BLE001 - 갈아 끼우는 중이면 확인 불가로 본다
        return None


def _date_is_selected(page, wanted: list[str]) -> bool | None:
    """날짜 스트립에서 원하는 날이 활성인지. 확인할 수 없으면 None.

    `_assert_date_selected`와 달리 예외를 내지 않는다 — 딥링크가 먹었는지 보고
    아니면 예전 클릭 경로로 되돌아가야 하므로, 판정만 돌려준다.

    **세 값의 뜻이 다르다.** True는 그 날짜가 골라져 있다는 것, False는 다른
    날짜가 골라져 있다는 것, None은 확인할 수 없다는 것이다(활성 표시를 못 찾음).
    딥링크 성공 판정이 여기 달려 있어서 셋을 뭉개면 안 된다.
    """
    state = _date_state(page)
    if not state or not state.get("actives"):
        return None                     # 활성 표시가 없다 — 확인 불가
    return any(text in wanted for text in state["actives"])


def showtime_ids(row: dict | None) -> tuple[str, str]:
    """상영표 한 줄에서 회차를 가리키는 두 값 (scnsNo, scnSseq). 없으면 빈 문자열.

    `seats.seat_map_key`가 쓰는 것과 같은 필드다 — 회차를 가리키는 이름을 한
    가지로 두어야 요청 검증과 좌석맵 조회가 같은 회차를 말한다.
    """
    if not row:
        return "", ""
    return str(row.get("scnsNo") or ""), str(row.get("scnSseq") or "")


def warm_key(ctx: dict) -> str:
    """예매 화면 탭을 가르는 키 — 영화·극장·날짜가 같으면 같은 화면이다."""
    return "|".join((str(ctx.get("mov_no") or ""), str(ctx.get("site_no") or ""),
                     str(ctx.get("scn_ymd") or "")))


def booking_page(session, ctx: dict):
    """이 ctx의 예매 화면을 담당할 탭. 세션이 탭을 못 주면 기본 페이지를 쓴다."""
    try:
        return session.booking_page(warm_key(ctx))
    except Exception as exc:  # noqa: BLE001 - 탭을 못 열면 예전처럼 한 장으로 간다
        log.debug("예매 탭을 얻지 못했습니다 (%s) — 기본 페이지를 씁니다", exc)
        return session.page


def prewarm(session, ctx: dict) -> bool:
    """예매 화면을 미리 띄워 둔다. 쓸 준비가 됐으면 True.

    딥링크로 예매 화면을 **새로 여는 데만 6.2초**가 든다(회차 목록이 그려질
    때까지). 좌석이 난 순간 그 6.2초를 쓰면 이미 늦는다 — 그래서 자동 예매를 켠
    감시의 화면을 미리 열어 두고, 선점할 때는 그 탭을 그대로 쓴다(실측 0초).

    이미 그 날짜가 열려 있으면 아무것도 하지 않는다. 조용히 실패해도 되는 일이라
    (선점할 때 어차피 다시 확인하고 필요하면 그때 연다) 예외를 밖으로 내지 않는다.
    """
    if not ctx.get("mov_no"):
        return False
    try:
        page = booking_page(session, ctx)
        if _already_on_booking(page, ctx):
            return True
        return _open_booking_direct(page, ctx)
    except Exception as exc:  # noqa: BLE001 - 미리 여는 일이 실패해도 선점은 시도한다
        log.debug("예매 화면 프리워밍 실패: %s", exc)
        return False


def _already_on_booking(page, ctx: dict) -> bool:
    """그 탭이 이미 이 조합의 예매 화면을 띄우고 있는지.

    주소만 보면 안 된다 — 날짜는 화면 상태이지 주소가 아니고, 우리가 띄워 둔 뒤
    CGV가 화면을 되돌렸을 수도 있다. 회차 목록이 떠 있고 날짜가 그 날짜로 골라져
    있을 때만 '준비됐다'고 본다.
    """
    # 주소·회차 목록·날짜를 evaluate **한 번**에 읽는다. 이 판정은 자동 예매를
    # 켠 감시마다 매 사이클 도는 자리라, 노드마다 왕복하면 그게 곧 사이클
    # 길이가 된다(배포 실측 783ms → 프리워밍 6회에 4.7초).
    state = _date_state(page, with_showtimes=True)
    if not state:
        return False
    if BOOKING_PAGE not in (state.get("url") or ""):
        return False
    if not state.get("showtimes"):
        return False
    wanted = date_labels(ctx["scn_ymd"])
    return any(text in wanted for text in state.get("actives") or [])


def _open_booking_direct(page, ctx: dict) -> bool:
    """딥링크로 그 날짜의 상영표까지 바로 연다. 확인되면 True.

    날짜가 맞다고 **확인될 때만** True다. 확인 못 하면 False를 내 호출자가 예전
    클릭 경로로 돌아가게 한다 — 그쪽도 자기 검증을 한다. 잘못된 날짜를 선점하는
    것보다 10초 더 쓰는 편이 훨씬 낫다.
    """
    if not ctx.get("mov_no"):
        return False
    url = booking_url(ctx["mov_no"], ctx.get("site_no") or "",
                      ctx.get("site_nm") or "", ctx["scn_ymd"])
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:  # noqa: BLE001 - 클릭 경로로 폴백한다
        log.warning("예매 화면 딥링크 접속 실패 (%s) — 클릭으로 진행합니다", exc)
        return False

    # 고정 대기 대신 날짜 스트립이 그려질 때까지 기다린다.
    _wait_for_any(page, DATE_BUTTON_SELECTORS)
    verdict = _date_is_selected(page, date_labels(ctx["scn_ymd"]))
    if verdict:
        log.info("예매 화면을 %s로 바로 열었습니다 (클릭 3단계 생략)",
                 ctx["scn_ymd"])
        return True
    log.warning("딥링크가 %s를 열지 못했습니다(%s) — 클릭으로 진행합니다",
                ctx["scn_ymd"],
                "다른 날짜" if verdict is False else "날짜 확인 불가")
    return False


def _click_date(page, scn_ymd: str) -> None:
    """상영 날짜를 고른다.

    **이 단계가 없으면 예매 화면은 오늘 상영표를 보여준다.** 그래서 감시가 8/31인데
    오늘(8/25)의 회차 중에서 같은 시각을 찾게 되고, 운 나쁘게 시각이 겹치면
    **엉뚱한 날짜의 좌석을 선점한다.** 눌렀는지에서 멈추지 않고 실제로 그 날짜가
    선택됐는지까지 확인하는 이유다.
    """
    wanted = date_labels(scn_ymd)
    _wait_for_any(page, DATE_BUTTON_SELECTORS)
    last_exc: Exception | None = None

    for i, selector in enumerate(DATE_BUTTON_SELECTORS):
        try:
            buttons = _visible(page.locator(selector).all())
        except Exception as exc:  # noqa: BLE001 - 다음 셀렉터로
            last_exc = exc
            continue
        for button in buttons:
            try:
                number = button.locator(DATE_NUMBER_SELECTOR).first
                text = (number.text_content() or "").strip()
            except Exception:  # noqa: BLE001 - number 스팬이 없는 버튼은 건너뛴다
                continue
            if text not in wanted:
                continue
            button.click(timeout=6000)
            # 눌린 날짜가 활성으로 바뀌기를 기다린다. 바로 뒤 _assert_date_selected가
            # 어차피 검증하므로, 여기서 고정 2.5초를 잘 이유가 없다.
            _wait_until(page, lambda: _date_is_selected(page, wanted) is True,
                        DATE_SELECT_MS)
            _assert_date_selected(page, wanted, scn_ymd)
            if i:
                log.warning("날짜 버튼을 대체 셀렉터로 찾았습니다 (%s) — CGV가 "
                            "화면을 바꾼 것 같습니다.", selector)
            return
    raise RuntimeError(
        f"{scn_ymd} 날짜 버튼을 찾지 못했습니다 (찾은 표기: {' 또는 '.join(wanted)})"
        f"{f' — {last_exc}' if last_exc else ''}")


def _assert_date_selected(page, wanted: list[str], scn_ymd: str) -> None:
    """고른 날짜가 실제로 활성화됐는지 본다. 아니면 진행하지 않는다.

    클릭이 먹지 않았는데 그대로 밀고 나가면 오늘 상영표에서 회차를 고르게 된다 —
    선점까지 성공하면 되돌리기 어려우므로 여기서 끊는다.
    """
    verdict = _date_is_selected(page, wanted)
    if verdict is True:
        return
    if verdict is False:
        raise RuntimeError(
            f"{scn_ymd} 날짜가 선택되지 않았습니다 — 화면은 다른 날짜를 "
            f"보고 있습니다. 엉뚱한 날짜를 선점하지 않도록 멈춥니다.")
    log.warning("날짜 선택 상태를 확인할 수 없습니다 (활성 표시를 못 찾음) — "
                "%s로 진행합니다", scn_ymd)


class _ShowtimeBlocked(RuntimeError):
    """그 회차가 매진·예매종료로 표시돼 눌리지 않는다.

    '못 찾았다'와 구분해야 한다 — 목록을 다시 받아 풀 수 있는 건 이쪽뿐이다.
    """


def _click_showtime(page, start_hhmm: str, screen_name: str = "",
                    scn_ymd: str = "") -> None:
    """상영 시작 시각 버튼을 누른다. 셀렉터를 순서대로 내려가며 찾는다.

    시작 시각이 **정확히** 일치하는 요소만 본다. 부분일치로 두면 '18:00'이
    '18:00-21:02'의 끝 시각이나 '218:00' 같은 것에도 걸릴 수 있다.

    같은 시각의 회차가 여러 상영관에 있으면(IMAX 21:00과 일반관 21:00) 상영관
    이름으로 가린다. 그래도 못 가리면 **아무거나 누르지 않고 멈춘다** — 잘못
    고르면 다른 상영관의 좌석을 선점하게 되고, 그건 안 잡느니만 못하다.

    화면이 '매진'이라고 하면 **화면 쪽이 낡은 것으로 본다.** 좌석이 났다는 건
    방금 API로 확인한 사실이고, 미리 띄워 둔 탭의 회차 목록은 최대 30분 전
    스냅샷이기 때문이다(scn_ymd를 주면 다시 받아 한 번 더 시도한다).
    """
    _wait_for_any(page, SHOWTIME_SELECTORS)
    try:
        _find_and_click_showtime(page, start_hhmm, screen_name)
        return
    except _ShowtimeBlocked:
        if not scn_ymd:
            raise
    log.info("%s 회차가 화면에는 매진입니다 — 미리 띄운 화면이 낡았을 수 있어 "
             "회차 목록을 다시 받습니다", start_hhmm)
    if not _refresh_showtimes(page, scn_ymd):
        raise RuntimeError(
            f"{start_hhmm} 회차가 매진으로 표시돼 있고 목록을 다시 받지도 "
            f"못했습니다")
    _wait_for_any(page, SHOWTIME_SELECTORS)
    # 날짜가 활성으로 바뀌는 것과 목록이 다시 그려지는 것 사이에 틈이 있다.
    # 그 틈에 확인하면 아직 낡은 목록을 보고 "정말 매진"이라고 단정하게 된다.
    _wait_until(page, lambda: not _showtime_is_blocked(page, start_hhmm,
                                                       screen_name),
                SHOWTIME_STALE_MS)
    # 다시 받은 뒤에도 막혀 있으면 정말 매진이다 — 여기서 6초를 더 쓰지 않는다.
    _find_and_click_showtime(page, start_hhmm, screen_name, refreshed=True)


def _showtime_is_blocked(page, start_hhmm: str, screen_name: str) -> bool:
    """지금 화면에서 그 회차가 매진으로 표시돼 있는지. 못 찾으면 막힌 것으로 본다."""
    for selector in SHOWTIME_SELECTORS:
        try:
            nodes = [n for n in _visible(page.locator(selector).all())
                     if (n.text_content() or "").strip() == start_hhmm]
        except Exception:  # noqa: BLE001 - 다음 셀렉터로
            continue
        if not nodes:
            continue
        nodes = _narrow_by_screen(nodes, screen_name)
        return bool(nodes) and _showtime_blocked(nodes[0])
    return True


def _find_and_click_showtime(page, start_hhmm: str, screen_name: str,
                             *, refreshed: bool = False) -> None:
    last_exc: Exception | None = None
    for i, selector in enumerate(SHOWTIME_SELECTORS):
        try:
            nodes = [n for n in _visible(page.locator(selector).all())
                     if (n.text_content() or "").strip() == start_hhmm]
        except Exception as exc:  # noqa: BLE001 - 다음 셀렉터로 넘어간다
            last_exc = exc
            continue
        if not nodes:
            continue

        nodes = _narrow_by_screen(nodes, screen_name)
        if len(nodes) > 1:
            raise RuntimeError(
                f"{start_hhmm} 회차가 {len(nodes)}곳에 있어 어느 상영관인지 "
                f"가리지 못했습니다 (상영관: {screen_name or '지정 없음'}). "
                f"엉뚱한 상영관을 선점하지 않도록 멈춥니다.")

        if _showtime_blocked(nodes[0]):
            # 눌러 봐야 Playwright가 타임아웃까지 기다렸다 죽는다.
            if refreshed:
                raise RuntimeError(
                    f"{start_hhmm} 회차가 매진입니다 (목록을 다시 받아 "
                    f"확인했습니다) — 그 사이 팔린 것 같습니다")
            raise _ShowtimeBlocked(
                f"{start_hhmm} 회차가 매진으로 표시돼 있습니다")
        _click_showtime_node(nodes[0], start_hhmm)
        if i:
            log.warning("회차 버튼을 대체 셀렉터로 찾았습니다 (%s) — CGV가 "
                        "화면을 바꾼 것 같습니다. SHOWTIME_SELECTORS를 "
                        "확인하세요.", selector)
        return
    raise RuntimeError(
        f"{start_hhmm} 회차 버튼을 찾지 못했습니다"
        f"{f' — {last_exc}' if last_exc else ''}")


def _showtime_blocked(node) -> bool:
    """그 회차 버튼이 지금 눌리지 않는 상태인지(매진·예매종료).

    CGV는 `disabled` 속성이 아니라 **aria-disabled="true"** 로 표시한다(실측).
    Playwright는 이걸 '실행 불가'로 보고 타임아웃까지 기다리므로, 미리 알아채면
    6초를 버리지 않는다.
    """
    try:
        button = node.locator("xpath=ancestor::button[1]")
        if not button.count():
            return False
        first = button.first
        return (first.get_attribute("aria-disabled") == "true"
                or first.get_attribute("disabled") is not None)
    except Exception:  # noqa: BLE001 - 못 읽으면 막힌 것으로 보지 않는다
        return False


def _refresh_showtimes(page, scn_ymd: str) -> bool:
    """회차 목록을 다시 받는다. 되돌아와 그 날짜가 다시 골라졌으면 True.

    날짜를 **다른 날로 옮겼다 되돌린다.** 같은 날짜를 다시 누르면 SPA가 요청을
    보내지 않아 아무 일도 일어나지 않는다(실측). 리로드는 확실하지만 6.2초라,
    좌석 경쟁 중에 쓰기에는 너무 비싸다.

    되돌아온 뒤 날짜가 맞는지 반드시 확인한다 — 여기서 틀리면 **다른 날짜의 같은
    시각 회차를 선점하게 된다.** 그건 안 잡느니만 못하다.
    """
    wanted = date_labels(scn_ymd)
    try:
        buttons = []
        for selector in DATE_BUTTON_SELECTORS:
            buttons = _visible(page.locator(selector).all())
            if buttons:
                break
        other = None
        for button in buttons:
            try:
                text = (button.locator(DATE_NUMBER_SELECTOR).first
                        .text_content() or "").strip()
            except Exception:  # noqa: BLE001 - 번호 없는 버튼은 건너뛴다
                continue
            if text and text not in wanted:
                other = button
                break
        if other is None:
            log.warning("옮겨 갈 다른 날짜가 없어 회차 목록을 다시 받지 못했습니다")
            return False

        other.click(timeout=DATE_SWITCH_MS)
        # **정말 떠났는지 확인한다.** 클릭이 먹지 않았는데 되돌아오는 클릭만
        # 하면, 날짜는 처음부터 그대로라 목록을 다시 받은 적이 없다. 그걸
        # "다시 받았다"고 답하면 낡은 매진 표시를 보고 "정말 매진"이라 단정한다.
        if not _wait_until(page, lambda: _date_is_selected(page, wanted) is False,
                           DATE_SWITCH_MS):
            log.warning("날짜를 옮기지 못해 회차 목록을 다시 받지 못했습니다")
            return False
        _click_date(page, scn_ymd)      # 되돌아오며 날짜 확인까지 한다
        return True
    except Exception as exc:  # noqa: BLE001 - 실패하면 낡은 목록 그대로 간다
        log.warning("회차 목록을 다시 받지 못했습니다: %s", exc)
        return False


def _click_showtime_node(node, start_hhmm: str) -> None:
    """시작 시각 스팬을 감싼 버튼을 누른다. 버튼이 없으면 스팬을 직접."""
    try:
        button = node.locator("xpath=ancestor::button[1]")
        if button.count():
            button.first.click(timeout=6000)
            return
    except Exception as exc:  # noqa: BLE001 - 스팬 직접 클릭으로 폴백
        log.debug("%s 회차의 감싼 버튼을 누르지 못했습니다: %s", start_hhmm, exc)
    node.click(timeout=6000)


def seat_notice(page) -> str:
    """좌석을 누른 뒤 뜬 안내 팝업의 문구. 없으면 ''.

    좌석 종류마다 규칙이 다르다 — 실제로 '선택하신 패밀리 리클라이너는 4인 단위로
    인원을 선택해주세요'가 떠서 나머지 좌석을 누를 수 없었다. 이 문구를 읽지 않으면
    "그 사이 팔린 것 같습니다"라고 **틀리게** 보고하게 된다. 팔린 게 아니라 인원이
    맞지 않는 것이라, 사용자가 할 일이 전혀 다르다.
    """
    try:
        texts = page.evaluate("""() => [...document.querySelectorAll(
              '.cgv-modal, [role="dialog"]')]
            .filter(d => d.offsetWidth || d.offsetHeight)
            .map(d => (d.innerText || '').replace(/\\s+/g, ' ').trim())""")
    except Exception:  # noqa: BLE001 - 못 읽으면 안내가 없는 것으로 본다
        return ""
    for text in texts or []:
        # 좌석맵 자체가 큰 모달로 떠 있다 — 좌석 라벨이 잔뜩 든 건 안내가 아니다.
        if 8 <= len(text) <= 200 and "닫기" not in text[:6]:
            return text
    return ""


# 좌석 클릭이 막혔을 때 그 자리를 들여다보는 스크립트. Playwright는 "무엇이
# 가로챘다"까지만 알려 주는데, IMAX처럼 좌석맵이 한 화면에 다 안 들어오는 관에서는
# **좌석이 보이는 영역 밖이라** 좌표가 배경에 떨어진 것일 수 있다. 둘은 대처가
# 전혀 다르다(팝업이면 닫으면 되고, 화면 밖이면 좌석맵을 밀어야 한다).
#
# elementFromPoint가 그 둘을 가른다 — 좌석 좌표에 실제로 무엇이 있는지 본다.
# 조상들의 scrollWidth·overflow·transform까지 함께 봐야 어떻게 밀어야 하는지
# (네이티브 스크롤인지 transform 팬인지) 알 수 있다.
# 좌석이 좌석맵의 보이는 영역 밖이면 밀어 넣고, 누를 좌표를 돌려준다.
#
# 2026-08-28 용산아이파크몰 IMAX관 실측으로 확인한 구조다. 좌석맵은 잘라내는
# 상자(client 600px / scroll 2090px · overflow:hidden) 안에서 **transform으로**
# 미는 방식이라, Playwright의 자동 스크롤(scrollIntoViewIfNeeded)로는 반쪽만
# 닿는다. 스크롤 범위는 변형 전 레이아웃 기준으로 잡히는데 transform이 내용을
# 왼쪽으로 824px 밀어 두므로, scrollLeft가 0이어도 왼쪽 끝 좌석은 상자 밖에
# 남는다(실측: A3이 상자보다 579px 왼쪽). 그래서 transform을 직접 옮긴다.
#
# 화면 밖 좌석을 그냥 누르면 좌표가 배경 위에 떨어져 Playwright가 "modal-bg
# intercepts pointer events"라고 적는다 — 팝업이 뜬 것과 **글자가 똑같아서**
# 오래 헷갈렸다. 실제로 못 잡던 좌석이 전부 K1·L1처럼 번호 1~2번(왼쪽 끝)이었다.
_SEAT_REACH_JS = r"""(arg) => {
  const {sel, label} = arg;
  const hits = [...document.querySelectorAll(sel)]
      .filter(e => (e.textContent || '').trim() === label);
  if (!hits.length) return {error: '좌석 요소가 DOM에 없습니다'};
  const el = hits[hits.length - 1];        // 파이썬 쪽 .last와 같은 것을 본다

  // 눌렸는지 확인할 지문. 선택되면 어딘가에 상태 클래스가 붙는데 그 이름을
  // 우리가 모르므로, 좌석과 그 위 두 단계의 class를 통째로 비교한다.
  const fp = (e) => [e, e.parentElement,
                     e.parentElement && e.parentElement.parentElement]
      .filter(Boolean).map(n => String(n.className)).join('|');

  const reach = () => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const t = document.elementFromPoint(cx, cy);
    return {x: cx, y: cy,
            ok: !!t && (t === el || el.contains(t) || t.contains(el))};
  };

  let s = reach();
  if (s.ok) return {reachable: true, panned: false,
                    x: s.x, y: s.y, fingerprint: fp(el)};

  let clip = null, pan = null, p = el.parentElement;
  for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (!clip && p.scrollWidth > p.clientWidth + 1
        && /hidden|auto|scroll/.test(cs.overflowX)) clip = p;
    if (!pan && cs.transform && cs.transform !== 'none') pan = p;
  }
  if (!clip || !pan) return {reachable: false, panned: false,
                             x: s.x, y: s.y, reason: '미는 구조를 못 찾았습니다'};

  const cr = clip.getBoundingClientRect(), er = el.getBoundingClientRect();
  // **밖으로 나간 축만** 옮긴다. 세로가 멀쩡한데 세로까지 건드리면 멀쩡하던
  // 좌석을 오히려 상자 밖으로 밀어낸다.
  let dx = 0, dy = 0;
  if (er.left < cr.left || er.right > cr.right)
    dx = (cr.left + cr.width / 2) - (er.left + er.width / 2);
  if (er.top < cr.top || er.bottom > cr.bottom)
    dy = (cr.top + cr.height / 2) - (er.top + er.height / 2);

  const before = pan.style.transform;
  const m = new DOMMatrix(getComputedStyle(pan).transform);
  pan.style.transform =
      `matrix(${m.a},${m.b},${m.c},${m.d},${m.e + dx},${m.f + dy})`;
  s = reach();
  if (!s.ok) {
    pan.style.transform = before;   // 못 닿으면 화면을 어질러 두지 않는다
    return {reachable: false, panned: false, x: s.x, y: s.y,
            reason: '밀어도 좌석에 닿지 않습니다'};
  }
  return {reachable: true, panned: true, x: s.x, y: s.y,
          moved: [Math.round(dx), Math.round(dy)], fingerprint: fp(el)};
}"""

# 좌석이 눌렸는지 보는 지문만 다시 읽는다.
_SEAT_FP_JS = r"""(arg) => {
  const {sel, label} = arg;
  const hits = [...document.querySelectorAll(sel)]
      .filter(e => (e.textContent || '').trim() === label);
  if (!hits.length) return null;
  const el = hits[hits.length - 1];
  return [el, el.parentElement,
          el.parentElement && el.parentElement.parentElement]
      .filter(Boolean).map(n => String(n.className)).join('|');
}"""


_SEAT_DIAG_JS = r"""(arg) => {
  const {sel, label} = arg;
  const hits = [...document.querySelectorAll(sel)]
      .filter(e => (e.textContent || '').trim() === label);
  if (!hits.length) return {error: '좌석 요소가 DOM에 없습니다'};
  const el = hits[hits.length - 1];        // 파이썬 쪽 .last와 같은 것을 본다
  const desc = (n) => {
    if (!n) return 'null';
    const cls = (typeof n.className === 'string' ? n.className : '').trim();
    return n.tagName.toLowerCase()
         + (cls ? '.' + cls.split(/\s+/).slice(0, 2).join('.') : '');
  };
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const top = document.elementFromPoint(cx, cy);

  // 좌석을 잘라내는 조상과 좌석맵을 미는 조상을 찾는다. 창 뷰포트 기준으로는
  // 판정할 수 없다 — IMAX 실측에서 잘린 좌석도 창 안에는 들어 있었다(창이
  // 2560이고 좌석맵은 600만 보이는 구조라 그렇다).
  let clip = null, pan = null, p = el.parentElement;
  for (let i = 0; i < 12 && p; i++, p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (!clip && p.scrollWidth > p.clientWidth + 1
        && /hidden|auto|scroll/.test(cs.overflowX)) clip = p;
    if (!pan && cs.transform && cs.transform !== 'none') pan = p;
  }
  const box = clip && clip.getBoundingClientRect();
  return {
    seat_rect: [r.left, r.top, r.right, r.bottom].map(Math.round),
    at_point: desc(top),
    // 좌석 자신(또는 그 자식)이 나오면 덮인 게 아니다 — 다른 이유로 막힌 것이다.
    reaches_seat: !!top && (top === el || el.contains(top) || top.contains(el)),
    clip: clip ? {
      node: desc(clip),
      rect: [box.left, box.top, box.right, box.bottom].map(Math.round),
      client_w: clip.clientWidth, scroll_w: clip.scrollWidth,
      scroll_left: Math.round(clip.scrollLeft),
      scroll_max: Math.round(clip.scrollWidth - clip.clientWidth),
      // 이게 거짓이면 '팝업이 덮은 것'이 아니라 '보이는 영역 밖'이다.
      seat_inside: cx >= box.left && cx <= box.right
                   && cy >= box.top && cy <= box.bottom,
    } : null,
    pan: pan ? {node: desc(pan), transform: getComputedStyle(pan).transform} : null,
    window: [innerWidth, innerHeight],
  };
}"""


def _seat_click_diagnosis(page, label: str) -> str:
    """좌석 클릭이 막힌 자리를 들여다본 결과. 못 읽으면 ''.

    **판정하지 않고 사실만 남긴다.** 지금 우리는 IMAX 좌석맵이 네이티브 스크롤인지
    transform 팬인지조차 모른다 — 추측으로 대응 코드를 넣으면 지금 잘 되는 가운데
    좌석까지 망친다. 우선 한 번이라도 실물을 보고 나서 정한다.
    """
    try:
        import json as _json

        out = page.evaluate(_SEAT_DIAG_JS,
                            {"sel": SEAT_MAP_SELECTOR, "label": label})
        return _json.dumps(out, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 진단 실패가 선점을 막으면 안 된다
        log.debug("좌석 진단을 읽지 못했습니다: %s", exc)
        return ""


def _click_seat(page, label: str) -> None:
    """좌석 하나를 누른다. 보이는 영역 밖이면 좌석맵을 밀어 넣고 누른다.

    화면 안에 있는 좌석은 **지금까지의 경로 그대로** 간다. 그쪽은 실제로 잘 되고
    있고(실측 G36·G37 선점 성공), Playwright의 실행 가능 판정을 그대로 쓰는 편이
    안전하다. 밀어야 하는 좌석만 다른 길로 보낸다.

    민 좌석은 `locator.click()`을 쓸 수 없다. 그게 누르기 직전에 다시
    scrollIntoViewIfNeeded를 돌려 방금 맞춰 둔 위치를 흐트러뜨리기 때문이다.
    좌표로 누르는 대신, **눌린 게 맞는지 따로 확인한다** — 좌표 클릭은 빗나가도
    예외를 내지 않아서, 확인이 없으면 안 눌린 좌석을 눌렀다고 세게 된다. 그건
    인원수를 채운 줄 알고 넘어가 엉뚱한 자리를 선점하는 길이다.
    """
    node = page.get_by_text(label, exact=True).last
    try:
        spot = page.evaluate(_SEAT_REACH_JS,
                             {"sel": SEAT_MAP_SELECTOR, "label": label})
    except Exception as exc:  # noqa: BLE001 - 못 읽으면 예전 경로로 간다
        log.debug("좌석 %s 위치를 읽지 못했습니다: %s", label, exc)
        spot = None

    if not spot or not spot.get("panned"):
        # 이미 닿거나, 밀 구조가 아니거나, 밀어도 안 되는 경우. 마지막 둘은 여기서
        # 제대로 된 실패 문구와 함께 죽는 편이 낫다.
        _click_through_modals(page, node, what=f"좌석 {label}",
                              timeout=SEAT_CLICK_MS, retries=1)
        return

    log.info("좌석 %s가 좌석맵 밖에 있어 %s만큼 밀어 넣었습니다",
             label, spot.get("moved"))
    page.mouse.click(spot["x"], spot["y"])
    try:
        after = page.evaluate(_SEAT_FP_JS,
                              {"sel": SEAT_MAP_SELECTOR, "label": label})
    except Exception as exc:  # noqa: BLE001
        log.debug("좌석 %s 상태를 다시 읽지 못했습니다: %s", label, exc)
        return  # 확인할 수단이 없으면 눌린 것으로 본다 — 예전과 같은 수준이다
    if after is not None and after == spot.get("fingerprint"):
        raise RuntimeError(
            f"좌석 {label}을 밀어 넣고 눌렀지만 선택되지 않았습니다 "
            f"(화면이 다시 그려져 위치가 어긋난 것일 수 있습니다)")


def _pick_seats(page, seat_labels) -> tuple[list[str], list[str], str, bool]:
    """좌석맵에서 지정한 좌석을 하나씩 누른다.

    반환: (누른 것, 못 누른 것, 안내 문구, 팝업에 막혔는지).
    안내 문구가 있으면 그게 실패 사유다.

    한 좌석이 실패해도 나머지는 마저 눌러 본다 — 무엇이 팔렸는지 전부 알아야
    쓸 만한 오류 문구가 나오고, 어차피 여기서는 아직 아무것도 선점되지 않는다.
    다만 **화면을 덮은 것이 있으면 거기서 멈춘다**: 그 아래 좌석은 어차피
    타임아웃까지 기다렸다 실패할 뿐이다.

    막히는 방식이 둘이라 따로 다룬다. 좌석 안내 팝업(seat_notice)은 "이 좌석은
    이렇게 못 삽니다"라는 **답**이라 다시 고를 이유가 없고, 포인터를 가로채는
    오버레이는 **사고**라 닫고 다시 누르면 풀린다(_click_through_modals).
    닫아도 안 풀리면 그때는 나머지 좌석을 눌러 봐야 같은 것에 막힐 뿐이다 —
    실측에서 좌석 둘에 3초씩 버리고 재시도까지 돌아 42.9초를 썼다.

    **무엇을 눌렀는지도 함께 돌려준다.** 다시 고를 수 있는지가 여기에 달렸다:
    하나도 못 눌렀으면 화면에 아무 흔적이 없어 그냥 다시 고르면 되지만, 일부가
    이미 선택돼 있으면 인원수를 넘겨 엉뚱한 자리를 선점할 수 있다.
    """
    clicked, missed = [], []
    diagnosed = False
    for i, label in enumerate(seat_labels):
        try:
            _click_seat(page, label)
        except Exception as exc:  # noqa: BLE001 - 못 누른 좌석은 모아서 보고
            log.warning("좌석 %s 클릭 실패: %s", label, exc)
            missed.append(label)
            # 첫 실패에서 한 번만 들여다본다. 좌석마다 찍으면 로그만 불어나고,
            # 어차피 같은 화면이라 첫 장이면 원인을 가리기에 충분하다.
            if not diagnosed:
                diagnosed = True
                detail = _seat_click_diagnosis(page, label)
                if detail:
                    log.warning("좌석 %s 자리 진단: %s", label, detail)
            if _blocked_by_modal(exc):
                # 닫고 다시 눌렀는데도 가로막혔다. 나머지는 시도하지 않는다.
                log.warning("좌석맵이 팝업에 덮여 있습니다 — 남은 좌석(%s)은 "
                            "누르지 않습니다",
                            ", ".join(seat_labels[i + 1:]) or "없음")
                missed.extend(seat_labels[i + 1:])
                return clicked, missed, "", True
        else:
            clicked.append(label)

        notice = seat_notice(page)
        if notice:
            log.warning("좌석 안내 팝업: %s", notice)
            dismiss_modals(page)
            missed.extend(seat_labels[i + 1:])
            return clicked, missed, notice, False
    return clicked, missed, "", False


def live_seats(session, ctx: dict) -> list[dict]:
    """이 회차의 좌석 배치도를 **지금** 다시 읽는다.

    같은 오리진 fetch라 0.2~0.3초다. 감지 때 읽은 목록으로 그냥 클릭하면, UI를
    모는 30초 남짓이 통째로 경쟁 구간이 된다 — auto-book이 "그 사이 팔린 것
    같습니다"로 끝나던 주된 이유다.
    """
    row = ctx["row"]
    data = session.seat_map(
        site_no=row.get("siteNo") or ctx.get("site_no") or "",
        scns_no=row["scnsNo"], ymd=ctx["scn_ymd"], scn_sseq=row["scnSseq"])
    return seats_mod.parse_seats(data)


def _select_block(session, page, ctx: dict, *, seats_fn=None) -> dict:
    """좌석맵에서 지금 비어 있는 블록을 골라 누른다.

    반환: {"ok": bool, "labels": [...], "error": str}

    한 바퀴는 [좌석맵 다시 읽기 → pick_block → 클릭]이다. 읽고 누르기까지가
    1초 안쪽이라, 못 누르는 일 자체가 드물어진다. 그래도 밀렸다면 **하나도 못
    눌렀을 때만** 다시 고른다 — 일부가 이미 선택된 채로 다른 블록을 누르면
    인원수를 넘겨 엉뚱한 자리를 선점하게 되고, 그건 안 잡느니만 못하다.

    seats_fn을 주입하면 브라우저 없이 이 판단만 시험할 수 있다.
    """
    seats_fn = seats_fn or live_seats
    party = ctx["party"]
    candidate = list(ctx.get("seat_labels") or [])
    deadline = time.monotonic() + SEAT_PICK_DEADLINE
    shot: str | None = None
    shot_saved = False
    last_error = "좌석을 고르지 못했습니다"

    for attempt in range(1, SEAT_PICK_ATTEMPTS + 1):
        blind = False
        try:
            live = seats_fn(session, ctx)
        except Exception as exc:  # noqa: BLE001 - 다시 못 읽어도 시도는 해 본다
            log.warning("좌석 배치도를 다시 읽지 못했습니다 (%s) — 감지 때 고른 "
                        "좌석으로 진행합니다", exc)
            live, blind = None, True

        if blind:
            labels = candidate
            if not labels:
                return {"ok": False, "labels": [],
                        "error": "좌석 배치도를 읽지 못했습니다"}
        else:
            block = seats_mod.pick_block(live, party, ctx.get("rows"),
                                         ctx.get("num_from") or 0,
                                         ctx.get("num_to") or 0)
            if len(block) < party:
                # 후보를 고른 뒤 여기 오는 사이에 다 팔린 것이다. 낡은 좌석을
                # 눌러 보는 것보다 사실대로 끝내는 편이 낫다.
                return {"ok": False, "labels": [],
                        "error": f"{party}석 연속 빈자리가 사라졌습니다 "
                                 f"(좌석맵을 다시 읽었습니다)"}
            labels = [s["label"] for s in block]
            if labels != candidate:
                log.info("좌석을 다시 골랐습니다: %s → %s",
                         ", ".join(candidate) or "(없음)", ", ".join(labels))

        # 누르기 직전에 화면을 한 번 정리한다. _click_through_modals는 막힌
        # **뒤에야** 닫으므로, 이걸 안 하면 첫 좌석은 반드시 한 번 막힌다.
        # 로딩이 걷히는 순간 팝업이 뜨는 경합이 있어(커밋 bf71721) 로딩을 먼저
        # 기다려야 의미가 있다. 좌석맵 자체는 MODAL_KEEP_TEXTS가 지켜 준다.
        _wait_for_loading(page)
        dismiss_modals(page, rounds=1)

        clicked, missed, notice, blocked = _pick_seats(page, labels)
        if not missed:
            return {"ok": True, "labels": labels, "error": ""}

        # 오버레이가 안 걷힌다. 같은 블록을 다시 골라 봐야 그 아래를 누르는 것은
        # 마찬가지라 좌석 수만큼 타임아웃을 또 버린다 — 사실대로 끝낸다.
        if blocked:
            shot = _save_screenshot(page, ctx) if not shot_saved else shot
            # "그 사이 팔렸다"(_partial_seats_error)와 섞으면 안 된다. 좌석은
            # 멀쩡히 있었고 우리가 못 누른 것이라, 사람이 직접 예매하면 된다.
            detail = (f" — {', '.join(clicked)}는 골라진 채로 남았습니다"
                      if clicked else "")
            return {"ok": False, "labels": [],
                    "error": f"좌석맵이 팝업에 덮여 좌석을 누르지 못했습니다"
                             f"{detail}" + (f" (화면: {shot})" if shot else "")}

        # 안내 팝업이 있으면 그게 진짜 사유다. 좌석 종류가 요구하는 인원 단위가
        # 안 맞는 것 같은 경우라, 다시 고른다고 풀리지 않는다 — 바로 끝낸다.
        if notice:
            shot = _save_screenshot(page, ctx) if not shot_saved else shot
            return {"ok": False, "labels": [],
                    "error": f"{notice}"
                             + (f" (화면: {shot})" if shot else "")}

        last_error = _partial_seats_error(labels, missed)
        if not shot_saved:
            # 셀렉터가 깨진 건지 정말 팔린 건지는 화면 없이 사후에 못 가린다.
            # 이 경로만 스크린샷을 안 남기고 있어서 원인을 좁힐 수가 없었다.
            shot = _save_screenshot(page, ctx)
            shot_saved = True

        if clicked:
            log.warning("좌석 %s는 골라졌고 %s는 못 골랐습니다 — 되돌릴 수단이 "
                        "확실하지 않아 다시 고르지 않습니다",
                        ", ".join(clicked), ", ".join(missed))
            break
        if blind or time.monotonic() >= deadline:
            break
        log.info("좌석을 다시 골라 재시도합니다 (%d/%d)",
                 attempt + 1, SEAT_PICK_ATTEMPTS)

    if shot:
        # 배치도를 방금 읽고 눌렀는데도 실패했다면 정말 밀린 것일 수도, 좌석
        # 셀렉터가 깨진 것일 수도 있다. 어느 쪽인지는 이 화면을 봐야 안다.
        last_error += f" (화면: {shot})"
    return {"ok": False, "labels": [], "error": last_error}


def _partial_seats_error(wanted, missed) -> str:
    """일부만 잡혔을 때의 오류 문구.

    **하나라도 못 잡으면 선점하지 않는다.** 일행이 함께 앉으려고 2석을 건 감시에서
    1석만 잡히면 그건 성공이 아니다 — 쓸모없는 좌석을 선점해 두고 그 감시까지
    꺼져서(try_auto_book), 정작 두 자리가 났을 때 아무도 안 잡는다.

    선점은 '결제하기'에서 걸리므로 이 시점엔 아직 아무것도 잡히지 않았다.
    되돌릴 것 없이 그냥 멈추면 된다.
    """
    return (f"{len(wanted)}석 중 {len(missed)}석을 고르지 못했습니다 "
            f"({', '.join(seats_mod.sort_labels(missed))}) — 그 사이 팔린 것 "
            f"같습니다. 일부만 선점하지 않고 멈춥니다.")


def payment_mark(url: str) -> str | None:
    """결제 확정 계열로 보이는 경로면 걸린 표식을 돌려준다. 아니면 None.

    선점 응답(seatTempPrmp)은 절대 걸리면 안 되므로 먼저 걸러낸다.
    """
    text = url or ""
    if SEAT_HOLD_URL_MARK in text:
        return None
    lowered = text.lower()
    return next((m for m in PAYMENT_URL_MARKS if m.lower() in lowered), None)


def page_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText || ''")
    except Exception:  # noqa: BLE001 - 못 읽으면 판단 근거가 없는 것으로 본다
        return ""


def in_queue(text: str) -> bool:
    """가상 대기열 화면인지."""
    return any(mark in text for mark in QUEUE_MARKS)


def wait_past_queue(page, timeout_ms: int = QUEUE_WAIT_MS) -> bool:
    """회차를 고른 뒤 인원 선택 화면에 닿을 때까지 기다린다. 닿으면 True.

    CGV는 접속이 몰리면 가상 대기열을 세운다. 그동안 주소도 화면도 예매 목록
    그대로라, 고정 시간만 자고 넘어가면 인원 선택 버튼을 못 찾고 통째로 실패한다.
    대기열은 몇 초에서 몇 분까지 가므로 **화면이 바뀔 때까지** 기다려야 한다.

    좌석 경쟁 중이라도 여기서 기다리는 건 손해가 아니다 — 줄을 안 서면 아예
    예매 화면에 못 들어간다.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    announced = False
    while time.monotonic() < deadline:
        try:
            if VISITOR_PAGE_MARK in page.url:
                if announced:
                    log.info("대기열을 통과했습니다")
                return True
        except Exception:  # noqa: BLE001 - 페이지가 전환 중일 수 있다
            pass
        text = page_text(page)
        if in_queue(text):
            if not announced:
                announced = True
                log.info("CGV 대기열에 들어갔습니다 — 통과할 때까지 기다립니다")
            # 줄을 섰으면 급할 것 없다. 화면 전문을 읽는 비용이 있으니 1초씩 본다.
            page.wait_for_timeout(1000)
        else:
            # 줄이 없으면 전환이 곧 끝난다 — 촘촘히 봐야 그만큼 빨리 넘어간다.
            page.wait_for_timeout(QUEUE_POLL_MS)
    return False


def dismiss_modals(page, rounds: int = 3) -> int:
    """화면을 덮은 팝업을 닫는다. 닫은 개수를 돌려준다.

    예매 화면은 이벤트·안내 팝업을 띄우는데, 이게 떠 있으면 그 아래 버튼을 누를 수
    없다 — Playwright가 "modal-bg intercepts pointer events"로 타임아웃까지 기다렸다
    죽는다. 실제로 인원 선택에서 이 모양으로 막혔다.

    닫기 버튼도 **보이는 것만** 누른다. 팝업은 숨은 사본을 흔히 남긴다.
    여러 개가 겹쳐 뜰 수 있어 몇 바퀴 돈다.

    **닫기 버튼은 그 팝업 안에서 찾는다.** 예전에는 화면 전체에서 '닫기'·'확인'
    이름의 버튼을 찾아 눌렀는데, 이 화면에는 접힌 안내(우대·경로 권종 등)가 자기
    닫기 버튼을 여럿 달고 숨어 있다. 그래서 엉뚱한 것을 누르고 "닫았다"고 세는
    바람에, 정작 화면을 덮은 SCREENX관 안내는 그대로 남아 인원 선택 클릭을
    5초 내내 가로막았다.
    """
    closed = 0
    for _ in range(rounds):
        try:
            modals = [m for m in page.locator(MODAL_SELECTOR).all()
                      if m.is_visible()]
        except Exception:  # noqa: BLE001 - 못 세면 이번 바퀴는 넘어간다
            break
        if not modals:
            return closed

        hit = False
        for modal in modals:
            if _modal_is_ours(modal):
                continue
            for selector in MODAL_CLOSE_SELECTORS:
                try:
                    buttons = [b for b in modal.locator(selector).all()
                               if b.is_visible()]
                except Exception:  # noqa: BLE001 - 다음 후보로
                    continue
                if not buttons:
                    continue
                try:
                    buttons[0].click(timeout=2500)
                except Exception:  # noqa: BLE001 - 닫히는 중이었을 수 있다
                    continue
                closed += 1
                hit = True
                page.wait_for_timeout(400)
                break
            if hit:
                break
        if not hit:
            break
    return closed


def _modal_is_ours(modal) -> bool:
    """그 모달이 **우리가 통과해야 할 화면**인지 (닫으면 안 되는 것인지)."""
    try:
        text = modal.text_content() or ""
    except Exception:  # noqa: BLE001 - 못 읽으면 판단 근거가 없다
        return False
    return any(keep in text for keep in MODAL_KEEP_TEXTS)


def _ancestor_text(node, cls: str) -> str:
    """그 클래스 조각을 가진 가장 가까운 조상의 텍스트. 없으면 빈 문자열."""
    try:
        container = node.locator(
            f"xpath=ancestor::*[contains(@class, '{cls}')][1]")
        return (container.first.text_content() or "") if container.count() else ""
    except Exception:  # noqa: BLE001 - 못 읽으면 이 층은 판단 근거가 없다
        return ""


def _narrow_by_screen(nodes: list, screen_name: str) -> list:
    """같은 시각 후보들을 상영관 이름으로 좁힌다. 하나로 갈릴 때만 좁혀서 돌려준다.

    화면은 상영관 블록을 겹쳐 놓는다. 바깥 컨테이너는 그 날 상영관을 전부 품고
    있어서 **어느 후보로 물어도 이름이 걸린다** — 그 층에서 판단하면 아무것도
    못 가린다. 그래서 안쪽 층부터 훑으며 후보가 딱 하나로 갈리는 층을 찾는다.

    예: '17관[PREMIUM] (Laser)'와 '17관 (Laser)'가 같은 10:25에 있을 때, 회차
    층에서는 후자에만 이름이 붙어 하나로 갈린다.

    끝내 못 가리면 원래 목록을 그대로 돌려준다 — 호출자가 "가리지 못했습니다"로
    멈춘다. 찍어서 엉뚱한 상영관을 선점하느니 멈추는 게 낫다.
    """
    if not screen_name or len(nodes) < 2:
        return nodes
    from watch import normalize

    want = normalize(screen_name)
    for cls in SCREEN_CONTAINER_CLASSES:
        hits = [n for n in nodes if want in normalize(_ancestor_text(n, cls))]
        if len(hits) == 1:
            return hits
    return nodes


def _wait_until(page, check, timeout_ms: int, poll_ms: int = STEP_POLL_MS) -> bool:
    """check()가 참이 될 때까지 짧게 폴링한다. 됐으면 True, 시간이 다하면 False.

    고정 대기(`wait_for_timeout`) 대신 쓴다. 화면이 0.3초 만에 준비돼도 1.5초를
    자던 자리들이 선점 시간의 절반을 먹고 있었다.

    검사 도중 나는 예외는 '아직 아님'으로 본다 — 화면이 갈아 끼워지는 중이면
    locator가 잠깐 터진다.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            if check():
                return True
        except Exception:  # noqa: BLE001 - 전환 중이면 다음 바퀴에 다시 본다
            pass
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(poll_ms)


class _Steps:
    """단계별 소요를 재서 한 줄로 남긴다.

    "느린 것 같다"는 인상만으로는 어디를 고쳐야 할지 알 수 없다. 선점이 실패했을
    때도 어느 단계에서 시간을 썼는지가 로그에 남아야 다음에 판단할 수 있다.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._last = self._t0
        self._marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self._marks.append((name, now - self._last))
        self._last = now

    def summary(self) -> str:
        total = time.monotonic() - self._t0
        parts = " · ".join(f"{n} {d:.1f}s" for n, d in self._marks)
        return f"합계 {total:.1f}s ({parts})" if parts else f"합계 {total:.1f}s"


def _wait_for_loading(page, timeout_ms: int = LOADING_WAIT_MS) -> bool:
    """화면을 덮은 로딩 가림막이 걷힐 때까지 기다린다. 걷혔으면 True."""
    def gone() -> bool:
        nodes = page.locator(LOADING_SELECTOR)
        return nodes.count() == 0 or not nodes.first.is_visible()

    return _wait_until(page, gone, timeout_ms)


def _blocked_by_modal(exc: Exception) -> bool:
    """그 클릭 실패가 '팝업이 가로막아서'인지.

    Playwright는 이런 경우 "<div ...> intercepts pointer events"라고 적어 준다.
    그 밖의 실패(요소가 없다·안 보인다)는 팝업을 닫아도 풀리지 않으므로 구분한다.
    """
    return "intercepts pointer events" in str(exc)


def _click_through_modals(page, node, *, what: str, timeout: int,
                          retries: int = 2) -> None:
    """팝업이 클릭을 가로채면 닫고 다시 누른다.

    안내 팝업은 **우리가 팝업을 닫고 지나간 뒤에** 뜨기도 한다. 실제로 SCREENX관
    안내가 로딩이 걷히는 순간 떠서 인원 선택을 가로막았다 — 미리 한 번 닫아 두는
    것만으로는 막을 수 없는 경합이라, 막히면 그 자리에서 닫고 다시 누른다.
    """
    for attempt in range(retries + 1):
        try:
            node.click(timeout=timeout)
            return
        except Exception as exc:  # noqa: BLE001
            if attempt >= retries or not _blocked_by_modal(exc):
                raise
            log.info("%s 클릭이 팝업에 막혔습니다 — 닫고 다시 누릅니다 (%d/%d)",
                     what, attempt + 1, retries)
            dismiss_modals(page)


def _seatmap_ready(page) -> bool:
    """좌석맵 모달이 열려 **누를 수 있는** 좌석이 화면에 있는지.

    개수만 세면 안 된다 — 좌석 요소는 모달이 닫혀 있을 때도 DOM에 들어 있다.
    실제로 보이는지까지 봐야 '열렸다'는 뜻이 된다.
    """
    nodes = page.locator(SEAT_MAP_SELECTOR)
    if nodes.count() == 0:
        return False
    # 좌석맵은 반응형으로 두 벌이 렌더된다(그래서 좌석 클릭도 `.last`를 쓴다).
    # 어느 쪽이 보이는지 모르므로 양끝을 본다 — 전부 훑으면 수백 번 왕복이다.
    for node in (nodes.last, nodes.first):
        try:
            if node.is_visible():
                return True
        except Exception:  # noqa: BLE001 - 갈아 끼우는 중이면 다음 바퀴에
            continue
    return False


def _visible_pay_buttons(page) -> list:
    """지금 화면에 보이는 '결제하기' 버튼들.

    숨겨진 사본이 함께 있어서 `.first`로 잡으면 "element is not visible"로
    타임아웃까지 기다렸다 죽는다 — 선점이 한 번도 성공하지 못한 이유였다.
    """
    return [b for b in page.locator("button", has_text="결제하기").all()
            if b.is_visible()]


def _save_screenshot(page, ctx: dict) -> str | None:
    """실패 순간의 화면을 남긴다. 실패해도 조용히 넘어간다(부가 기능이다)."""
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in str(ctx.get("mov_nm", ""))
                       if ch.isalnum() or ch in " -_")[:40].strip() or "unknown"
        path = SHOT_DIR / f"{stamp}_{safe}_{ctx.get('start_hhmm', '')}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("자동 예매 실패 화면을 남겼습니다: %s", path)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.debug("실패 화면을 남기지 못했습니다: %s", exc)
        return None



def _is_secret(name: str) -> bool:
    """그 이름이 가려야 할 값인지. 대소문자·구분자를 무시하고 조각으로 본다."""
    flat = "".join(ch for ch in str(name).lower() if ch.isalnum())
    return any(hint.lower() in flat for hint in SECRET_HINTS)


def mask_secrets(value):
    """기록에 남기기 전에 자격증명을 지운다. 구조는 그대로 두고 값만 가린다.

    형태를 배우는 게 목적이라 **키와 중첩 구조는 남겨야** 한다. 그래서 값을
    지우되 무엇이 있었는지는 "(가림: 39자)"로 적어 둔다 — 나중에 직접 채울 때
    자리를 알아볼 수 있다.
    """
    if isinstance(value, dict):
        return {k: (f"(가림: {len(str(v))}자)" if _is_secret(k)
                    else mask_secrets(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


# 선점 요청 본문에서 "어느 회차인가"를 말하는 필드들. logs/holdspec/*.json으로
# 실제 요청을 확인해 정했다.
HOLD_BODY_IDENTITY = (
    ("scnYmd", "scn_ymd", "상영일"),
    ("siteNo", "site_no", "극장"),
)


def hold_request_mismatch(post_data, ctx: dict) -> str | None:
    """나가려는 선점 요청이 우리가 의도한 것과 다른 점. 같으면 None.

    **선점 요청은 자기 자신을 설명한다.** 본문에 상영일·극장·상영관·회차 순번과
    좌석이 모두 들어 있다(logs/holdspec/*.json). 그래서 CGV에 닿기 전에 우리가
    의도한 것과 맞는지 확인할 수 있다.

    이 확인이 필요한 이유: 예매 화면을 미리 진행해 두면 그 탭이 정말 그 회차에
    있는지를 **DOM만으로는 확정할 수 없다**(인원 선택부터 결제까지 주소가
    `/cnm/selectVisitorCnt`로 고정이다 — README의 '결제하기 두 번' 항목). 화면
    판정은 "이 탭이 망가졌는가"를 보는 데까지만 쓰고, "맞는 회차인가"는 나가는
    요청에서 확정한다.

    **읽을 수 없으면 막지 않는다.** 본문 형태가 바뀌었을 때 모든 선점을 세우는
    편이 더 나쁘다 — 그때는 경고만 남기고 통과시킨다. 막는 것은 **읽었고 또한
    달랐을 때**뿐이다.
    """
    import json as _json

    if isinstance(post_data, (bytes, bytearray)):
        try:
            post_data = post_data.decode("utf-8")
        except Exception:  # noqa: BLE001 - 못 읽으면 판단 근거가 없다
            return None
    if isinstance(post_data, str):
        try:
            body = _json.loads(post_data)
        except Exception:  # noqa: BLE001
            return None
    else:
        body = post_data
    if not isinstance(body, dict):
        return None

    for field, ctx_key, label in HOLD_BODY_IDENTITY:
        want = str(ctx.get(ctx_key) or "")
        got = str(body.get(field) or "")
        if want and got and want != got:
            return f"{label}이 다릅니다 (의도 {want}, 요청 {got})"

    scns_no, scn_sseq = showtime_ids(ctx.get("row"))
    for field, want, label in (("scnsNo", scns_no, "상영관"),
                               ("scnSseq", scn_sseq, "회차 순번")):
        got = str(body.get(field) or "")
        if want and got and want != got:
            return f"{label}이 다릅니다 (의도 {want}, 요청 {got})"

    seats = body.get("seatPrmpDataList")
    party = ctx.get("party")
    if isinstance(seats, list) and party:
        if len(seats) != int(party):
            return f"좌석 수가 다릅니다 (의도 {int(party)}석, 요청 {len(seats)}석)"
    return None


def _record_hold_request(captured: dict, ctx: dict) -> None:
    """성공한 선점 요청의 형태를 파일로 남긴다. 실패해도 조용히 넘어간다.

    **동작은 바꾸지 않는다.** 선점은 지금까지처럼 UI로 하고, 여기서는 그때 나간
    요청을 들여다볼 뿐이다. 이 기록이 쌓여야 "UI를 몰지 않고 직접 부른다"를
    설계할 수 있다 — 지금은 요청 바디가 코드 어디에도 없어 설계 자체가 불가능하다.
    """
    req = captured.get("request")
    if not req:
        return
    try:
        import json as _json

        HOLD_SPEC_DIR.mkdir(parents=True, exist_ok=True)
        body = req.get("post_data")
        try:
            body = mask_secrets(_json.loads(body)) if body else None
        except (ValueError, TypeError):
            body = "(JSON이 아님)" if body else None

        record = {
            "관측시각": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url": req.get("url"),
            "method": req.get("method"),
            "headers": mask_secrets(req.get("headers") or {}),
            "body": body,
            # 어떤 상황의 요청인지 — 인원수·좌석 수가 바디의 어느 값과 맞는지
            # 대조하려면 이게 있어야 한다.
            "party": ctx.get("party"),
            "seats": list(ctx.get("seat_labels") or []),
            "scn_ymd": ctx.get("scn_ymd"),
            "start_hhmm": ctx.get("start_hhmm"),
        }
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = HOLD_SPEC_DIR / f"{stamp}_hold.json"
        path.write_text(_json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        log.info("선점 요청의 형태를 남겼습니다: %s", path)
    except Exception as exc:  # noqa: BLE001 - 관찰 실패로 선점을 망치지 않는다
        log.debug("선점 요청을 기록하지 못했습니다: %s", exc)


def hold_block(session, ctx: dict) -> dict:
    """CGV 예매 UI를 구동해 좌석을 임시 선점한다. 결제 버튼 이후로는 가지 않는다.

    ctx: {mov_nm, site_nm, scn_ymd, start_hhmm, seat_labels, party, scns_nm, row}
    반환: {ok, mov_atkt_no, hold_expires_at, amount, error}

    순서가 곧 화면의 순서다: 영화 → 극장 → **날짜** → 회차 → 인원 → 좌석 → 결제하기.
    날짜를 빼먹으면 화면은 오늘에 머물러 있다.

    사이트의 자체 JS가 seatTempPrmp 요청을 만들어 보내므로(custNo 등 포함), 우리는
    UI만 조작하고 그 응답을 가로채 예매번호·만료시각을 읽는다.
    """
    import json as _json

    # 미리 띄워 둔 탭이 있으면 그걸 쓴다 — 예매 화면을 새로 여는 6.2초를 아낀다.
    page = booking_page(session, ctx)
    # 결제 단계가 같은 화면에서 이어져야 한다.
    ctx["_page"] = page
    captured = {}
    steps = _Steps()
    # 좌석맵에 닿기 전에 죽으면 후보가 곧 '시도한 좌석'이다.
    chosen = list(ctx.get("seat_labels") or [])

    def on_resp(r):
        if SEAT_HOLD_URL_MARK in r.url:
            try:
                captured["body"] = _json.loads(r.text())
            except Exception:  # noqa: BLE001
                captured["body"] = None
            return
        if payment_mark(r.url):
            # 여기 걸리면 우리가 '선점'으로 알고 누른 버튼이 결제를 진행시킨
            # 것이다. 첫 번째 것만 남긴다 — 뒤따르는 요청은 같은 사건이다.
            captured.setdefault("payment_url", r.url)

    def on_req(r):
        # 관찰 전용 — 나가는 선점 요청의 생김새만 챙긴다. 여기서 손대는 것은
        # 없고, 실패해도 선점에는 영향이 없어야 한다.
        if SEAT_HOLD_URL_MARK not in r.url:
            return
        try:
            captured.setdefault("request", {
                "url": r.url, "method": r.method,
                "headers": dict(r.headers or {}), "post_data": r.post_data,
            })
        except Exception:  # noqa: BLE001 - 못 읽으면 그냥 안 남긴다
            pass

    def on_route(route):
        # **마지막 방어선.** 나가는 선점 요청이 우리가 의도한 회차인지 보고, 다르면
        # CGV에 닿기 전에 끊는다. 화면을 미리 진행해 두면 그 탭이 정말 그 회차인지
        # DOM만으로는 확정할 수 없어서(주소가 고정이다), 확정을 여기서 한다.
        try:
            reason = hold_request_mismatch(route.request.post_data, ctx)
        except Exception as exc:  # noqa: BLE001 - 검증이 선점을 깨면 안 된다
            log.debug("선점 요청 검증을 못 했습니다 (%s) — 그대로 보냅니다", exc)
            reason = None
        if reason:
            captured["blocked"] = reason
            log.error("선점 요청을 막았습니다 — %s. 화면이 의도한 회차가 아닙니다 "
                      "(의도: %s %s, 좌석 %s)", reason, ctx.get("scn_ymd"),
                      ctx.get("start_hhmm"), ctx.get("seat_labels"))
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - 이미 지나갔으면 어쩔 수 없다
                pass
            return
        try:
            route.continue_()
        except Exception:  # noqa: BLE001 - 흐름을 막지 않는다
            pass

    page.on("response", on_resp)
    page.on("request", on_req)
    # 이 한 주소만 가로챈다 — 좁게 걸어야 나머지 요청에 값이 붙지 않는다.
    routed = False
    try:
        page.route(f"**/{SEAT_HOLD_URL_MARK}", on_route)
        routed = True
    except Exception as exc:  # noqa: BLE001 - 못 걸면 검증 없이 진행한다
        log.warning("선점 요청 검증을 걸지 못했습니다 (%s) — 검증 없이 갑니다", exc)

    try:
        # 영화 → 극장 → 날짜를 주소 하나로 건너뛴다. 그 세 클릭이 자동 예매가 가장
        # 자주 죽던 구간이다 — 스와이퍼·바텀시트가 만든 **숨겨진 사본**을 눌러
        # 타임아웃이 났다(`.first`는 보이는 것을 고른다는 보장이 없다).
        if _already_on_booking(page, ctx):
            log.info("미리 띄워 둔 예매 화면을 그대로 씁니다 (%s)", ctx["scn_ymd"])
        elif not _open_booking_direct(page, ctx):
            page.goto(BOOKING_PAGE, wait_until="domcontentloaded", timeout=40000)
            # 여기 있던 고정 대기 셋(3.5 + 2.5 + 2.5초)을 걷어냈다. 뒤따르는 셋이
            # 이미 같은 것을 기다린다 — _click_visible은 wait_for_selector로
            # 보일 때까지, _click_date는 _wait_for_any로 날짜 스트립이 그려질
            # 때까지. 자고 나서 또 기다리고 있었던 셈이다.
            _click_visible(page, ctx["mov_nm"], exact=True, what="영화",
                           timeout=10000)
            _click_visible(page, ctx["site_nm"], exact=False, what="극장",
                           timeout=6000)
            # 날짜를 고른다. 이 화면은 기본이 **오늘**이라, 건너뛰면 오늘 상영표에서
            # 회차를 찾게 된다 — 없으면 실패하고, 하필 같은 시각이 있으면 엉뚱한
            # 날짜를 선점한다.
            _click_date(page, ctx["scn_ymd"])
        steps.mark("화면진입")
        _click_showtime(page, ctx["start_hhmm"], ctx.get("scns_nm", ""),
                        ctx.get("scn_ymd", ""))
        steps.mark("회차")
        # 인원 선택 화면에 닿을 때까지 기다린다. 고정 시간으로 넘겨짚으면 안 된다 —
        # 접속이 몰리면 CGV가 가상 대기열을 세우고, 그동안 화면은 그대로다.
        if not wait_past_queue(page):
            raise RuntimeError(
                "인원 선택 화면으로 넘어가지 못했습니다"
                + (" (대기열이 길어 시간 안에 통과하지 못했습니다)"
                   if in_queue(page_text(page)) else ""))
        steps.mark("대기열")
        # 이벤트·안내 팝업이 떠 있으면 아래 버튼을 누를 수 없다. 로딩 가림막이
        # 걷히면서 팝업이 뜨므로 **걷히기를 기다렸다가** 닫는다 — 로딩 중에
        # 닫으러 가면 아직 없어서 헛걸음하고, 그 팝업은 바로 다음 클릭을 막는다.
        _wait_for_loading(page)
        dismiss_modals(page)
        # 관람인원: party명 (일반 기준). 권종 세분화는 후속 단계.
        # 버튼 이름은 접근성 이름으로만 잡힌다 — 안의 숫자와 '선택'이 서로 다른
        # 요소라 텍스트로 찾으면 걸리지 않는다.
        _click_role(page, f"{ctx['party']} 선택", what="관람인원", timeout=5000)
        steps.mark("인원")
        # 좌석맵 열기 — 좌석은 이 모달 안에서만 누를 수 있다. 이미 열려 있으면
        # 버튼이 없으므로 짧게만 기다리고 넘어간다.
        if not _seatmap_ready(page):
            try:
                _click_role(page, "선택", what="좌석 선택", exact=True,
                            timeout=SEATMAP_OPEN_MS)
            except Exception:  # noqa: BLE001 - 없으면 이미 좌석맵이다
                pass
        # 좌석이 눌릴 수 있게 될 때까지만 기다린다. 못 봐도 멈추지는 않는다 —
        # 클래스명이 바뀌었을 수 있고, 그때는 _select_block의 재시도가 받아 준다.
        if not _wait_until(page, lambda: _seatmap_ready(page), SEATMAP_READY_MS):
            log.warning("좌석맵이 열린 것을 확인하지 못했습니다 — 그대로 골라 봅니다")
        steps.mark("좌석맵")
        # 좌석은 **여기 도착해서** 다시 고른다. 감지 때 읽은 배치도는 위의 화면
        # 전환을 지나오는 동안 낡는다.
        picked = _select_block(session, page, ctx)
        if not picked["ok"]:
            steps.mark("좌석고르기")
            return {"ok": False, "error": picked["error"],
                    "seat_labels": picked["labels"]}
        chosen = picked["labels"]
        steps.mark("좌석고르기")
        _click_role(page, "선택완료", what="좌석 선택완료", timeout=4000)
        # 결제하기 클릭 = 선점 트리거 (여기까지만! 결제 확정/푸시는 안 한다)
        if not _wait_until(page, lambda: bool(_visible_pay_buttons(page)),
                           PAY_BUTTON_READY_MS):
            raise RuntimeError("결제하기 버튼이 화면에 없습니다 "
                               "(좌석 선택이 끝나지 않았을 수 있습니다)")
        _click_through_modals(page, _visible_pay_buttons(page)[0],
                              what="결제하기", timeout=5000)
        # 선점 응답이 오면 바로 나간다. 예전에는 무조건 5초를 잤는데, 응답은 보통
        # 1초 안쪽에 오고 그 차이가 다음 회차를 잡을 수 있느냐를 가른다.
        if not _wait_until(page, lambda: "body" in captured, HOLD_RESPONSE_MS):
            log.warning("선점 응답을 %d초 안에 받지 못했습니다",
                        HOLD_RESPONSE_MS // 1000)
        steps.mark("선점")
    except Exception as exc:  # noqa: BLE001
        shot = _save_screenshot(page, ctx)
        detail = f" (화면: {shot})" if shot else ""
        # 도중에 죽었어도 결제 요청이 이미 나갔을 수 있다 — 그 사실이 예외 문구에
        # 묻히면 안 된다.
        if captured.get("payment_url"):
            log.error("자동 예매가 실패했지만 결제 계열 요청이 먼저 나갔습니다 "
                      "— CGV 예매 내역을 확인하세요 (요청: %s)",
                      captured["payment_url"])
            detail += " · 결제 계열 요청이 감지됐습니다 — CGV 예매 내역을 확인하세요"
        return {"ok": False, "error": f"UI 구동 실패: {exc}{detail}",
                "seat_labels": chosen}
    finally:
        # 하나씩 따로 뗀다. 한 묶음으로 두면 앞엣것이 터졌을 때 뒤엣것이 남는데,
        # 이 페이지는 미리 띄워 둔 상주 탭이라 선점할 때마다 리스너가 쌓인다.
        for event, handler in (("response", on_resp), ("request", on_req)):
            try:
                page.remove_listener(event, handler)
            except Exception:  # noqa: BLE001
                pass
        if routed:
            try:
                page.unroute(f"**/{SEAT_HOLD_URL_MARK}", on_route)
            except Exception:  # noqa: BLE001
                pass
        # 성공이든 실패든 어디에 시간을 썼는지 남긴다 — "느린 것 같다"는 인상만
        # 가지고는 어느 단계를 고쳐야 할지 알 수 없다.
        log.info("자동 예매 소요 — %s", steps.summary())

    # 결제 확정 요청이 나갔다면 우리가 알고 있던 화면 흐름이 아니다. 선점이
    # 됐는지와 무관하게 사람이 즉시 확인해야 하므로, 성공으로 넘기지 않고
    # 무슨 일이 있었는지 그대로 올린다.
    if captured.get("payment_url"):
        shot = _save_screenshot(page, ctx)
        log.error("자동 예매 중 결제 계열 요청이 나갔습니다 — '결제하기'가 더는 "
                  "선점 단계가 아닐 수 있습니다. 자동 예매를 멈추고 CGV 예매 "
                  "내역을 확인하세요. (요청: %s, 화면: %s)",
                  captured["payment_url"], shot or "저장 실패")
        return {"ok": False, "error":
                "결제 계열 요청이 감지돼 중단했습니다 — CGV 예매 내역을 직접 "
                "확인하세요. 화면 구성이 바뀐 것일 수 있으니 자동 예매를 "
                "잠시 꺼 두는 편이 안전합니다.", "seat_labels": chosen}

    # 관문이 요청을 끊었다면 선점은 일어나지 않았다. 화면이 의도한 회차가 아니라는
    # 뜻이므로 사람이 봐야 한다 — 조용히 "선점 실패"로 뭉개지 않는다.
    if captured.get("blocked"):
        shot = _save_screenshot(page, ctx)
        return {"ok": False, "seat_labels": chosen, "error":
                f"의도한 회차가 아니어서 선점을 막았습니다 — {captured['blocked']}"
                + (f" (화면: {shot})" if shot else "")}

    body = captured.get("body") or {}
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    if data.get("resultCode") in ("0", 0):
        _record_hold_request(captured, ctx)
        return {
            "ok": True,
            "seat_labels": chosen,
            "mov_atkt_no": data.get("movAtktNo"),
            "hold_expires_at": _parse_limit_dt(data.get("seatTempPrmpLimitDt")),
            "amount": None,  # 금액은 후속 단계에서 searchMovAtktSeatPrcList로 채운다
        }
    shot = _save_screenshot(page, ctx)
    detail = f" (화면: {shot})" if shot else ""
    return {"ok": False, "error": f"선점 응답을 확인하지 못했습니다{detail}",
            "seat_labels": chosen}


# ── 자동 결제: 카카오페이 결제 요청 ─────────────────────────────────────────
def parse_amount(text) -> int | None:
    """'15,000원' → 15000. 숫자가 없으면 None.

    금액은 화면에서 읽는다 — 선점 응답에는 들어 있지 않고, 쿠폰·포인트가 붙으면
    권종 가격을 우리가 다시 계산해 봐야 어차피 어긋난다.
    """
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(digits) if digits else None


def kakao_link_from_bridge(body) -> str | None:
    """카카오페이 브릿지 API 응답에서 **휴대폰용 결제 주소**를 만든다.

    쓸 값은 응답의 `hash` 필드다. QR을 디코드해 확인한 결과 화면의 QR도 이
    해시로 같은 주소를 담고 있었다.

    **`ios_app_url` 안의 `url=`을 쓰면 안 된다.** 그건 다른 주소이고 열면
    "인증정보를 찾을 수 없습니다"가 뜬다. 해시도 한 글자 짧아서, 눈으로는 같은
    값처럼 보인다 — 실제로 그렇게 만든 링크를 사용자가 받았고 열리지 않았다.

    iframe 주소 끝의 해시도 마찬가지로 한 글자 짧으므로 대신 쓸 수 없다.
    응답을 못 받았으면 링크가 없다고 하는 편이 죽은 링크를 보내는 것보다 낫다.
    """
    if not isinstance(body, dict):
        return None
    value = str(body.get("hash") or "").strip()
    # 해시는 hex 문자열이다. 엉뚱한 값으로 링크를 만들면 죽은 주소를 보내게 된다.
    if len(value) < 32 or not all(c in "0123456789abcdef" for c in value.lower()):
        return None
    return KAKAO_PAY_LINK.format(hash=value)


def bridge_expires_at(body) -> datetime | None:
    """브릿지 응답의 expired_timestamp → 링크가 죽는 KST 시각. 없으면 None.

    결제 링크는 선점(보통 10분)보다도 짧게(실측 15분) 죽는다. 알림에 만료를 함께
    적지 않으면 사람이 한참 뒤에 눌러 보고 왜 안 되는지 모른다.

    **epoch처럼 생겼지만 epoch이 아니다.** 카카오페이는 한국 벽시계 시각을 UTC인
    척 인코딩해서 준다 — 실측에서 15:14:58(KST)에 띄운 결제창의 값이
    1787758198이었고, 그걸 UTC로 읽으면 15:29:58(= 정확히 15분 뒤)이 나온다.
    그대로 `fromtimestamp(..., tz=KST)`로 읽으면 9시간 뒤가 되어, 이미 죽은
    링크를 "아직 유효"로 보여 주게 된다.
    """
    if not isinstance(body, dict):
        return None
    raw = body.get("expired_timestamp")
    try:
        wall = datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return wall.replace(tzinfo=KST)


def _payment_page_ready(page) -> bool:
    """결제 화면(결제수단 목록)이 떠 있는지."""
    try:
        return page.locator(PAY_LIST_SELECTOR).count() > 0
    except Exception:  # noqa: BLE001 - 전환 중이면 아직 아닌 것으로 본다
        return False


def _open_payment_page(page) -> bool:
    """결제 화면까지 밀어 넣는다. 닿으면 True.

    좌석 선택 뒤의 [결제하기]는 선점만 걸고 화면을 그대로 두므로, 결제수단 목록이
    보일 때까지 같은 버튼을 다시 누른다. 이미 선점된 상태라 다시 눌러도 좌석이
    이중으로 잡히지 않는다 — 같은 예매 건으로 이어진다.
    """
    for _ in range(PAY_PAGE_ROUNDS):
        if _payment_page_ready(page):
            return True
        dismiss_modals(page, rounds=1)
        buttons = [b for b in page.locator("button", has_text="결제하기").all()
                   if b.is_visible()]
        if buttons:
            try:
                buttons[-1].click(timeout=4000)
            except Exception as exc:  # noqa: BLE001 - 다음 바퀴에 다시 본다
                log.debug("결제 화면으로 넘기는 클릭 실패: %s", exc)
        # 예전에는 여기서 무조건 2.5초를 잤다. 화면이 0.3초 만에 떠도 그대로
        # 자고 **다음 바퀴에 가서야** True를 돌려주니, 성공한 경우에도 2.5초가
        # 통째로 버려졌다. 떴으면 그 자리에서 끝낸다.
        if _wait_until(page, lambda: _payment_page_ready(page),
                       PAY_PAGE_ROUND_MS):
            return True
    return _payment_page_ready(page)


def _choose_pay_method(page, method: str) -> None:
    """결제수단을 고른다. 못 고르면 RuntimeError.

    **기본값에 기대면 안 된다.** CGV는 그 계정이 **마지막에 쓴 수단**을 미리
    골라 두므로(searchLastPayknd), 어떤 때는 카카오페이가 이미 켜져 있고 어떤
    때는 Npay가 켜져 있다 — 실측에서 둘 다 봤다. 안 누르고 넘어가면 엉뚱한
    수단으로 결제창이 뜬다.
    """
    alt = PAY_METHODS.get(method)
    if not alt:
        raise RuntimeError(f"모르는 결제수단입니다: {method}")
    nodes = page.locator(f'{PAY_LIST_SELECTOR} > li:has(img[alt="{alt}"]) button')
    visible = [n for n in nodes.all() if n.is_visible()]
    if not visible:
        raise RuntimeError(f"결제수단에 {alt}가 없습니다 "
                           f"(이 극장·상품에서 못 쓰는 수단일 수 있습니다)")
    visible[0].click(timeout=5000)
    # 눌린 게 화면에 반영되기를 기다린다. 상한은 예전 고정 대기와 같게 두므로
    # 느려지는 경우는 없고, 빨리 반영되면 그만큼 일찍 넘어간다.
    if not _wait_until(page, lambda: _pay_method_active(page, alt),
                       PAY_METHOD_ACTIVE_MS):
        raise RuntimeError(f"{alt}를 눌렀지만 선택되지 않았습니다")


def _pay_method_active(page, alt: str) -> bool:
    """그 결제수단이 실제로 골라졌는지 — <li>에 active 표식이 붙었는지 본다."""
    try:
        cls = page.evaluate(
            """(sel) => {
                 const img = document.querySelector(
                   sel.list + ' img[alt="' + sel.alt + '"]');
                 const li = img && img.closest('li');
                 return li ? li.className : '';
               }""",
            {"list": PAY_LIST_SELECTOR, "alt": alt})
    except Exception:  # noqa: BLE001 - 못 읽으면 확인 못 한 것으로 본다
        return False
    return PAY_ACTIVE_MARK in (cls or "")


def _agree_terms(page) -> None:
    """'전체 약관 동의'를 켠다. 안 켜지면 RuntimeError.

    체크박스 자체는 화면에 없고(스타일용으로 숨겨 둔다) 라벨이 눌리는 구조라,
    input을 직접 누르려 하면 "not visible"로 타임아웃까지 기다렸다 죽는다.
    """
    try:
        page.locator(TERMS_ALL_LABEL).first.click(timeout=4000)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"약관 동의를 누르지 못했습니다: {exc}") from exc

    def checked():
        return page.evaluate(
            "(sel) => { const el = document.querySelector(sel);"
            "           return el ? el.checked : null; }", TERMS_ALL_INPUT)

    # 켜졌으면 바로 넘어간다. 못 읽는 경우(None)는 예전처럼 통과시킨다 —
    # 확인할 수단이 없는 것과 꺼져 있는 것은 다르다.
    _wait_until(page, lambda: checked() is not False, TERMS_CHECK_MS)
    try:
        state = checked()
    except Exception:  # noqa: BLE001
        state = None
    if state is False:
        raise RuntimeError("약관 동의가 켜지지 않았습니다")


def _read_amount(page) -> int | None:
    try:
        text = page.locator(FINAL_AMOUNT_SELECTOR).first.text_content()
    except Exception:  # noqa: BLE001 - 금액은 있으면 좋은 값이지 필수는 아니다
        return None
    return parse_amount(text)


def _click_final_pay(page) -> None:
    """최종 [N원 결제하기]를 누른다.

    이 화면에도 '결제하기'라는 글자가 여럿 있어서, **금액이 함께 적힌** 버튼만
    고른다 — 그게 PG로 넘기는 버튼이다.
    """
    buttons = [b for b in page.locator("button", has_text="결제하기").all()
               if b.is_visible() and "원" in (b.inner_text() or "")]
    if not buttons:
        raise RuntimeError("최종 결제 버튼을 찾지 못했습니다")
    buttons[0].click(timeout=5000)


def _wait_for_bridge(page, timeout_ms: int = PAY_BRIDGE_WAIT_MS):
    """카카오페이 결제창(iframe)이 뜰 때까지 기다린다. 그 프레임을 돌려준다."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            for frame in page.frames:
                if KAKAO_BRIDGE_MARK in (frame.url or ""):
                    return frame
        except Exception:  # noqa: BLE001 - 프레임 목록이 바뀌는 중일 수 있다
            pass
        page.wait_for_timeout(STEP_POLL_MS)
    return None


def pay_block(session, ctx: dict, *, method: str = DEFAULT_PAY_METHOD) -> dict:
    """선점된 예매 건을 결제 화면까지 몰아 **카카오페이 결제 링크**를 받아 온다.

    hold_block이 끝난 **직후의 같은 페이지**에서 이어서 돈다. 반환:
      {ok, pay_url, pay_expires_at, amount, method, error}

    카카오페이 인증(카톡 승인·비밀번호)은 하지 않는다 — 할 수도 없고, 그게 이
    설계의 요점이다. 결제창이 뜨면 거기 담긴 휴대폰용 주소만 챙겨서 나온다.
    """
    import json as _json

    # 선점을 건 바로 그 화면에서 이어서 돈다.
    page = ctx.get("_page") or session.page
    captured: dict = {}

    def on_resp(r):
        url = r.url or ""
        if KAKAO_BRIDGE_MARK not in url and "kakaopay.com" not in url:
            return
        if KAKAO_BRIDGE_API_MARK not in url:
            return
        try:
            body = _json.loads(r.text())
        except Exception:  # noqa: BLE001 - HTML이면 우리가 찾는 응답이 아니다
            return
        if isinstance(body, dict) and body.get("tid"):
            captured.setdefault("bridge", body)
    page.on("response", on_resp)

    try:
        if not _open_payment_page(page):
            raise RuntimeError("결제 화면으로 넘어가지 못했습니다")
        _choose_pay_method(page, method)
        amount = _read_amount(page)
        _agree_terms(page)
        _click_final_pay(page)
        frame = _wait_for_bridge(page)
        if frame is None:
            raise RuntimeError("카카오페이 결제창이 뜨지 않았습니다")
        # 브릿지 응답이 프레임보다 늦게 올 수 있다. **리스너를 떼기 전에** 기다려야
        # 한다 — 떼고 나서 기다리면 그 사이 온 응답을 아무도 받지 않는다.
        # 못 받아도 프레임 주소로 같은 링크를 만들 수 있으니 오래 끌지는 않는다.
        _wait_until(page, lambda: bool(captured.get("bridge")),
                    PAY_BRIDGE_BODY_MS)
    except Exception as exc:  # noqa: BLE001 - 결제 요청 실패는 선점을 무르지 않는다
        shot = _save_screenshot(page, ctx)
        detail = f" (화면: {shot})" if shot else ""
        return {"ok": False, "method": method, "pay_url": None,
                "pay_expires_at": None, "amount": None,
                "error": f"{exc}{detail}"}
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:  # noqa: BLE001
            pass

    body = captured.get("bridge")
    pay_url = kakao_link_from_bridge(body)
    if not pay_url:
        shot = _save_screenshot(page, ctx)
        return {"ok": False, "method": method, "pay_url": None,
                "pay_expires_at": None, "amount": amount,
                "error": "결제창은 떴지만 결제 링크를 읽지 못했습니다"
                         + (f" (화면: {shot})" if shot else "")}
    return {"ok": True, "method": method, "pay_url": pay_url,
            "pay_expires_at": bridge_expires_at(body), "amount": amount,
            "error": ""}
