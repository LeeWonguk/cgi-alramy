#!/usr/bin/env python3
"""자동 예매(좌석 선점) — Phase 1 auto-book.

좌석 감시에서 빈자리(또는 연속 블록)가 감지되고 그 감시의 auto_book이 켜져 있으면,
인원수만큼 좌석을 골라 CGV에 **임시 선점(seatTempPrmp)** 까지 한다. **결제 확정은
하지 않는다** — 돈이 움직이는 단계는 사람이 자기 기기에서 마친다(설계 문서 참고).

두 층으로 나뉜다:
  - try_auto_book(...)  — 순수 오케스트레이션(좌석 선택·중복 방지·이력 기록·감시 비활성).
    hold_fn을 주입할 수 있어 브라우저 없이 단위 테스트가 된다.
  - hold_block(...)     — 실제 CGV 예매 UI를 몰아 선점을 거는 라이브 구동. 사이트의
    자체 JS가 요청 바디(custNo 등)를 채우므로 우리가 재구성하지 않는다. seatTempPrmp
    응답에서 예매번호·만료시각을 읽고 **결제 버튼 이후로는 진행하지 않는다.**

라이브 구동은 브라우저 워커 스레드에서(세션을 소유한 스레드에서) 실행돼야 한다.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import seats as seats_mod
import store

log = logging.getLogger("cgv-watch.booking")

SEAT_HOLD_URL_MARK = "seatTemp/seatTempPrmp"

BOOKING_PAGE = "https://cgv.co.kr/cnm/movieBook/movie"
# '결제하기'를 누르는 것이 **선점까지만** 한다는 전제 위에 이 모듈의 안전성이
# 통째로 서 있다. CGV가 그 버튼의 뜻을 바꾸면 우리는 아무것도 눈치채지 못한 채
# 돈을 쓰게 된다 — 코드가 스스로 알아챌 수 있는 유일한 지점이 그때 나가는
# 요청이므로, 결제 확정 계열 경로가 보이면 크게 남기고 실패로 끊는다.
# 선점 응답(seatTempPrmp)은 이 목록과 겹치지 않아야 한다.
PAYMENT_URL_MARKS = (
    "/pay/", "payApprov", "payApprv", "atktPay", "movAtktPay",
    "settle", "approvePayment", "paymentComplete",
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
# 가리는 데 쓴다. 이 컨테이너의 텍스트는 "IMAX관 | IMAX LASER 2D | 07:30-10:32 | …"
# 처럼 상영관 이름으로 시작한다.
SCREEN_CONTAINER_CLASSES = ("screenInfoStore_container", "screenInfo_contentWrap")

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

# 좌석 고르기 재시도. 한 바퀴가 [배치도 다시 읽기 → 고르기 → 클릭]이라 1초 안쪽이니
# 몇 번을 돌아도 싸다. 다만 여기서 오래 끌 이유는 없다 — 이 시각을 넘길 만큼
# 경쟁이 심하면 어차피 다음 사이클에 다시 본다.
SEAT_PICK_ATTEMPTS = 3
SEAT_PICK_DEADLINE = 10.0  # 초


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
                  hold_fn=None, dry_run: bool = False) -> dict:
    """감시 하나의 한 회차에서 자동 선점을 시도한다.

    반환: {"action": skip|held|failed|no_seats, ...}. hold_fn(session, ctx)->result 를
    주입하면 라이브 구동 대신 그걸 쓴다(테스트용). 기본은 hold_block.

    여기서 고르는 좌석은 **후보**다. 감지 때 읽은 배치도는 UI를 모는 동안 낡으므로,
    실제로 누를 좌석은 좌석맵에 도착해서 다시 고른다(hold_block → _select_block).
    그래서 이력에 남는 좌석도 hold가 실제로 고른 것으로 덮어쓴다.
    """
    if not watch.get("auto_book"):
        return {"action": "skip", "reason": "auto_book off"}

    watch_id = watch["id"]
    # 이미 유효한 선점이 있으면 다시 잡지 않는다.
    if not dry_run and store.active_hold(watch_id):
        return {"action": "skip", "reason": "already held"}

    party = max(1, int(watch.get("party_size") or 1))
    chosen = seats_mod.pick_block(parsed_seats, party, watch.get("rows"))
    if len(chosen) < party:
        return {"action": "no_seats", "reason": f"{party}석 연속 없음"}

    labels = [s["label"] for s in chosen]
    loc_nos = [s["seat_loc_no"] for s in chosen]
    showtime_key = seats_mod.showtime_key(row)
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
           "rows": watch.get("rows"), "site_no": site_no,
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
        store.finish_booking_attempt(
            attempt_id, "held", mov_atkt_no=result.get("mov_atkt_no"),
            amount=result.get("amount"), seat_labels=final,
            hold_expires_at=result.get("hold_expires_at"))
        # 선점에 성공하면 그 감시는 꺼서 중복 선점을 막는다.
        store.set_seat_watch(watch_id, enabled=False)
        return {"action": "held", "attempt_id": attempt_id, "seats": final,
                "mov_atkt_no": result.get("mov_atkt_no"),
                "hold_expires_at": result.get("hold_expires_at"),
                "amount": result.get("amount")}

    store.finish_booking_attempt(attempt_id, "failed", seat_labels=final,
                                 error=result.get("error") or "선점 실패")
    return {"action": "failed", "error": result.get("error"),
            "attempt_id": attempt_id, "seats": final}


def build_hold_alert(mov_nm: str, site_nm: str, scn_ymd: str, start_hhmm: str,
                     seat_labels: list[str], hold_expires_at, amount) -> str:
    """선점 성공 알림. 사용자가 만료 전에 결제를 마치도록 안내한다."""
    from watch import BOOKING_URL, fmt_date

    when = ""
    if hold_expires_at is not None:
        # 읽는 사람은 극장 앞에 서 있다 — 서버가 어디서 돌든 한국 시각으로 적는다.
        try:
            when = hold_expires_at.astimezone(KST).strftime("%H:%M")
        except Exception:  # noqa: BLE001
            when = str(hold_expires_at)
    amt = f"\n💳 예상 금액 {amount:,}원" if amount else ""
    limit = f"\n⏰ *{when}까지 결제*해야 좌석이 유지됩니다" if when else ""
    return (f"🎫 *좌석 선점 완료 — 결제만 남았습니다*\n"
            f"*{mov_nm}* · CGV {site_nm}\n"
            f"{fmt_date(scn_ymd)} {start_hhmm} · {', '.join(seats_mod.sort_labels(seat_labels))}"
            f"{amt}{limit}\n"
            f"▶ CGV 앱/웹에서 예매 진행 중인 건으로 결제를 완료하세요: {BOOKING_URL}")


# ── 라이브 구동: CGV 예매 UI를 몰아 선점 (결제 확정 안 함) ────────────────────
def date_labels(scn_ymd: str) -> list[str]:
    """'20260831' → 날짜 스트립에 적힐 법한 표기들 ['31', '8.31'].

    같은 달이면 일자만('31'), 달을 넘기면 '9.1'처럼 적힌다. 어느 쪽인지는 오늘이
    며칠인지에 달려 있으므로 둘 다 후보로 두고 실제로 있는 쪽을 쓴다.
    """
    ymd = "".join(ch for ch in (scn_ymd or "") if ch.isdigit())
    if len(ymd) != 8:
        raise RuntimeError(f"날짜를 이해할 수 없습니다: {scn_ymd!r}")
    month, day = int(ymd[4:6]), int(ymd[6:8])
    return [str(day), f"{month}.{day}"]


def _visible(nodes) -> list:
    return [n for n in nodes if n.is_visible()]


def _wait_for_any(page, selectors, timeout: int = 9000) -> None:
    """셀렉터 중 하나라도 화면에 나타날 때까지 기다린다.

    아래에서 `locator(...).all()`로 요소를 훑는데, `.all()`은 Playwright의 자동
    대기를 **거치지 않는다** — 아직 안 그려졌으면 그냥 빈 목록이 온다. 그대로 두면
    화면이 조금만 느려도 "버튼을 찾지 못했습니다"로 끝난다.

    끝내 안 나타나도 여기서 예외를 내지는 않는다. 판정은 호출자가 해야 더 쓸모
    있는 문구(어떤 날짜를, 어떤 시각을 찾고 있었는지)를 낼 수 있다.
    """
    share = max(1500, int(timeout / max(1, len(selectors))))
    for selector in selectors:
        try:
            page.wait_for_selector(selector, timeout=share, state="visible")
            return
        except Exception:  # noqa: BLE001 - 다음 후보 셀렉터로
            continue


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
            page.wait_for_timeout(2500)
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
    for selector in DATE_BUTTON_SELECTORS:
        actives = [b for b in _visible(page.locator(selector).all())
                   if DATE_ACTIVE_MARK in (b.get_attribute("class") or "")]
        if not actives:
            continue
        for button in actives:
            try:
                text = (button.locator(DATE_NUMBER_SELECTOR).first
                        .text_content() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text in wanted:
                return
        raise RuntimeError(
            f"{scn_ymd} 날짜가 선택되지 않았습니다 — 화면은 다른 날짜를 "
            f"보고 있습니다. 엉뚱한 날짜를 선점하지 않도록 멈춥니다.")
    log.warning("날짜 선택 상태를 확인할 수 없습니다 (활성 표시를 못 찾음) — "
                "%s로 진행합니다", scn_ymd)


def _click_showtime(page, start_hhmm: str, screen_name: str = "") -> None:
    """상영 시작 시각 버튼을 누른다. 셀렉터를 순서대로 내려가며 찾는다.

    시작 시각이 **정확히** 일치하는 요소만 본다. 부분일치로 두면 '18:00'이
    '18:00-21:02'의 끝 시각이나 '218:00' 같은 것에도 걸릴 수 있다.

    같은 시각의 회차가 여러 상영관에 있으면(IMAX 21:00과 일반관 21:00) 상영관
    이름으로 가린다. 그래도 못 가리면 **아무거나 누르지 않고 멈춘다** — 잘못
    고르면 다른 상영관의 좌석을 선점하게 되고, 그건 안 잡느니만 못하다.
    """
    _wait_for_any(page, SHOWTIME_SELECTORS)
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

        if len(nodes) > 1 and screen_name:
            narrowed = [n for n in nodes if _in_screen(n, screen_name)]
            if narrowed:
                nodes = narrowed
        if len(nodes) > 1:
            raise RuntimeError(
                f"{start_hhmm} 회차가 {len(nodes)}곳에 있어 어느 상영관인지 "
                f"가리지 못했습니다 (상영관: {screen_name or '지정 없음'}). "
                f"엉뚱한 상영관을 선점하지 않도록 멈춥니다.")

        _click_showtime_node(nodes[0], start_hhmm)
        if i:
            log.warning("회차 버튼을 대체 셀렉터로 찾았습니다 (%s) — CGV가 "
                        "화면을 바꾼 것 같습니다. SHOWTIME_SELECTORS를 "
                        "확인하세요.", selector)
        return
    raise RuntimeError(
        f"{start_hhmm} 회차 버튼을 찾지 못했습니다"
        f"{f' — {last_exc}' if last_exc else ''}")


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


def _pick_seats(page, seat_labels) -> tuple[list[str], list[str]]:
    """좌석맵에서 지정한 좌석을 하나씩 누른다.

    한 좌석이 실패해도 나머지는 마저 눌러 본다 — 무엇이 팔렸는지 전부 알아야
    쓸 만한 오류 문구가 나오고, 어차피 여기서는 아직 아무것도 선점되지 않는다.
    **무엇을 눌렀는지도 함께 돌려준다.** 다시 고를 수 있는지가 여기에 달렸다:
    하나도 못 눌렀으면 화면에 아무 흔적이 없어 그냥 다시 고르면 되지만, 일부가
    이미 선택돼 있으면 인원수를 넘겨 엉뚱한 자리를 선점할 수 있다.
    """
    clicked, missed = [], []
    for label in seat_labels:
        try:
            page.get_by_text(label, exact=True).last.click(timeout=3000)
        except Exception as exc:  # noqa: BLE001 - 못 누른 좌석은 모아서 보고
            log.warning("좌석 %s 클릭 실패: %s", label, exc)
            missed.append(label)
        else:
            clicked.append(label)
    return clicked, missed


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
            block = seats_mod.pick_block(live, party, ctx.get("rows"))
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

        clicked, missed = _pick_seats(page, labels)
        if not missed:
            return {"ok": True, "labels": labels, "error": ""}

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


def _in_screen(node, screen_name: str) -> bool:
    """이 회차가 지정한 상영관 블록 안에 있는지."""
    for cls in SCREEN_CONTAINER_CLASSES:
        try:
            container = node.locator(
                f"xpath=ancestor::*[contains(@class, '{cls}')][1]")
            if container.count():
                return screen_name in (container.first.text_content() or "")
        except Exception:  # noqa: BLE001 - 다음 컨테이너 후보로
            continue
    return False


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

    page = session.page
    captured = {}
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
    page.on("response", on_resp)

    try:
        page.goto(BOOKING_PAGE, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(3500)
        page.get_by_text(ctx["mov_nm"], exact=True).first.click(timeout=10000)
        page.wait_for_timeout(2500)
        page.get_by_text(ctx["site_nm"], exact=False).first.click(timeout=6000)
        page.wait_for_timeout(2500)
        # 날짜를 고른다. 이 화면은 기본이 **오늘**이라, 건너뛰면 오늘 상영표에서
        # 회차를 찾게 된다 — 없으면 실패하고, 하필 같은 시각이 있으면 엉뚱한
        # 날짜를 선점한다.
        _click_date(page, ctx["scn_ymd"])
        _click_showtime(page, ctx["start_hhmm"], ctx.get("scns_nm", ""))
        page.wait_for_timeout(5000)
        for t in ("확인", "닫기"):
            try:
                page.get_by_role("button", name=t).first.click(timeout=2500)
                break
            except Exception:  # noqa: BLE001
                pass
        # 관람인원: party명 (일반 기준). 권종 세분화는 후속 단계.
        page.get_by_role("button", name=f"{ctx['party']} 선택").first.click(timeout=5000)
        page.wait_for_timeout(1500)
        # 좌석맵 열기
        try:
            page.get_by_role("button", name="선택", exact=True).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        # 좌석은 **여기 도착해서** 다시 고른다. 감지 때 읽은 배치도는 위의 화면
        # 전환을 지나오는 30초 남짓 동안 낡는다.
        picked = _select_block(session, page, ctx)
        if not picked["ok"]:
            return {"ok": False, "error": picked["error"],
                    "seat_labels": picked["labels"]}
        chosen = picked["labels"]
        page.get_by_role("button", name="선택완료").first.click(timeout=4000)
        page.wait_for_timeout(2500)
        # 결제하기 클릭 = 선점 트리거 (여기까지만! 결제 확정/푸시는 안 한다)
        btn = page.locator("button", has_text="결제하기").first
        btn.click(timeout=5000)
        page.wait_for_timeout(5000)
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
        try:
            page.remove_listener("response", on_resp)
        except Exception:  # noqa: BLE001
            pass

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

    body = captured.get("body") or {}
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    if data.get("resultCode") in ("0", 0):
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
