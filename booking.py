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
from datetime import datetime

import seats as seats_mod
import store

log = logging.getLogger("cgv-watch.booking")

SEAT_HOLD_URL_MARK = "seatTemp/seatTempPrmp"


def _fmt_hhmm(scnsrt: str) -> str:
    """'2210' → '22:10'. 이미 콜론이 있으면 그대로."""
    s = (scnsrt or "").strip()
    if ":" in s or len(s) < 4:
        return s
    return f"{s[:2]}:{s[2:4]}"


def _parse_limit_dt(raw: str):
    """'20260825160018' → datetime(aware). 실패하면 None."""
    if not raw or len(raw) < 12:
        return None
    try:
        naive = datetime.strptime(raw[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
        return naive.astimezone()
    except ValueError:
        return None


def try_auto_book(session, watch: dict, row: dict, parsed_seats: list[dict],
                  *, mov_nm: str = "", site_nm: str = "",
                  hold_fn=None, dry_run: bool = False) -> dict:
    """감시 하나의 한 회차에서 자동 선점을 시도한다.

    반환: {"action": skip|held|failed|no_seats, ...}. hold_fn(session, ctx)->result 를
    주입하면 라이브 구동 대신 그걸 쓴다(테스트용). 기본은 hold_block.
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
           "row": row}
    fn = hold_fn or hold_block
    try:
        result = fn(session, ctx)
    except Exception as exc:  # noqa: BLE001 - 라이브 구동 실패는 이력에 남기고 넘어간다
        log.warning("auto-book 선점 실패(예외): %s", exc)
        store.finish_booking_attempt(attempt_id, "failed", error=str(exc))
        return {"action": "failed", "error": str(exc), "attempt_id": attempt_id,
                "seats": labels}

    if result.get("ok"):
        store.finish_booking_attempt(
            attempt_id, "held", mov_atkt_no=result.get("mov_atkt_no"),
            amount=result.get("amount"),
            hold_expires_at=result.get("hold_expires_at"))
        # 선점에 성공하면 그 감시는 꺼서 중복 선점을 막는다.
        store.set_seat_watch(watch_id, enabled=False)
        return {"action": "held", "attempt_id": attempt_id, "seats": labels,
                "mov_atkt_no": result.get("mov_atkt_no"),
                "hold_expires_at": result.get("hold_expires_at"),
                "amount": result.get("amount")}

    store.finish_booking_attempt(attempt_id, "failed",
                                 error=result.get("error") or "선점 실패")
    return {"action": "failed", "error": result.get("error"),
            "attempt_id": attempt_id, "seats": labels}


def build_hold_alert(mov_nm: str, site_nm: str, scn_ymd: str, start_hhmm: str,
                     seat_labels: list[str], hold_expires_at, amount) -> str:
    """선점 성공 알림. 사용자가 만료 전에 결제를 마치도록 안내한다."""
    from watch import BOOKING_URL, fmt_date

    when = ""
    if hold_expires_at is not None:
        try:
            when = hold_expires_at.strftime("%H:%M")
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
def hold_block(session, ctx: dict) -> dict:
    """CGV 예매 UI를 구동해 좌석을 임시 선점한다. 결제 버튼 이후로는 가지 않는다.

    ctx: {mov_nm, site_nm, scn_ymd, start_hhmm, seat_labels, party, row}
    반환: {ok, mov_atkt_no, hold_expires_at, amount, error}

    사이트의 자체 JS가 seatTempPrmp 요청을 만들어 보내므로(custNo 등 포함), 우리는
    UI만 조작하고 그 응답을 가로채 예매번호·만료시각을 읽는다.
    """
    import json as _json

    page = session._page
    captured = {}

    def on_resp(r):
        if SEAT_HOLD_URL_MARK in r.url:
            try:
                captured["body"] = _json.loads(r.text())
            except Exception:  # noqa: BLE001
                captured["body"] = None
    page.on("response", on_resp)

    try:
        page.goto("https://cgv.co.kr/cnm/movieBook/movie",
                  wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(3500)
        page.get_by_text(ctx["mov_nm"], exact=True).first.click(timeout=10000)
        page.wait_for_timeout(2500)
        page.get_by_text(ctx["site_nm"], exact=False).first.click(timeout=6000)
        page.wait_for_timeout(2500)
        page.locator("span.cinemaSchedule_startTime__ZE5Zp",
                     has_text=ctx["start_hhmm"]).first.click(timeout=10000)
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
        # 지정 좌석 클릭
        picked = 0
        for lbl in ctx["seat_labels"]:
            try:
                page.get_by_text(lbl, exact=True).last.click(timeout=3000)
                picked += 1
            except Exception:  # noqa: BLE001
                log.warning("좌석 %s 클릭 실패", lbl)
        if picked == 0:
            return {"ok": False, "error": "좌석을 선택하지 못했습니다"}
        page.get_by_role("button", name="선택완료").first.click(timeout=4000)
        page.wait_for_timeout(2500)
        # 결제하기 클릭 = 선점 트리거 (여기까지만! 결제 확정/푸시는 안 한다)
        btn = page.locator("button", has_text="결제하기").first
        btn.click(timeout=5000)
        page.wait_for_timeout(5000)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"UI 구동 실패: {exc}"}
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:  # noqa: BLE001
            pass

    body = captured.get("body") or {}
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    if data.get("resultCode") in ("0", 0):
        return {
            "ok": True,
            "mov_atkt_no": data.get("movAtktNo"),
            "hold_expires_at": _parse_limit_dt(data.get("seatTempPrmpLimitDt")),
            "amount": None,  # 금액은 후속 단계에서 searchMovAtktSeatPrcList로 채운다
        }
    return {"ok": False, "error": "선점 응답을 확인하지 못했습니다"}
