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

import contextlib
import time

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
            # 판매형태 — 선점 바디에도 실리지만, 휠체어 전용석을 가려내는
            # 유일한 단서이기도 하다(is_restricted).
            "seat_salfrm_cd": s.get("seatSalfrmCd") or "",
        })
    return out


# 좌석의 판매형태(seatSalfrmCd). 04는 **휠체어 전용(장애인)석**이다.
# 좌석맵 응답은 이 자리를 일반석과 똑같이 내려준다 — stkndNm도 "일반석",
# szoneKindNm도 옆자리와 같고, 다른 것은 이 코드 하나뿐이다. 그래 놓고 실제로
# 누르면 "장애인 좌석 예매 제한" 팝업으로 막는다. 매진이 가까운 회차에서는
# 이 자리만 남는 일이 흔해서(실측: 624석 중 판매 가능 6석이 전부 여기였다),
# 걸러 내지 않으면 자동 예매가 매번 그 팝업에 걸려 죽는다.
WHEELCHAIR_SALFRM_CD = "04"


def is_restricted(seat: dict) -> bool:
    """일반 고객이 살 수 없는 좌석인지 — 지금은 휠체어 전용석."""
    return (seat.get("seat_salfrm_cd") or "") == WHEELCHAIR_SALFRM_CD


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


def normalize_seat_nums(num_from, num_to) -> tuple[int, int]:
    """좌석 번호 범위를 (from, to)로. 0은 '제한 없음'이다.

    거꾸로 들어오면 바로잡는다 — 32~13이라고 적어도 13~32로 본다. 사람이 큰
    숫자를 먼저 적는 일은 흔한데, 그대로 두면 아무 좌석도 안 걸려서 감시가
    조용히 멎는다.
    """
    def one(v) -> int:
        n = _to_int(v)
        return n if n and n > 0 else 0

    lo, hi = one(num_from), one(num_to)
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def in_scope(seat: dict, rows=None, num_from=0, num_to=0) -> bool:
    """그 좌석이 감시 범위 안인지 — 열(세로)과 번호(가로) 둘 다 본다.

    열 필터가 세로를 자른다면 번호 범위는 가로를 자른다. 같은 H열이라도 1번은
    화면 왼쪽 끝이고 20번은 한가운데라, IMAX처럼 한 열이 45석까지 가는 관에서는
    열만으로 '좋은 자리'가 가려지지 않는다.

    번호를 못 읽는 좌석은 **범위를 걸었으면 뺀다.** 번호로 고른다고 해 놓고
    번호를 모르는 자리를 끼워 주면 범위 밖 좌석을 잡게 된다.

    휠체어 전용석은 열·번호를 따지기 전에 뺀다. 열 필터를 안 건 감시라도 그
    자리는 우리가 살 수 있는 좌석이 아니다.
    """
    if is_restricted(seat):
        return False
    wanted = normalize_rows(rows)
    if wanted and seat["row"].upper() not in wanted:
        return False
    lo, hi = normalize_seat_nums(num_from, num_to)
    if not lo and not hi:
        return True
    n = _to_int(seat["no"])
    if n is None:
        return False
    return (not lo or n >= lo) and (not hi or n <= hi)


def available_labels(seats: list[dict], rows=None, num_from=0,
                     num_to=0) -> set[str]:
    """감시 범위에서 지금 비어 있는(판매 가능) 좌석 라벨 집합.

    rows가 비면 전 열, 번호 범위가 0이면 그 열의 모든 번호를 본다.
    """
    return {
        s["label"] for s in seats
        if s["available"] and in_scope(s, rows, num_from, num_to)
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


def _rows_by_x(seats: list[dict], rows=None, num_from=0,
               num_to=0) -> dict[str, list[dict]]:
    """열별로 x좌표(없으면 번호) 순으로 정렬한 좌석 리스트. 범위 밖은 뺀다.

    **범위 밖 좌석은 아예 빼고 연속을 센다.** 12번과 13번이 붙어 있어도 범위가
    13번부터면 그 둘은 한 구간이 아니다 — 잡을 수 없는 자리를 구간에 넣으면
    "2석 연속 있음"이라고 알려 놓고 정작 못 잡는다.
    """
    grouped: dict[str, list[dict]] = {}
    for s in seats:
        if not in_scope(s, rows, num_from, num_to):
            continue
        grouped.setdefault(s["row"], []).append(s)
    for row in grouped:
        grouped[row].sort(key=lambda s: (s["x_start"] if s["x_start"] is not None
                                         else (_to_int(s["no"]) or 0)))
    return grouped


def consecutive_runs(seats: list[dict], available: set[str] | None = None,
                     rows=None, num_from=0, num_to=0) -> list[list[str]]:
    """비어 있는 좌석들이 이루는 '연속 구간'들의 라벨 리스트.

    available을 주면 그 집합에 든 좌석만 비어 있는 것으로 본다(이전 상태를 현재
    배치에 대입해 비교할 때 쓴다). 안 주면 지금 판매 가능한 좌석을 쓴다.
    """
    runs: list[list[str]] = []
    for row_seats in _rows_by_x(seats, rows, num_from, num_to).values():
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
                    rows=None, num_from=0, num_to=0) -> int:
    """지금 나란히 붙은 빈자리의 최대 개수. 아무도 없으면 0."""
    runs = consecutive_runs(seats, available, rows, num_from, num_to)
    return max((len(r) for r in runs), default=0)


def consecutive_starts(seats: list[dict], available: set[str], n: int,
                       rows=None, num_from=0, num_to=0) -> set[str]:
    """n석을 나란히 앉을 수 있는 '시작 좌석' 라벨 집합.

    길이 L인 연속 구간은 시작점이 L-n+1개 나온다(A1~A3에서 2연속이면 A1·A2).
    이 집합을 이전/현재로 각각 만들어 차집합을 내면 '새로 생긴 n연속'만 남는다.
    """
    if n < 1:
        return set()
    starts: set[str] = set()
    for run in consecutive_runs(seats, available, rows, num_from, num_to):
        for i in range(len(run) - n + 1):
            starts.add(run[i])
    return starts


def pick_block(seats: list[dict], party: int, rows=None, num_from=0,
               num_to=0) -> list[dict]:
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
    runs = [r for r in consecutive_runs(seats, rows=rows, num_from=num_from,
                                        num_to=num_to) if len(r) >= party]
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


def summarize(seats: list[dict], rows=None, num_from=0, num_to=0) -> dict:
    """감시 범위(열·번호)를 적용한 좌석 요약 — {total, available, rows}."""
    scoped = [s for s in seats if in_scope(s, rows, num_from, num_to)]
    return {
        "total": len(scoped),
        "available": sum(1 for s in scoped if s["available"]),
        "rows": sorted({s["row"] for s in scoped}),
        "max_consecutive": max_consecutive(seats, rows=rows, num_from=num_from,
                                           num_to=num_to),
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
                         current: set[str], n: int, rows=None,
                         num_from=0, num_to=0) -> list[list[str]]:
    """이전엔 n연속이 안 되던 곳에 이번에 새로 생긴 n석 이상 연속 구간들.

    현재 연속 구간 중, 그 시작 좌석이 '새로 생긴 n연속 시작'을 포함하는 구간만
    골라 돌려준다 — 이미 알린 구간을 다시 알리지 않는다.
    """
    prev_starts = consecutive_starts(seats, prev_available, n, rows,
                                     num_from, num_to)
    cur_starts = consecutive_starts(seats, current, n, rows, num_from, num_to)
    fresh_starts = cur_starts - prev_starts
    if not fresh_starts:
        return []
    result = []
    for run in consecutive_runs(seats, current, rows, num_from, num_to):
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


# parse_seats가 실제로 읽는 필드. 좌석맵을 묶어 받을 때 브라우저에서 이것만
# 남겨 보낸다 — 원본은 좌석당 39개라 안 깎으면 32건이 17.6MB로 넘어온다.
# **이름을 그대로 두므로 parse_seats 출력은 바뀌지 않는다.**
SEAT_FIELDS = [
    "seatRowNm", "seatNo", "seatSaleYn", "stkndNm", "szoneExpoNm", "szoneNm",
    "xcoordStartVal", "xcoordEndVal", "leftPwayYn", "rghtPwayYn",
    "seatLocNo", "sbordNo", "seatAreaNo", "szoneNo", "stkndCd",
    "szoneKindCd", "seatSalfrmCd",
]


# 상영일이 얼마나 남았느냐로 정하는 **우선순위**. 작을수록 급하다.
#
# 예전에는 이 값으로 "몇 바퀴에 한 번 볼지"를 정해 먼 날짜를 아예 건너뛰었다.
# 지금은 건너뛰지 않고 **순서만** 정한다 — 예산이 남으면 먼 날짜도 그 바퀴에
# 처리되고, 모자라면 큐에 남아 다음 창에서 처리된다. 버리는 게 아니라 미룬다.
SEAT_PRIORITY_BANDS = ((1, 0), (3, 1))   # 1일 이내 0순위 · 3일 이내 1순위
SEAT_PRIORITY_FAR = 2

# 순위별 **재확인 간격(초)**. 이 시간이 지나기 전에는 큐에 다시 올리지 않는다.
#
# 우선순위만 두면 가까운 날짜가 매 창 예산을 채워 먼 날짜가 영원히 밀린다
# (테스트가 이 기아를 잡았다). 간격을 두면 급한 것은 매 바퀴 보면서도 먼 것이
# 반드시 차례를 받는다. 기본 부하도 이 값으로 정해진다.
SEAT_RECHECK_SECONDS = (0.0, 20.0, 90.0)

# 회차별 마지막 조회 시각 {키: monotonic}. 재확인 간격을 재는 데만 쓴다.
_last_fetched: dict = {}

# 회차별 마지막으로 본 잔여 좌석 수 {키: int}.
#
# **상영표가 이미 답을 들고 있다.** searchSchByMov 응답의 frSeatCnt가 그 회차의
# 빈자리 수고, 좌석맵을 열어 센 것과 정확히 일치한다(2026-08-31 실측: 6/5/6석
# 세 회차 모두 일치). 상영표 1건이 그 날짜의 **모든 회차**를 덮으므로, 이 값이
# 그대로면 좌석맵을 열 이유가 없다.
#
# 예전에는 회차마다 좌석맵을 받았다 — 사이클당 35건. 이제 상영표 6건으로
# 판단하고, 숫자가 움직인 회차만 연다.
_last_count: dict = {}

# 숫자가 그대로여도 이만큼 지나면 한 번은 연다.
#
# **왜 필요한가.** frSeatCnt는 총합이라, 우리가 보는 열에서 한 자리가 풀리고
# 동시에 다른 곳에서 한 자리가 팔리면 숫자가 그대로다 — 그 사이에 난 자리를
# 놓친다. 3초 간격에서 그런 동시 교차는 드물지만 0은 아니므로, 주기적으로
# 실제 좌석맵을 확인해 놓칠 수 있는 시간을 이 값으로 묶어 둔다.
#
# **120초는 취소표보다 느렸다.** 실측(2026-09-01, 용산 IMAX 9/4 21:30)으로 취소표
# 한 석이 떠 있던 시간은 33·45·37·4초였다. 놓칠 수 있는 시간의 상한이 그보다
# 길면 상한이라는 말에 뜻이 없다 — 그 안에 자리가 나고 팔리는 일이 끝난다.
FULL_REFRESH_SECONDS = 30.0

# 큐에서 이만큼 기다릴 때마다 순위가 한 칸 올라간다.
#
# **순위만으로는 굶는다.** 급한 회차가 매 창 예산을 다 채우면 먼 회차는 영영
# 차례가 오지 않는다(테스트가 이걸 잡았다). 오래 기다린 일감을 끌어올려
# 반드시 처리되게 한다.
AGE_PROMOTE_SECONDS = 45.0

# 아직 못 받은 좌석맵. 소유자별 {키: (우선순위, 경로)}이고, 예산이 허락하는
# 만큼만 앞에서 꺼내 쓴다. 창이 지나면 예산이 되살아나 나머지가 처리된다.
_pending: dict = {}
# 큐가 무한정 자라지 않게. 넘치면 가장 안 급한 것부터 버린다 — 그 회차는
# 다음 바퀴에 어차피 다시 올라온다.
PENDING_LIMIT = 400


def seat_priority(scn_ymd: str, today=None) -> int:
    """이 상영일이 얼마나 급한지. 작을수록 먼저 처리한다.

    날짜를 못 읽으면 가장 급한 것으로 본다 — 모르는 것을 뒤로 미루면 놓친다.
    """
    from datetime import date as _date

    digits = "".join(ch for ch in (scn_ymd or "") if ch.isdigit())
    if len(digits) != 8:
        return 0
    try:
        target = _date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return 0
    days = (target - (today or _date.today())).days
    for limit, rank in SEAT_PRIORITY_BANDS:
        if days <= limit:
            return rank
    return SEAT_PRIORITY_FAR


def rows_to_check(w: dict, schedule: list[dict]) -> list[dict]:
    """이 감시가 실제로 좌석을 볼 회차들.

    **프리페치와 본 루프가 이 함수를 함께 쓴다.** 각자 고르면 어긋날 수 있고,
    어긋나면 미리 받아 둔 것이 조용히 무용지물이 된다 — 결과는 맞으니 알아채기
    어렵다. 고르는 규칙은 한 곳에만 둔다.
    """
    import watch

    import store

    wanted = store.normalize_screen_types(w["screen_types"])
    return [row for row in schedule
            if not wanted or watch.matches_screen_types(row, wanted)]


def seat_map_key(w: dict, row: dict, site_no: str) -> tuple:
    """좌석맵 하나를 가리키는 키. 프리페치 결과를 찾을 때 쓴다."""
    return (row.get("siteNo") or site_no, row["scnsNo"], w["scn_ymd"],
            row["scnSseq"])


def _seat_map(session, guard, **kwargs) -> dict:
    """좌석 배치도를 읽는다. 401이면 세션을 되살리고 한 번만 다시 시도한다."""
    import watch

    try:
        return session.seat_map(**kwargs)
    except watch.AuthRequired:
        if guard is None or not guard.recover(session):
            raise
        return session.seat_map(**kwargs)


class _CycleCost:
    """좌석 감시 한 바퀴에서 CGV를 몇 번 부르고 얼마나 걸렸는지 센다.

    poll_cycles에는 check_all만 기록되는데(실측 0.009초), 정작 시간을 쓰는 건
    이 좌석 사이클이다. 그래서 "3초로 맞췄는데 왜 6초마다 도는지"를 로그만으로는
    가릴 수 없었다 — 사이클이 폴링 간격보다 길면 다음 슬롯을 통째로 놓친다.

    호출 종류별로 재는 이유는 어디를 줄일지가 거기서 갈리기 때문이다. 같은
    (극장·영화·날짜)를 여러 감시가 함께 보면 showtimes가 중복되고, 카탈로그는
    감시와 무관하게 매 바퀴 두 번 나간다.
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._counts: dict[str, int] = {}
        self._spent: dict[str, float] = {}

    @contextlib.contextmanager
    def call(self, kind: str):
        """이 블록에서 CGV를 한 번 부른다고 세고 걸린 시간을 더한다."""
        t = time.monotonic()
        try:
            yield
        finally:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._spent[kind] = self._spent.get(kind, 0.0) + (time.monotonic() - t)

    def hit(self, kind: str) -> None:
        """부르지 않고 캐시로 넘어간 횟수. 줄인 게 실제로 줄었는지 본다."""
        self._counts[kind] = self._counts.get(kind, 0) + 1

    def summary(self) -> str:
        total = time.monotonic() - self._t0
        if not self._counts:
            return f"합계 {total:.1f}s"
        parts = " · ".join(
            f"{k} {self._counts[k]}회"
            + (f" {self._spent[k]:.1f}s" if k in self._spent else "")
            for k in sorted(self._counts))
        return f"합계 {total:.1f}s ({parts})"


def _seat_count(row: dict) -> int | None:
    """상영표 한 회차의 잔여 좌석 수. 못 읽으면 None.

    frSeatCnt는 좌석맵을 열어 센 것과 일치한다(실측). frtmpSeatCnt는 임시
    선점분까지 더한 값이라 다르다 — 이쪽을 쓰면 안 된다.
    """
    try:
        return int(row["frSeatCnt"])
    except (KeyError, TypeError, ValueError):
        return None


def _schedule(session, key, *, cost=None, sched_cache=None):
    """상영표를 받는다. 같은 바퀴에 이미 받았으면 그걸 쓴다. 실패하면 None.

    상영표에는 회차별 잔여 좌석 수가 들어 있어(frSeatCnt) 좌석맵을 열지 말지를
    이걸로 판단한다 — **이 호출의 빈도가 곧 감지 주기다.** 그래서 상영일이
    멀어도 매 바퀴 받는다.

    한때는 좌석맵과 같은 우선순위 간격(0·20·90초)을 여기에도 걸었다. 그러자
    3일 뒤 상영은 20초마다, 4일 뒤는 90초마다만 잔여 좌석 수를 보게 됐는데,
    실측한 취소표 수명이 4~45초였다(2026-09-01, 용산 IMAX 9/4 21:30). 자리가
    나고 팔리는 일이 두 샘플 사이에서 끝나면 숫자가 그대로라 좌석맵을 열지도
    않는다 — 90초 간격은 취소표를 구조적으로 못 본다.

    **간격을 걸 이유도 없었다.** 좌석맵은 회차 수만큼(회차당 214KB) 나가지만
    상영표는 **날짜 수**만큼이다. 상영표 1건이 그 날짜의 모든 회차를 덮으므로,
    3초마다 날짜 3개를 받아도 분당 60건이다.
    """
    if sched_cache is not None and key in sched_cache:
        return sched_cache[key]     # 같은 바퀴 안에서는 한 번만

    try:
        with cost.call("상영표") if cost else contextlib.nullcontext():
            rows = session.showtimes(key[0], key[1], key[2])
    except RuntimeError:
        return None
    if sched_cache is not None:
        sched_cache[key] = rows
    return rows


def _prefetch_seat_maps(session, catalog, group, *, cost=None,
                        sched_cache=None, unchanged: set | None = None) -> dict:
    """이 소유자가 볼 좌석맵을 큐에 쌓고, **예산만큼만** 받아 온다.

    예전에는 볼 것을 매 바퀴 전부 받으려 했다. 사이클을 11.8초에서 2.2초로
    줄이자 폴링 3초와 겹쳐 요청이 초당 3.4건에서 13.7건이 됐고 CGV가 429로
    거절했다. 물러나기만 해선 멈추지 않았다 — 쉰 뒤에 또 전부 보냈으니까.

    그래서 **보낼 양을 먼저 정한다.** 이번 창에 남은 예산만큼 큐 앞에서
    꺼내 처리하고, 나머지는 큐에 그대로 둔다. 창이 지나면 예산이 되살아나
    이어서 처리된다 — 버리는 게 아니라 미루는 것이다.

    큐 순서는 상영일이 가까운 것부터다(seat_priority). 예산이 빠듯하면 급한
    것부터 쓰이고, 남으면 먼 날짜도 같은 바퀴에 처리된다.
    """
    import watch

    owner = group[0]["owner_id"] if group else None
    queue: dict = _pending.setdefault(owner, {})
    if unchanged is None:
        unchanged = set()

    # 1) 이번 바퀴에 볼 것을 큐에 올린다. 이미 있으면 그대로 둔다 — 먼저 들어온
    #    것이 더 오래 기다렸다는 뜻이라 순서를 흔들 이유가 없다.
    for w in group:
        movie, _ = catalog.resolve_movie(w["movie_query"])
        site, _ = catalog.resolve_site(w["site_query"])
        if movie is None or site is None:
            continue
        site_no, mov_no = site["siteNo"], movie["movNo"]
        sched_key = (site_no, mov_no, w["scn_ymd"])
        rank = seat_priority(w["scn_ymd"])
        schedule = _schedule(session, sched_key, cost=cost,
                             sched_cache=sched_cache)
        if schedule is None:
            continue            # 본 루프가 같은 실패를 만나 사유를 기록한다

        schedule = select_showtimes(
            schedule, scn_time=w.get("scn_time") or "",
            scn_time_from=w.get("scn_time_from") or "",
            scn_time_to=w.get("scn_time_to") or "")
        gap = SEAT_RECHECK_SECONDS[min(rank, len(SEAT_RECHECK_SECONDS) - 1)]
        for row in rows_to_check(w, schedule):
            key = seat_map_key(w, row, site_no)
            if key in queue:
                continue
            # **상영표가 이미 잔여 좌석 수를 알려 줬다.** 그 값이 그대로면 이
            # 회차의 좌석 배치는 바뀌지 않았으므로 좌석맵을 열 이유가 없다.
            count = _seat_count(row)
            moved = False
            if count is not None:
                before = _last_count.get(key)
                _last_count[key] = count
                fetched = _last_fetched.get(key)
                stale = (fetched is None
                         or time.monotonic() - fetched >= FULL_REFRESH_SECONDS)
                if before == count and not stale:
                    unchanged.add(key)
                    continue
                moved = before is not None and before != count
            # 방금 본 회차는 다시 올리지 않는다. 이게 없으면 가까운 날짜가 매 창
            # 예산을 채워 먼 날짜가 영영 밀린다.
            #
            # **숫자가 움직였으면 간격을 무시한다.** 간격은 알아낼 게 없는 재확인을
            # 막으려고 있는 것인데, 잔여 좌석 수가 변한 회차는 알아낼 게 있다는 뜻
            # 그 자체다. 여기서 걸러 내면 _last_count는 이미 새 값으로 갱신된 뒤라
            # 다음 바퀴엔 '변화 없음'이 되어 **그 취소표를 영영 못 본다.**
            seen = _last_fetched.get(key)
            if not moved and seen is not None and time.monotonic() - seen < gap:
                continue
            queue[key] = (rank, time.monotonic(), watch.EP_SEAT.format(
                site_no=key[0], scns_no=key[1], ymd=key[2], scn_sseq=key[3]))

    if not queue:
        return {}
    now = time.monotonic()

    # 2) 큐가 무한정 자라지 않게. 넘치면 가장 안 급한 것부터 버린다 — 그 회차는
    #    다음 바퀴에 어차피 다시 올라온다.
    if len(queue) > PENDING_LIMIT:
        for key in sorted(queue, key=lambda k: -queue[k][0])[
                :len(queue) - PENDING_LIMIT]:
            queue.pop(key, None)

    def order(key):
        rank, since, _ = queue[key]
        # 기다린 만큼 순위를 끌어올린다. 같은 순위면 오래 기다린 것부터.
        aged = rank - int((now - since) / AGE_PROMOTE_SECONDS)
        return (aged, since)

    # 3) 예산만큼만 꺼낸다. 급한 것부터.
    allowance = session.allowance()
    if allowance <= 0:
        if cost:
            cost.hit(f"예산소진 대기{len(queue)}건")
        return {}
    ordered = sorted(queue, key=order)[:allowance]
    paths = [queue[k][2] for k in ordered]

    with cost.call("좌석맵(묶음)") if cost else contextlib.nullcontext():
        results = session.get_json_many(paths, seat_fields=SEAT_FIELDS)

    out = {}
    for key, payload in zip(ordered, results):
        if payload is None:
            continue            # 못 받았다 — 큐에 남겨 다음 창에 다시 본다
        out[key] = payload.get("data") or {}
        queue.pop(key, None)
        _last_fetched[key] = time.monotonic()
    if queue and cost:
        cost.hit(f"대기{len(queue)}건")
    return out


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

    # CGV가 그만하라고 한 동안은 통째로 쉰다. 여기서 걸러 내지 않으면 회차마다
    # 같은 경고가 찍혀(35건이면 35줄) 정작 무슨 일인지 안 보인다.
    if session.allowance() <= 0:
        # 창의 예산을 다 썼다. 일감은 큐에 그대로 있으니 다음 바퀴에 이어서 한다.
        watch.log.info("요청 예산을 다 써 이번 바퀴는 건너뜁니다 "
                       "(대기 %d건)", sum(len(q) for q in _pending.values()))
        return summary
    session.budget.relax()

    catalog = watch.Catalog(session)
    cost = _CycleCost()
    # 이 바퀴 안에서만 사는 캐시. 같은 (극장·영화·날짜)를 여러 감시가 함께 보면
    # 상영표가 그만큼 중복으로 나가고(실측 8건 중 4건이 중복), 예매 화면 프리워밍도
    # 같은 탭을 몇 번씩 다시 검증한다. 한 바퀴 안에서는 같은 답이므로 한 번만 받는다.
    #
    # **바퀴를 넘겨 두지는 않는다.** 상영표는 회차가 새로 열리는 걸 봐야 하는
    # 자료라, 오래 들고 있으면 그게 곧 감지 지연이 된다.
    sched_cache: dict[tuple, list] = {}
    warmed: set[str] = set()

    by_owner: dict[int, list[dict]] = {}
    for w in watches:
        if w["owner_id"] is None:
            # 주인 없는 좌석 감시는 로그인할 계정이 없어 확인할 수 없다.
            continue
        by_owner.setdefault(w["owner_id"], []).append(w)

    for owner_id, group in by_owner.items():
        # 이 소유자의 공간으로 옮긴다. 컨텍스트가 갈려 있어 로그인도 예매 탭도
        # 각자 유지된다 — 예전처럼 쿠키를 비우고 다시 로그인하지 않는다.
        session.use(owner_id)
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
        # 좌석맵을 미리 묶어 받는다. 실패해도 본 루프가 개별로 받으므로
        # 여기서 예외를 밖으로 내지 않는다.
        unchanged: set = set()
        try:
            prefetched = _prefetch_seat_maps(session, catalog, group, cost=cost,
                                             sched_cache=sched_cache,
                                             unchanged=unchanged)
        except Exception as exc:  # noqa: BLE001 - 미리 받기는 부가 기능이다
            watch.log.warning("좌석맵을 미리 받지 못했습니다 (%s) — "
                              "하나씩 받습니다", exc)
            prefetched = {}
        for w in group:
            summary["watches_checked"] += 1
            sent = _check_one_seat_watch(
                session, catalog, w, webhook, webhook_kind, guard=guard,
                dry_run=dry_run, cost=cost, sched_cache=sched_cache,
                warmed=warmed, prefetched=prefetched, unchanged=unchanged)
            summary["alerts_sent"] += sent

    # 사이클이 폴링 간격을 넘으면 다음 슬롯을 통째로 놓친다 — 그 사실이 로그에
    # 남아야 간격을 줄인 게 실제로 먹었는지 알 수 있다.
    watch.log.info("좌석 감시 소요 — %s", cost.summary())
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
                          dry_run: bool, cost: "_CycleCost | None" = None,
                          sched_cache: dict | None = None,
                          warmed: set | None = None,
                          prefetched: dict | None = None,
                          unchanged: set | None = None) -> int:
    """좌석 감시 하나를 확인하고 보낸 알림 수를 돌려준다.

    cost·sched_cache·warmed는 한 바퀴를 함께 도는 감시들이 나눠 쓴다. 안 넘겨도
    동작은 같다 — 세지 않고 캐시 없이 돌 뿐이라 CLI·테스트에서 그냥 부를 수 있다.
    """
    import store
    import watch

    # DB 캐시를 먼저 본다 — 못 찾을 때만 목록을 새로 받으므로, 이미 열린 영화를
    # 보는 감시는 매 바퀴 나가던 두 번의 조회가 사라진다. 아직 안 열린 영화는
    # 예전처럼 매번 실물을 확인한다(목록에 뜨는 순간이 곧 오픈이다).
    movie, movie_problem = catalog.resolve_movie(w["movie_query"])
    site, site_problem = catalog.resolve_site(w["site_query"])
    if movie is None or site is None:
        problem = movie_problem if movie is None else site_problem
        if not dry_run:
            store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                  error=_resolve_error(w, movie, problem))
        return 0

    mov_no, site_no = movie["movNo"], site["siteNo"]
    mov_nm, site_nm = movie["movNm"], site["siteNm"]

    sched_key = (site_no, mov_no, w["scn_ymd"])
    try:
        if sched_cache is not None and sched_key in sched_cache:
            schedule = sched_cache[sched_key]
            if cost:
                cost.hit("상영표(캐시)")
        else:
            with cost.call("상영표") if cost else contextlib.nullcontext():
                schedule = session.showtimes(site_no, mov_no, w["scn_ymd"])
            if sched_cache is not None:
                sched_cache[sched_key] = schedule
    except RuntimeError as exc:
        if not dry_run:
            store.save_seat_state(w["id"], store.prev_seat_state(w["id"]),
                                  error=f"시간표 조회 실패: {exc}")
        return 0

    # 볼 회차를 고른다. 시간대로 걸었다면 늦은 회차부터 나오고, 그 순서가 곧
    # 자동 선점의 우선순위가 된다(첫 성공에서 멈추므로).
    schedule = select_showtimes(
        schedule, scn_time=w.get("scn_time") or "",
        scn_time_from=w.get("scn_time_from") or "",
        scn_time_to=w.get("scn_time_to") or "")
    rows = w["rows"]
    # 좌석 번호 범위(가로). 열 필터와 함께 '어디를 볼지'를 정한다 — 화면의
    # '선호좌석' 버튼이 H~O열 · 13~32번을 한 번에 채운다.
    num_from, num_to = normalize_seat_nums(w.get("seat_num_from"),
                                           w.get("seat_num_to"))
    need = int(w.get("min_consecutive") or 0)   # 0·1 = 개별 좌석, 2+ = 연속 좌석

    # 자동 예매를 켠 감시라면 예매 화면을 **미리 띄워 둔다.** 좌석이 난 순간
    # 화면을 새로 여는 데만 6.2초가 드는데(회차 목록이 그려질 때까지), 그 6.2초가
    # 곧 "그 사이 팔린 것 같습니다"가 된다. 화면은 감시 조합마다 탭 하나로
    # 유지되므로 이미 떠 있으면 아무 일도 하지 않는다.
    #
    # 좌석 확인은 이 탭이 아니라 기본 페이지에서 fetch로 하니(session.get_json),
    # 띄워 둬도 감시 자체에는 영향이 없다.
    if w.get("auto_book") and not dry_run:
        import booking
        warm_ctx = {"mov_no": mov_no, "site_no": site_no,
                    "site_nm": site_nm, "scn_ymd": w["scn_ymd"]}
        # 탭은 (영화·극장·날짜)로 갈리므로 같은 날짜를 보는 감시들은 한 탭을
        # 함께 쓴다. 이미 이 바퀴에 확인했으면 다시 볼 것이 없다 — 확인 자체가
        # 날짜 버튼을 하나씩 훑는 일이라 공짜가 아니다.
        wkey = booking.warm_key(warm_ctx)
        if warmed is not None and wkey in warmed:
            if cost:
                cost.hit("화면준비(캐시)")
        else:
            with cost.call("화면준비") if cost else contextlib.nullcontext():
                booking.prewarm(session, warm_ctx)
            if warmed is not None:
                warmed.add(wkey)

    prev = store.prev_seat_state(w["id"])
    fresh_state: dict[str, list[str]] = {}
    alerts: list[dict] = []
    # 실제로 좌석을 본 회차와 실패한 회차를 따로 센다. fresh_state만으로는 구분이
    # 안 된다 — 실패한 회차도 직전 상태를 복사해 넣기 때문에, 전부 실패해도
    # fresh_state가 비어 있지 않아 "정상 확인"처럼 보인다.
    checked = 0
    failures: list[str] = []

    for row in rows_to_check(w, schedule):
        key = showtime_key(row)
        # 상영표의 잔여 좌석 수가 그대로였다 — 좌석 배치가 안 바뀌었으므로
        # 직전 상태를 그대로 쓴다. **확인한 것이지 건너뛴 게 아니다**(checked).
        if unchanged and seat_map_key(w, row, site_no) in unchanged:
            if key in prev:
                fresh_state[key] = prev[key]
            checked += 1
            if cost:
                cost.hit("좌석맵(변화없음)")
            continue
        try:
            # 사이클 앞에서 묶어 받아 둔 것이 있으면 쓴다. 없으면(그 항목만
            # 실패했거나 프리페치가 통째로 안 됐으면) 지금처럼 개별로 받는다 —
            # 401 복구와 회차별 실패 처리가 그 경로에 그대로 있다.
            # pop이 아니라 get이다. 같은 회차를 여러 감시가 보는 일이 흔한데
            # (같은 영화·극장·날짜에 열만 다르게 걸어 둔 경우), 한 번 쓰고
            # 지우면 나머지 감시가 전부 개별로 다시 받는다 — 실측으로 묶음
            # 3건에 개별 9건이 나갔다. 한 사이클 안에서는 같은 순간의 같은
            # 데이터이므로 나눠 쓰는 게 맞다.
            hit = None
            if prefetched is not None:
                hit = prefetched.get(seat_map_key(w, row, site_no))
            if hit is not None:
                if cost:
                    cost.hit("좌석맵(묶음)")
                seat_data = hit
            else:
                with cost.call("좌석맵") if cost else contextlib.nullcontext():
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
        current = available_labels(seats, rows, num_from, num_to)
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
            runs = new_consecutive_runs(seats, set(prev[key]), current, need,
                                        rows, num_from, num_to)
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
                site_no=site_no,
                # 예매 화면을 딥링크로 바로 열 때 쓴다 (booking.booking_url).
                mov_no=mov_no,
                # 좌석맵에서 다시 고를 때도 같은 범위를 써야 한다 — 여기서
                # 빠지면 후보는 범위 안인데 실제로 누르는 좌석은 범위 밖이 된다.
                num_from=num_from, num_to=num_to)
            act = outcome.get("action")
            if act == "held":
                alerts.append({"kind": "book_held", "body": booking.build_hold_alert(
                    mov_nm, site_nm, w["scn_ymd"], start_hhmm, outcome["seats"],
                    outcome.get("hold_expires_at"), outcome.get("amount"),
                    # 자동 결제를 켠 감시면 카카오페이 결제 링크가 함께 온다.
                    pay_url=outcome.get("pay_url"),
                    pay_expires_at=outcome.get("pay_expires_at"),
                    pay_error=outcome.get("pay_error"))})
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
