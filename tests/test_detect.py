#!/usr/bin/env python3
"""감지·판정 로직 단위 테스트.

CGV도 Postgres도 건드리지 않는다 — 여기 있는 함수들은 전부 순수 함수이거나
그에 가깝다. 이 프로젝트는 .env 로더·OAuth·웹훅을 모두 표준 라이브러리로 직접
구현하는 쪽을 택했으므로, 테스트도 새 의존성 없이 unittest로 맞춘다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402
import watch  # noqa: E402


def ymd(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).strftime("%Y%m%d")


class TestWithinLookahead(unittest.TestCase):
    def test_zero_means_unlimited(self):
        self.assertTrue(watch.within_lookahead(ymd(365), 0))
        self.assertTrue(watch.within_lookahead(ymd(0), 0))

    def test_negative_treated_as_unlimited(self):
        self.assertTrue(watch.within_lookahead(ymd(365), -1))

    def test_boundary_is_inclusive(self):
        self.assertTrue(watch.within_lookahead(ymd(3), 3))
        self.assertFalse(watch.within_lookahead(ymd(4), 3))

    def test_unparsable_date_passes(self):
        # 판정할 수 없는 값 때문에 알림을 잃는 것보다 통과시키는 게 낫다.
        self.assertTrue(watch.within_lookahead("이상한값", 3))


class TestDiffDates(unittest.TestCase):
    """기억할 집합과 알릴 집합이 항상 같은 필터를 지나는지."""

    def test_new_date_is_reported_once(self):
        tracked, new = watch.diff_dates([ymd(1), ymd(2)], {ymd(1)}, 0)
        self.assertEqual(tracked, [ymd(1), ymd(2)])
        self.assertEqual(new, [ymd(2)])

    def test_first_observation_reports_everything(self):
        # 첫 관측에서 알림을 막는 책임은 check_all의 `not prev` 분기에 있다.
        # 여기서는 known이 비면 전부 새 날짜로 나오는 게 맞다.
        tracked, new = watch.diff_dates([ymd(1), ymd(2)], set(), 0)
        self.assertEqual(new, tracked)

    def test_expired_dates_leave_the_baseline(self):
        # CGV가 더 이상 주지 않는 날짜는 기준선에서도 빠져야 한다.
        tracked, _ = watch.diff_dates([ymd(5)], {ymd(1), ymd(5)}, 0)
        self.assertEqual(tracked, [ymd(5)])

    def test_out_of_range_date_is_not_remembered(self):
        """G2 회귀: 범위 밖 날짜를 기준선에 넣으면 안 된다.

        넣어 버리면 나중에 범위 안으로 들어와도 "이미 아는 날짜"가 되어
        그 날짜는 영구히 알림이 가지 않는다.
        """
        tracked, new = watch.diff_dates([ymd(1), ymd(10)], set(), 3)
        self.assertEqual(tracked, [ymd(1)], "범위 밖 날짜가 기준선에 들어갔다")
        self.assertEqual(new, [ymd(1)])

    def test_out_of_range_date_alerts_when_it_comes_into_range(self):
        """G2 회귀의 본질 — 시간이 흘러 범위에 들어오면 알림이 와야 한다."""
        far, near = ymd(10), ymd(2)

        # 1일차: lookahead 3일. far는 범위 밖이라 알리지도 기억하지도 않는다.
        tracked, new = watch.diff_dates([near, far], set(), 3)
        self.assertNotIn(far, new)
        baseline = set(tracked)

        # N일 뒤: far가 범위 안으로 들어왔다 (lookahead를 넓혀 같은 상황을 만든다).
        _, new_later = watch.diff_dates([near, far], baseline, 30)
        self.assertIn(far, new_later,
                      "범위 안으로 들어온 날짜의 알림이 유실됐다")

    def test_lookahead_off_keeps_previous_behaviour(self):
        # 기본값 0에서는 예전 동작과 완전히 같아야 한다.
        dates = [ymd(1), ymd(90)]
        tracked, new = watch.diff_dates(dates, set(), 0)
        self.assertEqual(tracked, dates)
        self.assertEqual(new, dates)


class TestScreenTypeMatching(unittest.TestCase):
    IMAX = {"movkndDsplNm": "IMAX LASER 2D", "expoScnsNm": "IMAX관"}
    FOURDX = {"movkndDsplNm": "4DX 2D", "expoScnsNm": "4DX관"}
    SCREENX = {"movkndDsplNm": "SCREENX 2D", "expoScnsNm": "4관[SCREENX]"}
    PLAIN = {"movkndDsplNm": "2D", "expoScnsNm": "5관 (Laser)"}
    CHEF = {"movkndDsplNm": "2D", "expoScnsNm": "스트레스리스 시네마[CINE de CHEF]"}

    def test_empty_filter_matches_everything(self):
        self.assertTrue(watch.matches_screen_types(self.PLAIN, []))

    def test_one_word_catches_derived_screens(self):
        self.assertTrue(watch.matches_screen_types(self.IMAX, ["IMAX"]))
        self.assertFalse(watch.matches_screen_types(self.PLAIN, ["IMAX"]))

    def test_any_of_several(self):
        wanted = ["IMAX", "4DX"]
        self.assertTrue(watch.matches_screen_types(self.FOURDX, wanted))
        self.assertTrue(watch.matches_screen_types(self.IMAX, wanted))
        self.assertFalse(watch.matches_screen_types(self.SCREENX, wanted))

    def test_matching_ignores_case_and_spaces(self):
        # normalize()가 공백·대소문자를 지운다 — 'cine de chef'로도 걸려야 한다.
        self.assertTrue(watch.matches_screen_types(self.CHEF, ["cine de chef"]))

    def test_bracketed_screen_name(self):
        self.assertTrue(watch.matches_screen_types(self.SCREENX, ["SCREENX"]))


class TestScreenLabelAndGrouping(unittest.TestCase):
    def test_label_joins_kind_and_screen(self):
        self.assertEqual(
            watch.screen_label({"movkndDsplNm": "IMAX LASER 2D",
                                "expoScnsNm": "IMAX관"}),
            "IMAX LASER 2D IMAX관",
        )

    def test_label_skips_missing_parts(self):
        self.assertEqual(watch.screen_label({"expoScnsNm": "5관"}), "5관")
        self.assertEqual(watch.screen_label({}), "")

    def test_grouping_collects_times_per_screen(self):
        rows = [
            {"movkndDsplNm": "2D", "expoScnsNm": "5관", "scnsrtTm": "1030"},
            {"movkndDsplNm": "2D", "expoScnsNm": "5관", "scnsrtTm": "1330"},
            {"movkndDsplNm": "IMAX LASER 2D", "expoScnsNm": "IMAX관",
             "scnsrtTm": "0630"},
        ]
        groups = watch.group_showtimes(rows)
        by_label = {g["label"]: g["times"] for g in groups}
        self.assertEqual(by_label["2D 5관"], ["10:30", "13:30"])
        self.assertEqual(by_label["IMAX LASER 2D IMAX관"], ["06:30"])


class TestFormatting(unittest.TestCase):
    def test_date_shows_weekday(self):
        # 2026-08-17은 월요일.
        self.assertEqual(watch.fmt_date("20260817"), "8/17(월)")

    def test_bad_date_passes_through(self):
        self.assertEqual(watch.fmt_date("nope"), "nope")

    def test_time(self):
        self.assertEqual(watch.fmt_time("1400"), "14:00")

    def test_late_night_time_is_kept_as_is(self):
        # CGV는 다음날 01:25 상영을 '2525'로 준다 — 실측에 실제로 나온다.
        self.assertEqual(watch.fmt_time("2525"), "25:25")

    def test_empty_time(self):
        self.assertEqual(watch.fmt_time(""), "")
        self.assertEqual(watch.fmt_time(None), "")


class TestResolve(unittest.TestCase):
    SITES = [
        {"siteNm": "CGV용산아이파크몰", "siteNo": "0013"},
        {"siteNm": "CGV왕십리", "siteNo": "0059"},
        {"siteNm": "CGV영등포", "siteNo": "0056"},
        {"siteNm": "씨네드쉐프 용산", "siteNo": "P013"},
    ]

    def test_exact_match_wins_over_partial(self):
        movies = [{"movNm": "호프"}, {"movNm": "호프집 이야기"}]
        found, problem = watch.resolve("호프", movies, "movNm")
        self.assertEqual(found, {"movNm": "호프"})
        self.assertEqual(problem, "")

    def test_cgv_prefix_is_ignored(self):
        found, _ = watch.resolve("왕십리", self.SITES, "siteNm")
        self.assertEqual(found["siteNo"], "0059")

    def test_ambiguous_query_is_an_error(self):
        # '용산'은 CGV용산아이파크몰과 씨네드쉐프 용산 둘에 걸린다.
        found, problem = watch.resolve("용산", self.SITES, "siteNm")
        self.assertIsNone(found)
        self.assertIn("여러 항목", problem)

    def test_no_match(self):
        found, problem = watch.resolve("없는극장", self.SITES, "siteNm")
        self.assertIsNone(found)
        self.assertEqual(problem, "일치하는 항목이 없습니다")

    def test_empty_query_matches_nothing(self):
        found, problem = watch.resolve("", self.SITES, "siteNm")
        self.assertIsNone(found)
        self.assertEqual(problem, "일치하는 항목이 없습니다")


class TestDiscordConversion(unittest.TestCase):
    def test_bold_is_doubled(self):
        # Discord에서 별 하나는 기울임이라 그대로 보내면 강조가 어긋난다.
        self.assertEqual(watch.to_discord_markdown("*굵게*"), "**굵게**")

    def test_labeled_link(self):
        self.assertEqual(
            watch.to_discord_markdown("<https://cgv.co.kr|예매>"),
            "[예매](https://cgv.co.kr)",
        )

    def test_bare_link_loses_brackets(self):
        self.assertEqual(
            watch.to_discord_markdown("<https://cgv.co.kr>"),
            "https://cgv.co.kr",
        )

    def test_already_doubled_bold_is_untouched(self):
        self.assertEqual(watch.to_discord_markdown("**굵게**"), "**굵게**")


class TestWebhookPayload(unittest.TestCase):
    def test_slack_uses_text(self):
        self.assertEqual(watch.webhook_payload("안녕", "slack"), {"text": "안녕"})

    def test_discord_uses_content_and_converts(self):
        payload = watch.webhook_payload("*굵게*", "discord")
        self.assertEqual(payload, {"content": "**굵게**"})

    def test_discord_truncates_instead_of_failing(self):
        # 2000자를 넘기면 400이 떨어져 알림을 통째로 잃는다 — 잘라 보내는 게 낫다.
        payload = watch.webhook_payload("가" * 3000, "discord")
        self.assertEqual(len(payload["content"]), watch.DISCORD_LIMIT)


class TestShowtimeMinutes(unittest.TestCase):
    """상영 시각을 분으로 펴기. CGV의 24시 이상 표기를 그대로 살린다."""

    def test_both_notations(self):
        self.assertEqual(store.hhmm_minutes("22:10"), 22 * 60 + 10)
        self.assertEqual(store.hhmm_minutes("2210"), 22 * 60 + 10)

    def test_past_midnight_stays_above_24h(self):
        # '2530'을 01:30으로 접으면 "23:00보다 늦은 회차" 비교가 뒤집힌다.
        self.assertEqual(store.hhmm_minutes("2530"), 25 * 60 + 30)
        self.assertGreater(store.hhmm_minutes("2530"), store.hhmm_minutes("2300"))

    def test_unreadable_values(self):
        for bad in ("", None, "abc", "99:99", "2999"):
            self.assertIsNone(store.hhmm_minutes(bad), bad)


class TestNormalizeTimeRange(unittest.TestCase):
    def test_normalizes_both_notations(self):
        self.assertEqual(store.normalize_time_range("1800", "23:30"),
                         ("18:00", "23:30"))

    def test_half_open_range_is_dropped(self):
        self.assertEqual(store.normalize_time_range("18:00", ""), ("", ""))
        self.assertEqual(store.normalize_time_range("", "23:30"), ("", ""))
        self.assertEqual(store.normalize_time_range("", ""), ("", ""))

    def test_overnight_is_kept_as_written(self):
        # 26:00으로 미리 펴지 않는다 — 화면의 <input type=time>이 못 그린다.
        self.assertEqual(store.normalize_time_range("22:00", "02:00"),
                         ("22:00", "02:00"))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            store.normalize_time_range("99:99", "10:00")


class TestWebhookUrlAllowlist(unittest.TestCase):
    """서버가 사용자가 적어 준 주소로 직접 요청을 나가므로, 저장 시점에 막는다.

    막지 않으면 승인된 사용자가 사설망 주소를 넣고 '테스트 전송'을 눌러 내부
    서비스의 응답을 알아낼 수 있다(SSRF).
    """

    def test_slack_and_discord_pass(self):
        for url in ("https://hooks.slack.com/services/T00/B00/xxx",
                    "https://discord.com/api/webhooks/123/abc",
                    "https://discordapp.com/api/webhooks/123/abc",
                    "https://canary.discord.com/api/webhooks/123/abc"):
            self.assertEqual(store.normalize_webhook_url(url), url)

    def test_blank_falls_back_to_global(self):
        self.assertIsNone(store.normalize_webhook_url(""))
        self.assertIsNone(store.normalize_webhook_url("   "))
        self.assertIsNone(store.normalize_webhook_url(None))

    def test_private_and_metadata_addresses_are_rejected(self):
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://127.0.0.1:8787/api/users",
                    "https://192.168.0.1/",
                    "http://[::1]:5432/"):
            with self.assertRaises(ValueError):
                store.normalize_webhook_url(url)

    def test_http_is_rejected_even_for_allowed_host(self):
        with self.assertRaises(ValueError):
            store.normalize_webhook_url("http://hooks.slack.com/services/x")

    def test_lookalike_domain_is_rejected(self):
        # endswith 비교를 문자열로만 하면 통과해 버리는 모양들.
        for url in ("https://evil-discord.com/api/webhooks/1/x",
                    "https://discord.com.attacker.example/x",
                    "https://notslack.com/"):
            with self.assertRaises(ValueError):
                store.normalize_webhook_url(url)

    def test_subdomain_of_allowed_host_passes(self):
        url = "https://ptb.discord.com/api/webhooks/1/x"
        self.assertEqual(store.normalize_webhook_url(url), url)


class TestMessageBuilders(unittest.TestCase):
    def test_new_dates_message_lists_dates_and_link(self):
        body = watch.build_new_dates_message(
            "오디세이", "용산아이파크몰", ["20260817", "20260818"], {}, ["IMAX"],
        )
        self.assertIn("새 IMAX 예매 날짜 오픈", body)
        self.assertIn("*오디세이* · CGV 용산아이파크몰", body)
        self.assertIn("8/17(월), 8/18(화)", body)
        self.assertIn(watch.BOOKING_URL, body)

    def test_new_dates_message_without_filter_omits_screen_name(self):
        body = watch.build_new_dates_message(
            "오디세이", "왕십리", ["20260817"], {}, [],
        )
        self.assertIn("새 예매 날짜 오픈", body)

    def test_new_dates_message_attaches_showtimes(self):
        showtimes = {
            "20260817": [{"movkndDsplNm": "IMAX LASER 2D",
                          "expoScnsNm": "IMAX관", "scnsrtTm": "0630"}]
        }
        body = watch.build_new_dates_message(
            "오디세이", "용산", ["20260817"], showtimes, ["IMAX"],
        )
        self.assertIn("06:30", body)

    def test_open_message_shows_span(self):
        body = watch.build_open_message(
            "오디세이", "용산", ["20260817", "20260820"], ["IMAX"],
        )
        self.assertIn("IMAX 예매 오픈!", body)
        self.assertIn("8/17(월) ~ 8/20(목)", body)
        self.assertIn("총 2일", body)

    def test_open_message_single_date(self):
        body = watch.build_open_message("오디세이", "용산", ["20260817"])
        self.assertIn("8/17(월)", body)
        self.assertNotIn("~", body)

    def test_open_message_with_no_dates_does_not_crash(self):
        body = watch.build_open_message("오디세이", "용산", [])
        self.assertIn("날짜 정보 없음", body)


if __name__ == "__main__":
    unittest.main()
