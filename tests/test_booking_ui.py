#!/usr/bin/env python3
"""자동 예매 UI 구동 헬퍼 (브라우저·DB 불필요).

`booking.hold_block`은 CGV 화면을 직접 몰기 때문에 통째로는 테스트할 수 없다.
그래도 **무엇을 어떤 순서로 찾고, 애매하면 어떻게 하는지**는 브라우저 없이
고정할 수 있고, 그게 이 파일이 지키는 것이다:

  · 해시 클래스명 하나에 매달리지 않는다 (CGV가 다시 빌드하면 사라진다)
  · 날짜를 반드시 고르고, 골라졌는지 확인한다 (안 하면 오늘 상영표를 본다)
  · 어느 회차인지 애매하면 아무거나 누르지 않는다 (엉뚱한 선점은 되돌리기 어렵다)

아래 FakeNode/FakePage는 실제로 쓰는 Playwright locator API만 흉내낸다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import booking  # noqa: E402


# ── 가짜 DOM ────────────────────────────────────────────────────────────────
class FakeNode:
    def __init__(self, tag="span", cls="", text="", visible=True, children=()):
        self.tag = tag
        self.cls = cls
        self.own_text = text
        self.visible = visible
        self.children = list(children)
        self.parent = None
        self.clicks = 0
        for child in self.children:
            child.parent = self

    # -- Playwright locator API 중 우리가 쓰는 것만 --
    def text_content(self):
        if self.own_text:
            return self.own_text
        return " ".join(c.text_content() or "" for c in self.children)

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def click(self, timeout=None):
        self.clicks += 1
        root = self
        while root.parent is not None:
            root = root.parent
        root.clicked.append(self)

    def locator(self, selector):
        if selector.startswith("xpath=ancestor::"):
            return FakeLocator(self._ancestor(selector))
        return FakeLocator(_select(self.descendants(), selector))

    # -- 트리 헬퍼 --
    def descendants(self):
        out = []
        for child in self.children:
            out.append(child)
            out.extend(child.descendants())
        return out

    def _ancestor(self, xpath):
        want_tag = "button" if "ancestor::button" in xpath else None
        frag = None
        found = re.search(r"contains\(@class,\s*'([^']+)'\)", xpath)
        if found:
            frag = found.group(1)
        node = self.parent
        while node is not None:
            if want_tag and node.tag == want_tag:
                return [node]
            if frag and frag in node.cls:
                return [node]
            node = node.parent
        return []


class FakeLocator:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    def all(self):
        return self.nodes

    def count(self):
        return len(self.nodes)

    @property
    def first(self):
        if not self.nodes:
            raise RuntimeError("일치하는 요소가 없습니다")
        return self.nodes[0]

    def click(self, timeout=None):
        self.first.click(timeout=timeout)

    def text_content(self):
        return self.first.text_content()


class FakePage(FakeNode):
    def __init__(self, children=()):
        super().__init__(tag="html", children=children)
        self.clicked: list[FakeNode] = []
        self.waits = 0

    def wait_for_timeout(self, ms):
        self.waits += 1

    def wait_for_selector(self, selector, timeout=None, state=None):
        found = _select(self.descendants(), selector)
        if not [n for n in found if n.is_visible()]:
            raise RuntimeError(f"나타나지 않음: {selector}")
        return found[0]


def _select(nodes, selector):
    """`tag[class*="frag"]`과 `A B` 자손 결합자만 지원하는 아주 작은 매처."""
    parts = selector.split()
    if len(parts) == 2:
        parents = _select(nodes, parts[0])
        out = []
        for parent in parents:
            out.extend(n for n in parent.descendants() if _match(n, parts[1]))
        return out
    return [n for n in nodes if _match(n, selector)]


def _match(node, token):
    found = re.match(r'^([a-z]*)(?:\[class\*="([^"]+)"\])?$', token)
    if not found:
        raise AssertionError(f"테스트 매처가 모르는 셀렉터: {token}")
    tag, frag = found.group(1), found.group(2)
    if tag and node.tag != tag:
        return False
    return frag in node.cls if frag else True


# ── 화면 조각 만들기 ────────────────────────────────────────────────────────
def date_button(number: str, active=False, visible=True) -> FakeNode:
    cls = "dayScroll_scrollItem__IZ35T"
    if active:
        cls += " dayScroll_itemActive__fZ5Sq"
    return FakeNode("button", cls, visible=visible, children=[
        FakeNode("span", "dayScroll_txt__GEtA0", "월"),
        FakeNode("span", "dayScroll_number__o8i9s", number),
    ])


def date_strip(numbers, active=None, with_hidden_twins=True) -> list[FakeNode]:
    """실제 화면처럼 스와이퍼가 숨김 사본을 한 벌 더 만든 날짜 스트립."""
    visible = [date_button(n, active=(n == active)) for n in numbers]
    if not with_hidden_twins:
        return visible
    hidden = [date_button(n, active=(n == active), visible=False)
              for n in numbers]
    return visible + hidden


def showtime(start: str, end: str, screen="IMAX관") -> FakeNode:
    return FakeNode("button", "screenInfo_timeLink cinemaSchedule_scrollItemBtn",
                    children=[
                        FakeNode("span", "screenInfo_start__6BZbu "
                                         "cinemaSchedule_startTime__ZE5Zp", start),
                        FakeNode("span", "cinemaSchedule_endTime__wV0zp", end),
                    ])


def screen_block(name: str, showtimes) -> FakeNode:
    return FakeNode("div", "screenInfoStore_container__XZ7Dy", children=[
        FakeNode("strong", "screenInfo_screenName", name), *showtimes])


# ── 날짜 ────────────────────────────────────────────────────────────────────
class TestDateLabels(unittest.TestCase):
    def test_same_month_uses_bare_day(self):
        self.assertIn("31", booking.date_labels("20260831"))

    def test_next_month_form_is_also_offered(self):
        # 달을 넘기면 스트립이 '9.1'로 적는다.
        self.assertIn("9.1", booking.date_labels("20260901"))
        self.assertIn("1", booking.date_labels("20260901"))

    def test_no_zero_padding(self):
        # 스트립은 '01'이 아니라 '1'로 적는다.
        self.assertEqual(booking.date_labels("20260901")[0], "1")

    def test_bad_date_raises(self):
        for bad in ("", "2026", "abc", None):
            with self.assertRaises(RuntimeError):
                booking.date_labels(bad)


class TestClickDate(unittest.TestCase):
    """날짜를 고르지 않으면 화면은 **오늘**에 머문다 — 이 단계가 없어서
    8/31 감시가 8/25 상영표에서 18:00을 찾다 실패했다."""

    def test_clicks_the_matching_day(self):
        page = FakePage(date_strip(["25", "26", "31"], active="25"))
        # 클릭하면 그 버튼이 활성화되도록 흉내낸다.
        target = [b for b in page.descendants()
                  if b.tag == "button" and b.visible
                  and b.text_content().strip().endswith("31")][0]

        original = target.click

        def activate(timeout=None):
            for b in page.descendants():
                if b.tag == "button" and "dayScroll_scrollItem" in b.cls:
                    b.cls = b.cls.replace(" dayScroll_itemActive__fZ5Sq", "")
            target.cls += " dayScroll_itemActive__fZ5Sq"
            original(timeout=timeout)

        target.click = activate
        booking._click_date(page, "20260831")
        self.assertIs(page.clicked[0], target)

    def test_ignores_hidden_swiper_twins(self):
        # 스와이퍼가 같은 버튼을 숨김 사본으로 한 벌 더 만든다. 숨은 걸 누르면
        # Playwright가 actionability에서 멎는다.
        page = FakePage(date_strip(["31"], active="31"))
        booking._click_date(page, "20260831")
        self.assertTrue(page.clicked[0].visible)

    def test_raises_when_the_day_is_not_on_the_strip(self):
        page = FakePage(date_strip(["25", "26"], active="25"))
        with self.assertRaises(RuntimeError) as caught:
            booking._click_date(page, "20260831")
        self.assertIn("31", str(caught.exception))

    def test_stops_when_the_click_did_not_take(self):
        # 눌렀는데 다른 날짜가 활성인 채로 남아 있으면 진행하면 안 된다 —
        # 그대로 밀고 가면 오늘 상영표에서 회차를 고른다.
        page = FakePage(date_strip(["25", "31"], active="25"))
        with self.assertRaises(RuntimeError) as caught:
            booking._click_date(page, "20260831")
        self.assertIn("선택되지 않았습니다", str(caught.exception))


# ── 회차 ────────────────────────────────────────────────────────────────────
class TestClickShowtime(unittest.TestCase):
    def test_clicks_the_button_around_the_start_time(self):
        wanted = showtime("18:00", "-21:02")
        page = FakePage([screen_block("IMAX관", [showtime("14:30", "-17:32"),
                                                 wanted])])
        booking._click_showtime(page, "18:00")
        self.assertIs(page.clicked[0], wanted)      # 스팬이 아니라 버튼

    def test_does_not_match_an_end_time(self):
        # 버튼 전체 텍스트는 '15:10-18:00'을 품는다. 부분일치로 두면 18:00을
        # 찾다가 15:10 회차를 눌러 **엉뚱한 회차를 선점한다.**
        page = FakePage([screen_block("2D", [showtime("15:10", "-18:00")])])
        with self.assertRaises(RuntimeError):
            booking._click_showtime(page, "18:00")

    def test_disambiguates_by_screen_name(self):
        imax = showtime("21:00", "-24:02")
        cinedechef = showtime("21:00", "-23:57")
        page = FakePage([screen_block("IMAX관", [imax]),
                         screen_block("템퍼 시네마 B[CINE de CHEF]", [cinedechef])])
        booking._click_showtime(page, "21:00", "템퍼 시네마 B[CINE de CHEF]")
        self.assertIs(page.clicked[0], cinedechef)

    def test_refuses_to_guess_between_screens(self):
        # 상영관을 모르면 아무거나 누르지 않는다 — 다른 관의 좌석을 잡느니 만다.
        page = FakePage([screen_block("IMAX관", [showtime("21:00", "-24:02")]),
                         screen_block("2D", [showtime("21:00", "-23:57")])])
        with self.assertRaises(RuntimeError) as caught:
            booking._click_showtime(page, "21:00")
        self.assertIn("멈춥니다", str(caught.exception))
        self.assertEqual(page.clicked, [])

    def test_raises_with_the_time_when_nothing_matches(self):
        page = FakePage([screen_block("IMAX관", [showtime("20:05", "-23:07")])])
        with self.assertRaises(RuntimeError) as caught:
            booking._click_showtime(page, "18:00")
        self.assertIn("18:00", str(caught.exception))

    def test_hidden_showtimes_are_ignored(self):
        hidden = showtime("18:00", "-21:02")
        hidden.visible = False
        for child in hidden.descendants():
            child.visible = False
        visible = showtime("18:00", "-21:02")
        page = FakePage([screen_block("IMAX관", [hidden, visible])])
        booking._click_showtime(page, "18:00")
        self.assertIs(page.clicked[0], visible)


class TestPickSeats(unittest.TestCase):
    """일부만 잡히면 선점하지 않는다.

    2석을 건 감시에서 1석만 잡히면 쓸모없는 좌석을 선점해 두고 그 감시까지
    꺼져서(try_auto_book), 정작 두 자리가 났을 때 아무도 안 잡는다.
    """

    class SeatPage:
        def __init__(self, available):
            self.available = set(available)
            self.clicked: list[str] = []

        def get_by_text(self, label, exact=False):
            page = self

            class Loc:
                @property
                def last(self):
                    return self

                def click(self, timeout=None):
                    if label not in page.available:
                        raise RuntimeError(f"좌석 없음: {label}")
                    page.clicked.append(label)

            return Loc()

    def test_all_seats_clicked_reports_nothing_missed(self):
        page = self.SeatPage(["J22", "J23"])
        self.assertEqual(booking._pick_seats(page, ["J22", "J23"]), [])
        self.assertEqual(page.clicked, ["J22", "J23"])

    def test_reports_every_seat_it_could_not_click(self):
        # 하나가 실패해도 나머지를 마저 눌러 봐야 무엇이 팔렸는지 다 알 수 있다.
        page = self.SeatPage(["J22"])
        self.assertEqual(booking._pick_seats(page, ["J22", "J23", "J24"]),
                         ["J23", "J24"])

    def test_error_message_names_the_missing_seats(self):
        msg = booking._partial_seats_error(["J22", "J23"], ["J23"])
        self.assertIn("J23", msg)
        self.assertIn("2석 중 1석", msg)

    def test_no_seat_clicked_at_all_is_reported_the_same_way(self):
        page = self.SeatPage([])
        missed = booking._pick_seats(page, ["J22", "J23"])
        self.assertEqual(missed, ["J22", "J23"])
        self.assertEqual(page.clicked, [])
        self.assertIn("2석 중 2석",
                      booking._partial_seats_error(["J22", "J23"], missed))


class TestSelectorsAreNotPinnedToHashes(unittest.TestCase):
    """CSS 모듈 해시는 CGV가 다시 빌드하면 바뀐다 — 셀렉터가 거기 매달리면 안 된다."""

    def test_showtime_selectors_use_partial_class_match(self):
        for selector in booking.SHOWTIME_SELECTORS:
            self.assertIn('class*=', selector, selector)

    def test_date_selectors_use_partial_class_match(self):
        for selector in booking.DATE_BUTTON_SELECTORS:
            self.assertIn('class*=', selector, selector)

    def test_showtime_selectors_target_the_start_time_only(self):
        # 끝 시각까지 품은 컨테이너를 보면 '15:10-18:00'이 18:00으로 걸린다.
        for selector in booking.SHOWTIME_SELECTORS:
            self.assertNotIn("endTime", selector)
            self.assertNotIn("timeWrap", selector)


class TestScreenshotIsBestEffort(unittest.TestCase):
    """스크린샷은 부가 기능이다 — 실패해도 예매 실패 경로를 망치면 안 된다."""

    class BrokenPage:
        def screenshot(self, **kwargs):
            raise RuntimeError("브라우저가 죽었다")

    def test_returns_none_instead_of_raising(self):
        self.assertIsNone(
            booking._save_screenshot(self.BrokenPage(),
                                     {"mov_nm": "오디세이", "start_hhmm": "18:00"}))


if __name__ == "__main__":
    unittest.main()
