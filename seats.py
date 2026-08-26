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
            # 예매(선점)에 필요한 좌석 식별자 — seatTempPrmp 바디가 이 값들을 쓴다.
            "seat_loc_no": s.get("seatLocNo") or "",
            "sbord_no": s.get("sbordNo") or "",
            "seat_area_no": s.get("seatAreaNo") or "",
            "szone_no": s.get("szoneNo") or "",
            "stknd_cd": s.get("stkndCd") or "",
            "szone_kind_cd": s.get("szoneKindCd") or "",
            "seat_salfrm_cd": s.get("seatSalfrmCd") or "",
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


def pick_block(seats: list[dict], party: int, rows=None) -> list[dict]:
    """인원수(party)만큼 나란히 붙은 '가장 좋은' 좌석 블록을 고른다.

    자동 예매가 이 함수로 잡을 좌석을 정한다. 규칙:
      1. 지금 비어 있고 서로 인접한 구간(consecutive_runs) 중 길이 ≥ party인 것만 후보.
      2. 후보 중 **뒤쪽 열**을 선호한다(열 이름이 뒤일수록 스크린에서 멀다 — 통상 선호).
      3. 같은 열이면 구간이 짧을수록(딱 맞을수록) 선호해 큰 연속 블록을 쪼개지 않는다.
      4. 고른 구간 안에서 **가운데**로 party석을 잘라낸다.
    좌석이 부족하면 빈 리스트. party ≤ 1이면 가장 좋은 한 자리를 고른다.

    반환은 seats의 원소(dict) 리스트라 seat_loc_no 등 예매 식별자를 그대로 쓴다.
    """
    party = max(1, int(party or 1))
    by_label = {s["label"]: s for s in seats}
    runs = [r for r in consecutive_runs(seats, rows=rows) if len(r) >= party]
    if not runs:
        return []

    def row_rank(label: str) -> str:
        # 라벨 앞의 열 문자. 뒤쪽 열(사전순 뒤)일수록 선호하므로 그대로 비교값.
        i = 0
        while i < len(label) and not label[i].isdigit():
            i += 1
        return label[:i]

    # 뒤쪽 열 우선(내림차순), 그다음 party에 가까운(=덜 낭비하는) 구간 우선.
    best = sorted(runs, key=lambda r: (row_rank(sort_labels(r)[0]), -len(r)),
                  reverse=True)[0]
    ordered = sort_labels(best)
    start = (len(ordered) - party) // 2          # 구간 가운데에서 party석
    chosen = ordered[start:start + party]
    return [by_label[l] for l in chosen]


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


# ── 회차 고르기 ─────────────────────────────────────────────────────────────
# 하루 상영표는 자정을 넘긴 회차를 24시 이상으로 적는다('2530' = 새벽 1:30).
# 그 표기를 분으로 그대로 펴 두면 "23:00보다 늦은 회차"가 자연스럽게 맞는다.
DAY_MINUTES = 24 * 60


def showtime_minutes(row: dict) -> int | None:
    """회차의 시작 시각을 자정부터의 분으로. 읽을 수 없으면 None."""
    import store

    return store.hhmm_minutes(row.get("scnsrtTm"))


def time_range_minutes(start: str, end: str) -> tuple[int, int] | None:
    """('22:00','02:00') → (1320, 1560). 범위가 아니면 None.

    끝이 시작보다 이르면 자정을 넘긴 것으로 보고 24시간을 더한다 — CGV가 심야
    회차를 26:00으로 적는 것과 같은 뜻이 되어, 그대로 비교하면 맞아떨어진다.
    """
    import store

    a, b = store.hhmm_minutes(start), store.hhmm_minutes(end)
    if a is None or b is None:
        return None
    return (a, b if b >= a else b + DAY_MINUTES)


def _late_first(rows: list[dict]) -> list[dict]:
    """늦은 회차부터. 시각을 못 읽는 회차는 순서를 지킨 채 맨 뒤로 보낸다."""
    keyed = [(showtime_minutes(r), i, r) for i, r in enumerate(rows)]
    unreadable = [r for at, _, r in keyed if at is None]
    readable = sorted((k for k in keyed if k[0] is not None),
                      key=lambda k: (k[0], -k[1]), reverse=True)
    return [r for _, _, r in readable] + unreadable


def select_showtimes(schedule: list[dict], *, scn_time: str = "",
                     scn_time_from: str = "", scn_time_to: str = "") -> list[dict]:
    """감시가 볼 회차만 골라 **늦은 시각부터** 돌려준다.

    고르는 방법은 셋이다:
      · scn_time      — 그 회차 하나 (상영표가 이미 열렸을 때 화면에서 고른 값)
      · from~to       — 그 시간대의 모든 회차 (미상영 영화를 미리 걸어 둔 경우)
      · 아무것도 없음 — 그 날짜의 모든 회차

    순서가 곧 자동 선점의 우선순위다. 시간대로 걸었다면 그 안에서 **가장 늦은
    회차**를 먼저 잡는다 — 시간대를 적는 사람은 "이 중 아무거나"가 아니라 보통
    "되도록 늦게"를 뜻하기 때문이다. 시각을 못 읽는 회차는 맨 뒤로 보낸다.
    """
    want = "".join(ch for ch in (scn_time or "") if ch.isdigit())
    if want:
        # 시각을 콕 집어도 같은 시각이 여러 상영관에 있을 수 있다. 순서가 곧
        # 선점 우선순위이므로 범위 지정과 같은 규칙(늦은 회차 우선)을 쓴다.
        return _late_first([r for r in schedule
                            if (r.get("scnsrtTm") or "").startswith(want)])

    span = time_range_minutes(scn_time_from, scn_time_to)
    if span is None:
        return list(schedule)

    low, high = span
    picked: list[tuple[int, dict]] = []
    for row in schedule:
        at = showtime_minutes(row)
        if at is None:
            continue
        # 22:00~26:00 범위는 새벽 회차를 '0130'으로 적은 극장도 잡아야 하므로
        # 하루를 더해 한 번 더 본다. **범위에 걸린 그 값을 정렬 키로 쓴다** —
        # 자정을 넘긴 01:30은 22:10보다 늦은 회차다. 적힌 숫자로 줄을 세우면
        # 그게 뒤집혀서, 하필 '늦은 회차 우선'이 가장 이른 회차를 고른다.
        for minutes in (at, at + DAY_MINUTES):
            if low <= minutes <= high:
                picked.append((minutes, row))
                break
    picked.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in picked]


class _AuthGuard:
    """401을 만났을 때 세션을 되살린다 — 소유자당 사이클 1회까지.

    좌석 조회는 회차마다 한 번씩 나가므로, 토큰이 죽었으면 401이 줄줄이 난다.
    그때마다 캡차를 다시 푸는 재로그인을 돌리면 CGV 쪽에도 우리 쪽에도 부담이라
    한 소유자에 대해 사이클당 한 번만 시도하고, 실패하면 이번 바퀴는 포기한다.
    """

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.tried = False
        self.ok = False

    def recover(self, session) -> bool:
        import cgv_login

        if self.tried:
            return self.ok
        self.tried = True
        self.ok = cgv_login.recover_session(self.owner_id, session)
        return self.ok


def _seat_map(session, guard, **kwargs) -> dict:
    """좌석 배치도를 읽는다. 401이면 세션을 되살리고 한 번만 다시 시도한다."""
    import watch

    try:
        return session.seat_map(**kwargs)
    except watch.AuthRequired:
        if guard is None or not guard.recover(session):
            raise
        return session.seat_map(**kwargs)


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

        # 이 소유자의 감시들이 공유한다 — 401 복구는 사이클당 한 번이면 충분하다.
        guard = _AuthGuard(owner_id)
        for w in group:
            summary["watches_checked"] += 1
            sent = _check_one_seat_watch(
                session, catalog, w, webhook, webhook_kind, guard=guard,
                dry_run=dry_run)
            summary["alerts_sent"] += sent

    return summary


def _resolve_error(w: dict, movie: dict | None, problem: str) -> str:
    """영화·극장을 못 찾았을 때 화면에 적을 말.

    영화 목록에는 **예매가 열린 영화만** 들어 있다(watch.Catalog). 그래서 미상영
    영화를 미리 걸어 두면 오픈 전까지는 반드시 "일치하는 항목이 없습니다"가 뜬다 —
    그게 정상 대기 상태인데 '확인 실패'라고만 적으면 잘못 건 줄 알고 지우게 된다.
    """
    if movie is None and problem == "일치하는 항목이 없습니다":
        return (f"'{w['movie_query']}': 아직 예매가 열리지 않았습니다"
                f" (또는 영화 이름이 다릅니다) — 열리면 자동으로 확인합니다")
    return f"영화·극장 확인 실패: {problem}"


def _cycle_error(checked: int, failures: list[str]) -> str | None:
    """이번 바퀴를 화면에 뭐라고 적을지. 정상이면 None.

    회차를 하나도 못 봤으면 실패다 — 예전에는 실패한 회차도 직전 상태를 복사해
    넣는 바람에 "전부 실패"가 "정상"으로 보였다. 일부만 실패한 경우도 조용히
    넘기지 않는다: 감시하던 회차가 빠지면 알림이 안 오는 게 정상처럼 보인다.
    """
    if not failures:
        return None if checked else "확인된 회차가 없습니다"
    head = failures[0]
    if not checked:
        more = f" 외 {len(failures) - 1}건" if len(failures) > 1 else ""
        return f"회차 {len(failures)}건을 모두 확인하지 못했습니다 — {head}{more}"
    return f"{checked}개 회차 확인, {len(failures)}개 실패 — {head}"


def _check_one_seat_watch(session, catalog, w, webhook, webhook_kind,
                          *, guard: "_AuthGuard | None" = None,
                          dry_run: bool) -> int:
    """좌석 감시 하나를 확인하고 보낸 알림 수를 돌려준다."""
    import store
    import watch

    movie, movie_problem = watch.resolve(w["movie_query"], catalog.movies, "movNm")
    site, site_problem = watch.resolve(w["site_query"], catalog.sites, "siteNm")
    if movie is None or site is None:
        problem = movie_problem if movie is None else site_problem
        if not dry_run:
            store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                  error=_resolve_error(w, movie, problem))
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
    # 볼 회차를 고른다. 시간대로 걸었다면 늦은 회차부터 나오고, 그 순서가 곧
    # 자동 선점의 우선순위가 된다(첫 성공에서 멈추므로).
    schedule = select_showtimes(
        schedule, scn_time=w.get("scn_time") or "",
        scn_time_from=w.get("scn_time_from") or "",
        scn_time_to=w.get("scn_time_to") or "")
    rows = w["rows"]
    need = int(w.get("min_consecutive") or 0)   # 0·1 = 개별 좌석, 2+ = 연속 좌석
    prev = store.prev_seat_state(w["id"])
    fresh_state: dict[str, list[str]] = {}
    alerts: list[dict] = []
    # 실제로 좌석을 본 회차와 실패한 회차를 따로 센다. fresh_state만으로는 구분이
    # 안 된다 — 실패한 회차도 직전 상태를 복사해 넣기 때문에, 전부 실패해도
    # fresh_state가 비어 있지 않아 "정상 확인"처럼 보인다.
    checked = 0
    failures: list[str] = []

    for row in schedule:
        if wanted_screens and not watch.matches_screen_types(row, wanted_screens):
            continue
        key = showtime_key(row)
        try:
            seat_data = _seat_map(
                session, guard,
                site_no=row.get("siteNo") or site_no,
                scns_no=row["scnsNo"], ymd=w["scn_ymd"],
                scn_sseq=row["scnSseq"])
        except RuntimeError as exc:
            # 이 회차만 실패 — 직전 상태를 유지해 다음에 다시 본다.
            if key in prev:
                fresh_state[key] = prev[key]
            failures.append(f"{row.get('scnsrtTm') or key}: {exc}")
            watch.log.warning("좌석 배치도 조회 실패 (%s %s): %s",
                              site_nm, key, exc)
            continue
        checked += 1

        seats = parse_seats(seat_data)
        current = available_labels(seats, rows)
        fresh_state[key] = sort_labels(current)
        start_hhmm = row.get("scnsrtTm") or ""

        first_sight = key not in prev
        booking_on = bool(w.get("auto_book")) and not dry_run

        # 첫 관측은 기준선만 잡고 넘어간다 — 처음 본 회차의 빈자리를 "새로
        # 생겼다"고 알릴 수는 없다. **자동 예매는 예외다.** 미상영 영화를 미리
        # 걸어 두는 게 시간대 감시의 목적인데, 회차가 처음 열리는 순간을 기준선으로
        # 흘려보내면 다음 사이클까지 기다리게 된다 — 오픈 직후 좌석이 가장 빨리
        # 빠지는 그 한 바퀴를 통째로 놓치는 셈이다.
        if first_sight and not booking_on:
            continue

        if first_sight:
            # "새로 생긴 자리"라는 개념이 없다. 지금 비어 있는 자리를 그대로
            # 후보로 넘기고, 실제로 몇 석을 잡을 수 있는지는 try_auto_book이 본다.
            event = bool(current)
            seat_alert = None
        elif need >= 2:
            # 나란히 붙은 n석이 새로 생긴 회차만 본다.
            runs = new_consecutive_runs(seats, set(prev[key]), current, need, rows)
            event = bool(runs)
            seat_alert = (build_consecutive_alert(
                mov_nm, site_nm, w["scn_ymd"], start_hhmm,
                watch.screen_label(row), runs, need, rows) if runs else None)
        else:
            newly = diff_available(set(prev[key]), current)
            event = bool(newly)
            seat_alert = (build_seat_alert(
                mov_nm, site_nm, w["scn_ymd"], start_hhmm,
                watch.screen_label(row), newly, len(current), rows) if newly else None)

        if not event:
            continue

        # 자동 예매가 켜져 있으면 선점을 시도하고, 그 결과를 알린다(좌석 알림 대체).
        if booking_on:
            import booking
            outcome = booking.try_auto_book(
                session, w, row, seats, mov_nm=mov_nm, site_nm=site_nm,
                # 좌석맵을 다시 읽으려면 회차에 siteNo가 없을 때의 폴백이 필요하다
                # — 위 _seat_map 호출과 같은 값을 쓴다.
                site_no=site_no)
            act = outcome.get("action")
            if act == "held":
                alerts.append({"kind": "book_held", "body": booking.build_hold_alert(
                    mov_nm, site_nm, w["scn_ymd"], start_hhmm, outcome["seats"],
                    outcome.get("hold_expires_at"), outcome.get("amount"))})
                # 선점 성공 시 이 감시는 비활성화됐다 — 남은 회차는 보지 않는다.
                break
            elif act == "failed":
                alerts.append({"kind": "book_failed", "body":
                    f"⚠️ *자동 예매 실패*\n*{mov_nm}* · CGV {site_nm}\n"
                    f"{watch.fmt_date(w['scn_ymd'])} "
                    f"{watch.fmt_time(start_hhmm)} · "
                    f"{', '.join(outcome.get('seats') or [])}\n"
                    f"사유: {outcome.get('error') or '알 수 없음'}\n"
                    f"직접 예매를 진행해 주세요."})
            elif act == "skip":
                pass  # 이미 선점됨/자동예매 꺼짐 — 조용히
            else:  # no_seats 등 — 자리 알림으로 대체
                if seat_alert:
                    alerts.append({"kind": "seat_open", "body": seat_alert})
        elif seat_alert:
            alerts.append({"kind": "seat_open", "body": seat_alert})

    error = _cycle_error(checked, failures)
    if not dry_run:
        # 실패 회차만 있고 새 상태가 하나도 없으면 이전 상태를 지키지 않도록,
        # fresh_state가 비어 있어도 error만 남기고 상태는 유지한다.
        store.save_seat_state(w["id"], fresh_state or prev, error=error)

    sent = 0
    for alert in alerts:
        if watch.deliver_alert(alert.get("kind", "seat_open"), alert["body"],
                               dry_run=dry_run, owner_id=w["owner_id"],
                               webhook_url=webhook, webhook_kind=webhook_kind,
                               mov_nm=mov_nm, site_nm=site_nm,
                               dates=[w["scn_ymd"]], seat_watch_id=w["id"]):
            sent += 1
    return sent
