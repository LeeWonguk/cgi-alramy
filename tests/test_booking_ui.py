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
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import booking  # noqa: E402


# ── 가짜 DOM ────────────────────────────────────────────────────────────────
class FakeNode:
    def __init__(self, tag="span", cls="", text="", visible=True, children=(),
                 attrs=None):
        self.tag = tag
        self.cls = cls
        self.own_text = text
        self.visible = visible
        self.attrs = dict(attrs or {})
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
        if name == "class":
            return self.cls
        return self.attrs.get(name)

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
    """`tag[class*="frag"]`, `A B` 자손 결합자, `A, B` 묶음만 지원하는 매처."""
    if "," in selector:
        # 여러 셀렉터를 한꺼번에 기다리는 경로(booking._wait_for_any). 순서를
        # 지키며 중복만 걸러 낸다.
        out = []
        for part in selector.split(","):
            for node in _select(nodes, part.strip()):
                if node not in out:
                    out.append(node)
        return out
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


def showtime(start: str, end: str, screen="IMAX관",
             soldout=False) -> FakeNode:
    """회차 버튼 하나. soldout이면 실제 화면처럼 aria-disabled를 단다.

    CGV는 매진을 `disabled` 속성이 아니라 aria-disabled로 표시한다(2026-08 실측).
    """
    return FakeNode("button", "screenInfo_timeLink cinemaSchedule_scrollItemBtn",
                    attrs={"aria-disabled": "true"} if soldout else None,
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

    def test_a_soldout_showtime_is_not_clicked(self):
        """매진 버튼을 그냥 누르면 Playwright가 6초를 기다렸다 죽는다.

        CGV는 매진을 aria-disabled로 표시하고, Playwright는 그걸 '실행 불가'로
        본다 — 2026-08-28 실패가 정확히 이 모양이었다("element is not enabled").
        """
        page = FakePage([screen_block("IMAX관",
                                      [showtime("25:00", "-28:02", soldout=True)])])
        with self.assertRaises(booking._ShowtimeBlocked):
            booking._click_showtime(page, "25:00")
        self.assertEqual(page.clicked, [], "매진인 걸 알고도 눌렀다")


class TestStaleShowtimeList(unittest.TestCase):
    """미리 띄워 둔 화면의 회차 목록은 그때의 스냅샷이다.

    2026-08-28 실측: 15:03:07에 띄운 탭을 15:04:58에 그대로 썼는데, 그 사이
    좌석이 나서 자동 예매가 돌았다. 화면은 여전히 '매진'이었고 클릭은 6초를
    기다렸다 죽었다. 탭은 세션 재기동(30분)에만 다시 열려 최대 30분 낡는다.

    좌석이 났다는 건 방금 API로 확인한 사실이므로, 화면이 매진이라고 하면
    **화면 쪽을 의심한다.**
    """

    def setUp(self):
        for name in ("DATE_SWITCH_MS", "SHOWTIME_STALE_MS"):
            self.addCleanup(setattr, booking, name, getattr(booking, name))
            setattr(booking, name, 50)

    def build(self, *, soldout: bool, revive: bool):
        """매진으로 그려진 화면. revive면 날짜를 다시 고를 때 목록이 되살아난다."""
        target = showtime("25:00", "-28:02", soldout=soldout)
        page = FakePage(date_strip(["28", "29"], active="28")
                        + [screen_block("IMAX관", [target])])

        def activate(button):
            original = button.click

            def go(timeout=None):
                for b in page.descendants():
                    if b.tag == "button" and "dayScroll_scrollItem" in b.cls:
                        b.cls = b.cls.replace(" dayScroll_itemActive__fZ5Sq", "")
                button.cls += " dayScroll_itemActive__fZ5Sq"
                if revive and button.text_content().strip().endswith("28"):
                    # 목록을 다시 받아 오니 매진이 풀렸다.
                    target.attrs.pop("aria-disabled", None)
                original(timeout=timeout)

            button.click = go

        for b in page.descendants():
            if b.tag == "button" and "dayScroll_scrollItem" in b.cls and b.visible:
                activate(b)
        return page, target

    def test_a_stale_soldout_is_refreshed_and_then_clicked(self):
        page, target = self.build(soldout=True, revive=True)
        booking._click_showtime(page, "25:00", "IMAX관", "20260828")
        self.assertIn(target, page.clicked, "목록을 다시 받고도 회차를 못 눌렀다")

    def test_a_real_soldout_survives_the_refresh_and_fails_clearly(self):
        """다시 받아도 매진이면 정말 매진이다 — 사유가 그렇게 적혀야 한다."""
        page, target = self.build(soldout=True, revive=False)
        with self.assertRaises(RuntimeError) as caught:
            booking._click_showtime(page, "25:00", "IMAX관", "20260828")
        msg = str(caught.exception)
        self.assertIn("매진", msg)
        self.assertIn("다시 받아", msg)
        self.assertNotIn(target, page.clicked)

    def test_an_available_showtime_never_pays_for_a_refresh(self):
        # 잘 되던 경로는 그대로여야 한다 — 날짜를 건드리지 않는다.
        page, target = self.build(soldout=False, revive=False)
        booking._click_showtime(page, "25:00", "IMAX관", "20260828")
        self.assertEqual(page.clicked, [target],
                         "멀쩡한 회차인데 날짜를 옮겼다")

    def test_without_a_date_it_reports_instead_of_guessing(self):
        # 날짜를 모르면 목록을 다시 받을 수 없다 — 조용히 넘어가지 않는다.
        page, _ = self.build(soldout=True, revive=True)
        with self.assertRaises(booking._ShowtimeBlocked):
            booking._click_showtime(page, "25:00", "IMAX관")


class TestRefreshShowtimes(unittest.TestCase):
    """목록을 다시 받는 길은 날짜 왕복이다 — 되돌아온 날짜를 반드시 확인한다."""

    def setUp(self):
        # 실패 경로가 실제 상한만큼 자면 테스트가 몇 초씩 멎는다.
        for name in ("DATE_SWITCH_MS", "SHOWTIME_STALE_MS"):
            self.addCleanup(setattr, booking, name, getattr(booking, name))
            setattr(booking, name, 50)

    def test_it_bounces_to_another_date_and_back(self):
        page = FakePage(date_strip(["28", "29"], active="28"))
        for b in page.descendants():
            if b.tag == "button" and "dayScroll_scrollItem" in b.cls and b.visible:
                def make(button):
                    original = button.click

                    def go(timeout=None):
                        for n in page.descendants():
                            if n.tag == "button" and "dayScroll_scrollItem" in n.cls:
                                n.cls = n.cls.replace(
                                    " dayScroll_itemActive__fZ5Sq", "")
                        button.cls += " dayScroll_itemActive__fZ5Sq"
                        original(timeout=timeout)

                    button.click = go
                make(b)

        self.assertTrue(booking._refresh_showtimes(page, "20260828"))
        pressed = [n.text_content().strip()[-2:] for n in page.clicked]
        self.assertEqual(pressed, ["29", "28"], "다른 날짜로 갔다 되돌아와야 한다")

    def test_a_single_date_strip_cannot_be_refreshed(self):
        # 옮겨 갈 날짜가 없으면 거짓을 돌려준다 — 같은 날짜 재클릭은 무효다(실측).
        page = FakePage(date_strip(["28"], active="28"))
        self.assertFalse(booking._refresh_showtimes(page, "20260828"))
        self.assertEqual(page.clicked, [])

    def test_a_bounce_that_does_not_come_back_is_reported(self):
        """되돌아오지 못하면 실패다.

        여기서 참을 돌려주면 **다른 날짜의 같은 시각 회차를 선점한다.**
        """
        page = FakePage(date_strip(["28", "29"], active="28"))
        self.assertFalse(booking._refresh_showtimes(page, "20260828"))


class TestClickShowtimeMore(unittest.TestCase):
    def test_raises_with_the_time_when_nothing_matches(self):
        page = FakePage([screen_block("IMAX관", [showtime("20:05", "-23:07")])])
        with self.assertRaises(RuntimeError) as caught:
            booking._click_showtime(page, "18:00")
        self.assertIn("18:00", str(caught.exception))

    def test_disambiguates_screens_nested_in_one_outer_container(self):
        """실제 화면 회귀: 두 상영관이 바깥 컨테이너 하나를 공유한다.

        2026-08-28 용산 10:25에 '17관[PREMIUM] (Laser)'(12석)와
        '17관 (Laser)'(156석)가 함께 있었다. 바깥 컨테이너는 둘을 모두 품어서
        어느 후보로 물어도 이름이 걸린다 — 그 층에서 판단하면 아무것도 못 가리고,
        안전장치가 "가리지 못했습니다"로 멈춰 잡을 수 있는 좌석을 놓친다.
        """
        premium = showtime("10:25", "-12:26")
        regular = showtime("10:25", "-12:26")
        page = FakePage([
            FakeNode("div", "screenInfoStore_container__XZ7Dy", children=[
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName",
                             "17관[PREMIUM] (Laser)"),
                    premium]),
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName", "17관 (Laser)"),
                    regular]),
            ])])

        booking._click_showtime(page, "10:25", "17관 (Laser)")
        self.assertIs(page.clicked[0], regular,
                      "PREMIUM관을 잡았다 — 좌석 수가 전혀 다른 관이다")

    def test_premium_screen_is_reachable_too(self):
        premium = showtime("10:25", "-12:26")
        regular = showtime("10:25", "-12:26")
        page = FakePage([
            FakeNode("div", "screenInfoStore_container__XZ7Dy", children=[
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName",
                             "17관[PREMIUM] (Laser)"),
                    premium]),
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName", "17관 (Laser)"),
                    regular]),
            ])])

        booking._click_showtime(page, "10:25", "17관[PREMIUM] (Laser)")
        self.assertIs(page.clicked[0], premium)

    def test_screen_name_matching_ignores_spacing(self):
        # text_content()가 조각을 이어 붙이면 공백이 어긋난다.
        a = showtime("21:00", "-23:30")
        b = showtime("21:00", "-23:30")
        page = FakePage([
            FakeNode("div", "screenInfoStore_container__XZ7Dy", children=[
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName", "1관(Laser)"), a]),
                FakeNode("div", "screenInfo_contentWrap__95SyT", children=[
                    FakeNode("strong", "screenInfo_screenName", "2관(Laser)"), b]),
            ])])
        booking._click_showtime(page, "21:00", "2관 (Laser)")
        self.assertIs(page.clicked[0], b)

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
        def __init__(self, available, *, blocked_until: int = 0):
            self.available = set(available)
            self.clicked: list[str] = []
            # 0이면 막힘 없음. n이면 앞의 n번 클릭이 오버레이에 가로막힌다 —
            # 실제 화면에서 modal-bg가 포인터를 먹던 상황을 그대로 흉내 낸다.
            self.blocked_until = blocked_until
            self.click_attempts = 0
            self.dismissed = 0

        def get_by_text(self, label, exact=False):
            page = self

            class Loc:
                @property
                def last(self):
                    return self

                def click(self, timeout=None):
                    page.click_attempts += 1
                    if page.click_attempts <= page.blocked_until:
                        raise RuntimeError(
                            '<div class="modal-bg"></div> intercepts pointer '
                            'events')
                    if label not in page.available:
                        raise RuntimeError(f"좌석 없음: {label}")
                    page.clicked.append(label)

            return Loc()

        # _wait_for_loading·dismiss_modals가 쓰는 최소한의 표면. 둘 다 "덮은 게
        # 없다"로 답하게 해 두고, 오버레이는 위 click에서만 흉내 낸다.
        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector, **kwargs):
            page = self

            class Empty:
                def count(self):
                    return 0

                def all(self):
                    return []

                @property
                def first(self):
                    return self

                def is_visible(self):
                    return False

            return Empty()

    def test_all_seats_clicked_reports_nothing_missed(self):
        page = self.SeatPage(["J22", "J23"])
        self.assertEqual(booking._pick_seats(page, ["J22", "J23"]),
                         (["J22", "J23"], [], "", False))
        self.assertEqual(page.clicked, ["J22", "J23"])

    def test_reports_every_seat_it_could_not_click(self):
        # 하나가 실패해도 나머지를 마저 눌러 봐야 무엇이 팔렸는지 다 알 수 있다.
        page = self.SeatPage(["J22"])
        self.assertEqual(booking._pick_seats(page, ["J22", "J23", "J24"]),
                         (["J22"], ["J23", "J24"], "", False))

    def test_reports_what_it_managed_to_click(self):
        # 다시 고를 수 있는지가 여기에 달렸다 — 이미 골라 둔 게 있으면 못 고친다.
        page = self.SeatPage(["J23"])
        clicked, missed, _, _ = booking._pick_seats(page, ["J22", "J23"])
        self.assertEqual(clicked, ["J23"])
        self.assertEqual(missed, ["J22"])

    def test_a_blocking_overlay_is_closed_and_the_click_retried(self):
        """오버레이는 사고라 닫고 다시 누르면 풀린다 — 그 좌석을 포기하면 안 된다."""
        page = self.SeatPage(["J22", "J23"], blocked_until=1)
        clicked, missed, notice, blocked = booking._pick_seats(
            page, ["J22", "J23"])
        self.assertEqual(clicked, ["J22", "J23"])
        self.assertEqual(missed, [])
        self.assertFalse(blocked)
        self.assertEqual(notice, "")

    def test_seats_under_a_stuck_overlay_are_not_hammered(self):
        """닫아도 안 걷히면 남은 좌석은 누르지 않는다.

        실측에서 좌석 둘에 3초씩 버리고 재시도까지 돌아 42.9초를 썼다. 그 아래
        좌석은 어차피 같은 것에 막히므로 시도 자체가 낭비다.
        """
        page = self.SeatPage(["K1", "K2", "K3"], blocked_until=99)
        clicked, missed, notice, blocked = booking._pick_seats(
            page, ["K1", "K2", "K3"])
        self.assertTrue(blocked)
        self.assertEqual(clicked, [])
        self.assertEqual(missed, ["K1", "K2", "K3"])
        self.assertEqual(notice, "", "오버레이는 좌석 안내 팝업이 아니다")
        # 첫 좌석에 두 번(원클릭 + 팝업 닫고 재시도)까지가 전부다.
        self.assertEqual(page.click_attempts, 2,
                         "막힌 걸 알고도 남은 좌석을 계속 눌렀다")

    def test_error_message_names_the_missing_seats(self):
        msg = booking._partial_seats_error(["J22", "J23"], ["J23"])
        self.assertIn("J23", msg)
        self.assertIn("2석 중 1석", msg)

    def test_no_seat_clicked_at_all_is_reported_the_same_way(self):
        page = self.SeatPage([])
        clicked, missed, _, _ = booking._pick_seats(page, ["J22", "J23"])
        self.assertEqual(missed, ["J22", "J23"])
        self.assertEqual(clicked, [])
        self.assertEqual(page.clicked, [])
        self.assertIn("2석 중 2석",
                      booking._partial_seats_error(["J22", "J23"], missed))


def seat_row(label: str, x: int, available: bool) -> dict:
    """pick_block이 읽는 모양의 좌석 한 자리."""
    i = 0
    while i < len(label) and not label[i].isdigit():
        i += 1
    return {"row": label[:i], "no": label[i:], "label": label,
            "available": available, "kind": "", "zone": "",
            "x_start": x, "x_end": x + 2,
            "left_pway": False, "right_pway": False,
            "seat_loc_no": f"LOC{label}", "sbord_no": "001",
            "seat_area_no": "001", "szone_no": "01001", "stknd_cd": "27",
            "szone_kind_cd": "01", "seat_salfrm_cd": "01"}


def seat_map(available) -> list[dict]:
    """K1~K6 한 줄. available에 든 라벨만 비어 있다."""
    free = set(available)
    return [seat_row(f"K{i}", i * 2, f"K{i}" in free) for i in range(1, 7)]


class TestSelectBlockRepicksAtTheSeatMap(unittest.TestCase):
    """감지 때 고른 좌석은 좌석맵에 닿을 무렵이면 낡아 있다.

    UI를 모는 데 30초 남짓이 걸리는데 그동안 취소표는 팔린다. 그래서 좌석맵에
    도착해서 배치도를 다시 읽고 그 자리에서 고른다.
    """

    def setUp(self) -> None:
        # 스크린샷은 부가 기능이라 여기서는 저장 여부만 세고 디스크는 건드리지 않는다.
        self.shots: list[dict] = []
        self._real_shot = booking._save_screenshot
        booking._save_screenshot = lambda page, ctx: (
            self.shots.append(ctx) or "/tmp/fake.png")
        self.addCleanup(setattr, booking, "_save_screenshot", self._real_shot)

    def ctx(self, candidate, party=2):
        return {"mov_nm": "오디세이", "site_nm": "용산", "scn_ymd": "20260831",
                "start_hhmm": "18:00", "seat_labels": list(candidate),
                "party": party, "rows": ["K"], "site_no": "0013",
                "row": {"scnsNo": "S1", "scnSseq": "3"}}

    def test_uses_the_live_map_not_the_candidate(self):
        # 감지 때는 K1·K2였지만 그 사이 팔리고 K5·K6이 났다.
        page = TestPickSeats.SeatPage(["K5", "K6"])
        out = booking._select_block(
            None, page, self.ctx(["K1", "K2"]),
            seats_fn=lambda s, c: seat_map(["K5", "K6"]))

        self.assertTrue(out["ok"])
        self.assertEqual(out["labels"], ["K5", "K6"])
        self.assertEqual(page.clicked, ["K5", "K6"],
                         "낡은 후보를 눌렀다")

    def test_retries_with_a_fresh_block_when_nothing_was_clicked(self):
        # 첫 바퀴의 K1·K2는 화면에서 이미 사라졌고, 두 바퀴째엔 K5·K6이 보인다.
        page = TestPickSeats.SeatPage(["K5", "K6"])
        maps = iter([seat_map(["K1", "K2"]), seat_map(["K5", "K6"])])
        out = booking._select_block(None, page, self.ctx(["K1", "K2"]),
                                    seats_fn=lambda s, c: next(maps))

        self.assertTrue(out["ok"])
        self.assertEqual(out["labels"], ["K5", "K6"])
        self.assertEqual(len(self.shots), 1, "첫 실패의 화면은 남겨야 한다")

    def test_stops_when_some_seats_are_already_selected(self):
        # K5는 골라졌고 K6은 밀렸다. 여기서 다른 블록을 누르면 인원수를 넘겨
        # 엉뚱한 자리를 선점하게 되므로 다시 고르지 않는다.
        page = TestPickSeats.SeatPage(["K5"])
        calls = {"n": 0}

        def maps(s, c):
            calls["n"] += 1
            return seat_map(["K5", "K6"])

        out = booking._select_block(None, page, self.ctx(["K5", "K6"]),
                                    seats_fn=maps)

        self.assertFalse(out["ok"])
        self.assertEqual(calls["n"], 1, "이미 고른 좌석이 있는데 다시 골랐다")
        self.assertEqual(page.clicked, ["K5"])
        self.assertIn("K6", out["error"])

    def test_gives_up_when_the_block_is_gone(self):
        page = TestPickSeats.SeatPage([])
        out = booking._select_block(None, page, self.ctx(["K1", "K2"]),
                                    seats_fn=lambda s, c: seat_map(["K1"]))

        self.assertFalse(out["ok"])
        self.assertIn("사라졌습니다", out["error"])
        self.assertEqual(page.clicked, [], "잡을 수 없는데 눌러 봤다")

    def test_falls_back_to_the_candidate_when_the_map_cannot_be_read(self):
        # 배치도를 다시 못 읽어도 후보로 시도는 해 본다 — 낡았을 수 있지만
        # 아무것도 안 하는 것보다 낫다.
        page = TestPickSeats.SeatPage(["K1", "K2"])

        def boom(s, c):
            raise RuntimeError("좌석 배치도 조회 실패")

        out = booking._select_block(None, page, self.ctx(["K1", "K2"]),
                                    seats_fn=boom)
        self.assertTrue(out["ok"])
        self.assertEqual(page.clicked, ["K1", "K2"])

    def test_blind_attempt_is_not_retried(self):
        # 다시 읽지도 못하는데 같은 좌석을 세 번 눌러 봐야 답은 같다.
        page = TestPickSeats.SeatPage([])
        calls = {"n": 0}

        def boom(s, c):
            calls["n"] += 1
            raise RuntimeError("좌석 배치도 조회 실패")

        out = booking._select_block(None, page, self.ctx(["K1", "K2"]),
                                    seats_fn=boom)
        self.assertFalse(out["ok"])
        self.assertEqual(calls["n"], 1)

    def test_seat_click_failure_saves_a_screenshot(self):
        # 이 경로만 화면을 안 남기고 있어서, 셀렉터가 깨진 건지 정말 팔린 건지
        # 사후에 가릴 수가 없었다.
        page = TestPickSeats.SeatPage([])
        booking._select_block(None, page, self.ctx(["K1", "K2"]),
                              seats_fn=lambda s, c: seat_map(["K1", "K2"]))
        self.assertEqual(len(self.shots), 1)
        self.assertEqual(self.shots[0]["start_hhmm"], "18:00")

    def test_a_stuck_overlay_stops_the_retries(self):
        """오버레이에 막혔으면 같은 블록을 다시 고르지 않는다.

        다시 골라 봐야 그 아래를 누르는 건 마찬가지라 좌석 수만큼 타임아웃을 또
        버린다 — 실측 44.5초(좌석고르기 42.9초)가 이 경로였다.
        """
        page = TestPickSeats.SeatPage(["K5", "K6"], blocked_until=99)
        calls = []

        def seats_fn(session, ctx):
            calls.append(1)
            return seat_map(["K5", "K6"])

        out = booking._select_block(
            None, page, self.ctx(["K5", "K6"]), seats_fn=seats_fn)

        self.assertFalse(out["ok"])
        self.assertIn("팝업에 덮여", out["error"])
        self.assertNotIn("팔린", out["error"],
                         "좌석은 멀쩡했다 — 팔렸다고 하면 사용자가 헛짚는다")
        self.assertEqual(len(calls), 1, "막힌 걸 알고도 좌석을 다시 골랐다")
        self.assertEqual(page.clicked, [])
        self.assertTrue(self.shots, "원인을 좁힐 화면을 안 남겼다")

    def test_retry_count_is_bounded(self):
        page = TestPickSeats.SeatPage([])          # 무엇을 골라도 못 누른다
        calls = {"n": 0}

        def maps(s, c):
            calls["n"] += 1
            return seat_map(["K1", "K2", "K3", "K4", "K5", "K6"])

        out = booking._select_block(None, page, self.ctx(["K1", "K2"]),
                                    seats_fn=maps)
        self.assertFalse(out["ok"])
        self.assertEqual(calls["n"], booking.SEAT_PICK_ATTEMPTS)
        self.assertEqual(len(self.shots), 1, "화면은 첫 실패에 한 번만")


class TestBookingUrl(unittest.TestCase):
    """예매 화면 딥링크 — 영화·극장·날짜를 주소로 넘긴다."""

    def url(self):
        return booking.booking_url("30001323", "0013", "용산아이파크몰", "20260831")

    def test_carries_every_parameter_the_page_reads(self):
        from urllib.parse import parse_qs, urlsplit

        q = parse_qs(urlsplit(self.url()).query)
        self.assertEqual(q["movNo"], ["30001323"])
        self.assertEqual(q["siteNo"], ["0013"])
        self.assertEqual(q["scnYmd"], ["20260831"])

    def test_includes_the_theater_name(self):
        # siteNo만으로는 화면이 극장을 고르지 못한다 — 실측으로 확인한 조건이라
        # 누가 '코드만 있으면 되겠지' 하고 지우지 않게 고정해 둔다.
        from urllib.parse import parse_qs, urlsplit

        self.assertEqual(
            parse_qs(urlsplit(self.url()).query)["siteNm"], ["용산아이파크몰"])

    def test_theater_name_is_percent_encoded(self):
        self.assertNotIn("용산", self.url())
        self.assertIn("%EC%9A%A9%EC%82%B0", self.url())

    def test_points_at_the_booking_page(self):
        self.assertTrue(self.url().startswith(booking.BOOKING_PAGE + "?"))


class TestDirectOpenFallsBackWhenUnsure(unittest.TestCase):
    """딥링크가 먹었는지 확인되지 않으면 예전 클릭 경로로 돌아간다.

    잘못된 날짜를 선점하는 것보다 10초 더 쓰는 편이 훨씬 낫다.
    """

    class DatePage:
        """날짜 스트립만 흉내낸다. active에 든 날이 선택된 상태."""

        def __init__(self, days, active, goto_fails=False):
            self.days, self.active = days, set(active)
            self.goto_fails = goto_fails
            self.visited = []

        def goto(self, url, **kw):
            if self.goto_fails:
                raise RuntimeError("접속 실패")
            self.visited.append(url)

        def wait_for_selector(self, selector, **kw):
            if "dayScroll" not in selector:
                raise RuntimeError("없음")

        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector):
            page = self

            class Btn:
                def __init__(self, day):
                    self.day = day

                def is_visible(self):
                    return True

                def get_attribute(self, name):
                    return ("dayScroll_scrollItem itemActive"
                            if self.day in page.active else "dayScroll_scrollItem")

                def locator(self, sel):
                    day = self.day

                    class N:
                        @property
                        def first(self):
                            return self

                        def text_content(self):
                            return day
                    return N()

            class Loc:
                def all(self):
                    return ([Btn(d) for d in page.days]
                            if "dayScroll" in selector else [])
            return Loc()

    def ctx(self, ymd="20260831"):
        return {"mov_no": "30001323", "site_no": "0013",
                "site_nm": "용산아이파크몰", "scn_ymd": ymd}

    def test_uses_the_link_when_the_date_is_confirmed(self):
        page = self.DatePage(["30", "31", "9.1"], active=["31"])
        self.assertTrue(booking._open_booking_direct(page, self.ctx()))
        self.assertIn("movNo=30001323", page.visited[0])

    def test_falls_back_when_another_date_is_selected(self):
        # 링크는 8/31인데 화면은 30일에 머물러 있다 — 그대로 가면 엉뚱한 날짜다.
        page = self.DatePage(["30", "31"], active=["30"])
        self.assertFalse(booking._open_booking_direct(page, self.ctx()))

    def test_falls_back_when_no_date_looks_selected(self):
        # 활성 표시를 못 찾으면 '맞다'고 볼 근거가 없다.
        page = self.DatePage(["30", "31"], active=[])
        self.assertFalse(booking._open_booking_direct(page, self.ctx()))

    def test_falls_back_when_navigation_fails(self):
        page = self.DatePage(["31"], active=["31"], goto_fails=True)
        self.assertFalse(booking._open_booking_direct(page, self.ctx()))

    def test_falls_back_without_a_movie_code(self):
        # 코드가 없으면 주소를 만들 수 없다 (이름으로만 건 감시 등).
        page = self.DatePage(["31"], active=["31"])
        ctx = self.ctx()
        ctx["mov_no"] = ""
        self.assertFalse(booking._open_booking_direct(page, ctx))
        self.assertEqual(page.visited, [], "주소도 못 만드는데 접속했다")

    def test_month_crossing_date_is_matched(self):
        page = self.DatePage(["31", "9.1"], active=["9.1"])
        self.assertTrue(booking._open_booking_direct(page, self.ctx("20260901")))


class TestClickVisibleSkipsHiddenTwins(unittest.TestCase):
    """8/25 실패 재현: `.first`가 숨겨진 사본을 잡아 타임아웃까지 기다렸다.

    스와이퍼와 접힌 바텀시트가 같은 제목을 한 벌 더 만들어 둔다.
    """

    class TitlePage:
        def __init__(self, nodes):
            self.nodes = nodes          # [(텍스트, 보이는가), ...]
            self.clicked = []

        def wait_for_selector(self, selector, **kw):
            pass

        def get_by_text(self, text, exact=False):
            page = self
            hits = [(t, vis) for t, vis in page.nodes
                    if (t == text if exact else text in t)]

            class Node:
                def __init__(self, label, vis):
                    self.label, self.vis = label, vis

                def is_visible(self):
                    return self.vis

                def click(self, timeout=None):
                    if not self.vis:
                        raise RuntimeError("element is not visible")
                    page.clicked.append(self.label)

            class Loc:
                def all(self):
                    return [Node(t, v) for t, v in hits]

                def count(self):
                    return len(hits)

                @property
                def first(self):
                    return Node(*hits[0])
            return Loc()

    def test_clicks_the_visible_copy_not_the_hidden_first_one(self):
        page = self.TitlePage([("오디세이", False), ("오디세이", True)])
        booking._click_visible(page, "오디세이", exact=True, what="영화")
        self.assertEqual(page.clicked, ["오디세이"])

    def test_reports_clearly_when_every_copy_is_hidden(self):
        page = self.TitlePage([("오디세이", False), ("오디세이", False)])
        with self.assertRaises(RuntimeError) as caught:
            booking._click_visible(page, "오디세이", exact=True, what="영화")
        msg = str(caught.exception)
        self.assertIn("영화", msg)
        self.assertIn("숨겨진 것 2개", msg)
        self.assertEqual(page.clicked, [])


class TestSeatNoticeIsReportedHonestly(unittest.TestCase):
    """좌석 종류가 요구하는 인원 단위가 안 맞으면 그렇다고 말해야 한다.

    실제로 씨네드쉐프에서 3석을 잡으려다 '패밀리 리클라이너는 4인 단위로 인원을
    선택해주세요'가 떴다. 이걸 안 읽으면 "그 사이 팔린 것 같습니다"라고 보고하는데,
    좌석은 멀쩡히 비어 있으므로 사용자가 할 일이 전혀 다르다.
    """

    class NoticePage(TestPickSeats.SeatPage):
        """첫 좌석을 누르면 안내 팝업이 떠 나머지를 못 누르는 화면."""

        def __init__(self, available, notice):
            super().__init__(available)
            self.notice = notice
            self.shown = False

        def get_by_text(self, label, exact=False):
            if self.shown:                      # 팝업이 덮고 있으면 못 누른다
                page = self

                class Blocked:
                    @property
                    def last(self):
                        return self

                    def click(self, timeout=None):
                        raise RuntimeError("팝업이 가림")
                return Blocked()
            loc = super().get_by_text(label, exact=exact)
            page = self
            inner = loc.last

            class Wrap:
                @property
                def last(self):
                    return self

                def click(self, timeout=None):
                    inner.click(timeout=timeout)
                    page.shown = True
            return Wrap()

        def evaluate(self, script):
            return [self.notice] if self.shown else []

        def locator(self, selector):
            page = self

            class Loc:
                def all(self):
                    return []
            return Loc()

        def get_by_role(self, role, name=None, exact=False):
            page = self

            class Btn:
                def is_visible(self):
                    return page.shown

                def click(self, timeout=None):
                    page.shown = False

            class Loc:
                def all(self):
                    return [Btn()] if page.shown else []
            return Loc()

        def wait_for_timeout(self, ms):
            pass

    NOTICE = "선택하신 패밀리 리클라이너는 4인 단위로 인원을 선택해주세요. H1,H2,H3,H4"

    def test_notice_is_returned_instead_of_a_sold_out_guess(self):
        page = self.NoticePage(["H1", "H2", "H3"], self.NOTICE)
        clicked, missed, notice, _ = booking._pick_seats(
            page, ["H1", "H2", "H3"])
        self.assertEqual(clicked, ["H1"])
        self.assertEqual(missed, ["H2", "H3"])
        self.assertIn("4인 단위", notice)

    def test_remaining_seats_are_not_hammered_after_a_notice(self):
        # 팝업이 화면을 덮은 뒤 남은 좌석을 계속 눌러 봐야 좌석당 3초씩 버릴 뿐이다.
        page = self.NoticePage(["H1", "H2", "H3"], self.NOTICE)
        booking._pick_seats(page, ["H1", "H2", "H3"])
        self.assertEqual(page.clicked, ["H1"], "팝업이 뜬 뒤에도 계속 눌렀다")

    def test_select_block_reports_the_notice(self):
        page = self.NoticePage(["H1", "H2", "H3"], self.NOTICE)
        booking._save_screenshot = lambda p, c: None
        out = booking._select_block(
            None, page, {"party": 3, "rows": None, "seat_labels": ["H1", "H2", "H3"],
                         "mov_nm": "오디세이", "start_hhmm": "22:20"},
            seats_fn=lambda s, c: [
                seat_row("H1", 1, True), seat_row("H2", 3, True),
                seat_row("H3", 5, True)])
        self.assertFalse(out["ok"])
        self.assertIn("4인 단위", out["error"])
        self.assertNotIn("팔린 것", out["error"])

    def test_seat_map_itself_is_not_mistaken_for_a_notice(self):
        # 좌석맵도 큰 모달로 뜬다 — 좌석 라벨이 잔뜩 든 건 안내가 아니다.
        class MapPage:
            def evaluate(self, script):
                return ["씨네드쉐프 용산 닫기 " + " ".join(
                    f"{r}{n}" for r in "ABCDEFGH" for n in range(1, 9))]
        self.assertEqual(booking.seat_notice(MapPage()), "")


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


class TestHoldExpiryIsKoreanTime(unittest.TestCase):
    """선점 만료는 CGV가 준 **한국 시각**이다.

    회귀: `.astimezone()`으로 시간대를 붙이면 실행 환경 기준이라, UTC 컨테이너에서
    같은 숫자가 9시간 뒤를 가리킨다. 그러면 store.active_hold()가 이미 끝난
    선점을 유효하다고 봐서 그 감시는 영영 다시 잡지 않는다.
    """

    def test_parsed_instant_is_kst_regardless_of_local_timezone(self):
        from datetime import datetime, timezone

        got = booking._parse_limit_dt("20260825160018")
        self.assertIsNotNone(got)
        # 2026-08-25 16:00:18 KST == 07:00:18 UTC. 서버 시계와 무관하다.
        self.assertEqual(
            got.astimezone(timezone.utc),
            datetime(2026, 8, 25, 7, 0, 18, tzinfo=timezone.utc))

    def test_alert_prints_korean_wall_clock(self):
        msg = booking.build_hold_alert(
            "오디세이", "용산", "20260825", "22:10", ["A4", "A5"],
            booking._parse_limit_dt("20260825160018"), 28000)
        self.assertIn("16:00까지", msg)

    def test_unparsable_limit_is_none(self):
        self.assertIsNone(booking._parse_limit_dt(""))
        self.assertIsNone(booking._parse_limit_dt("20261332000000"))


class TestSeatOutsideTheVisibleMap(unittest.TestCase):
    """좌석맵 밖에 있는 좌석도 잡아야 한다.

    2026-08-28 용산아이파크몰 IMAX관 실측: 624석 중 한 번에 보이는 건 600px
    폭뿐이고(내용은 2090px), 미는 방식이 transform이라 Playwright의 자동
    스크롤로는 왼쪽 끝에 닿지 못한다. 못 잡던 좌석이 전부 K1·L1처럼 번호
    1~2번이었던 것이 그 증거다.
    """

    class MapPage:
        def __init__(self, plan):
            self.plan = plan          # _SEAT_REACH_JS가 돌려줄 값
            self.fp_after = None      # 클릭 뒤의 지문
            self.mouse = self
            self.clicked_at = []
            self.locator_clicks = []

        # page.mouse.click
        def click(self, x, y):
            self.clicked_at.append((x, y))

        def evaluate(self, script, arg=None):
            if "fingerprint" in script or "reachable" in script:
                return self.plan
            return self.fp_after

        def get_by_text(self, label, exact=False):
            page = self

            class Loc:
                @property
                def last(self):
                    return self

                def click(self, timeout=None):
                    page.locator_clicks.append(label)

            return Loc()

        def wait_for_timeout(self, ms):
            pass

        def locator(self, selector, **kwargs):
            class Empty:
                def count(self): return 0
                def all(self): return []
                @property
                def first(self): return self
                def is_visible(self): return False
            return Empty()

    def test_a_seat_already_in_view_takes_the_proven_path(self):
        """잘 되고 있는 경로를 건드리지 않는다 — 좌표 클릭으로 갈아타지 않는다."""
        page = self.MapPage({"reachable": True, "panned": False,
                             "x": 100, "y": 200, "fingerprint": "a|b"})
        booking._click_seat(page, "A17")
        self.assertEqual(page.locator_clicks, ["A17"])
        self.assertEqual(page.clicked_at, [], "밀 필요가 없는데 좌표로 눌렀다")

    def test_an_off_screen_seat_is_panned_in_and_clicked_by_point(self):
        page = self.MapPage({"reachable": True, "panned": True,
                             "x": 1280, "y": 371, "moved": [859, 0],
                             "fingerprint": "seat|row|map"})
        page.fp_after = "seat selected|row|map"      # 눌려서 클래스가 바뀌었다
        booking._click_seat(page, "A3")
        self.assertEqual(page.clicked_at, [(1280, 371)])
        self.assertEqual(page.locator_clicks, [],
                         "민 좌석을 locator.click으로 누르면 위치가 흐트러진다")

    def test_a_point_click_that_did_not_take_is_an_error(self):
        """좌표 클릭은 빗나가도 예외를 내지 않는다 — 확인이 없으면 조용히 샌다.

        안 눌린 좌석을 눌렀다고 세면 인원수를 채운 줄 알고 넘어가, 결국 엉뚱한
        자리를 선점한다. 일부만 잡느니 멈추는 게 이 코드의 원칙이다.
        """
        page = self.MapPage({"reachable": True, "panned": True,
                             "x": 1280, "y": 371, "moved": [859, 0],
                             "fingerprint": "seat|row|map"})
        page.fp_after = "seat|row|map"               # 그대로 = 안 눌렸다
        with self.assertRaises(RuntimeError) as caught:
            booking._click_seat(page, "A3")
        self.assertIn("A3", str(caught.exception))

    def test_a_seat_that_cannot_be_reached_even_by_panning_falls_back(self):
        # 밀어도 안 되면 예전 경로로 보내 제대로 된 실패 문구를 받는다.
        page = self.MapPage({"reachable": False, "panned": False,
                             "x": 0, "y": 0, "reason": "밀어도 좌석에 닿지 않습니다"})
        booking._click_seat(page, "A3")
        self.assertEqual(page.locator_clicks, ["A3"])
        self.assertEqual(page.clicked_at, [])

    def test_a_broken_probe_falls_back_instead_of_dying(self):
        page = self.MapPage(None)

        def boom(script, arg=None):
            raise RuntimeError("페이지가 갈아 끼워지는 중")

        page.evaluate = boom
        booking._click_seat(page, "A3")
        self.assertEqual(page.locator_clicks, ["A3"])


class TestSeatClickDiagnosis(unittest.TestCase):
    """좌석 클릭이 막혔을 때 '화면 밖'과 '팝업이 덮음'을 가릴 수 있어야 한다.

    Playwright는 "modal-bg가 가로챘다"까지만 알려 준다. IMAX처럼 좌석맵이 한
    화면에 다 안 들어오는 관에서는 좌석이 보이는 영역 밖이라 좌표가 배경에
    떨어진 것일 수도 있는데, 그 둘은 대처가 정반대다.
    """

    class DiagPage:
        def __init__(self, result):
            self.result = result
            self.asked = []

        def evaluate(self, script, arg=None):
            self.asked.append(arg)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def test_it_reports_what_actually_sits_at_the_seat(self):
        page = self.DiagPage({
            "at_point": "div.modal-bg", "reaches_seat": False,
            "seat_rect": [100, 200, 120, 220],
        })
        out = booking._seat_click_diagnosis(page, "K1")
        self.assertIn("modal-bg", out)
        # 어느 좌석을 물었는지 스크립트에 실려 나가야 한다.
        self.assertEqual(page.asked[0]["label"], "K1")
        self.assertEqual(page.asked[0]["sel"], booking.SEAT_MAP_SELECTOR)

    def test_off_screen_and_covered_are_told_apart(self):
        """둘 다 at_point는 modal-bg다 — 가르는 것은 seat_inside다.

        2026-08-28 IMAX관 실측: 창은 2560px인데 좌석맵은 600px만 보이고, 잘린
        좌석도 창 안에는 들어 있었다. 그래서 창 뷰포트로는 판정할 수 없다.
        """
        off = booking._seat_click_diagnosis(self.DiagPage({
            "at_point": "div.modal-bg", "reaches_seat": False,
            "clip": {"node": "div.seatMap", "rect": [980, 48, 1580, 1140],
                     "client_w": 600, "scroll_w": 2090, "scroll_left": 0,
                     "scroll_max": 1490, "seat_inside": False},
            "pan": {"node": "div.pan",
                    "transform": "matrix(1.076, 0, 0, 1.076, -824.42, 13.38)"},
        }), "A3")
        self.assertIn('"seat_inside": false', off)
        self.assertIn("matrix", off, "얼마나 밀어야 하는지 단서가 남아야 한다")
        self.assertIn("2090", off, "잘린 폭을 알아야 한다")

        covered = booking._seat_click_diagnosis(self.DiagPage({
            "at_point": "div.modal-bg", "reaches_seat": False,
            "clip": {"node": "div.seatMap", "seat_inside": True},
        }), "A17")
        self.assertIn('"seat_inside": true', covered)

    def test_korean_survives_the_json(self):
        page = self.DiagPage({"at_point": "div.안내팝업"})
        self.assertIn("안내팝업", booking._seat_click_diagnosis(page, "K1"))

    def test_a_diagnosis_that_fails_is_silent(self):
        """진단은 부가 기능이다 — 여기서 터져서 선점을 망치면 안 된다."""
        page = self.DiagPage(RuntimeError("페이지가 갈아 끼워지는 중"))
        self.assertEqual(booking._seat_click_diagnosis(page, "K1"), "")


class TestHoldRequestRecording(unittest.TestCase):
    """선점 요청을 기록하되, 그 파일이 자격증명이 되면 안 된다.

    이 기록은 언젠가 UI를 몰지 않고 seatTempPrmp를 직접 부르기 위한 관찰이다.
    형태를 배우는 게 목적이므로 **키와 구조는 남기고 값만 가린다.**
    """

    def test_credentials_are_masked_but_the_shape_survives(self):
        out = booking.mask_secrets({
            "accessToken": "eyJhbGciOi...",
            "custNo": "12345678",
            "scnsNo": "0013",
            "seats": [{"seatLocNo": "L01", "refreshToken": "zzz"}],
        })
        self.assertNotIn("eyJhbGciOi", str(out))
        self.assertNotIn("12345678", str(out))
        self.assertNotIn("zzz", str(out))
        # 값만 가리고 키·구조·무해한 값은 그대로 남아야 배울 수 있다.
        self.assertEqual(out["scnsNo"], "0013")
        self.assertEqual(out["seats"][0]["seatLocNo"], "L01")
        self.assertIn("accessToken", out)
        self.assertIn("refreshToken", out["seats"][0])

    def test_masking_notes_how_long_the_hidden_value_was(self):
        # 나중에 직접 채울 때 그 자리가 무엇이었는지 가늠할 수 있어야 한다.
        out = booking.mask_secrets({"Authorization": "Bearer abc"})
        self.assertIn(str(len("Bearer abc")), out["Authorization"])

    def test_secret_names_are_matched_regardless_of_style(self):
        for name in ("Set-Cookie", "access_token", "CUST_NO", "X-CSRF-Token"):
            self.assertTrue(booking._is_secret(name), name)
        for name in ("scnsNo", "seatLocNo", "siteNo", "content-type"):
            self.assertFalse(booking._is_secret(name), name)

    def test_recording_never_breaks_the_hold(self):
        """관찰이 선점을 망치면 안 된다 — 무엇이 터져도 조용히 넘어간다."""
        booking._record_hold_request({"request": None}, {})   # 기록할 게 없음
        booking._record_hold_request({}, {})                  # 리스너가 못 잡음
        booking._record_hold_request({"request": object()}, {})  # 모양이 틀림


class TestPaymentStepsDoNotSleepWhenReady(unittest.TestCase):
    """결제 단계는 준비되면 바로 넘어간다.

    예전에는 셋 다 검증 함수를 **가지고 있으면서도** 고정 시간을 잤다. 특히
    _open_payment_page는 조건 확인이 루프 위에 있어, 화면이 0.3초 만에 떠도
    2.5초를 자고 다음 바퀴에 가서야 True를 돌려줬다.
    """

    class PayPage:
        """결제 화면을 흉내 낸다. 잔 시간을 합산해 낭비를 잡아낸다.

        wait_for_timeout은 **실제로 잔다.** _wait_until이 벽시계로 마감을 재기
        때문에, 자는 시늉만 하면 마감까지 루프가 폭주해 호출 횟수가 무의미해진다.
        """

        def __init__(self, *, ready_after_clicks=0, active_after_evals=0,
                     checked_after_evals=0):
            self.slept = 0
            self.clicks = 0
            self.evals = 0
            self._ready_after = ready_after_clicks
            # None이면 끝내 켜지지 않는다.
            self._active_after = active_after_evals
            self._checked_after = checked_after_evals

        def wait_for_timeout(self, ms):
            self.slept += ms
            time.sleep(ms / 1000)

        @property
        def ready(self):
            return self.clicks >= self._ready_after

        def locator(self, selector, **kwargs):
            page = self

            class Node:
                def is_visible(self):
                    return True

                def click(self, timeout=None):
                    page.clicks += 1

            class Loc:
                def count(self):
                    if selector == booking.PAY_LIST_SELECTOR:
                        return 1 if page.ready else 0
                    return 0

                def all(self):
                    # '결제하기' 버튼 목록과 결제수단 <li> 안의 버튼 양쪽에
                    # 답한다. 모달 셀렉터에는 답하지 않는다 — 덮은 게 없다.
                    if selector.endswith("button"):
                        return [Node()]
                    return []

                @property
                def first(self):
                    return Node()

            return Loc()

        def evaluate(self, script, arg=None):
            # _pay_method_active(클래스 문자열)와 _agree_terms(checked) 양쪽을
            # 같은 훅으로 답한다 — 인자 모양으로 구분한다.
            self.evals += 1
            if isinstance(arg, dict):
                return (booking.PAY_ACTIVE_MARK
                        if self._reached(self._active_after) else "")
            return self._reached(self._checked_after)

        def _reached(self, threshold):
            return threshold is not None and self.evals > threshold

    def test_reaching_the_payment_screen_costs_no_extra_wait(self):
        # 첫 클릭에 결제수단 목록이 뜬다 — 예전에는 여기서 2.5초를 버렸다.
        page = self.PayPage(ready_after_clicks=1)
        self.assertTrue(booking._open_payment_page(page))
        self.assertEqual(page.clicks, 1)
        self.assertLessEqual(page.slept, booking.STEP_POLL_MS,
                             f"준비됐는데도 {page.slept}ms를 잤다")

    def test_already_on_the_payment_screen_clicks_nothing(self):
        page = self.PayPage(ready_after_clicks=0)
        self.assertTrue(booking._open_payment_page(page))
        self.assertEqual(page.clicks, 0)
        self.assertEqual(page.slept, 0)

    def test_payment_screen_that_never_comes_still_gives_up(self):
        # 한 바퀴 상한을 줄여 둔다 — 실제 값(2.5초 × 6)이면 이 테스트만 15초다.
        real = booking.PAY_PAGE_ROUND_MS
        booking.PAY_PAGE_ROUND_MS = 10
        self.addCleanup(setattr, booking, "PAY_PAGE_ROUND_MS", real)

        page = self.PayPage(ready_after_clicks=99)
        self.assertFalse(booking._open_payment_page(page))
        self.assertEqual(page.clicks, booking.PAY_PAGE_ROUNDS,
                         "정해진 횟수만큼만 눌러야 한다")

    def test_pay_method_selection_returns_as_soon_as_it_takes(self):
        page = self.PayPage(active_after_evals=0)   # 누르자마자 활성
        booking._choose_pay_method(page, "kakaopay")
        self.assertEqual(page.slept, 0, "켜졌는데도 잤다")

    def test_pay_method_that_does_not_take_is_still_an_error(self):
        page = self.PayPage(active_after_evals=None)   # 끝내 안 켜진다
        with self.assertRaises(RuntimeError):
            booking._choose_pay_method(page, "kakaopay")
        self.assertLessEqual(page.slept, booking.PAY_METHOD_ACTIVE_MS
                             + booking.STEP_POLL_MS,
                             "예전 고정 대기보다 오래 기다리면 안 된다")

    def test_terms_agreement_returns_as_soon_as_it_is_checked(self):
        page = self.PayPage(checked_after_evals=0)
        booking._agree_terms(page)
        self.assertEqual(page.slept, 0, "켜졌는데도 잤다")

    def test_terms_that_stay_off_are_reported(self):
        page = self.PayPage(checked_after_evals=None)   # 끝내 안 켜진다
        with self.assertRaises(RuntimeError):
            booking._agree_terms(page)
        self.assertLessEqual(page.slept, booking.TERMS_CHECK_MS
                             + booking.STEP_POLL_MS,
                             "예전 고정 대기보다 오래 기다리면 안 된다")


class TestPaymentTripwire(unittest.TestCase):
    """'결제하기'가 선점 단계라는 전제가 깨지면 돈이 나간다 — 알아챌 수 있어야 한다."""

    def test_seat_hold_response_is_never_mistaken_for_payment(self):
        url = "https://cgv.co.kr/api/v1/booking/seatTemp/seatTempPrmp"
        self.assertIsNone(booking.payment_mark(url))

    def test_payment_paths_are_flagged(self):
        for url in ("https://cgv.co.kr/api/v1/booking/movAtktPayApprov",
                    "https://cgv.co.kr/api/v1/order/approvePayment",
                    "https://onepg.cjsystems.co.kr/v2/pay/kakaoPay/"
                    "authCertType/KKC260826151458419A9/0000"):
            self.assertIsNotNone(booking.payment_mark(url), url)

    def test_drawing_the_payment_screen_is_not_an_approval(self):
        """결제 화면을 **그리기만 하는** 조회는 걸리면 안 된다.

        예전에는 '/pay/'가 표식에 있어서 `payment/pay/searchCrdCocdList` 같은
        조회에도 걸렸다. 그 조회들은 선점 +9초쯤에 나가고 감시 구간은 +5초라
        지금까지 안 걸린 건 타이밍 운이었을 뿐이다 — 걸렸다면 성공한 선점이
        "결제 계열 요청 감지"로 되돌려졌다.
        """
        for url in ("https://cgv.co.kr/api/v1/payment/pay/searchCrdCocdList",
                    "https://cgv.co.kr/api/v1/payment/pay/searchGroupedPaymdList"
                    "?siteNo=0013",
                    "https://cgv.co.kr/api/v1/payment/mpy/searchLastPayknd",
                    "https://cgv.co.kr/api/v1/payment/pay/commonGetPayId"):
            self.assertIsNone(booking.payment_mark(url), url)

    def test_ordinary_traffic_is_not_flagged(self):
        for url in ("https://cgv.co.kr/api/v1/booking/searchSchByMov",
                    "https://cgv.co.kr/api/v1/booking/searchIfSeatData",
                    "https://cgv.co.kr/_next/static/chunk.js"):
            self.assertIsNone(booking.payment_mark(url), url)


class TestScreenshotIsBestEffort(unittest.TestCase):
    """스크린샷은 부가 기능이다 — 실패해도 예매 실패 경로를 망치면 안 된다."""

    class BrokenPage:
        def screenshot(self, **kwargs):
            raise RuntimeError("브라우저가 죽었다")

    def test_returns_none_instead_of_raising(self):
        self.assertIsNone(
            booking._save_screenshot(self.BrokenPage(),
                                     {"mov_nm": "오디세이", "start_hhmm": "18:00"}))


class TestModalsWeMustNotClose(unittest.TestCase):
    """예매 흐름 자체가 모달로 되어 있다 — 좌석맵도, 결제 바텀시트도.

    그것들도 role=dialog에 자기 ✕를 달고 있어서, 광고 팝업과 같이 취급하면
    우리가 눌러야 할 버튼을 스스로 치워 버린다. 실제로 결제 바텀시트를 닫았다
    뜨기를 반복하다 결제 화면에 닿지 못했다.
    """

    class Modal:
        def __init__(self, text):
            self._text = text

        def text_content(self):
            return self._text

    def test_the_payment_sheet_is_ours(self):
        modal = self.Modal("결제 전 확인해 주세요 … 취소/환불 불가 안내 … 결제하기")
        self.assertTrue(booking._modal_is_ours(modal))

    def test_the_seatmap_sheet_is_ours(self):
        self.assertTrue(booking._modal_is_ours(self.Modal("좌석 선택 … 선택완료")))

    def test_an_ad_popup_is_not_ours(self):
        modal = self.Modal("세계 최초 4면 상영관, 용산 SCREENX관은 … 확인")
        self.assertFalse(booking._modal_is_ours(modal))

    def test_unreadable_modal_is_not_protected(self):
        class Broken:
            def text_content(self):
                raise RuntimeError("사라지는 중")

        self.assertFalse(booking._modal_is_ours(Broken()))


class TestPrewarm(unittest.TestCase):
    """예매 화면을 미리 띄워 두는 부분.

    딥링크로 화면을 **새로 여는 데만 6.2초**가 든다(실측). 좌석이 난 순간 그
    6.2초를 쓰면 이미 늦으므로, 자동 예매를 켠 감시의 화면은 탭 하나로 띄워 두고
    선점할 때 그대로 쓴다(실측 0초).
    """

    CTX = {"mov_no": "30001323", "site_no": "0013", "scn_ymd": "20260828"}

    def test_key_splits_by_movie_site_and_date(self):
        other_date = dict(self.CTX, scn_ymd="20260901")
        other_site = dict(self.CTX, site_no="0001")
        self.assertNotEqual(booking.warm_key(self.CTX),
                            booking.warm_key(other_date))
        self.assertNotEqual(booking.warm_key(self.CTX),
                            booking.warm_key(other_site))
        self.assertEqual(booking.warm_key(self.CTX), booking.warm_key(dict(self.CTX)))

    class Session:
        """탭을 내주는 세션. give=False면 못 내주는 세션을 흉내낸다."""

        def __init__(self, tab, give=True):
            self.tab = tab
            self.page = "기본페이지"
            self.give = give
            self.asked = []

        def booking_page(self, key):
            self.asked.append(key)
            if not self.give:
                raise RuntimeError("탭을 열 수 없습니다")
            return self.tab

    def test_uses_the_tab_the_session_gives(self):
        session = self.Session(tab="예매탭")
        self.assertEqual(booking.booking_page(session, self.CTX), "예매탭")
        self.assertEqual(session.asked, [booking.warm_key(self.CTX)])

    def test_falls_back_to_the_main_page_when_no_tab(self):
        """탭을 못 열어도 선점을 포기하지는 않는다 — 예전처럼 한 장으로 간다."""
        session = self.Session(tab=None, give=False)
        self.assertEqual(booking.booking_page(session, self.CTX), "기본페이지")

    def test_a_ready_screen_is_left_alone(self):
        """이미 그 날짜가 떠 있으면 다시 열지 않는다 — 그게 이 최적화의 전부다."""
        session = self.Session(tab="예매탭")
        opened = []
        real_ready, real_open = booking._already_on_booking, booking._open_booking_direct
        booking._already_on_booking = lambda page, ctx: True
        booking._open_booking_direct = lambda page, ctx: opened.append(ctx) or True
        try:
            self.assertTrue(booking.prewarm(session, self.CTX))
        finally:
            booking._already_on_booking = real_ready
            booking._open_booking_direct = real_open
        self.assertEqual(opened, [], "이미 준비된 화면을 다시 열면 안 된다")

    def test_an_empty_screen_is_opened(self):
        session = self.Session(tab="예매탭")
        opened = []
        real_ready, real_open = booking._already_on_booking, booking._open_booking_direct
        booking._already_on_booking = lambda page, ctx: False
        booking._open_booking_direct = lambda page, ctx: (opened.append(page), True)[1]
        try:
            self.assertTrue(booking.prewarm(session, self.CTX))
        finally:
            booking._already_on_booking = real_ready
            booking._open_booking_direct = real_open
        self.assertEqual(opened, ["예매탭"])

    def test_without_a_movie_number_there_is_nothing_to_open(self):
        session = self.Session(tab="예매탭")
        self.assertFalse(booking.prewarm(session, {"scn_ymd": "20260828"}))
        self.assertEqual(session.asked, [])

    def test_failure_is_swallowed(self):
        """미리 여는 일이 실패해도 감시가 멈추면 안 된다 — 선점 때 다시 연다."""
        session = self.Session(tab=None, give=False)
        real = booking._already_on_booking
        booking._already_on_booking = lambda page, ctx: (_ for _ in ()).throw(
            RuntimeError("화면이 이상하다"))
        try:
            self.assertFalse(booking.prewarm(session, self.CTX))
        finally:
            booking._already_on_booking = real


class TestClickThroughModals(unittest.TestCase):
    """안내 팝업은 **우리가 팝업을 닫고 지나간 뒤에** 뜨기도 한다.

    실제로 SCREENX관 안내가 로딩이 걷히는 순간 떠서 인원 선택 클릭을 5초 내내
    가로막았다("intercepts pointer events"). 미리 한 번 닫아 두는 것으로는 막을
    수 없는 경합이라, 막히면 그 자리에서 닫고 다시 눌러야 한다.
    """

    class Blocking:
        """N번째 클릭까지는 팝업에 막히고, 그 뒤로는 눌리는 버튼."""

        def __init__(self, page, block_times: int, error: str):
            self.page = page
            self.left = block_times
            self.error = error
            self.clicks = 0

        def click(self, timeout=None):
            if self.left > 0:
                self.left -= 1
                raise RuntimeError(self.error)
            self.clicks += 1

    class Page:
        def __init__(self):
            self.dismissed = 0

        def wait_for_timeout(self, ms):
            pass

    def _page(self):
        page = self.Page()
        # dismiss_modals를 부르면 팝업이 닫혔다고 치고 세기만 한다.
        booking.dismiss_modals = lambda p, rounds=3: (
            setattr(p, "dismissed", p.dismissed + 1), 1)[1]
        return page

    def setUp(self):
        self._real_dismiss = booking.dismiss_modals

    def tearDown(self):
        booking.dismiss_modals = self._real_dismiss

    INTERCEPT = ('Locator.click: Timeout 5000ms exceeded.\n'
                 '  - <div class="modal-bg"></div> from <div role="dialog" '
                 'aria-modal="true" class="cgv-modal modal-alert active">…</div> '
                 'subtree intercepts pointer events')

    def test_closes_the_popup_and_clicks_again(self):
        page = self._page()
        node = self.Blocking(page, 1, self.INTERCEPT)
        booking._click_through_modals(page, node, what="관람인원", timeout=5000)
        self.assertEqual(node.clicks, 1)
        self.assertEqual(page.dismissed, 1, "팝업을 닫고 다시 눌러야 한다")

    def test_gives_up_after_the_retries(self):
        page = self._page()
        node = self.Blocking(page, 9, self.INTERCEPT)
        with self.assertRaises(RuntimeError):
            booking._click_through_modals(page, node, what="관람인원",
                                          timeout=5000, retries=2)
        self.assertEqual(node.clicks, 0)
        self.assertEqual(page.dismissed, 2)

    def test_other_failures_are_not_retried(self):
        """팝업 때문이 아니면 닫아 봐야 소용없다 — 바로 올려 보낸다."""
        page = self._page()
        node = self.Blocking(page, 9, "element is not visible")
        with self.assertRaises(RuntimeError):
            booking._click_through_modals(page, node, what="관람인원",
                                          timeout=5000)
        self.assertEqual(page.dismissed, 0)

    def test_a_clean_click_does_not_touch_modals(self):
        page = self._page()
        node = self.Blocking(page, 0, self.INTERCEPT)
        booking._click_through_modals(page, node, what="관람인원", timeout=5000)
        self.assertEqual(node.clicks, 1)
        self.assertEqual(page.dismissed, 0)


class TestPaymentAmount(unittest.TestCase):
    def test_reads_the_won_amount(self):
        self.assertEqual(booking.parse_amount("15,000원"), 15000)
        self.assertEqual(booking.parse_amount("21,000원"), 21000)

    def test_no_digits_is_none(self):
        self.assertIsNone(booking.parse_amount(""))
        self.assertIsNone(booking.parse_amount(None))
        self.assertIsNone(booking.parse_amount("금액 없음"))


class TestKakaoPayLink(unittest.TestCase):
    """결제창에서 **휴대폰으로 열 링크**를 뽑아내는 부분.

    실측(2026-08)한 브릿지 응답 모양을 그대로 쓴다. 링크 형태는 화면의 QR을
    디코드해서 확인했다 — 응답의 `hash` 필드로 mobile-pc 브릿지 주소를 만든다.
    """

    # 응답의 hash는 65자이고, ios_app_url 안의 url= 과 iframe 주소에 들어 있는
    # 해시는 그보다 **한 글자 짧다**. 눈으로는 같아 보여서 실제로 그 짧은 쪽으로
    # 링크를 만들어 보냈고, 사용자가 열었을 때 "인증정보를 찾을 수 없습니다"가 떴다.
    HASH = "08b5ad0b442356f97a511e973a767f8cb9683b0fa0a7bc5050e05dc1e6d2c0bc3"
    SHORT = HASH[:-1]
    LINK = ("https://online-payment.kakaopay.com"
            f"/bridge/mobile-pc/reseller/one-time/payment/{HASH}")

    def _bridge(self, **over):
        body = {
            "tid": "ta8e84623dfd7ad169de",
            "hash": self.HASH,
            "ios_app_url": (
                "kakaotalk://kakaopay/pg?payweb_talk_min_version=11.3.0"
                f"&url=https://online-pay.kakaopay.com/pay/r1/{self.SHORT}"),
            "expired_timestamp": 1787758198,
        }
        body.update(over)
        return body

    def test_builds_the_link_from_the_hash_field(self):
        self.assertEqual(booking.kakao_link_from_bridge(self._bridge()), self.LINK)

    def test_the_app_scheme_url_is_not_used(self):
        """ios_app_url 안의 주소는 다른 주소다 — 열면 인증정보를 찾을 수 없다."""
        got = booking.kakao_link_from_bridge(self._bridge())
        self.assertNotIn("online-pay.kakaopay.com/pay/r1", got)
        self.assertTrue(got.endswith(self.HASH),
                        "해시가 한 글자라도 잘리면 죽은 링크가 된다")

    def test_missing_body_is_none(self):
        self.assertIsNone(booking.kakao_link_from_bridge(None))
        self.assertIsNone(booking.kakao_link_from_bridge({}))

    def test_a_hash_that_is_not_hex_is_refused(self):
        """엉뚱한 값으로 링크를 만들면 죽은 주소를 사람에게 보내게 된다."""
        self.assertIsNone(booking.kakao_link_from_bridge({"hash": "없음"}))
        self.assertIsNone(booking.kakao_link_from_bridge({"hash": "abc"}))

    def test_expiry_is_the_korean_wall_clock_kakao_meant(self):
        """epoch처럼 생겼지만 epoch이 아니다 — 한국 벽시계를 UTC인 척 담아 준다.

        실측: 15:14:58(KST)에 띄운 결제창의 값이 1787758198이었고, 결제창은
        15분 뒤에 죽는다. 그대로 epoch으로 읽으면 9시간 뒤가 되어 **이미 죽은
        링크를 아직 유효한 것처럼** 보여 주게 된다.
        """
        from datetime import datetime

        got = booking.bridge_expires_at(self._bridge())
        self.assertIsNotNone(got)
        self.assertEqual(got.tzinfo.key, "Asia/Seoul")
        self.assertEqual(got.replace(tzinfo=None),
                         datetime(2026, 8, 26, 15, 29, 58))

    def test_expiry_missing_or_broken_is_none(self):
        self.assertIsNone(booking.bridge_expires_at({}))
        self.assertIsNone(booking.bridge_expires_at({"expired_timestamp": "?"}))


class TestHoldAlertWithPayLink(unittest.TestCase):
    """알림은 사람이 다음에 무엇을 할지 정한다 — 링크가 있으면 그게 전부다."""

    def _msg(self, **kw):
        return booking.build_hold_alert(
            "오디세이", "용산아이파크몰", "20260828", "25:30", ["N15"],
            booking._parse_limit_dt("20260826151316"), 15000, **kw)

    def test_link_is_shown_when_payment_was_requested(self):
        link = "https://online-pay.kakaopay.com/pay/r1/abc123"
        msg = self._msg(pay_url=link,
                        pay_expires_at=booking._parse_limit_dt("20260826152958"))
        self.assertIn(link, msg)
        self.assertIn("카카오페이", msg)

    def test_the_deadline_is_whichever_dies_first(self):
        """실측에서 선점은 5분 남짓, 링크는 15분을 버텼다.

        링크 만료(15:29)만 적으면 좌석이 풀린 뒤(15:13)에도 아직 시간이 있는 줄
        알고 결제를 시도하게 된다. 마감은 하나만, 이른 쪽으로 적는다.
        """
        msg = self._msg(pay_url="https://online-pay.kakaopay.com/pay/r1/abc123",
                        pay_expires_at=booking._parse_limit_dt("20260826152958"))
        self.assertIn("15:13까지", msg)      # 선점이 먼저 죽는다
        self.assertNotIn("15:29", msg)

    def test_a_link_that_dies_first_is_the_deadline_instead(self):
        msg = booking.build_hold_alert(
            "오디세이", "용산", "20260828", "25:30", ["N15"],
            booking._parse_limit_dt("20260826160000"), 15000,
            pay_url="https://online-pay.kakaopay.com/pay/r1/abc123",
            pay_expires_at=booking._parse_limit_dt("20260826154500"))
        self.assertIn("15:45까지", msg)
        self.assertNotIn("16:00", msg)

    def test_without_a_link_it_still_points_at_cgv(self):
        from watch import BOOKING_URL

        msg = self._msg()
        self.assertIn(BOOKING_URL, msg)
        self.assertNotIn("kakaopay", msg)

    def test_a_failed_payment_says_why(self):
        msg = self._msg(pay_error="결제 화면으로 넘어가지 못했습니다")
        self.assertIn("결제 화면으로 넘어가지 못했습니다", msg)
        # 링크가 없으면 사람이 CGV에서 마쳐야 한다 — 그 안내가 사라지면 안 된다.
        self.assertIn("CGV", msg)


if __name__ == "__main__":
    unittest.main()
