#!/usr/bin/env python3
"""결제 판매정보 만들기 (booking.pay_mov_block · compare_pay_body).

**아직 보내지 않는다.** 이 코드가 만든 것을 실제 관측과 대조만 한다
(compare_payspec.py). 그래도 여기서 고정해 둘 것이 있다 — 대조는 관측이 있어야
돌지만, 아래 규칙들은 관측 없이도 지켜져야 하고 조용히 어긋나면 대조 결과를
믿을 수 없게 된다:

  · 좌석과 가격은 **seatLocNo로 짝짓는다** (순서를 믿지 않는다)
  · 상영표가 null로 주는 포스터 주소를 경로 조각으로 조립한다
  · 값이 아니라 **어긋난 자리**를 돌려준다 (값에는 남기면 안 되는 게 섞여 있다)
  · 가려진 값은 비교하지 않는다 (기록 파일로 대조할 때 만난다)

필드의 출처는 logs/payspec/ 관측 5건에서 확정했다. 2026-09-07 기준으로 관측
3건에 대해 차이 0이다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import booking  # noqa: E402


ROW = {
    "siteNo": "0013", "scnYmd": "20260908", "scnsNo": "012", "scnSseq": "1",
    "scnsrtTm": "0900", "scnendTm": "1135", "scnsNm": "14관", "movNm": "스파이더맨",
    "expoScnsNm": "14관[SCREENX] (Laser)", "prodNm": "스파이더맨(SCREENX 2D)",
    "prodNo": "20054557", "movNo": "30001192", "movfNo": "50002588",
    "physcFilePathnm": "030001/30001192/30001192_185.jpg",
    "scnsGradCd": "0401", "tcscnsGradCd": "04", "sascnsGradCd": "01",
    "bzplcNo": "0013001", "cxprdYn": "N", "cratgClsCd": "03", "vatincYn": "Y",
    "prcrulDivCd": "01", "prdcmpTypCd": "01", "prddtlTypCd": "0101",
    "prdtypCd": "01", "movTirCd": "01", "movkndCd": "38", "salsTznCd": "01",
    "siteGradCd": "01", "srvltKindCd": "01", "movkndDsplEnm": "SCREENX 2D",
    "speclIndctTypCd": "01",
}


def seat(row: str, no: int, loc: str):
    return {"row": row, "no": str(no), "label": f"{row}{no}",
            "seat_loc_no": loc, "sbord_no": "002", "seat_area_no": "001",
            "szone_no": "01001", "stknd_cd": "29", "szone_kind_cd": "01",
            "seat_salfrm_cd": "01", "kind": "컴포트석B", "zone": "일반존",
            "szone_kind_nm": "일반", "hrzone_cd": ""}


SEATS = [seat("E", 7, "00200100190011"), seat("E", 8, "00200100210011")]
PRICES = [
    {"seatLocNo": "00200100210011", "salAmt": 15000, "scnAmt": 11000,
     "tcsvcAmt": 3000, "sasvcAmt": 1000, "prodBnduCd": "01"},
    {"seatLocNo": "00200100190011", "salAmt": 14000, "scnAmt": 10000,
     "tcsvcAmt": 3000, "sasvcAmt": 1000, "prodBnduCd": "01"},
]
CTX = {"scn_ymd": "20260908", "site_no": "0013", "row": ROW}
ADNC = {"rlsYmd": "20260729", "koficMovfCd": "202627703"}


def build(**over):
    args = {"ctx": CTX, "identity": {"cust_no": "118012814"}, "seats": SEATS,
            "prices": PRICES, "mov_atkt_no": "0013260907035024676",
            "adnc": ADNC}
    args.update(over)
    return booking.pay_mov_block(args["ctx"], args["identity"], args["seats"],
                                 args["prices"], args["mov_atkt_no"],
                                 args["adnc"])


class TestSeatsAndPricesArePairedByLocation(unittest.TestCase):
    """가격 조회가 좌석 순서를 지킨다는 보장이 없다.

    **순서로 짝지으면 값이 조용히 뒤바뀐다.** 위 PRICES는 일부러 좌석과 반대
    순서로 두었다 — E7이 14000, E8이 15000이어야 한다.
    """

    def test_each_seat_gets_its_own_price(self):
        products = build()["sellProductsList"]
        self.assertEqual([p["ticketProducts"]["seatRowNm"] + p["ticketProducts"]["seatNo"]
                          for p in products], ["E7", "E8"])
        self.assertEqual([p["salAmt"] for p in products], [14000, 15000])

    def test_the_total_adds_up(self):
        self.assertEqual(build()["sumSalAmt"], 29000)

    def test_a_seat_without_a_price_does_not_crash(self):
        out = build(prices=[])
        self.assertIsNone(out["sellProductsList"][0]["salAmt"])
        self.assertEqual(out["sumSalAmt"], 0)


class TestValuesWeAssemble(unittest.TestCase):
    def test_the_poster_url_is_built_from_the_path_fragment(self):
        # 상영표는 prodImg를 null로 주고 경로 조각만 준다.
        self.assertEqual(
            build()["prodImg"],
            "https://cdn.cgv.co.kr/cgvpomsfilm/Movie/Thumbnail/Poster/"
            "030001/30001192/30001192_185.jpg")

    def test_no_path_fragment_means_no_poster(self):
        row = {k: v for k, v in ROW.items() if k != "physcFilePathnm"}
        self.assertIsNone(build(ctx={**CTX, "row": row})["prodImg"])

    def test_the_showtime_is_a_range(self):
        # 상영표는 '0900'·'1135'로 준다.
        self.assertEqual(build()["scnTm"], "09:00~11:35")

    def test_the_display_title_wins_over_the_original(self):
        # ticketProducts.movNm은 원제가 아니라 '…(SCREENX 2D)'다.
        out = build()
        self.assertEqual(out["movNm"], "스파이더맨(SCREENX 2D)")
        self.assertEqual(out["orgMovNm"], "스파이더맨")
        self.assertEqual(
            out["sellProductsList"][0]["ticketProducts"]["movNm"],
            "스파이더맨(SCREENX 2D)")

    def test_the_combined_grade_comes_from_the_showtime(self):
        # 04 + 01을 우리가 붙이는 게 아니라 상영표의 scnsGradCd를 쓴다.
        self.assertEqual(build()["itgrScnsGradCd"], "0401")

    def test_the_ticket_release_date_is_the_screening_date(self):
        # ticketProducts.rlsYmd는 개봉일이 아니라 상영일이다 (실측).
        out = build()["sellProductsList"][0]["ticketProducts"]
        self.assertEqual(out["rlsYmd"], "20260908")
        # mov 층의 rlsYmd는 진짜 개봉일이고, 회차 예매 정보에서만 온다.
        self.assertEqual(build()["rlsYmd"], "20260729")

    def test_the_zone_code_follows_the_showtime_not_the_seat(self):
        """`hrzoneCd`는 판매시간대코드(salsTznCd)와 같은 값이다.

        이름이 구역(zone) 코드처럼 생겼지만 **회차의 속성**이다. 처음엔 "01"이
        상수인 줄 알았고(관측 8건이 그랬다), 다른 회차의 관측이 '24'를 내면서
        틀렸다는 게 드러났다. 좌석 단위로 넘겨짚었으면 같은 회차의 다른 좌석에서
        또 틀렸을 것이다 — 관측 10건·좌석 21개에서 두 값 모두 일치했다.
        """
        for zone in ("01", "24"):
            row = {**ROW, "salsTznCd": zone}
            out = build(ctx={**CTX, "row": row})["sellProductsList"]
            for product in out:
                self.assertEqual(product["ticketProducts"]["hrzoneCd"], zone)
                self.assertEqual(product["ticketProducts"]["salsTznCd"], zone)

    def test_the_ticket_count_is_announced(self):
        out = build()
        self.assertEqual(out["bnduQty"], "2")
        self.assertEqual(out["movDtlKindsList"],
                         [{"cratgClsNm": "일반", "atktQty": "2",
                           "prodBnduCd": "01"}])
        self.assertEqual(out["discountDatas"], [None, None])

    def test_vat_is_not_carried_at_the_movie_level(self):
        # 상영표는 "Y"를 주지만 mov 층에는 **키 자체가 없다**(실측).
        self.assertNotIn("vatincYn", build())

    def test_the_booking_number_reaches_every_seat(self):
        for product in build()["sellProductsList"]:
            self.assertEqual(product["movAtktNo"], "0013260907035024676")
            self.assertEqual(product["ticketProducts"]["movAtktNo"],
                             "0013260907035024676")


class TestComparing(unittest.TestCase):
    """대조는 **값이 아니라 어긋난 자리**를 돌려준다."""

    def test_identical_bodies_have_no_differences(self):
        self.assertEqual(booking.compare_pay_body(build(), build()), [])

    def test_a_changed_value_names_its_path(self):
        theirs = build()
        ours = build()
        ours["sellProductsList"][1]["salAmt"] = 99
        diffs = booking.compare_pay_body(ours, theirs)
        self.assertEqual(diffs, ["sellProductsList[1].salAmt"])

    def test_it_does_not_leak_the_values(self):
        theirs = build()
        ours = build()
        ours["custNo"] = "비밀"
        self.assertNotIn("비밀", " ".join(booking.compare_pay_body(ours, theirs)))

    def test_a_missing_field_says_which_side(self):
        theirs, ours = build(), build()
        del ours["movNo"]
        self.assertIn("movNo (우리 쪽에 없음)", booking.compare_pay_body(ours, theirs))
        theirs2, ours2 = build(), build()
        del theirs2["movNo"]
        self.assertIn("movNo (CGV 쪽에 없음)",
                      booking.compare_pay_body(ours2, theirs2))

    def test_a_number_and_its_string_are_the_same(self):
        # CGV는 같은 뜻을 자리마다 다르게 쓴다 — 그걸 차이로 세면 소음이다.
        self.assertEqual(booking.compare_pay_body({"a": 2}, {"a": "2"}), [])

    def test_a_masked_value_is_not_compared(self):
        # 기록 파일로 대조할 때 만난다. 가린 자리마다 차이가 나면 볼 것이 묻힌다.
        self.assertEqual(
            booking.compare_pay_body({"custNo": "118012814"},
                                     {"custNo": "(가림: 9자)"}), [])

    def test_a_different_length_list_is_one_difference_not_many(self):
        diffs = booking.compare_pay_body({"a": [1, 2]}, {"a": [1, 2, 3]})
        self.assertEqual(diffs, ["a (개수 2 ≠ 3)"])

    def test_fields_that_always_differ_are_skipped(self):
        # paymNo·paymVrifyNo·szoneExpTm 은 매번 달라지는 게 정상이다.
        for field in ("paymNo", "paymVrifyNo", "szoneExpTm", "traceNo"):
            self.assertEqual(
                booking.compare_pay_body({field: "a"}, {field: "b"}), [],
                f"{field}를 차이로 셌다")


class TestAmounts(unittest.TestCase):
    """부가세를 가르는 규칙. 틀리면 결제창에 다른 금액이 뜬다."""

    def test_the_observed_amounts_split_the_way_cgv_did(self):
        # 실측 3건 — 서로 다른 영화·극장·인원에서 나온 금액이다.
        for total, vat, tax in ((28000, 2545, 25455), (20000, 1818, 18182),
                                (45000, 4091, 40909)):
            got = booking.pay_amounts(total)
            self.assertEqual((got["amountVat"], got["amountTax"]), (vat, tax),
                             f"{total}원을 잘못 갈랐다")

    def test_the_parts_add_back_up(self):
        for total in (0, 1, 9999, 10000, 33333, 1_000_000):
            got = booking.pay_amounts(total)
            self.assertEqual(got["amountVat"] + got["amountTax"], total)

    def test_films_are_never_tax_free(self):
        self.assertEqual(booking.pay_amounts(28000)["amountTaxFree"], 0)


class TestTheVerifyNumber(unittest.TestCase):
    """대조번호는 **우리가 지어내는** 값이다 — CGV가 주지 않는다."""

    def test_it_looks_like_the_observed_ones(self):
        got = booking.pay_verify_no()
        self.assertEqual(len(got), 26)
        self.assertTrue(got.isalnum(), got)

    def test_two_calls_do_not_repeat(self):
        # 남이 맞힐 수 있으면 대조번호의 뜻이 없어진다.
        made = {booking.pay_verify_no() for _ in range(200)}
        self.assertEqual(len(made), 200)


class TestPaymentRequests(unittest.TestCase):
    IDENTITY = {"user_id": "tester", "user_nm": "홍길동"}
    CTX = {"row": ROW, "site_nm": "용산아이파크몰", "site_no": "0013"}

    def test_the_goods_name_is_title_plus_theatre(self):
        self.assertEqual(booking.pay_goods_name(self.CTX),
                         "스파이더맨(SCREENX 2D) 용산아이파크몰")

    def test_the_pay_id_request_carries_the_plain_account(self):
        # commonGetPayId만은 아이디·이름을 가리지 않고 싣는다(실측).
        got = booking.pay_id_body(self.CTX, self.IDENTITY, 28000, 2,
                                  today="20260907")
        self.assertEqual(got["userId"], "tester")
        self.assertEqual(got["userName"], "홍길동")
        self.assertEqual(got["goodsCnt"], "2")
        self.assertEqual(got["amountTotal"], 28000)
        # 이 요청에서는 세금 칸이 전부 0이다 — 가르는 것은 결제 요청 쪽이다.
        self.assertEqual((got["amountVat"], got["amountTax"]), (0, 0))

    def test_the_auth_request_matches_the_observed_shape(self):
        got = booking.pay_auth_body(
            "20260907000178278015", "WulZilqB8W8ncn8c3JpgzUmyAu", 28000,
            user_phone="01012345678", today="20260907")
        self.assertEqual(got["paykndCd"], "1007")
        self.assertEqual(got["payMethod"], "kakaoCert")
        self.assertEqual((got["amountVat"], got["amountTax"]), (2545, 25455))
        self.assertEqual(
            got["redirectUrl"],
            "https://cgv.co.kr/api/pg?paymNo=20260907000178278015"
            "&paymVrifyNo=WulZilqB8W8ncn8c3JpgzUmyAu"
            "&host=https%3A%2F%2Fcgv.co.kr")

    def test_the_verify_number_reaches_the_redirect(self):
        # 돌아올 때 이 값으로 그 결제가 우리 것인지 가린다 — 빠지면 못 찾는다.
        made = booking.pay_verify_no()
        got = booking.pay_auth_body("2026", made, 10000, user_phone=None)
        self.assertIn(f"paymVrifyNo={made}", got["redirectUrl"])


class TestTheKakaoLink(unittest.TestCase):
    """결제 링크는 **게이트웨이 응답의 hash**로만 만든다."""

    # 실제 관측(2026-09-07 13:41 예매)에서 나온 값.
    HASH = ("b681023c1c465105c827e5b1fd5d8a038b2734e2"
            "ea24972c375a6628c08d1fbf8")
    STAMP = 1788789382

    def test_the_link_matches_the_one_that_was_actually_sent(self):
        url, _ = booking.kakao_link_from_gateway(
            {"hash": self.HASH, "expired_timestamp": self.STAMP})
        self.assertEqual(
            url,
            "https://online-payment.kakaopay.com/bridge/mobile-pc/"
            f"reseller/one-time/payment/{self.HASH}")

    def test_the_expiry_is_read_as_korean_wall_clock(self):
        """**유닉스 시각이 아니다.**

        13:41에 만든 링크의 수명은 15분인데(README 실측), 유닉스로 읽으면
        22:56이 나온다. 만료를 늦게 잡으면 이미 죽은 링크를 살아 있다고
        알리게 되고, 사용자는 눌러 보고서야 안다.
        """
        _, expires = booking.kakao_link_from_gateway(
            {"hash": self.HASH, "expired_timestamp": self.STAMP})
        self.assertEqual(expires.strftime("%Y-%m-%d %H:%M:%S"),
                         "2026-09-07 13:56:22")
        self.assertEqual(str(expires.tzinfo), "Asia/Seoul")

    def test_no_hash_means_no_link(self):
        # 죽은 링크를 보내느니 링크 없이 실패로 알린다.
        for body in ({}, {"hash": ""}, {"expired_timestamp": 1}, None, "글자"):
            self.assertEqual(booking.kakao_link_from_gateway(body),
                             (None, None), repr(body))

    def test_an_unreadable_expiry_still_yields_the_link(self):
        # 만료를 못 읽어도 링크는 쓸 수 있다 — 링크까지 버리면 손해가 크다.
        url, expires = booking.kakao_link_from_gateway(
            {"hash": self.HASH, "expired_timestamp": "언제"})
        self.assertTrue(url)
        self.assertIsNone(expires)

    def test_the_form_action_hash_is_not_the_link(self):
        """폼 action의 것은 64자, 쓸 수 있는 해시는 65자다.

        한 글자 차이라 눈으로는 같아 보이고, 열면 "인증정보를 찾을 수 없습니다"가
        떠서 만료로 착각하기 딱 좋다. 이 코드를 만들면서 실제로 한 번 빠졌다.
        """
        html = ('<form id="kakaoPayForm" method="post" action='
                f'"https://online-payment.kakaopay.com/bridge/pc/reseller/'
                f'one-time/payment/{self.HASH[:64]}"><input name="tid"></form>')
        got = booking.pg_bridge_id(html)
        self.assertEqual(got, self.HASH[:64])
        self.assertEqual(len(got), 64)
        self.assertNotEqual(got, self.HASH, "64자를 링크에 쓰면 안 된다")

    def test_a_page_without_the_form_says_so(self):
        self.assertIsNone(booking.pg_bridge_id("<html>오류</html>"))
        self.assertIsNone(booking.pg_bridge_id(""))


class TestReadingTheAccountFromThePaymentPage(unittest.TestCase):
    """접속 IP와 고객 정보는 **결제 화면 문서에만** 있다.

    결제 단계의 어떤 API도 이 값을 주지 않는다 — CGV가 서버에서 그려 넣어 보낸다.
    한동안 못 찾았던 이유는 기록기가 그 문서를 16KB에서 잘라 버려서였다.
    """

    # 실제 문서의 모양만 흉내낸 것 — 값은 전부 지어낸 것이다.
    PAGE = (r'{\"state\":{},\"children\":[\"$\",\"$L31\",null,'
            r'{\"errorMessage\":null,\"ipAddress\":\"10.0.0.1\",'
            r'\"cust\":{\"coCd\":\"A420\",\"userId\":\"tester\",'
            r'\"userNo\":\"111111111\",\"userName\":\"홍길동\",'
            r'\"userCellPhone\":\"01000000000\",\"cusgdCd\":\"01\"},'
            r'\"cjOneUser\":{\"memberRegYn\":\"Y\",'
            r'\"memberNo\":\"9990000000000\",\"memberName\":\"홍길동\",'
            r'\"avlPoint\":100}}]}')

    def test_it_reads_the_three_blocks(self):
        got = booking.parse_pay_account(self.PAGE)
        self.assertEqual(got["ipAddress"], "10.0.0.1")
        self.assertEqual(got["cust"]["userId"], "tester")
        self.assertEqual(got["cust"]["userNo"], "111111111")
        self.assertEqual(got["cjOneUser"]["memberNo"], "9990000000000")
        self.assertEqual(got["cjOneUser"]["avlPoint"], 100)

    def test_a_page_without_the_data_is_empty_not_wrong(self):
        got = booking.parse_pay_account("<html>로그인이 필요합니다</html>")
        self.assertEqual(got["cust"], {})
        self.assertEqual(got["ipAddress"], "")

    def test_nested_braces_do_not_confuse_the_scan(self):
        page = r'\"cust\":{\"a\":{\"b\":{\"c\":1}},\"d\":2}, 뒤에 딴 것'
        self.assertEqual(booking.parse_pay_account(page)["cust"],
                         {"a": {"b": {"c": 1}}, "d": 2})

    def test_a_brace_inside_a_string_does_not_confuse_the_scan(self):
        page = r'\"cust\":{\"nm\":\"괄호 { 있음\",\"x\":1}'
        self.assertEqual(booking.parse_pay_account(page)["cust"],
                         {"nm": "괄호 { 있음", "x": 1})


class TestWrappingTheAccount(unittest.TestCase):
    """CGV가 감싸는 자리만 감싼다 — 더 가리면 CGV가 못 읽는다."""

    ACCOUNT = {"ipAddress": "10.0.0.1",
               "cust": {"userId": "tester", "userName": "홍길동",
                        "userCellPhone": "01000000000", "userEmail": "",
                        "userNo": "111111111", "custNo": "111111111",
                        "itgrCustNo": "999", "cusgdCd": "01"},
               "cjOneUser": {"memberName": "홍길동", "mobileNo": "01000000000",
                             "memberNo": "999", "avlPoint": 100}}

    def test_the_customer_numbers_stay_plain(self):
        # 관측에서 userNo·custNo·itgrCustNo·cusgdCd가 평문이었다.
        got = booking.encrypt_pay_account(self.ACCOUNT)["cust"]
        for field in ("userNo", "custNo", "itgrCustNo", "cusgdCd"):
            self.assertEqual(got[field], self.ACCOUNT["cust"][field], field)

    def test_the_personal_fields_are_wrapped(self):
        got = booking.encrypt_pay_account(self.ACCOUNT)
        for block, fields in booking.PAY_ENCRYPTED_FIELDS.items():
            for field in fields:
                plain = self.ACCOUNT[block].get(field)
                if plain:
                    self.assertNotEqual(got[block][field], plain,
                                        f"{block}.{field}를 안 감쌌다")
        self.assertNotEqual(got["ipAddress"], "10.0.0.1")

    def test_an_empty_field_stays_empty(self):
        # CGV도 빈 자리는 감싸지 않는다.
        self.assertEqual(
            booking.encrypt_pay_account(self.ACCOUNT)["cust"]["userEmail"], "")

    def test_wrapping_does_not_disturb_the_rest(self):
        got = booking.encrypt_pay_account(self.ACCOUNT)
        self.assertEqual(got["cjOneUser"]["avlPoint"], 100)

    def test_the_same_input_wraps_the_same_way(self):
        # ECB라 같은 값은 같은 결과다 — CGV가 그렇게 만든다.
        first = booking.encrypt_pay_account(self.ACCOUNT)
        second = booking.encrypt_pay_account(self.ACCOUNT)
        self.assertEqual(first, second)


class TestThePaymentTabIsKept(unittest.TestCase):
    """**링크가 나갔으면 그 탭을 닫지 않는다.**

    카카오페이 브릿지가 그 탭에서 승인 상태를 폴링하고, 승인이 오면 거기서
    CGV의 완료 주소로 넘어가면서 매출이 만들어진다. 링크만 챙기고 닫아 버렸더니
    사용자가 승인했는데 예매 내역이 0건이었다(2026-09-07). 돈은 나가고 표는 안
    나오는 자리라, 이 규칙은 테스트로 못박아야 한다.
    """

    HASH = "b" * 65

    class FakePage:
        def __init__(self, gateway):
            self.closed = False
            self._gateway = gateway
            self._handlers = []

        def on(self, event, handler):
            self._handlers.append(handler)

        def remove_listener(self, event, handler):
            pass

        def close(self):
            self.closed = True

        def goto(self, url, **kw):
            class _R:
                def __init__(self, u, body):
                    self.url, self._b = u, body
                    self.status = 200

                def text(self):
                    import json as _j
                    return _j.dumps(self._b)

            if self._gateway is not None:
                url = ("https://pay-api-gw.kakaopay.com/"
                       "online-payment-internal/v2/x")
                for handler in self._handlers:
                    handler(_R(url, self._gateway))

        def wait_for_timeout(self, ms):
            pass

        def evaluate(self, script, arg=None):
            return 1

    class FakeSession:
        def __init__(self, gateway):
            self.kept: list = []
            self._page = TestThePaymentTabIsKept.FakePage(gateway)

        @property
        def page(self):
            outer = self

            class _Ctx:
                def new_page(self_inner):
                    return outer._page

            class _P:
                context = _Ctx()

            return _P()

        def keep_page(self, page, seconds):
            self.kept.append((page, seconds))
            return True

    def open_link(self, gateway):
        """**`run`이라고 이름 짓지 않는다.** TestCase.run을 덮어써서 이 클래스의
        테스트가 통째로 실행되지 않았다 — "Ran 0 tests"로 조용히 넘어갔다.
        """
        session = self.FakeSession(gateway)
        out = booking.fetch_pay_link(session, "https://onepg/x", timeout_ms=50)
        return session, session._page, out

    def test_a_link_means_the_tab_is_handed_over_not_closed(self):
        session, page, out = self.open_link(
            {"hash": self.HASH, "expired_timestamp": 1788789382})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertFalse(page.closed, "링크가 나갔는데 탭을 닫았다")
        self.assertEqual(len(session.kept), 1, "탭을 맡기지 않았다")

    def test_the_tab_is_kept_at_least_a_minute(self):
        _, _, _ = self.open_link({"hash": self.HASH,
                            "expired_timestamp": 1788789382})
        session, _, _ = self.open_link({"hash": self.HASH})   # 만료 못 읽는 경우
        self.assertGreaterEqual(session.kept[0][1],
                                booking.PAY_PAGE_MIN_KEEP_SECONDS)

    def test_no_link_means_the_tab_is_closed(self):
        # 승인이 올 수 없다고 확정된 경우다 — 남겨 둘 이유가 없다.
        session, page, out = self.open_link(None)
        self.assertFalse(out["ok"])
        self.assertTrue(page.closed, "쓸 수 없는 탭을 남겼다")
        self.assertEqual(session.kept, [])


if __name__ == "__main__":
    unittest.main()
