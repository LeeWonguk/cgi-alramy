#!/usr/bin/env python3
"""좌석 단위 추적 (Phase 1).

기존 감시가 "예매 날짜가 열렸나"를 본다면, 좌석 감시는 한 걸음 더 들어가 **특정
날짜의 특정 회차에서 원하는 열(row)에 빈좌석이 생겼나**를 본다. 좌석 배치도는
로그인해야 열리므로(CgvSession.seat_map은 401을 내면 로그인부터 해야 한다),
cgv_login.ensure_logged_in으로 세션을 만든 뒤에 쓴다.

이 모듈은 배치도 원본을 다루는 **순수 로직**(파싱·필터·비교·문구)을 담는다 —
DB나 네트워크를 모른다. 그래서 저장된 픽스처만으로 단위 테스트가 된다.
"""

from __future__ import annotations

from watch import fmt_date, fmt_time


def parse_seats(seat_data: dict) -> list[dict]:
    """좌석 배치도 원본(CgvSession.seat_map 반환)에서 좌석 목록을 뽑는다.

    각 좌석: {row, no, label, available, kind, zone}. available은 판매 가능
    여부(seatSaleYn == 'Y')다 — 빈좌석이 곧 available이다.
    """
    items = seat_data.get("items") or []
    if not items:
        return []
    seats = items[0].get("seats") or []
    out = []
    for s in seats:
        row = (s.get("seatRowNm") or "").strip()
        no = str(s.get("seatNo") or "").strip()
        if not row or not no:
            continue
        out.append({
            "row": row,
            "no": no,
            "label": f"{row}{no}",
            "available": s.get("seatSaleYn") == "Y",
            "kind": s.get("stkndNm") or "",
            "zone": s.get("szoneExpoNm") or s.get("szoneNm") or "",
        })
    return out


def normalize_rows(rows) -> list[str]:
    """행 필터를 대문자·공백정리된 문자열 리스트로. 빈 값이면 [] (= 전체)."""
    if not rows:
        return []
    if isinstance(rows, str):
        rows = rows.replace(",", " ").split()
    seen, out = set(), []
    for r in rows:
        key = str(r).strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def available_labels(seats: list[dict], rows=None) -> set[str]:
    """지정한 열에서 지금 비어 있는(판매 가능) 좌석 라벨 집합. rows가 비면 전 열."""
    wanted = normalize_rows(rows)
    return {
        s["label"] for s in seats
        if s["available"] and (not wanted or s["row"].upper() in wanted)
    }


def summarize(seats: list[dict], rows=None) -> dict:
    """열 필터를 적용한 좌석 요약 — {total, available, rows}."""
    wanted = normalize_rows(rows)
    scoped = [s for s in seats
              if not wanted or s["row"].upper() in wanted]
    return {
        "total": len(scoped),
        "available": sum(1 for s in scoped if s["available"]),
        "rows": sorted({s["row"] for s in scoped}),
    }


def diff_available(known: set[str], current: set[str]) -> list[str]:
    """직전에 없던, 이번에 새로 생긴 빈좌석 라벨 (정렬)."""
    return sort_labels(current - set(known))


def sort_labels(labels) -> list[str]:
    """좌석 라벨을 열(알파벳)→번호(숫자) 순으로 정렬한다 ('A2'가 'A10'보다 앞)."""
    def key(lbl: str):
        i = 0
        while i < len(lbl) and not lbl[i].isdigit():
            i += 1
        row, num = lbl[:i], lbl[i:]
        return (row, int(num) if num.isdigit() else 0)
    return sorted(labels, key=key)


def build_seat_alert(mov_nm: str, site_nm: str, ymd: str, start_hhmm: str,
                     screen_label: str, new_labels: list[str],
                     available_now: int, rows=None) -> str:
    """새 빈좌석 알림 문구. 웹훅으로 그대로 나간다."""
    wanted = normalize_rows(rows)
    scope = f" ({'·'.join(wanted)}열)" if wanted else ""
    head = (f"💺 *빈좌석 발생*{scope}\n"
            f"*{mov_nm}* · CGV {site_nm}\n"
            f"{fmt_date(ymd)} {fmt_time(start_hhmm)} · {screen_label}")
    seats_line = "➕ " + ", ".join(sort_labels(new_labels))
    tail = f"▶ 지금 비어 있는 좌석 {available_now}석"
    return f"{head}\n{seats_line}\n{tail}"


def showtime_key(row: dict) -> str:
    """회차를 상태 맵의 키로. 상영관 번호 + 회차 순번이면 하루 안에서 유일하다."""
    return f"{row.get('scnsNo')}|{row.get('scnSseq')}"


# ── 사이클: 좌석 감시 한 바퀴 ────────────────────────────────────────────────
def check_seat_watches(session, *, dry_run: bool = False) -> dict:
    """모든 좌석 감시를 한 바퀴 확인한다. 세션은 로그인 가능한 상태여야 한다.

    좌석 배치도는 소유자별 로그인이 필요하므로 소유자 단위로 묶어, 먼저
    ensure_logged_in으로 세션을 그 사람 계정으로 만든 뒤 그 사람의 감시를 본다.
    첫 관측은 기준선만 잡고 알리지 않는다 — 이후 새로 생긴 빈좌석만 알린다.
    """
    import cgv_login
    import store
    import watch

    watches = store.seat_watches(enabled_only=True)
    summary = {"watches_checked": 0, "alerts_sent": 0}
    if not watches:
        return summary

    catalog = watch.Catalog(session)

    by_owner: dict[int, list[dict]] = {}
    for w in watches:
        if w["owner_id"] is None:
            # 주인 없는 좌석 감시는 로그인할 계정이 없어 확인할 수 없다.
            continue
        by_owner.setdefault(w["owner_id"], []).append(w)

    for owner_id, group in by_owner.items():
        if not cgv_login.ensure_logged_in(owner_id, session):
            for w in group:
                if not dry_run:
                    store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                          error="CGV 로그인이 필요합니다")
            continue

        webhook = None
        webhook_kind = None
        owner = store.user(owner_id)
        if owner:
            webhook, webhook_kind = owner.get("webhook_url"), owner.get("webhook_kind")

        for w in group:
            summary["watches_checked"] += 1
            sent = _check_one_seat_watch(
                session, catalog, w, webhook, webhook_kind, dry_run=dry_run)
            summary["alerts_sent"] += sent

    return summary


def _check_one_seat_watch(session, catalog, w, webhook, webhook_kind,
                          *, dry_run: bool) -> int:
    """좌석 감시 하나를 확인하고 보낸 알림 수를 돌려준다."""
    import store
    import watch

    movie, movie_problem = watch.resolve(w["movie_query"], catalog.movies, "movNm")
    site, site_problem = watch.resolve(w["site_query"], catalog.sites, "siteNm")
    if movie is None or site is None:
        problem = movie_problem if movie is None else site_problem
        if not dry_run:
            store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                  error=f"영화·극장 확인 실패: {problem}")
        return 0

    mov_no, site_no = movie["movNo"], site["siteNo"]
    mov_nm, site_nm = movie["movNm"], site["siteNm"]

    try:
        schedule = session.showtimes(site_no, mov_no, w["scn_ymd"])
    except RuntimeError as exc:
        if not dry_run:
            store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                  error=f"시간표 조회 실패: {exc}")
        return 0

    wanted_screens = store.normalize_screen_types(w["screen_types"])
    rows = w["rows"]
    prev = store.prev_seat_state(w["id"])
    fresh_state: dict[str, list[str]] = {}
    alerts: list[dict] = []

    for row in schedule:
        if wanted_screens and not watch.matches_screen_types(row, wanted_screens):
            continue
        key = showtime_key(row)
        try:
            seat_data = session.seat_map(
                site_no=row.get("siteNo") or site_no,
                scns_no=row["scnsNo"], ymd=w["scn_ymd"],
                scn_sseq=row["scnSseq"])
        except RuntimeError as exc:
            # 이 회차만 실패 — 직전 상태를 유지해 다음에 다시 본다.
            if key in prev:
                fresh_state[key] = prev[key]
            watch.log.warning("좌석 배치도 조회 실패 (%s %s): %s",
                              site_nm, key, exc)
            continue

        seats = parse_seats(seat_data)
        current = available_labels(seats, rows)
        fresh_state[key] = sort_labels(current)

        if key in prev:  # 이 회차를 전에 봤다면 새로 생긴 좌석만 알린다
            newly = diff_available(set(prev[key]), current)
            if newly:
                alerts.append({
                    "body": build_seat_alert(
                        mov_nm, site_nm, w["scn_ymd"], row.get("scnsrtTm") or "",
                        watch.screen_label(row), newly, len(current), rows),
                    "start": row.get("scnsrtTm") or "",
                })

    error = None if fresh_state else "확인된 회차가 없습니다"
    if not dry_run:
        # 실패 회차만 있고 새 상태가 하나도 없으면 이전 상태를 지키지 않도록,
        # fresh_state가 비어 있어도 error만 남기고 상태는 유지한다.
        store.save_seat_state(w["id"], fresh_state or prev, error=error)

    sent = 0
    for alert in alerts:
        if watch.deliver_alert("seat_open", alert["body"], dry_run=dry_run,
                               owner_id=w["owner_id"], webhook_url=webhook,
                               webhook_kind=webhook_kind, mov_nm=mov_nm,
                               site_nm=site_nm, dates=[w["scn_ymd"]]):
            sent += 1
    return sent
