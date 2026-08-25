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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import seats  # noqa: E402
import seats as seats_mod  # 지역변수 seats에 가려지지 않게 별칭도 둔다  # noqa: E402

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "seatdata.json").read_text(encoding="utf-8")
)
SEAT_DATA = FIXTURE["data"]


class TestParseSeats(unittest.TestCase):
    def test_parses_all_64_seats(self):
        parsed = seats.parse_seats(SEAT_DATA)
        self.assertEqual(len(parsed), 64)
        s = parsed[0]
        self.assertEqual(
            set(s),
            {"row", "no", "label", "available", "kind", "zone",
             "x_start", "x_end", "left_pway", "right_pway"})
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


def _seat(row, no, x_start, x_end, available, *, left=False, right=False):
    """테스트용 좌석 하나. parse_seats가 내는 모양과 같게 만든다."""
    return {"row": row, "no": str(no), "label": f"{row}{no}",
            "available": available, "kind": "", "zone": "",
            "x_start": x_start, "x_end": x_end,
            "left_pway": left, "right_pway": right}


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


if __name__ == "__main__":
    unittest.main()
