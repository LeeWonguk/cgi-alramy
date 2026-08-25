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
            # 인접(연속) 판정용 — 같은 열에서 x좌표가 딱 붙고 통로가 없으면 이웃이다.
            "x_start": _to_int(s.get("xcoordStartVal")),
            "x_end": _to_int(s.get("xcoordEndVal")),
            "left_pway": s.get("leftPwayYn") == "Y",
            "right_pway": s.get("rghtPwayYn") == "Y",
        })
    return out


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


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


# ── 연속(나란히 붙은) 좌석 ────────────────────────────────────────────────────
def _adjacent(a: dict, b: dict) -> bool:
    """b가 a의 바로 오른쪽 이웃인지. 같은 열에서 x좌표가 딱 붙고 통로가 없어야 한다.

    좌표가 없으면(예전·이형 응답) 좌석 번호가 1 차이인지로 보수적으로 판정한다.
    """
    if a["row"] != b["row"]:
        return False
    if a["x_end"] is not None and b["x_start"] is not None:
        touching = a["x_end"] == b["x_start"]
        no_aisle = not a["right_pway"] and not b["left_pway"]
        return touching and no_aisle
    # 좌표가 없을 때의 폴백 — 번호 연속 (통로를 못 보므로 과대평가일 수 있다)
    an, bn = _to_int(a["no"]), _to_int(b["no"])
    return an is not None and bn is not None and bn - an == 1


def _rows_by_x(seats: list[dict], rows=None) -> dict[str, list[dict]]:
    """열별로 x좌표(없으면 번호) 순으로 정렬한 좌석 리스트."""
    wanted = normalize_rows(rows)
    grouped: dict[str, list[dict]] = {}
    for s in seats:
        if wanted and s["row"].upper() not in wanted:
            continue
        grouped.setdefault(s["row"], []).append(s)
    for row in grouped:
        grouped[row].sort(key=lambda s: (s["x_start"] if s["x_start"] is not None
                                         else (_to_int(s["no"]) or 0)))
    return grouped


def consecutive_runs(seats: list[dict], available: set[str] | None = None,
                     rows=None) -> list[list[str]]:
    """비어 있는 좌석들이 이루는 '연속 구간'들의 라벨 리스트.

    available을 주면 그 집합에 든 좌석만 비어 있는 것으로 본다(이전 상태를 현재
    배치에 대입해 비교할 때 쓴다). 안 주면 지금 판매 가능한 좌석을 쓴다.
    """
    runs: list[list[str]] = []
    for row_seats in _rows_by_x(seats, rows).values():
        current: list[dict] = []
        for seat in row_seats:
            free = seat["label"] in available if available is not None \
                else seat["available"]
            if not free:
                if current:
                    runs.append([s["label"] for s in current])
                current = []
                continue
            if current and not _adjacent(current[-1], seat):
                runs.append([s["label"] for s in current])
                current = [seat]
            else:
                current.append(seat)
        if current:
            runs.append([s["label"] for s in current])
    return runs


def max_consecutive(seats: list[dict], available: set[str] | None = None,
                    rows=None) -> int:
    """지금 나란히 붙은 빈자리의 최대 개수. 아무도 없으면 0."""
    runs = consecutive_runs(seats, available, rows)
    return max((len(r) for r in runs), default=0)


def consecutive_starts(seats: list[dict], available: set[str], n: int,
                       rows=None) -> set[str]:
    """n석을 나란히 앉을 수 있는 '시작 좌석' 라벨 집합.

    길이 L인 연속 구간은 시작점이 L-n+1개 나온다(A1~A3에서 2연속이면 A1·A2).
    이 집합을 이전/현재로 각각 만들어 차집합을 내면 '새로 생긴 n연속'만 남는다.
    """
    if n < 1:
        return set()
    starts: set[str] = set()
    for run in consecutive_runs(seats, available, rows):
        for i in range(len(run) - n + 1):
            starts.add(run[i])
    return starts


def summarize(seats: list[dict], rows=None) -> dict:
    """열 필터를 적용한 좌석 요약 — {total, available, rows}."""
    wanted = normalize_rows(rows)
    scoped = [s for s in seats
              if not wanted or s["row"].upper() in wanted]
    return {
        "total": len(scoped),
        "available": sum(1 for s in scoped if s["available"]),
        "rows": sorted({s["row"] for s in scoped}),
        "max_consecutive": max_consecutive(seats, rows=rows),
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


def run_range(run: list[str]) -> str:
    """연속 구간을 'A3–A5'처럼 시작~끝으로 표기한다."""
    ordered = sort_labels(run)
    return ordered[0] if len(ordered) == 1 else f"{ordered[0]}–{ordered[-1]}"


def build_consecutive_alert(mov_nm: str, site_nm: str, ymd: str, start_hhmm: str,
                            screen_label: str, new_runs: list[list[str]], n: int,
                            rows=None) -> str:
    """새로 생긴 'n석 연속' 빈자리 알림 문구.

    new_runs는 n석 이상 나란히 붙은 새 구간들이다 — 함께 앉을 자리를 찾는 알림이다.
    """
    wanted = normalize_rows(rows)
    scope = f" ({'·'.join(wanted)}열)" if wanted else ""
    head = (f"👥 *{n}석 연속 빈자리*{scope}\n"
            f"*{mov_nm}* · CGV {site_nm}\n"
            f"{fmt_date(ymd)} {fmt_time(start_hhmm)} · {screen_label}")
    lines = "\n".join(f"➕ {run_range(r)} ({len(r)}연속)"
                      for r in sorted(new_runs, key=lambda r: sort_labels(r)[0]))
    return f"{head}\n{lines}"


def new_consecutive_runs(seats: list[dict], prev_available: set[str],
                         current: set[str], n: int, rows=None) -> list[list[str]]:
    """이전엔 n연속이 안 되던 곳에 이번에 새로 생긴 n석 이상 연속 구간들.

    현재 연속 구간 중, 그 시작 좌석이 '새로 생긴 n연속 시작'을 포함하는 구간만
    골라 돌려준다 — 이미 알린 구간을 다시 알리지 않는다.
    """
    prev_starts = consecutive_starts(seats, prev_available, n, rows)
    cur_starts = consecutive_starts(seats, current, n, rows)
    fresh_starts = cur_starts - prev_starts
    if not fresh_starts:
        return []
    result = []
    for run in consecutive_runs(seats, current, rows):
        if len(run) < n:
            continue
        # 이 구간의 n연속 시작점 중 새로 생긴 게 있으면 알린다.
        run_starts = set(sort_labels(run)[:len(run) - n + 1])
        if run_starts & fresh_starts:
            result.append(run)
    return result


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
    need = int(w.get("min_consecutive") or 0)   # 0·1 = 개별 좌석, 2+ = 연속 좌석
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
        start_hhmm = row.get("scnsrtTm") or ""

        if key not in prev:      # 첫 관측 — 기준선만 잡는다
            continue

        if need >= 2:
            # 나란히 붙은 n석이 새로 생긴 회차만 알린다.
            runs = new_consecutive_runs(seats, set(prev[key]), current, need, rows)
            if runs:
                alerts.append({"body": build_consecutive_alert(
                    mov_nm, site_nm, w["scn_ymd"], start_hhmm,
                    watch.screen_label(row), runs, need, rows)})
        else:
            newly = diff_available(set(prev[key]), current)
            if newly:
                alerts.append({"body": build_seat_alert(
                    mov_nm, site_nm, w["scn_ymd"], start_hhmm,
                    watch.screen_label(row), newly, len(current), rows)})

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
