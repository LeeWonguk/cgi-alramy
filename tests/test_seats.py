#!/usr/bin/env python3
"""좌석 추적 순수 로직 회귀 (DB·네트워크 불필요).

실제 좌석 배치도 응답(tests/fixtures/seatdata.json — 로그인해서 받아 둔 것)을
픽스처로 파싱·필터·비교·문구를 고정한다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seats  # noqa: E402
import seats as seats_mod  # 지역변수 seats에 가려지지 않게 별칭도 둔다  # noqa: E402
import watch  # noqa: E402

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "seatdata.json").read_text(encoding="utf-8")
)
SEAT_DATA = FIXTURE["data"]


class TestParseSeats(unittest.TestCase):
    def test_parses_all_64_seats(self):
        parsed = seats.parse_seats(SEAT_DATA)
        self.assertEqual(len(parsed), 64)
        s = parsed[0]
        self.assertLessEqual(
            {"row", "no", "label", "available", "kind", "zone",
             "x_start", "x_end", "left_pway", "right_pway"},
            set(s))
        self.assertEqual(s["label"], s["row"] + s["no"])

    def test_available_matches_saleyn(self):
        parsed = seats.parse_seats(SEAT_DATA)
        # 픽스처 실측: 판매가능(seatSaleYn=Y) 16석.
        self.assertEqual(sum(1 for s in parsed if s["available"]), 16)

    def test_empty_data_is_empty(self):
        self.assertEqual(seats.parse_seats({}), [])
        self.assertEqual(seats.parse_seats({"items": []}), [])


class TestRowFilter(unittest.TestCase):
    def test_normalize_rows(self):
        self.assertEqual(seats.normalize_rows("a, b ,A"), ["A", "B"])
        self.assertEqual(seats.normalize_rows(["c", "c"]), ["C"])
        self.assertEqual(seats.normalize_rows(None), [])
        self.assertEqual(seats.normalize_rows(""), [])

    def test_available_labels_all_rows(self):
        parsed = seats.parse_seats(SEAT_DATA)
        labels = seats.available_labels(parsed)
        self.assertEqual(len(labels), 16)
        self.assertIn("A6", labels)      # 실측 가능 좌석
        self.assertIn("G1", labels)

    def test_available_labels_scoped_to_rows(self):
        parsed = seats.parse_seats(SEAT_DATA)
        only_a = seats.available_labels(parsed, ["A"])
        self.assertTrue(only_a)
        self.assertTrue(all(l.startswith("A") for l in only_a))
        # A열 필터는 전체보다 좁거나 같다.
        self.assertLessEqual(only_a, seats.available_labels(parsed))

    def test_summarize_scoped(self):
        parsed = seats.parse_seats(SEAT_DATA)
        summ = seats.summarize(parsed, ["A"])
        self.assertEqual(summ["rows"], ["A"])
        self.assertEqual(summ["total"], 8)   # A열 8석


class TestSeatNumberRange(unittest.TestCase):
    """좌석 번호 범위 — 열 필터가 세로를 자른다면 이건 가로를 자른다.

    IMAX처럼 한 열이 45석까지 가는 관에서는 열만 걸어서는 화면 끝자리가 그대로
    후보에 남는다. 화면의 '선호좌석' 버튼이 H~O열 · 13~32번을 채운다.
    """

    def row(self, label, x, available=True):
        """H13처럼 라벨로 좌석 하나. x는 왼쪽부터의 좌표(인접 판정에 쓰인다)."""
        i = 0
        while i < len(label) and not label[i].isdigit():
            i += 1
        return _seat(label[:i], label[i:], x, x + 2, available)

    def test_normalize_accepts_only_positive_numbers(self):
        self.assertEqual(seats.normalize_seat_nums(13, 32), (13, 32))
        self.assertEqual(seats.normalize_seat_nums(0, 0), (0, 0))
        self.assertEqual(seats.normalize_seat_nums(None, None), (0, 0))
        self.assertEqual(seats.normalize_seat_nums("13", "32"), (13, 32))
        self.assertEqual(seats.normalize_seat_nums(-5, 32), (0, 32))
        self.assertEqual(seats.normalize_seat_nums("", ""), (0, 0))

    def test_a_reversed_range_is_corrected(self):
        """32~13이라고 적어도 13~32로 본다.

        큰 숫자를 먼저 적는 일은 흔한데, 그대로 두면 아무 좌석도 안 걸려
        감시가 조용히 멎는다 — 화면에는 정상으로 보이면서.
        """
        self.assertEqual(seats.normalize_seat_nums(32, 13), (13, 32))

    def test_only_seats_inside_the_range_are_watched(self):
        rows = [self.row(f"H{i}", i * 2) for i in range(1, 41)]
        labels = seats.available_labels(rows, ["H"], 13, 32)
        self.assertEqual(len(labels), 20)
        self.assertIn("H13", labels)
        self.assertIn("H32", labels)
        self.assertNotIn("H12", labels)
        self.assertNotIn("H33", labels)

    def test_an_open_ended_range_works_both_ways(self):
        rows = [self.row(f"H{i}", i * 2) for i in range(1, 21)]
        self.assertNotIn("H12", seats.available_labels(rows, None, 13, 0))
        self.assertIn("H13", seats.available_labels(rows, None, 13, 0))
        self.assertIn("H12", seats.available_labels(rows, None, 0, 13))
        self.assertNotIn("H14", seats.available_labels(rows, None, 0, 13))

    def test_no_range_means_every_number(self):
        rows = [self.row(f"H{i}", i * 2) for i in range(1, 21)]
        self.assertEqual(len(seats.available_labels(rows, ["H"], 0, 0)), 20)

    def test_rows_and_numbers_are_both_applied(self):
        rows = ([self.row(f"H{i}", i * 2) for i in range(1, 41)]
                + [self.row(f"A{i}", i * 2) for i in range(1, 41)])
        labels = seats.available_labels(rows, ["H"], 13, 32)
        self.assertTrue(all(l.startswith("H") for l in labels))
        self.assertEqual(len(labels), 20)

    def test_a_run_does_not_reach_across_the_edge(self):
        """12번과 13번이 붙어 있어도 범위가 13부터면 한 구간이 아니다.

        잡을 수 없는 자리를 구간에 넣으면 "2석 연속 있음"이라고 알려 놓고
        정작 못 잡는다.
        """
        rows = [self.row("H12", 24), self.row("H13", 26), self.row("H14", 28)]
        runs = seats.consecutive_runs(rows, None, ["H"], 13, 0)
        self.assertEqual(runs, [["H13", "H14"]])

    def test_pick_block_stays_inside_the_range(self):
        # 범위 밖(H1·H2)이 더 앞이지만 골라선 안 된다.
        rows = ([self.row("H1", 2), self.row("H2", 4)]
                + [self.row(f"H{i}", i * 2, available=(i >= 13))
                   for i in range(3, 21)])
        block = seats.pick_block(rows, 2, ["H"], 13, 32)
        self.assertEqual(len(block), 2)
        for s in block:
            self.assertGreaterEqual(int(s["no"]), 13)

    def test_summarize_counts_only_the_range(self):
        rows = [self.row(f"H{i}", i * 2) for i in range(1, 41)]
        self.assertEqual(seats.summarize(rows, ["H"], 13, 32)["total"], 20)

    def test_a_seat_without_a_number_is_dropped_when_a_range_is_set(self):
        """번호로 고른다고 해 놓고 번호를 모르는 자리를 끼워 주면 안 된다."""
        odd = _seat("H", "A", 2, 4, True)     # 번호를 읽을 수 없는 좌석
        self.assertTrue(seats.in_scope(odd, ["H"], 0, 0), "범위가 없으면 통과")
        self.assertFalse(seats.in_scope(odd, ["H"], 13, 32), "범위가 있으면 제외")


class TestDiffAndSort(unittest.TestCase):
    def test_diff_only_new(self):
        known = {"A1", "A2"}
        current = {"A2", "A3", "B1"}
        self.assertEqual(seats.diff_available(known, current), ["A3", "B1"])

    def test_diff_empty_when_nothing_new(self):
        self.assertEqual(seats.diff_available({"A1", "A2"}, {"A1"}), [])

    def test_sort_is_row_then_numeric(self):
        # 'A2'가 'A10'보다 앞 (문자열 정렬이면 반대가 된다).
        self.assertEqual(
            seats.sort_labels(["A10", "A2", "B1", "A1"]),
            ["A1", "A2", "A10", "B1"],
        )


def _seat(row, no, x_start, x_end, available, *, left=False, right=False,
          salfrm="01"):
    """테스트용 좌석 하나. parse_seats가 내는 모양과 같게 만든다."""
    return {"row": row, "no": str(no), "label": f"{row}{no}",
            "available": available, "kind": "", "zone": "",
            "x_start": x_start, "x_end": x_end,
            "left_pway": left, "right_pway": right,
            "seat_salfrm_cd": salfrm}


class TestWheelchairSeats(unittest.TestCase):
    """휠체어 전용석(seatSalfrmCd=04)은 어디에서도 후보가 되면 안 된다.

    실제로 있었던 일: 매진에 가까운 IMAX 회차에서 624석 중 판매 가능한 것이
    A17~A24의 6석뿐이었는데 그게 전부 휠체어석이었다. 좌석맵은 이 자리를
    일반석과 똑같이 "판매 가능"으로 내려주므로, 자동 예매가 A17·A18을 잡으러
    갔다가 "장애인 좌석 예매 제한" 팝업에 걸려 죽었다.
    """

    def _hall(self):
        """A열은 휠체어석, H열은 일반석. 둘 다 비어 있다."""
        return ([_seat("A", i, i * 2, i * 2 + 2, True, salfrm="04")
                 for i in range(17, 25)]
                + [_seat("H", i, i * 2, i * 2 + 2, True)
                   for i in range(1, 21)])

    def test_not_in_scope_even_without_a_row_filter(self):
        chair = _seat("A", 17, 34, 36, True, salfrm="04")
        self.assertFalse(seats.in_scope(chair))
        self.assertFalse(seats.in_scope(chair, ["A"]))

    def test_never_reported_as_available(self):
        labels = seats.available_labels(self._hall())
        self.assertTrue(all(l.startswith("H") for l in labels), labels)

    def test_pick_block_skips_them(self):
        block = seats.pick_block(self._hall(), 2)
        self.assertTrue(all(s["row"] == "H" for s in block), block)

    def test_no_seats_when_only_wheelchair_seats_are_left(self):
        """그 6석만 남은 회차 — 잡을 자리가 없다고 해야 한다."""
        only_chairs = [_seat("A", i, i * 2, i * 2 + 2, True, salfrm="04")
                       for i in range(17, 25)]
        self.assertEqual(seats.available_labels(only_chairs), set())
        self.assertEqual(seats.pick_block(only_chairs, 2), [])
        self.assertEqual(seats.max_consecutive(only_chairs), 0)

    def test_not_counted_in_the_summary(self):
        summary = seats.summarize(self._hall())
        self.assertEqual(summary["total"], 20)
        self.assertEqual(summary["rows"], ["H"])


class TestConsecutive(unittest.TestCase):
    def _row(self, avails, aisle_after=()):
        """A열 좌석들을 만든다. avails[i]=예매가능, aisle_after에 든 번호 뒤엔 통로."""
        seats, x = [], 1
        for i, free in enumerate(avails, start=1):
            right = i in aisle_after
            seats.append(_seat("A", i, x, x + 2, free,
                               left=(i - 1) in aisle_after, right=right))
            x = x + 2 + (2 if right else 0)  # 통로가 있으면 x가 벌어진다
        return seats

    def test_runs_split_by_taken_seat(self):
        # A1 A2 [X] A4 A5  → 두 구간
        seats = self._row([True, True, False, True, True])
        runs = seats_mod.consecutive_runs(seats)
        self.assertEqual([len(r) for r in runs], [2, 2])

    def test_runs_split_by_aisle(self):
        # 좌표·통로로 A2와 A3 사이가 끊긴다 → 다 비어도 2+2 (한 줄 4연속 아님)
        seats = self._row([True, True, True, True], aisle_after={2})
        runs = seats_mod.consecutive_runs(seats)
        self.assertEqual(sorted(len(r) for r in runs), [2, 2])

    def test_max_consecutive(self):
        seats = self._row([True, True, True, False, True])
        self.assertEqual(seats_mod.max_consecutive(seats), 3)

    def test_consecutive_starts(self):
        # A1 A2 A3 비면 2연속 시작은 A1·A2, 3연속 시작은 A1
        seats = self._row([True, True, True, False, False])
        self.assertEqual(seats_mod.consecutive_starts(seats, {"A1", "A2", "A3"}, 2),
                         {"A1", "A2"})
        self.assertEqual(seats_mod.consecutive_starts(seats, {"A1", "A2", "A3"}, 3),
                         {"A1"})

    def test_new_consecutive_runs_detects_new_pair(self):
        # 이전엔 A2만, 이번에 A1이 풀려 A1-A2 2연속이 새로 생김
        seats = self._row([True, True, False, False, False])
        runs = seats_mod.new_consecutive_runs(seats, {"A2"}, {"A1", "A2"}, 2)
        self.assertEqual(len(runs), 1)
        self.assertEqual(seats_mod.run_range(runs[0]), "A1–A2")

    def test_new_consecutive_runs_ignores_already_known(self):
        # 이전에 이미 A1-A2 2연속이었으면 다시 알리지 않는다
        seats = self._row([True, True, False, False, False])
        runs = seats_mod.new_consecutive_runs(seats, {"A1", "A2"}, {"A1", "A2"}, 2)
        self.assertEqual(runs, [])

    def test_fixture_max_consecutive_in_row_a(self):
        # 실제 픽스처: A열 8석 전부 판매가능이지만 통로로 끊겨 최대 연속은 2다.
        parsed = seats_mod.parse_seats(SEAT_DATA)
        self.assertEqual(seats_mod.max_consecutive(parsed, rows=["A"]), 2)


class TestParseSeatsBookingFields(unittest.TestCase):
    def test_keeps_seat_loc_no(self):
        parsed = seats.parse_seats(SEAT_DATA)
        s = parsed[0]
        for k in ("seat_loc_no", "sbord_no", "seat_area_no", "szone_no",
                  "stknd_cd", "szone_kind_cd", "seat_salfrm_cd"):
            self.assertIn(k, s)
        self.assertTrue(s["seat_loc_no"], "seatLocNo가 보존되지 않았다")


class TestPickBlock(unittest.TestCase):
    def _row(self, avails, aisle_after=(), rowname="A"):
        seats_, x = [], 1
        for i, free in enumerate(avails, start=1):
            right = i in aisle_after
            seats_.append(_seat(rowname, i, x, x + 2, free,
                                left=(i - 1) in aisle_after, right=right))
            x = x + 2 + (2 if right else 0)
        return seats_

    def test_picks_party_sized_block(self):
        s = self._row([True, True, True, True, True])  # A1..A5 연속
        got = seats_mod.pick_block(s, 3)
        self.assertEqual([x["label"] for x in got], ["A2", "A3", "A4"])  # 가운데 3석

    def test_returns_dicts_with_loc_no(self):
        s = [_seat("A", 1, 1, 3, True), _seat("A", 2, 3, 5, True)]
        s[0]["seat_loc_no"] = "LOC1"; s[1]["seat_loc_no"] = "LOC2"
        got = seats_mod.pick_block(s, 2)
        self.assertEqual([x["seat_loc_no"] for x in got], ["LOC1", "LOC2"])

    def test_none_when_not_enough_consecutive(self):
        # 통로로 2+2 → 3연속 불가
        s = self._row([True, True, True, True], aisle_after={2})
        self.assertEqual(seats_mod.pick_block(s, 3), [])

    def test_prefers_rear_row(self):
        front = self._row([True, True], rowname="A")
        rear = self._row([True, True], rowname="C")
        got = seats_mod.pick_block(front + rear, 2)
        self.assertTrue(all(x["row"] == "C" for x in got), "뒤쪽 열을 우선해야 한다")

    def test_fixture_pair_in_row_a(self):
        parsed = seats.parse_seats(SEAT_DATA)
        got = seats_mod.pick_block(parsed, 2, rows=["A"])
        self.assertEqual(len(got), 2)
        self.assertTrue(all(x["row"] == "A" for x in got))
        # 인접한 두 좌석이어야 한다
        self.assertTrue(seats_mod._adjacent(got[0], got[1]))


class TestAlertMessage(unittest.TestCase):
    def test_alert_contains_seats_and_scope(self):
        msg = seats.build_seat_alert(
            "오디세이", "용산아이파크몰", "20260825", "1430",
            "IMAX LASER 2D IMAX관", ["A2", "A10", "A1"], available_now=5,
            rows=["A"])
        self.assertIn("A열", msg)
        self.assertIn("오디세이", msg)
        self.assertIn("A1, A2, A10", msg)      # 정렬된 순서
        self.assertIn("5석", msg)

    def test_alert_without_row_filter(self):
        msg = seats.build_seat_alert(
            "오디세이", "용산", "20260825", "1430", "IMAX", ["C7"],
            available_now=1, rows=None)
        self.assertNotIn("열)", msg)           # 열 범위 표기 없음


def showtime(hhmm: str, seq: str = "1") -> dict:
    return {"scnsrtTm": hhmm, "scnsNo": "S1", "scnSseq": seq}


class TestSelectShowtimes(unittest.TestCase):
    """감시가 볼 회차 고르기. 순서가 곧 자동 선점의 우선순위다."""

    SCHEDULE = [showtime(t, str(i)) for i, t in enumerate(
        ["1030", "1330", "1700", "2030", "2210", "2530"])]

    def times(self, rows):
        return [r["scnsrtTm"] for r in rows]

    def test_no_filter_keeps_original_order(self):
        got = seats.select_showtimes(self.SCHEDULE)
        self.assertEqual(self.times(got),
                         ["1030", "1330", "1700", "2030", "2210", "2530"])

    def test_exact_time_picks_one(self):
        got = seats.select_showtimes(self.SCHEDULE, scn_time="22:10")
        self.assertEqual(self.times(got), ["2210"])

    def test_exact_time_across_screens_is_also_latest_first(self):
        # 같은 시각이 여러 상영관에 있으면 회차가 여럿 나온다. 순서가 곧 선점
        # 우선순위이므로 범위 지정과 같은 규칙을 따라야 한다.
        schedule = [
            {"scnsrtTm": "2210", "scnsNo": "IMAX", "scnSseq": "1"},
            {"scnsrtTm": "2210", "scnsNo": "일반", "scnSseq": "2"},
        ]
        got = seats.select_showtimes(schedule, scn_time="22:10")
        self.assertEqual([r["scnsNo"] for r in got], ["IMAX", "일반"],
                         "같은 시각끼리는 원래 순서를 지켜야 한다")

    def test_range_is_latest_first(self):
        # 이 기능의 핵심 — 시간대 안에서 늦은 회차부터 잡는다.
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="17:00",
                                     scn_time_to="22:10")
        self.assertEqual(self.times(got), ["2210", "2030", "1700"])

    def test_range_bounds_are_inclusive(self):
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="10:30",
                                     scn_time_to="13:30")
        self.assertEqual(self.times(got), ["1330", "1030"])

    def test_range_excludes_outside(self):
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="12:00",
                                     scn_time_to="18:00")
        self.assertEqual(self.times(got), ["1700", "1330"])

    def test_late_night_showtime_past_24h(self):
        # CGV는 새벽 1:30을 '2530'으로 준다. 22:00~26:00이 그걸 잡아야 한다.
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="22:00",
                                     scn_time_to="26:00")
        self.assertEqual(self.times(got), ["2530", "2210"])

    def test_overnight_range_wraps(self):
        # 끝이 시작보다 이르면 자정을 넘긴 것 — 22:00~02:00 = 22:00~26:00.
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="22:00",
                                     scn_time_to="02:00")
        self.assertEqual(self.times(got), ["2530", "2210"])

    def test_overnight_range_also_catches_plain_after_midnight_time(self):
        # 극장에 따라 새벽 회차를 '0130'으로 주는 경우도 잡아야 한다.
        # 그리고 01:30은 22:10보다 **늦은** 회차다 — 적힌 숫자로 줄을 세우면
        # 뒤집혀서 '늦은 회차 우선'이 가장 이른 회차를 고르게 된다.
        schedule = [showtime("2210"), showtime("0130", "2")]
        got = seats.select_showtimes(schedule, scn_time_from="22:00",
                                     scn_time_to="02:00")
        self.assertEqual(self.times(got), ["0130", "2210"])

    def test_exact_time_wins_over_range(self):
        got = seats.select_showtimes(self.SCHEDULE, scn_time="20:30",
                                     scn_time_from="10:00", scn_time_to="23:00")
        self.assertEqual(self.times(got), ["2030"])

    def test_half_open_range_is_ignored(self):
        # 한쪽만 적힌 범위는 범위가 아니다 — 조용히 모든 회차로 돌아간다.
        got = seats.select_showtimes(self.SCHEDULE, scn_time_from="18:00")
        self.assertEqual(len(got), len(self.SCHEDULE))

    def test_unreadable_showtime_is_dropped_from_range(self):
        schedule = [showtime("2030"), showtime("", "9"), showtime(None, "8")]
        got = seats.select_showtimes(schedule, scn_time_from="10:00",
                                     scn_time_to="23:00")
        self.assertEqual(self.times(got), ["2030"])

    def test_empty_schedule(self):
        self.assertEqual(
            seats.select_showtimes([], scn_time_from="10:00", scn_time_to="23:00"),
            [])


class TestTimeRangeMinutes(unittest.TestCase):
    def test_plain_range(self):
        self.assertEqual(seats.time_range_minutes("18:00", "23:30"),
                         (18 * 60, 23 * 60 + 30))

    def test_overnight_adds_a_day(self):
        self.assertEqual(seats.time_range_minutes("22:00", "02:00"),
                         (22 * 60, 26 * 60))

    def test_same_start_and_end_is_a_point(self):
        self.assertEqual(seats.time_range_minutes("22:10", "22:10"),
                         (22 * 60 + 10, 22 * 60 + 10))

    def test_missing_side_is_not_a_range(self):
        self.assertIsNone(seats.time_range_minutes("18:00", ""))
        self.assertIsNone(seats.time_range_minutes("", ""))


class TestCycleError(unittest.TestCase):
    """확인 결과를 화면에 뭐라고 적을지. 실패가 정상으로 보이면 안 된다."""

    def test_clean_run_has_no_error(self):
        self.assertIsNone(seats._cycle_error(checked=3, failures=[]))

    def test_nothing_to_check_is_reported(self):
        self.assertEqual(seats._cycle_error(checked=0, failures=[]),
                         "확인된 회차가 없습니다")

    def test_total_failure_is_not_silent(self):
        # 예전엔 실패 회차도 직전 상태를 복사해 넣어 error=None이 됐다.
        msg = seats._cycle_error(checked=0, failures=["2210: HTTP 500"])
        self.assertIsNotNone(msg)
        self.assertIn("2210", msg)

    def test_total_failure_counts_all(self):
        msg = seats._cycle_error(
            checked=0, failures=["2210: HTTP 500", "1930: HTTP 500"])
        self.assertIn("2건", msg)
        self.assertIn("외 1건", msg)

    def test_partial_failure_shows_both_counts(self):
        msg = seats._cycle_error(checked=4, failures=["2210: 시간초과"])
        self.assertIn("4", msg)
        self.assertIn("1", msg)
        self.assertIn("시간초과", msg)


class TestCatalogCache(unittest.TestCase):
    """영화·극장 목록을 매 바퀴 다시 받지 않는다 — 다만 오픈은 놓치면 안 된다.

    실측(2026-08-28): 폴링을 3초로 맞췄는데 실제로는 6초마다 돌았다. 사이클이
    ~4초라 다음 슬롯을 통째로 놓친 것이고, 그 안에서 감시와 무관하게 매번 나가는
    호출이 이 목록 두 건이었다.
    """

    class FakeCgv:
        def __init__(self):
            self.movie_calls = 0
            self.site_calls = 0

        def bookable_movies(self):
            self.movie_calls += 1
            return [{"movNo": "9", "movNm": "실물에만 있는 영화"}]

        def sites(self):
            self.site_calls += 1
            return [{"siteNo": "0013", "siteNm": "용산아이파크몰"}], {}

    def setUp(self):
        self.cgv = self.FakeCgv()
        self.cat = watch.Catalog(self.cgv, persist=False)

    def use_cache(self, movies, sites):
        """DB 캐시가 이렇게 들어 있다고 해 둔다. None이면 캐시 없음."""
        self.cat._db_cache = (  # noqa: SLF001 - 캐시 경로를 시험한다
            lambda kind: movies if kind == "movie" else sites)

    def test_a_hit_never_touches_the_network(self):
        self.use_cache([{"movNo": "1", "movNm": "오디세이"}],
                       [{"siteNo": "0013", "siteNm": "용산아이파크몰"}])
        movie, _ = self.cat.resolve_movie("오디세이")
        site, _ = self.cat.resolve_site("용산아이파크몰")
        self.assertEqual(movie["movNo"], "1")
        self.assertEqual(site["siteNo"], "0013")
        self.assertEqual(self.cgv.movie_calls, 0, "캐시에 있는데 다시 받았다")
        self.assertEqual(self.cgv.site_calls, 0)

    def test_a_miss_still_goes_and_looks(self):
        """캐시에 없으면 반드시 실물을 본다 — 목록에 뜨는 순간이 곧 오픈이다.

        여기서 캐시만 믿고 "없다"고 답하면, 아직 안 열린 영화를 미리 걸어 둔
        감시는 오픈을 영영 감지하지 못한다. 시간대 감시의 존재 이유가 그건데.
        """
        self.use_cache([{"movNo": "1", "movNm": "오디세이"}], [])
        movie, _ = self.cat.resolve_movie("실물에만 있는 영화")
        self.assertIsNotNone(movie, "캐시에 없다고 오픈을 놓쳤다")
        self.assertEqual(movie["movNo"], "9")
        self.assertEqual(self.cgv.movie_calls, 1)

    def test_no_cache_at_all_behaves_as_before(self):
        self.use_cache(None, None)
        movie, _ = self.cat.resolve_movie("실물에만 있는 영화")
        self.assertEqual(movie["movNo"], "9")
        self.assertEqual(self.cgv.movie_calls, 1)

    def test_a_live_fetch_is_shared_by_the_rest_of_the_cycle(self):
        # 한 번 실물을 받았으면 그 바퀴의 나머지는 그걸 쓴다.
        self.use_cache([], [])
        self.cat.resolve_movie("실물에만 있는 영화")
        self.cat.resolve_movie("실물에만 있는 영화")
        self.cat.resolve_site("용산아이파크몰")
        self.assertEqual(self.cgv.movie_calls, 1)
        self.assertEqual(self.cgv.site_calls, 1)


class TestCatalogCacheExpiry(unittest.TestCase):
    """낡은 캐시를 쓰면 안 된다 — 그 사이 새로 열린 영화가 통째로 빠진다."""

    def test_a_stale_cache_is_refused(self):
        real = watch.store.catalog_refreshed_at
        old = (watch.datetime.now().astimezone()
               - watch.timedelta(hours=watch.CATALOG_CACHE_MAX_AGE_HOURS + 1))
        watch.store.catalog_refreshed_at = lambda: old
        self.addCleanup(setattr, watch.store, "catalog_refreshed_at", real)
        self.assertIsNone(watch.Catalog._db_cache("movie"))

    def test_an_empty_cache_is_refused(self):
        real = watch.store.catalog_refreshed_at
        watch.store.catalog_refreshed_at = lambda: None
        self.addCleanup(setattr, watch.store, "catalog_refreshed_at", real)
        self.assertIsNone(watch.Catalog._db_cache("movie"))

    def test_an_unreadable_cache_is_refused_quietly(self):
        real = watch.store.catalog_refreshed_at

        def boom():
            raise RuntimeError("DB가 죽었다")

        watch.store.catalog_refreshed_at = boom
        self.addCleanup(setattr, watch.store, "catalog_refreshed_at", real)
        self.assertIsNone(watch.Catalog._db_cache("movie"))


class TestSeatPriority(unittest.TestCase):
    """예산이 빠듯하면 급한 것부터 쓴다.

    예전에는 먼 날짜를 아예 몇 바퀴 건너뛰었다. 지금은 건너뛰지 않고 **순서만**
    정한다 — 예산이 남으면 먼 날짜도 같은 바퀴에 처리되고, 모자라면 큐에 남아
    다음 창에서 처리된다. 버리는 게 아니라 미룬다.
    """

    TODAY = date(2026, 8, 31)

    def test_nearer_dates_come_first(self):
        near = seats.seat_priority("20260901", self.TODAY)
        mid = seats.seat_priority("20260903", self.TODAY)
        far = seats.seat_priority("20260905", self.TODAY)
        self.assertLess(near, mid)
        self.assertLess(mid, far)

    def test_today_is_the_most_urgent(self):
        self.assertEqual(seats.seat_priority("20260831", self.TODAY), 0)

    def test_a_past_date_is_still_urgent(self):
        # 남은 날이 음수다 — 가장 급한 칸에 걸려야 한다.
        self.assertEqual(seats.seat_priority("20260825", self.TODAY), 0)

    def test_an_unreadable_date_is_treated_as_urgent(self):
        """모르는 것을 뒤로 미루면 놓친다."""
        for bad in ("", "abc", "2026", None):
            self.assertEqual(seats.seat_priority(bad, self.TODAY), 0, repr(bad))


class TestPendingQueue(unittest.TestCase):
    """예산을 넘는 일감은 큐에 남았다가 다음 창에서 처리된다."""

    class Sess:
        def __init__(self, limit):
            import watch

            self.budget = watch.RateBudget(limit=limit)
            self.asked = []

        def allowance(self):
            return self.budget.allowance()

        def showtimes(self, site_no, mov_no, ymd):
            return [{"scnsNo": f"S{i}", "scnSseq": str(i), "scnsrtTm": "1800",
                     "siteNo": site_no, "scnsNm": "IMAX관",
                     "atktGoodsNm": "IMAX LASER 2D"} for i in range(4)]

        def get_json_many(self, paths, seat_fields=None):
            self.asked.append(len(paths))
            got = self.budget.take(len(paths))
            return ([{"data": {"items": [{"seats": []}]}}] * got
                    + [None] * (len(paths) - got))

    class Cat:
        def resolve_movie(self, q):
            return {"movNo": "M1", "movNm": q}, ""

        def resolve_site(self, q):
            return {"siteNo": "0013", "siteNm": q}, ""

    def setUp(self):
        for cache in (seats._pending, seats._last_fetched, seats._last_count,
                      seats._schedule_at, seats._schedule_rows):
            cache.clear()
            self.addCleanup(cache.clear)



    def group(self, dates):
        return [{"id": i, "owner_id": 7, "movie_query": "오디세이",
                 "site_query": "용산아이파크몰", "scn_ymd": d,
                 "screen_types": ["IMAX"], "rows": [], "scn_time": "",
                 "scn_time_from": "", "scn_time_to": "", "min_consecutive": 2,
                 "auto_book": False, "seat_num_from": 0, "seat_num_to": 0}
                for i, d in enumerate(dates, start=1)]

    def test_work_beyond_the_budget_waits_instead_of_vanishing(self):
        s = self.Sess(limit=5)
        group = self.group(["20260901", "20260902", "20260903"])   # 회차 12개
        got = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        self.assertEqual(len(got), 5, "예산만큼만 받아야 한다")
        self.assertEqual(len(seats._pending[7]), 7, "나머지는 큐에 남아야 한다")

    def test_the_next_window_picks_up_where_it_left_off(self):
        s = self.Sess(limit=5)
        group = self.group(["20260902", "20260903"])   # 둘 다 재확인 간격이 있다
        first = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        s.budget._sent.clear()          # 창이 지난 것으로
        second = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        self.assertTrue(second, "다음 창에서 이어서 처리돼야 한다")
        self.assertFalse(set(first) & set(second),
                         "간격이 안 지났는데 같은 회차를 또 받았다")

    def test_a_recently_checked_showtime_is_not_requeued(self):
        """가까운 날짜가 매 창 예산을 채우면 먼 날짜가 영영 밀린다."""
        s = self.Sess(limit=100)
        group = self.group(["20260903"])              # 재확인 간격이 있는 순위
        seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        s.budget._sent.clear()
        again = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        self.assertEqual(again, {}, "방금 본 회차를 곧바로 또 받았다")

    def test_an_urgent_showtime_is_always_rechecked(self):
        # 가까운 날짜는 간격이 0이라 매 바퀴 다시 본다.
        s = self.Sess(limit=100)
        group = self.group(["20260901"])
        first = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        s.budget._sent.clear()
        again = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        self.assertEqual(set(first), set(again), "급한 회차인데 안 봤다")

    def test_far_dates_are_not_starved(self):
        """급한 것이 계속 예산을 채워도 먼 것이 끝내 차례를 받아야 한다.

        순위만 두면 급한 회차가 매 창을 다 쓰고 먼 회차는 영영 밀린다. 오래
        기다린 일감을 끌어올려(aging) 반드시 처리되게 한다.
        """
        import time as _time

        self.addCleanup(setattr, seats, "AGE_PROMOTE_SECONDS",
                        seats.AGE_PROMOTE_SECONDS)
        seats.AGE_PROMOTE_SECONDS = 0.01      # 실제로는 45초

        s = self.Sess(limit=4)                # 급한 회차 4개로 딱 차는 예산
        group = self.group(["20260901", "20260905"])
        far = set()
        for _ in range(8):
            got = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
            far |= {k for k in got if k[2] == "20260905"}
            s.budget._sent.clear()
            _time.sleep(0.02)                 # 기다린 시간이 쌓인다
        self.assertTrue(far, "먼 날짜가 한 번도 처리되지 않았다")

    def test_aging_does_not_starve_the_urgent_either(self):
        # 오래 기다린 것이 올라와도 급한 것이 아주 밀려나면 안 된다.
        import time as _time

        self.addCleanup(setattr, seats, "AGE_PROMOTE_SECONDS",
                        seats.AGE_PROMOTE_SECONDS)
        seats.AGE_PROMOTE_SECONDS = 0.01

        s = self.Sess(limit=4)
        group = self.group(["20260901", "20260905"])
        near = set()
        for _ in range(8):
            got = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
            near |= {k for k in got if k[2] == "20260901"}
            s.budget._sent.clear()
            _time.sleep(0.02)
        self.assertEqual(len(near), 4, "급한 회차가 처리되지 않았다")

    def test_everything_is_eventually_fetched(self):
        s = self.Sess(limit=5)
        group = self.group(["20260902", "20260903"])   # 회차 8개
        seen = {}
        for _ in range(4):
            seen.update(seats._prefetch_seat_maps(s, self.Cat(), group,
                                                  sched_cache={}))
            s.budget._sent.clear()
        self.assertEqual(len(seen), 8, "미룬 것이 끝내 처리되지 않았다")
        self.assertFalse(seats._pending[7], "큐가 비어야 한다")

    def test_urgent_work_is_taken_first(self):
        s = self.Sess(limit=4)
        # 먼 날짜를 먼저 올려도 가까운 날짜가 앞서야 한다.
        group = self.group(["20260905", "20260901"])
        got = seats._prefetch_seat_maps(s, self.Cat(), group, sched_cache={})
        self.assertTrue(all(k[2] == "20260901" for k in got),
                        f"급한 것부터 안 가져갔다: {sorted(k[2] for k in got)}")

    def test_no_budget_means_no_request(self):
        s = self.Sess(limit=0)
        group = self.group(["20260901"])
        self.assertEqual(seats._prefetch_seat_maps(s, self.Cat(), group,
                                                   sched_cache={}), {})
        self.assertEqual(s.asked, [], "예산이 없는데 물었다")
        self.assertTrue(seats._pending[7], "일감은 남아 있어야 한다")

    def test_the_queue_does_not_grow_without_bound(self):
        s = self.Sess(limit=0)
        many = [f"202609{d:02d}" for d in range(1, 30)] * 6
        seats._prefetch_seat_maps(s, self.Cat(), self.group(many),
                                  sched_cache={})
        self.assertLessEqual(len(seats._pending[7]), seats.PENDING_LIMIT)


class TestScheduleCarriesTheAnswer(unittest.TestCase):
    """상영표가 이미 잔여 좌석 수를 들고 있다 — 좌석맵을 열 이유가 대개 없다.

    2026-08-31 실측: searchSchByMov의 frSeatCnt가 좌석맵을 열어 센 것과 정확히
    일치했다(6/5/6석 세 회차). 상영표 1건이 그 날짜의 **모든 회차**를 덮으므로,
    이 값이 그대로면 좌석 배치도 그대로다.
    """

    def test_it_reads_the_free_seat_count(self):
        self.assertEqual(seats._seat_count({"frSeatCnt": "6"}), 6)

    def test_the_temp_held_count_is_not_used(self):
        """frtmpSeatCnt는 임시 선점분까지 더한 값이라 좌석맵과 다르다."""
        row = {"frSeatCnt": "6", "frtmpSeatCnt": "12"}
        self.assertEqual(seats._seat_count(row), 6)

    def test_a_missing_count_is_unknown_not_zero(self):
        """못 읽으면 좌석맵을 열어야 한다 — 0으로 보면 '자리 없음'이 된다."""
        for row in ({}, {"frSeatCnt": None}, {"frSeatCnt": "?"}):
            self.assertIsNone(seats._seat_count(row), row)


class TestSeatFieldsAreEnough(unittest.TestCase):
    """좌석맵을 묶어 받을 때 브라우저에서 필드를 깎는다 — 그래도 결과가 같아야 한다.

    원본은 좌석당 39개 필드인데 parse_seats가 읽는 건 17개뿐이다. 안 깎으면
    32건이 한 프로토콜 메시지로 17.6MB가 되어 병렬화 이득을 도로 까먹는다.
    **필드 이름을 그대로 두므로 출력은 바뀌지 않아야 한다** — 이게 깨지면
    감시가 조용히 틀린 좌석을 본다.
    """

    def slim(self, data):
        """브라우저가 하는 것과 같은 방식으로 좌석 필드를 깎는다."""
        import copy

        out = copy.deepcopy(data)
        for item in out.get("items") or []:
            item["seats"] = [{k: v for k, v in s.items()
                              if k in seats.SEAT_FIELDS}
                             for s in item.get("seats") or []]
        return out

    def test_parsing_a_slimmed_map_gives_the_same_result(self):
        self.assertEqual(seats.parse_seats(SEAT_DATA),
                         seats.parse_seats(self.slim(SEAT_DATA)))

    def test_every_field_parse_seats_reads_is_kept(self):
        """parse_seats가 읽는 필드가 목록에서 빠지면 그 값이 조용히 사라진다."""
        import re

        source = (ROOT / "seats.py").read_text(encoding="utf-8")
        body = source.split("def parse_seats")[1].split("\ndef ")[0]
        read = set(re.findall(r's\.get\("(\w+)"', body))
        self.assertTrue(read, "parse_seats에서 필드를 못 읽었다")
        self.assertFalse(read - set(seats.SEAT_FIELDS),
                         f"SEAT_FIELDS에 빠진 필드: {read - set(seats.SEAT_FIELDS)}")

    def test_the_list_carries_nothing_extra(self):
        # 안 쓰는 필드를 남기면 그만큼 그냥 무겁다.
        source = (ROOT / "seats.py").read_text(encoding="utf-8")
        body = source.split("def parse_seats")[1].split("\ndef ")[0]
        import re
        read = set(re.findall(r's\.get\("(\w+)"', body))
        self.assertFalse(set(seats.SEAT_FIELDS) - read,
                         f"쓰지 않는 필드: {set(seats.SEAT_FIELDS) - read}")


class TestRowsToCheck(unittest.TestCase):
    """프리페치와 본 루프가 같은 회차를 골라야 한다.

    각자 고르면 어긋날 수 있고, 어긋나면 미리 받아 둔 것이 조용히 무용지물이
    된다 — 결과는 맞으니 알아채기 어렵다.
    """

    def row(self, screen):
        return {"scnsNo": "S1", "scnSseq": "1", "scnsNm": screen,
                "atktGoodsNm": screen}

    def test_no_filter_takes_everything(self):
        rows = [self.row("IMAX관"), self.row("4DX관")]
        self.assertEqual(len(seats.rows_to_check({"screen_types": []}, rows)), 2)

    def test_a_filter_narrows_it(self):
        rows = [self.row("IMAX관"), self.row("4DX관")]
        out = seats.rows_to_check({"screen_types": ["IMAX"]}, rows)
        self.assertEqual(len(out), 1)
        self.assertIn("IMAX", out[0]["scnsNm"])

    def test_the_key_follows_the_row_site(self):
        # 회차에 siteNo가 있으면 그걸 쓰고, 없으면 넘겨받은 값으로 떨어진다.
        w = {"scn_ymd": "20260905"}
        with_site = seats.seat_map_key(w, {**self.row("IMAX관"),
                                           "siteNo": "0013"}, "9999")
        without = seats.seat_map_key(w, self.row("IMAX관"), "9999")
        self.assertEqual(with_site[0], "0013")
        self.assertEqual(without[0], "9999")


class TestPrefetchIsShared(unittest.TestCase):
    """미리 받아 둔 좌석맵을 감시들이 나눠 써야 한다.

    같은 영화·극장·날짜를 열만 다르게 걸어 두는 건 흔하다. 처음엔 결과를
    한 번 쓰고 지웠더니(pop) 첫 감시만 쓰고 나머지는 전부 개별로 다시 받았다 —
    묶음 3건에 개별 9건이 나갔다. 한 사이클 안에서는 같은 순간의 같은
    데이터이므로 나눠 쓰는 게 맞다.
    """

    class Sess:
        def __init__(self):
            self.batched = 0
            self.individual = 0

        def allowance(self):
            return 1000          # 예산은 여기 관심사가 아니다

        def showtimes(self, site_no, mov_no, ymd):
            return [{"scnsNo": f"S{i}", "scnSseq": str(i), "scnsrtTm": "1800",
                     "siteNo": site_no, "scnsNm": "IMAX관",
                     "atktGoodsNm": "IMAX LASER 2D"} for i in range(3)]

        def get_json_many(self, paths, seat_fields=None):
            self.batched += len(paths)
            return [{"data": {"items": [{"seats": []}]}} for _ in paths]

        def seat_map(self, **kwargs):
            self.individual += 1
            return {"items": [{"seats": []}]}

    class Cat:
        def resolve_movie(self, q):
            return {"movNo": "M1", "movNm": q}, ""

        def resolve_site(self, q):
            return {"siteNo": "0013", "siteNm": q}, ""

    def setUp(self):
        for cache in (seats._pending, seats._last_fetched, seats._last_count,
                      seats._schedule_at, seats._schedule_rows):
            cache.clear()
            self.addCleanup(cache.clear)

    def group(self, n=4):
        return [{"id": i, "owner_id": 1, "movie_query": "오디세이",
                 "site_query": "용산아이파크몰", "scn_ymd": "20260905",
                 "screen_types": ["IMAX"], "rows": [], "scn_time": "",
                 "scn_time_from": "", "scn_time_to": "", "min_consecutive": 2,
                 "auto_book": False, "seat_num_from": 0, "seat_num_to": 0}
                for i in range(1, n + 1)]

    def run_cycle(self, session):
        import store

        real_save, real_prev = store.save_seat_state, store.prev_seat_state
        store.save_seat_state = lambda *a, **k: None
        store.prev_seat_state = lambda i: {}
        self.addCleanup(setattr, store, "save_seat_state", real_save)
        self.addCleanup(setattr, store, "prev_seat_state", real_prev)

        group, sched = self.group(), {}
        pre = seats._prefetch_seat_maps(session, self.Cat(), group,
                                        sched_cache=sched)
        for w in group:
            seats._check_one_seat_watch(session, self.Cat(), w, None, None,
                                        guard=None, dry_run=False,
                                        sched_cache=sched, warmed=set(),
                                        prefetched=pre)
        return pre

    def test_watches_sharing_a_showtime_share_the_fetch(self):
        s = self.Sess()
        self.run_cycle(s)
        self.assertEqual(s.individual, 0, "미리 받아 뒀는데 개별로 또 받았다")
        self.assertEqual(s.batched, 3, "같은 회차를 여러 번 받았다")

    def test_the_same_showtime_is_only_requested_once(self):
        s = self.Sess()
        pre = seats._prefetch_seat_maps(s, self.Cat(), self.group(),
                                        sched_cache={})
        self.assertEqual(len(pre), 3, "감시 4건이 같은 회차 3개를 본다")

    def test_a_missing_entry_falls_back_to_the_individual_path(self):
        """묶음이 못 받은 항목은 개별로 받아야 한다 — 조용히 빠뜨리면 안 된다."""
        s = self.Sess()
        import store

        real_save, real_prev = store.save_seat_state, store.prev_seat_state
        store.save_seat_state = lambda *a, **k: None
        store.prev_seat_state = lambda i: {}
        self.addCleanup(setattr, store, "save_seat_state", real_save)
        self.addCleanup(setattr, store, "prev_seat_state", real_prev)

        w = self.group(1)[0]
        seats._check_one_seat_watch(s, self.Cat(), w, None, None, guard=None,
                                    dry_run=False, sched_cache={},
                                    warmed=set(), prefetched={})
        self.assertEqual(s.individual, 3, "미리 받은 게 없으면 개별로 받아야 한다")


class TestCycleCost(unittest.TestCase):
    """사이클이 폴링 간격을 넘으면 다음 슬롯을 놓친다 — 그게 로그에 남아야 한다."""

    def test_it_counts_calls_and_cache_hits_apart(self):
        cost = seats._CycleCost()
        with cost.call("상영표"):
            pass
        cost.hit("상영표(캐시)")
        cost.hit("상영표(캐시)")
        out = cost.summary()
        self.assertIn("상영표 1회", out)
        self.assertIn("상영표(캐시) 2회", out)
        self.assertIn("합계", out)

    def test_a_failing_call_is_still_counted(self):
        """실패한 호출도 시간을 쓴다 — 안 세면 사이클 길이가 설명되지 않는다."""
        cost = seats._CycleCost()
        with self.assertRaises(RuntimeError):
            with cost.call("좌석맵"):
                raise RuntimeError("조회 실패")
        self.assertIn("좌석맵 1회", cost.summary())


class TestAuthRecovery(unittest.TestCase):
    """좌석 조회가 401을 내면 세션을 되살리고 한 번 다시 시도해야 한다.

    되살리기가 없으면, 저장된 accessToken이 만료되는 순간부터 그 사용자의 좌석
    감시가 영구히 멎는다(성공했을 때만 쿠키를 갱신하므로 stale 토큰이 계속 남는다).
    """

    class FakeSession:
        def __init__(self, fail_times: int):
            self.fail_times = fail_times
            self.calls = 0

        def seat_map(self, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise watch.AuthRequired("로그인이 필요합니다 (HTTP 401)")
            return {"ok": True}

    class FakeGuard:
        def __init__(self, succeeds: bool):
            self.succeeds = succeeds
            self.attempts = 0

        def recover(self, session):
            self.attempts += 1
            return self.succeeds

    def test_retries_once_after_successful_recovery(self):
        session = self.FakeSession(fail_times=1)
        guard = self.FakeGuard(succeeds=True)
        self.assertEqual(seats._seat_map(session, guard, site_no="1"),
                         {"ok": True})
        self.assertEqual(session.calls, 2)
        self.assertEqual(guard.attempts, 1)

    def test_gives_up_when_recovery_fails(self):
        session = self.FakeSession(fail_times=1)
        guard = self.FakeGuard(succeeds=False)
        with self.assertRaises(watch.AuthRequired):
            seats._seat_map(session, guard, site_no="1")
        self.assertEqual(session.calls, 1)      # 되살리기 실패 — 재시도 안 함

    def test_second_401_after_recovery_is_not_retried_again(self):
        # 되살렸는데도 또 401이면 그대로 올린다 — 무한 재로그인을 막는다.
        session = self.FakeSession(fail_times=2)
        guard = self.FakeGuard(succeeds=True)
        with self.assertRaises(watch.AuthRequired):
            seats._seat_map(session, guard, site_no="1")
        self.assertEqual(session.calls, 2)

    def test_guard_recovers_at_most_once(self):
        recoveries = []

        class Recorder(seats._AuthGuard):
            def recover(self, session):        # cgv_login을 부르지 않고 가로챈다
                if self.tried:
                    return self.ok
                self.tried = True
                recoveries.append(session)
                self.ok = False
                return self.ok

        guard = Recorder(owner_id=7)
        session = self.FakeSession(fail_times=99)
        for _ in range(5):
            with self.assertRaises(watch.AuthRequired):
                seats._seat_map(session, guard, site_no="1")
        self.assertEqual(len(recoveries), 1)


if __name__ == "__main__":
    unittest.main()
