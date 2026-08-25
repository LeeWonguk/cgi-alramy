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

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "seatdata.json").read_text(encoding="utf-8")
)
SEAT_DATA = FIXTURE["data"]


class TestParseSeats(unittest.TestCase):
    def test_parses_all_64_seats(self):
        parsed = seats.parse_seats(SEAT_DATA)
        self.assertEqual(len(parsed), 64)
        s = parsed[0]
        self.assertEqual(set(s), {"row", "no", "label", "available", "kind", "zone"})
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
