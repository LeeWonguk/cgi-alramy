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
        self.assertEqual(booking._pick_seats(page, ["J22", "J23"]),
                         (["J22", "J23"], [], ""))
        self.assertEqual(page.clicked, ["J22", "J23"])

    def test_reports_every_seat_it_could_not_click(self):
        # 하나가 실패해도 나머지를 마저 눌러 봐야 무엇이 팔렸는지 다 알 수 있다.
        page = self.SeatPage(["J22"])
        self.assertEqual(booking._pick_seats(page, ["J22", "J23", "J24"]),
                         (["J22"], ["J23", "J24"], ""))

    def test_reports_what_it_managed_to_click(self):
        # 다시 고를 수 있는지가 여기에 달렸다 — 이미 골라 둔 게 있으면 못 고친다.
        page = self.SeatPage(["J23"])
        clicked, missed, _ = booking._pick_seats(page, ["J22", "J23"])
        self.assertEqual(clicked, ["J23"])
        self.assertEqual(missed, ["J22"])

    def test_error_message_names_the_missing_seats(self):
        msg = booking._partial_seats_error(["J22", "J23"], ["J23"])
        self.assertIn("J23", msg)
        self.assertIn("2석 중 1석", msg)

    def test_no_seat_clicked_at_all_is_reported_the_same_way(self):
        page = self.SeatPage([])
        clicked, missed, _ = booking._pick_seats(page, ["J22", "J23"])
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
        clicked, missed, notice = booking._pick_seats(page, ["H1", "H2", "H3"])
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

    실측(2026-08)한 브릿지 응답 모양을 그대로 쓴다. 카카오톡 스킴을 그대로
    보내면 디스코드에서 누를 수조차 없으므로, 그 안의 https 주소를 꺼내야 한다.
    """

    HASH = "7fa7d5e7ceb4459d68bb44b2d36d90bdf789f2a7d645bacc9ad005c654fb4bd1"
    LINK = f"https://online-pay.kakaopay.com/pay/r1/{HASH}"

    def _bridge(self, **over):
        body = {
            "tid": "ta8e84623dfd7ad169de",
            "ios_app_url": (
                "kakaotalk://kakaopay/pg?payweb_talk_min_version=11.3.0"
                "&payweb_url=https%3A%2F%2Fonline-payment.kakaopay.com%2Fpay"
                f"&url={self.LINK}"),
            "aos_app_url": (
                "intent://kakaopay/pg?payweb_talk_min_version=11.3.0"
                f"&url={self.LINK}#Intent;scheme=kakaotalk;"
                "package=com.kakao.talk;end"),
            "expired_timestamp": 1787758198,
        }
        body.update(over)
        return body

    def test_takes_the_browser_url_out_of_the_app_scheme(self):
        self.assertEqual(booking.kakao_link_from_bridge(self._bridge()), self.LINK)

    def test_intent_tail_is_stripped(self):
        body = self._bridge(ios_app_url="")
        self.assertEqual(booking.kakao_link_from_bridge(body), self.LINK)

    def test_missing_body_is_none(self):
        self.assertIsNone(booking.kakao_link_from_bridge(None))
        self.assertIsNone(booking.kakao_link_from_bridge({}))

    def test_builds_the_link_from_the_frame_url_as_a_fallback(self):
        frame = ("https://online-payment.kakaopay.com/bridge/pc/reseller/"
                 f"one-time/payment/{self.HASH}")
        self.assertEqual(booking.kakao_link_from_frame(frame), self.LINK)

    def test_a_path_that_is_not_a_hash_is_refused(self):
        """해시가 아닌 꼬리를 링크로 만들면 죽은 주소를 사람에게 보내게 된다."""
        self.assertIsNone(booking.kakao_link_from_frame(
            "https://online-payment.kakaopay.com/bridge/pc/bridge"))
        self.assertIsNone(booking.kakao_link_from_frame(
            "https://cgv.co.kr/mpy/main"))
        self.assertIsNone(booking.kakao_link_from_frame(""))

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
