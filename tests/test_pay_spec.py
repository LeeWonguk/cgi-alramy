#!/usr/bin/env python3
"""결제 흐름 관측 기록 (booking.watch_pay_requests · record_pay_spec).

이 기능은 **동작을 바꾸지 않는 관찰**이다. 결제는 지금까지처럼 화면으로 하고,
여기서는 그때 오간 것을 모아 둔다 — 나중에 결제를 API로 하려면 그 형태를 알아야
하는데, 지금 우리는 아무 관측도 갖고 있지 않다(선점은 logs/holdspec/이 쌓인 뒤에야
추측 없이 만들 수 있었다).

관찰이라서 지켜야 할 것이 분명하다. 이 파일이 그것을 고정한다:

  · 무슨 일이 나도 결제를 깨뜨리지 않는다 (읽다 터져도 조용히 넘어간다)
  · 결제 흐름 밖은 담지 않는다 (광고·이미지가 섞이면 읽어야 할 것이 묻힌다)
  · 자격증명은 남기지 않는다 — 카드·연락처·카카오 거래 식별자까지
  · 그러면서 **키 이름과 구조는 남긴다** (그걸 배우는 게 목적이다)

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import booking  # noqa: E402


class FakeRequest:
    def __init__(self, url, method="POST", headers=None, post_data=None):
        self.url = url
        self.method = method
        self.headers = headers or {}
        self.post_data = post_data


class FakeResponse:
    def __init__(self, url, status=200, text="", raises=False):
        self.url = url
        self.status = status
        self._text = text
        self._raises = raises

    def text(self):
        if self._raises:
            raise RuntimeError("아직 못 읽는 응답")
        return self._text


class FakePage:
    """`on`/`remove_listener`만 있는 최소 페이지."""

    def __init__(self):
        self.handlers: dict[str, list] = {"request": [], "response": []}

    def on(self, event, handler):
        self.handlers[event].append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def fire(self, event, obj):
        for handler in list(self.handlers[event]):
            handler(obj)


PAY_URL = "https://cgv.co.kr/api/v1/payment/pay/searchGroupedPaymdList"
BRIDGE_URL = "https://online-payment.kakaopay.com/pc/bridge"


class TestWhatGetsRecorded(unittest.TestCase):
    """결제 흐름만 담고 나머지는 담지 않는다."""

    def test_payment_calls_are_kept(self):
        for url in (PAY_URL, BRIDGE_URL,
                    "https://cgv.co.kr/api/v1/booking/searchMovAtktSeatPrcList",
                    "https://onepg.cjsystems.co.kr/approve"):
            self.assertTrue(booking.pay_spec_match(url), url)

    def test_everything_else_is_dropped(self):
        for url in ("https://cdn.cgv.co.kr/static/logo.png",
                    "https://adimg.cgv.co.kr/banner.jpg",
                    "https://cgv.co.kr/_next/image?url=x",
                    "https://cgv.co.kr/api/v1/content/site/searchAllRegionAndSite"):
            self.assertFalse(booking.pay_spec_match(url), url)

    def test_the_booking_queries_are_kept_too(self):
        """선점 단계의 예매 조회도 담는다.

        좁게 걸었더니 `hrzoneCd`의 출처를 못 찾았다(2026-09-07) — 좌석맵에도
        예매정보에도 번들에도 없는 값이 판매정보에는 실려 있으니, 아직 안 보는
        조회가 하나 더 있다는 뜻이다. 이제 예매 조회 전부를 담는다.
        """
        for path in ("searchMovAtktSeatPrcList", "searchAtktAdncSeatInfo",
                     "searchIfSeatData"):
            self.assertTrue(
                booking.pay_spec_match(f"https://cgv.co.kr/api/v1/booking/{path}"),
                path)

    def test_the_real_payment_endpoints_are_covered(self):
        """번들에서 캐낸 실제 주소들 (2026-09-07, service/mpy/apiCpx.ts).

        **같은 오리진이 아니다.** 결제 모듈은 URL 매퍼를 쓰지 않고
        `https://api.cgv.co.kr/mpy/...` 를 직접 부른다. 처음에 `/api/v1/payment`만
        걸어 뒀다가 정작 결제 요청을 못 담는 것을 확인하고 고쳤다 — 관측 기회가
        자주 오지 않으므로 이걸 놓치면 그 한 번이 통째로 헛것이 된다.
        """
        for path in ("/mpy/pay/onlineAuthRequestReserve",   # 결제 요청
                     "/mpy/pay/searchGroupedPaymdList",     # 결제수단 목록
                     "/mpy/mpy/searchLastPayknd",           # 마지막 쓴 수단
                     "/mpy/pay/onlineApprovalSearchPayment",  # 승인 조회
                     "/mpy/iss/salCreateSal"):              # 매출 생성
            self.assertTrue(booking.pay_spec_match("https://api.cgv.co.kr" + path),
                            f"{path}를 놓친다 — 관측이 헛것이 된다")

    def test_the_pg_screens_own_assets_are_dropped(self):
        """PG 화면이 통째로 딸려 온다 — 주소가 결제 도메인인 것과 결제 요청인 것은 다르다.

        실제 관측에서 57건 중 30건이 onepg의 css·js·폰트였고, 파일을 454KB로
        부풀리면서 정작 읽어야 할 요청과 같은 건수 예산을 나눠 썼다.
        """
        base = "https://onepg.cjsystems.co.kr"
        for path in ("/css/style-GW-be09.css", "/js/jquery-3.6.0.min.js",
                     "/img/loading.gif", "/font/NotoSansKR-Regular.woff2",
                     "/favicon-8d54.ico", "/fa/css/all.min.css?v=3"):
            self.assertFalse(booking.pay_spec_match(base + path), path)
        # 정작 필요한 PG 페이지는 남아야 한다.
        self.assertTrue(booking.pay_spec_match(base + "/v2/pay/ready/RID20260907"))

    def test_the_approval_marks_are_all_covered(self):
        # 승인 계열이 나가면 반드시 남아야 한다 — 그게 가장 알고 싶은 순간이다.
        for mark in booking.PAYMENT_URL_MARKS:
            self.assertTrue(booking.pay_spec_match(f"https://cgv.co.kr/{mark}/x"),
                            mark)


class TestObservationNeverBreaksPayment(unittest.TestCase):
    """읽다 터져도 결제는 그대로 간다."""

    def setUp(self):
        self.page = FakePage()
        self.entries: list = []
        booking.watch_pay_requests(self.page, self.entries)

    def test_a_request_that_throws_is_skipped_quietly(self):
        class Exploding:
            @property
            def url(self):
                raise RuntimeError("못 읽는다")

        self.page.fire("request", Exploding())      # 예외가 밖으로 나오면 실패다
        self.assertEqual(self.entries, [])

    def test_a_body_that_cannot_be_read_still_records_the_call(self):
        self.page.fire("response", FakeResponse(PAY_URL, raises=True))
        self.assertEqual(len(self.entries), 1)
        self.assertIsNone(self.entries[0]["body"])
        self.assertEqual(self.entries[0]["url"], PAY_URL)

    def test_it_stops_at_the_cap(self):
        for _ in range(booking.PAY_SPEC_MAX_ENTRIES + 20):
            self.page.fire("request", FakeRequest(PAY_URL))
        self.assertEqual(len(self.entries), booking.PAY_SPEC_MAX_ENTRIES)

    def test_a_long_body_is_cut(self):
        self.page.fire("response", FakeResponse(PAY_URL, text="x" * 20000))
        body = self.entries[0]["body"]
        self.assertLess(len(body), 20000)
        self.assertIn("자름", body, "잘랐으면 잘랐다고 적어야 한다")

    def test_listeners_come_off(self):
        page = FakePage()
        handlers = booking.watch_pay_requests(page, [])
        for event, handler in zip(("request", "response"), handlers):
            page.remove_listener(event, handler)
        self.assertEqual(page.handlers, {"request": [], "response": []})


class TestSecretsAreNotWritten(unittest.TestCase):
    """가리되 형태는 남긴다 — 값을 지우고 키와 구조는 그대로 둔다."""

    def test_credentials_in_headers_are_masked(self):
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("request", FakeRequest(PAY_URL, headers={
            "authorization": "Bearer 아주-긴-토큰", "cookie": "accessToken=…",
            "content-type": "application/json"}))
        headers = entries[0]["headers"]
        self.assertNotIn("Bearer", str(headers["authorization"]))
        self.assertNotIn("accessToken", str(headers["cookie"]))
        # 가리지 않아도 되는 것까지 지우면 형태를 못 배운다.
        self.assertEqual(headers["content-type"], "application/json")

    def test_payment_details_are_masked(self):
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("request", FakeRequest(PAY_URL, post_data=json.dumps({
            "cardNo": "1234567812345678", "hpNo": "01012345678",
            "bymd": "19800101", "email": "a@b.c", "custNo": "118012814",
            "paymdCd": "KAKAOPAY", "amount": 15000})))
        body = entries[0]["body"]
        for field in ("cardNo", "hpNo", "bymd", "email", "custNo"):
            self.assertIn(field, body, f"{field} 키까지 사라졌다")
            self.assertIn("가림", str(body[field]), f"{field}가 그대로 남았다")
        # 결제수단·금액은 형태 그 자체라 남아야 한다.
        self.assertEqual(body["paymdCd"], "KAKAOPAY")
        self.assertEqual(body["amount"], 15000)

    def test_the_kakao_transaction_id_is_masked(self):
        # tid·hash만으로 그 결제를 이어받을 수 있다 — 짧게 사는 값이라도 남기지 않는다.
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("response", FakeResponse(BRIDGE_URL, text=json.dumps({
            "tid": "T1234567890abcdef", "hash": "abcdefghijklmnop",
            "expires_at": "2026-09-07T12:00:00"})))
        body = entries[0]["body"]
        self.assertIn("가림", str(body["tid"]))
        self.assertIn("가림", str(body["hash"]))
        self.assertEqual(body["expires_at"], "2026-09-07T12:00:00")

    def test_nested_structure_survives_masking(self):
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("response", FakeResponse(PAY_URL, text=json.dumps({
            "data": {"list": [{"paymdCd": "KAKAOPAY", "cardNo": "1111"}]}})))
        item = entries[0]["body"]["data"]["list"][0]
        self.assertEqual(item["paymdCd"], "KAKAOPAY")
        self.assertIn("가림", str(item["cardNo"]))

    def test_json_hidden_inside_a_string_is_masked_too(self):
        """`paymInfoCont`는 12KB짜리 JSON을 **문자열 하나로** 담는다.

        바깥만 훑으면 가린 줄 알고 안 가려진다 — 실제 관측(2026-09-07)에서 그
        안의 이름·휴대폰번호·회원번호가 그대로 남았다.
        """
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        inner = json.dumps({"cust": {"userNo": "118012814"},
                            "cjOneUser": {"memberNo": "9990023412181"},
                            "cashReceiptInfo": "01012345678",
                            "siteNm": "용산아이파크몰"}, ensure_ascii=False)
        page.fire("request", FakeRequest(PAY_URL, post_data=json.dumps(
            {"paymNo": "2026", "paymInfoCont": inner})))
        got = entries[0]["body"]["paymInfoCont"]
        self.assertIsInstance(got, str, "JSON 문자열이라는 사실이 사라지면 안 된다")
        for leak in ("118012814", "9990023412181", "01012345678"):
            self.assertNotIn(leak, got, f"{leak}가 그대로 남았다")
        # 구조와 안 가려도 되는 값은 남아야 한다.
        self.assertIn("cashReceiptInfo", got)
        self.assertIn("용산아이파크몰", got)

    def test_the_real_name_and_account_id_are_masked(self):
        # commonGetPayId 는 실명과 아이디를 평문으로 싣는다 (실제 관측).
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("request", FakeRequest(PAY_URL, post_data=json.dumps(
            {"userId": "hayato5246", "userName": "이원국",
             "goodsName": "스파이더맨 용산아이파크몰", "amountTotal": 28000},
            ensure_ascii=False)))
        body = entries[0]["body"]
        self.assertIn("가림", str(body["userId"]))
        self.assertIn("가림", str(body["userName"]))
        # 상품명·금액은 형태 그 자체다.
        self.assertEqual(body["amountTotal"], 28000)
        self.assertIn("스파이더맨", body["goodsName"])

    def test_deep_masking_survives_a_self_referencing_shape(self):
        deep = {"a": {}}
        node = deep["a"]
        for _ in range(40):
            node["a"] = {}
            node = node["a"]
        booking.mask_secrets_deep(deep)      # 멈추지 않으면 실패다

    def test_secrets_in_the_query_string_are_masked(self):
        """GET 요청은 신원을 쿼리로 나른다.

        헤더와 본문만 가리면 가린 줄 알고 안 가려진다 — 실제 관측에서
        `searchImdtlDcList?…&custNo=118012814`가 그대로 남았다.
        """
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("request", FakeRequest(
            "https://cgv.co.kr/api/v1/payment/iss/searchImdtlDcList"
            "?coCd=A420&scnYmd=20260908&custNo=118012814&movNo=30001192",
            method="GET"))
        url = entries[0]["url"]
        self.assertNotIn("118012814", url)
        # 형태 그 자체인 값은 남아야 한다.
        for keep in ("coCd=A420", "scnYmd=20260908", "movNo=30001192",
                     "searchImdtlDcList"):
            self.assertIn(keep, url)

    def test_a_url_without_a_query_is_untouched(self):
        plain = "https://api.cgv.co.kr/mpy/pay/onlineAuthRequestReserve"
        self.assertEqual(booking.mask_url_secrets(plain), plain)

    def test_a_non_json_body_is_kept_as_text(self):
        page, entries = FakePage(), []
        booking.watch_pay_requests(page, entries)
        page.fire("response", FakeResponse(PAY_URL, text="<html>오류</html>"))
        self.assertEqual(entries[0]["body"], "<html>오류</html>")


class TestTheRecordOnDisk(unittest.TestCase):
    CTX = {"party": 2, "seat_labels": ["E7", "E8"], "scn_ymd": "20260908",
           "start_hhmm": "09:00", "site_nm": "용산아이파크몰"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_dir = booking.PAY_SPEC_DIR
        booking.PAY_SPEC_DIR = Path(self.tmp.name) / "payspec"

    def tearDown(self):
        booking.PAY_SPEC_DIR = self.real_dir
        self.tmp.cleanup()

    def record(self, entries, **over):
        opts = {"method": "kakaopay", "error": None, "got_bridge": True}
        opts.update(over)
        path = booking.record_pay_spec(entries, self.CTX, **opts)
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else None

    def test_nothing_observed_writes_nothing(self):
        self.assertIsNone(booking.record_pay_spec([], self.CTX,
                                                  method="kakaopay", error=None,
                                                  got_bridge=False))

    def test_a_successful_run_is_marked_successful(self):
        out = self.record([{"쪽": "요청", "url": PAY_URL}])
        self.assertTrue(out["성공"])
        self.assertIsNone(out["오류"])
        self.assertEqual(out["결제수단"], "kakaopay")

    def test_a_failed_run_is_kept_too(self):
        # 어디까지 갔다가 무엇에서 멎었는지가 형태만큼 중요한 단서다.
        out = self.record([{"쪽": "요청", "url": PAY_URL}],
                          error="결제 화면으로 넘어가지 못했습니다", got_bridge=False)
        self.assertFalse(out["성공"])
        self.assertIn("넘어가지 못했습니다", out["오류"])

    def test_a_run_without_a_bridge_answer_is_not_successful(self):
        out = self.record([{"쪽": "요청", "url": PAY_URL}], got_bridge=False)
        self.assertFalse(out["성공"])

    def test_it_says_which_booking_this_was(self):
        # 바디의 값이 무엇과 맞는지 대조하려면 이게 있어야 한다.
        out = self.record([{"쪽": "요청", "url": PAY_URL}])
        self.assertEqual(out["seats"], ["E7", "E8"])
        self.assertEqual(out["party"], 2)
        self.assertEqual(out["scn_ymd"], "20260908")

    def test_the_exchange_is_kept_in_order(self):
        entries = [{"쪽": "요청", "url": PAY_URL}, {"쪽": "응답", "url": PAY_URL},
                   {"쪽": "요청", "url": BRIDGE_URL}]
        out = self.record(entries)
        self.assertEqual([e["url"] for e in out["오간것"]],
                         [PAY_URL, PAY_URL, BRIDGE_URL])

    def test_an_unwritable_directory_is_not_an_error(self):
        # 관찰 실패로 결제를 망치지 않는다.
        booking.PAY_SPEC_DIR = Path("/proc/그럴리없는/자리")
        self.assertIsNone(self.record([{"쪽": "요청", "url": PAY_URL}]))


if __name__ == "__main__":
    unittest.main()
