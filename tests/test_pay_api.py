#!/usr/bin/env python3
"""API로 결제 링크를 받아 오는 경로 (booking.pay_api).

**여기가 처음으로 진짜 결제 요청을 보내는 코드다.** 그래서 이 파일이 지키는 것은
"잘 되는가"보다 **"함부로 보내지 않는가"** 쪽이다:

  · 매출 생성(salCreateSal)에는 절대 손대지 않는다 — 돈이 확정되는 자리다
  · 금액을 우리가 정하지 않는다 (CGV가 준 좌석 가격만 쓴다)
  · 가격을 하나라도 못 받으면 **보내지 않는다**
  · 카카오페이가 아니면 시도하지 않는다 (관측이 없는 것을 지어내지 않는다)
  · 어느 단계에서 실패해도 선점을 무르지 않는다

브라우저도 DB도 필요 없다. 세션은 아래 FakeSession이 대신한다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import booking  # noqa: E402

# 좌석·가격·상영표 픽스처는 판매정보 테스트와 **같은 것을 쓴다** — 두 벌로
# 나뉘면 한쪽만 고쳐 놓고 통과하는 일이 생긴다.
from test_pay_body import ADNC, CTX, PRICES, SEATS  # noqa: E402


ACCOUNT = {"ipAddress": "10.0.0.1",
           "cust": {"userId": "tester", "userName": "홍길동",
                    "userCellPhone": "01000000000", "userNo": "111111111",
                    "custNo": "111111111", "itgrCustNo": "999",
                    "cusgdCd": "01"},
           "cjOneUser": {"memberNo": "999", "memberName": "홍길동",
                         "mobileNo": "01000000000", "avlPoint": 0}}

GROUPED = {"statusCode": 0, "data": [
    {"paymdList": [{"paykndCd": "1002", "payMethod": "mobile"},
                   {"paykndCd": "1007", "payMethod": "kakaoCert",
                    "paykndNm": "카카오페이(온라인)",
                    "custGuidWordCont": "<ul><li>안내</li></ul>"}]}]}
CARDS = {"statusCode": 0, "data": [{"cdcoCd": "BCC", "cdcoNm": "BC카드"}]}


def held_ctx(**over):
    ctx = {**CTX, "site_nm": "용산아이파크몰", "party": 2,
           "_held_seats": SEATS, "mov_atkt_no": "0013260907035024676"}
    ctx.update(over)
    return ctx


class FakeSession:
    """pay_api가 실제로 쓰는 것만 흉내낸다. 나간 요청을 전부 기록한다."""

    def __init__(self, *, prices=None, pay_id="20260907000000000001",
                 auth=None, fail=None, link=None):
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []
        self._prices = PRICES if prices is None else prices
        self._pay_id = pay_id
        self._auth = auth if auth is not None else {
            "returnCode": "0000", "paymNo": "2026",
            "paylinkUrl": "https://onepg.cjsystems.co.kr/v2/pay/ready/RID1"}
        self._fail = fail or {}
        self.link = link if link is not None else {
            "ok": True, "pay_url": "https://online-payment.kakaopay.com/x",
            "pay_expires_at": None, "error": ""}

    # 결제 화면 문서. fetch_pay_account가 여기서 계정 정보를 읽는다 — 값은
    # 전부 지어낸 것이다.
    PAGE = (r'{\"ipAddress\":\"10.0.0.1\",'
            r'\"cust\":{\"coCd\":\"A420\",\"userId\":\"tester\",'
            r'\"userNo\":\"111111111\",\"userName\":\"홍길동\",'
            r'\"userCellPhone\":\"01000000000\",\"custNo\":\"111111111\",'
            r'\"itgrCustNo\":\"999\",\"cusgdCd\":\"01\"},'
            r'\"cjOneUser\":{\"memberRegYn\":\"Y\",\"memberNo\":\"999\",'
            r'\"memberName\":\"홍길동\",\"mobileNo\":\"01000000000\",'
            r'\"avlPoint\":0}}')

    @property
    def page(self):
        outer = self

        class _Page:
            def evaluate(self, script, arg=None):
                return outer.PAGE

        return _Page()

    # ── pay_api가 부르는 것들 ──
    def identity(self):
        return {"cust_no": "111111111", "cusgd_cd": "01",
                "user_id": "tester", "user_nm": "홍길동"}

    def adnc_seat_info(self, *a, **k):
        return ADNC

    def get_json(self, path, retries=2):
        self.gets.append(path)
        if "searchGroupedPaymdList" in path:
            return GROUPED
        if "searchCrdCocdList" in path:
            return CARDS
        raise AssertionError(f"FakeSession이 모르는 GET: {path}")

    def post_json(self, path, body):
        self.posts.append((path, body))
        for mark, error in self._fail.items():
            if mark in path:
                return {"status": 200, "body": {"statusCode": -1,
                                                "statusMessage": error}}
        if booking.EP_SEAT_PRICE in path:
            data = self._prices
        elif booking.EP_PAY_ID in path:
            data = {"payId": self._pay_id}
        elif booking.EP_PAY_AUTH in path:
            data = self._auth
        else:
            data = {}
        return {"status": 200, "body": {"statusCode": 0, "data": data}}

    def paths(self) -> list[str]:
        return [p for p, _ in self.posts] + self.gets


class _LinkPatch:
    """fetch_pay_link을 잠시 갈아 둔다 — 브라우저를 쓰지 않는다."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        self.real = booking.fetch_pay_link
        booking.fetch_pay_link = lambda s, url, **k: self.session.link
        return self

    def __exit__(self, *exc):
        booking.fetch_pay_link = self.real


def run(session, ctx=None, **kw):
    with _LinkPatch(session):
        return booking.pay_api(session, ctx or held_ctx(), **kw)


class TestItNeverCommitsTheSale(unittest.TestCase):
    """**돈이 확정되는 자리에는 손대지 않는다.**

    `salCreateSal`(매출 생성)과 `createSalHotdl`이 그 자리다. 지금 UI 경로가
    결제 요청에서 멈추고 최종 승인을 사람에게 넘기는 것과 같은 경계를 지킨다.
    """

    FORBIDDEN = ("salCreateSal", "createSalHotdl", "onlineApprovalSearchPayment")

    def test_a_successful_run_never_touches_them(self):
        session = FakeSession()
        out = run(session)
        self.assertTrue(out["ok"], out.get("error"))
        for path in session.paths():
            for mark in self.FORBIDDEN:
                self.assertNotIn(mark, path, f"{mark}를 불렀다")

    def test_it_sends_only_the_steps_we_observed(self):
        session = FakeSession()
        run(session)
        self.assertEqual([p for p, _ in session.posts], [
            booking.EP_SEAT_PRICE,
            booking.EP_PAY_ID,
            booking.EP_PAY_TEMP_INSERT,
            booking.EP_PAY_TEMP_UPDATE,
            booking.EP_PAY_AUTH,
            booking.EP_PAY_TEMP_UPDATE,
        ])


class TestItRefusesToGuess(unittest.TestCase):
    def test_no_hold_means_no_payment(self):
        for ctx in (held_ctx(_held_seats=[]), held_ctx(mov_atkt_no="")):
            session = FakeSession()
            out = run(session, ctx)
            self.assertFalse(out["ok"])
            self.assertEqual(session.posts, [], "선점도 없이 요청을 보냈다")

    def test_only_kakaopay_is_attempted(self):
        # 다른 수단은 결제수단 목록에서 고르는 값이 달라 관측이 없다.
        session = FakeSession()
        out = run(session, method="tosspay")
        self.assertFalse(out["ok"])
        self.assertEqual(session.posts, [])
        self.assertIn("카카오페이", out["error"])

    def test_a_missing_price_stops_before_paying(self):
        """가격을 하나라도 못 받으면 **보내지 않는다.**

        모르는 금액으로 결제를 걸면 사용자가 엉뚱한 값을 승인하게 된다.
        """
        session = FakeSession(prices=PRICES[:1])
        out = run(session)
        self.assertFalse(out["ok"])
        self.assertEqual([p for p, _ in session.posts], [booking.EP_SEAT_PRICE])
        self.assertIn("가격만 받았습니다", out["error"])

    def test_a_refused_auth_is_reported_not_retried(self):
        session = FakeSession(auth={"returnCode": "9999",
                                    "returnMessage": "한도 초과"})
        out = run(session)
        self.assertFalse(out["ok"])
        self.assertIn("한도 초과", out["error"])

    def test_no_pay_link_means_no_success(self):
        session = FakeSession(auth={"returnCode": "0000", "paylinkUrl": ""})
        out = run(session)
        self.assertFalse(out["ok"])
        self.assertIn("결제창 주소", out["error"])

    def test_a_rejected_step_carries_cgvs_words(self):
        session = FakeSession(fail={"commonGetPayId": "이미 결제된 건입니다"})
        out = run(session)
        self.assertFalse(out["ok"])
        self.assertIn("이미 결제된 건입니다", out["error"])

    def test_a_kakao_failure_still_reports_the_amount(self):
        # 선점과 결제 요청은 이미 끝났다 — 사람이 CGV 앱에서 이어 결제할 수 있다.
        session = FakeSession(link={"ok": False, "error": "결제창이 안 떴습니다"})
        out = run(session)
        self.assertFalse(out["ok"])
        self.assertEqual(out["amount"], 29000)


class TestWhatGoesOnTheWire(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.out = run(self.session)
        self.bodies = dict(self.session.posts)

    def test_the_amount_comes_from_cgvs_prices(self):
        # 14000 + 15000 — 우리가 정한 값이 아니다.
        self.assertEqual(self.out["amount"], 29000)
        self.assertEqual(self.bodies[booking.EP_PAY_ID]["amountTotal"], 29000)

    def test_the_sale_info_is_sent_as_a_json_string(self):
        body = self.bodies[booking.EP_PAY_TEMP_INSERT]
        self.assertIsInstance(body["paymInfoCont"], str)
        info = json.loads(body["paymInfoCont"])
        self.assertEqual(info["amountTotal"], 29000)
        self.assertEqual(len(info["mov"]["sellProductsList"]), 2)

    def test_the_verify_number_is_the_same_across_the_steps(self):
        """대조번호가 어긋나면 CGV가 그 결제를 못 찾는다."""
        made = {body["paymVrifyNo"] for path, body in self.session.posts
                if "ProcTempInfo" in path}
        self.assertEqual(len(made), 1)
        auth = self.bodies[booking.EP_PAY_AUTH]
        self.assertIn(f"paymVrifyNo={made.pop()}", auth["redirectUrl"])

    def test_the_update_settles_the_amounts_the_insert_left_at_zero(self):
        first = json.loads(self.bodies[booking.EP_PAY_TEMP_INSERT]["paymInfoCont"])
        later = json.loads(self.bodies[booking.EP_PAY_TEMP_UPDATE]["paymInfoCont"])
        self.assertEqual((first["amountVat"], first["amountTax"]), (0, 0))
        self.assertEqual(later["amountVat"] + later["amountTax"], 29000)
        self.assertEqual(later["redirectUrl"], booking.BASE_ORIGIN)

    def test_the_seats_are_paired_with_their_prices(self):
        body = self.bodies[booking.EP_SEAT_PRICE]
        self.assertEqual([s["seatLocNo"] for s in body["seatList"]],
                         [s["seat_loc_no"] for s in SEATS])
        self.assertEqual(body["prodBnduList"],
                         [{"prodBnduCd": "01", "prodBnduQty": 2}])

    def test_the_guidance_text_is_cleared(self):
        info = json.loads(self.bodies[booking.EP_PAY_TEMP_INSERT]["paymInfoCont"])
        self.assertIsNone(info["payMethod"]["custGuidWordCont"])


class TestPointParam(unittest.TestCase):
    def test_it_uses_the_cj_one_member_number(self):
        got = booking.pay_point_param(ACCOUNT)
        self.assertEqual(got["memberNo"], "999")
        self.assertEqual(got["itgrCustNo"], "999")
        self.assertEqual(got["termUserId"], booking.PAY_POINT_TERM_USER_ID)

    def test_a_non_member_gets_nothing(self):
        self.assertIsNone(booking.pay_point_param({"cjOneUser": {}}))
        self.assertIsNone(booking.pay_point_param({}))


class TestPaymentRouting(unittest.TestCase):
    """결제 함수는 선점 방식과 짝을 맞춘다 — 섞으면 둘 다 실패한다."""

    def test_an_api_watch_pays_by_api(self):
        self.assertIs(booking.pay_for({"hold_mode": "api"}), booking.pay_api)

    def test_a_screen_watch_still_pays_by_screen(self):
        self.assertIs(booking.pay_for({}), booking.pay_block)
        self.assertIs(booking.pay_for({"hold_mode": "ui"}), booking.pay_block)

    def test_an_unknown_mode_falls_back_to_the_screen(self):
        for junk in ("", None, "rest", 7):
            self.assertIs(booking.pay_for({"hold_mode": junk}),
                          booking.pay_block, f"{junk!r}")


if __name__ == "__main__":
    unittest.main()
