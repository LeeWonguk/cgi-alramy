#!/usr/bin/env python3
"""API로 바로 거는 좌석 선점 (booking.hold_api · watch.booking_identity).

화면을 몰지 않는 대신 우리가 요청을 **직접 만든다**. 그러면 UI 경로에서는 CGV의
JS가 책임지던 것들이 전부 이쪽 책임이 된다 — 이 파일이 지키는 게 그것이다:

  · 바디의 회차·좌석이 의도한 것과 같다 (다르면 보내지 않는다)
  · 좌석 식별자가 하나라도 비면 보내지 않는다 (빈 값은 422로 돌아온다)
  · 거절당하면 CGV가 준 문구를 그대로 전한다 (우리 말로 바꾸지 않는다)
  · 실패해도 '시도한 좌석'을 잃지 않는다 (이력에 남아야 한다)

브라우저도 DB도 필요 없다. 세션은 아래 FakeSession이 대신한다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import booking  # noqa: E402
import watch  # noqa: E402


# ── 좌석맵 한 조각 ──────────────────────────────────────────────────────────
def seat(row: str, no: int, *, available=True, x=None, loc=None):
    """parse_seats가 내놓는 모양의 좌석 하나."""
    x = x if x is not None else no * 2
    return {
        "row": row, "no": str(no), "label": f"{row}{no}",
        "available": available, "kind": "일반석", "zone": "",
        "x_start": x, "x_end": x + 2, "left_pway": False, "right_pway": False,
        "seat_loc_no": loc if loc is not None else f"0020010{no:04d}0011",
        "sbord_no": "002", "seat_area_no": "001", "szone_no": "01001",
        "stknd_cd": "01", "szone_kind_cd": "01", "seat_salfrm_cd": "01",
    }


# E7·E8만 비어 있는 열. **딱 두 자리만 남겨 둔다** — 전부 비어 있으면
# pick_block이 구간 가운데(E5·E6)를 고르므로, 감지 때의 후보와 선점 직전에 다시
# 고른 결과가 우연히 달라져 무엇을 시험하는지가 흐려진다.
ROW_E = [seat("E", n, available=(n in (7, 8))) for n in range(1, 11)]

IDENTITY = {"cust_no": "123456789", "cusgd_cd": "01", "user_id": "tester"}

CTX = {
    "mov_nm": "오디세이", "site_nm": "용산아이파크몰", "scn_ymd": "20260908",
    "start_hhmm": "09:00", "seat_labels": ["E7", "E8"], "party": 2,
    "site_no": "0013", "rows": ["E"], "num_from": 0, "num_to": 0,
    "mov_no": "30001323", "scns_nm": "1관",
    "row": {"scnsNo": "012", "scnSseq": "1", "siteNo": "0013"},
}


class FakeSession:
    """CgvSession 중 hold_api가 실제로 쓰는 것만 흉내낸다."""

    def __init__(self, *, seats=None, response=None, identity=None,
                 seat_map_error=None, post_error=None):
        self._seats = ROW_E if seats is None else seats
        self._response = response or {"status": 200, "body": {
            "data": {"resultCode": "0", "movAtktNo": "A1234567",
                     "seatTempPrmpLimitDt": "20260908090500"}}, "text": ""}
        self._identity = identity if identity is not None else IDENTITY
        self._seat_map_error = seat_map_error
        self._post_error = post_error
        self.sent: list[tuple[str, dict]] = []
        self.seat_map_calls: list[dict] = []

    def identity(self):
        if isinstance(self._identity, Exception):
            raise self._identity
        return self._identity

    def seat_map(self, **kwargs):
        self.seat_map_calls.append(kwargs)
        if self._seat_map_error:
            raise self._seat_map_error
        # hold_api는 seats.parse_seats를 거치지 않고 live_seats를 쓰므로,
        # 여기서는 parse_seats가 읽는 원본 모양을 돌려줘야 한다.
        return {"items": [{"seats": [
            {"seatRowNm": s["row"], "seatNo": s["no"],
             "seatSaleYn": "Y" if s["available"] else "N",
             "stkndNm": s["kind"], "szoneNm": s["zone"],
             "xcoordStartVal": s["x_start"], "xcoordEndVal": s["x_end"],
             "leftPwayYn": "N", "rghtPwayYn": "N",
             "seatLocNo": s["seat_loc_no"], "sbordNo": s["sbord_no"],
             "seatAreaNo": s["seat_area_no"], "szoneNo": s["szone_no"],
             "stkndCd": s["stknd_cd"], "szoneKindCd": s["szone_kind_cd"],
             "seatSalfrmCd": s["seat_salfrm_cd"]}
            for s in self._seats]}]}

    def post_json(self, path, body):
        self.sent.append((path, body))
        if self._post_error:
            raise self._post_error
        return self._response


# ── 요청 바디 ───────────────────────────────────────────────────────────────
class TestHoldBody(unittest.TestCase):
    """우리가 만드는 바디가 CGV가 받던 것과 같은 모양인지.

    기준은 `logs/holdspec/20260904-160504_hold.json` — CGV의 JS가 실제로 보낸
    요청이다. 추측한 필드는 없어야 한다.
    """

    def body(self, **over):
        ctx = {**CTX, **over}
        chosen = [seat("E", 7), seat("E", 8)]
        return booking.hold_body(ctx, IDENTITY, chosen)

    def test_identity_comes_from_the_token_not_from_us(self):
        body = self.body()
        self.assertEqual(body["custNo"], "123456789")
        self.assertEqual(body["cusgdCd"], "01")

    def test_it_names_the_showtime_the_watch_meant(self):
        body = self.body()
        self.assertEqual(body["siteNo"], "0013")
        self.assertEqual(body["scnYmd"], "20260908")
        self.assertEqual(body["scnsNo"], "012")
        self.assertEqual(body["scnSseq"], "1")

    def test_every_observed_fixed_field_is_present(self):
        # 값이 늘 같다고 빼면 안 된다 — CGV는 키의 존재를 본다.
        body = self.body()
        for field, value in booking.HOLD_FIXED_FIELDS.items():
            self.assertIn(field, body, f"{field}가 빠졌다")
            self.assertEqual(body[field], value)
        self.assertEqual(body["coCd"], "A420")

    def test_each_seat_carries_all_six_identifiers(self):
        for item in self.body()["seatPrmpDataList"]:
            self.assertEqual(sorted(item), sorted(
                f for f, _ in booking.HOLD_SEAT_FIELDS))

    def test_a_seat_missing_an_identifier_is_refused(self):
        blank = seat("E", 7)
        blank["seat_loc_no"] = ""
        with self.assertRaises(ValueError) as caught:
            booking.hold_body(CTX, IDENTITY, [blank])
        # 무엇이 없어서 못 보내는지 사람이 알아야 한다.
        self.assertIn("seatLocNo", str(caught.exception))

    def test_no_seats_is_refused(self):
        with self.assertRaises(ValueError):
            booking.hold_body(CTX, IDENTITY, [])

    def test_the_body_passes_our_own_gate(self):
        # 만드는 코드와 검사하는 코드가 따로 있어야 한쪽이 틀렸을 때 잡힌다.
        self.assertIsNone(booking.hold_request_mismatch(self.body(), CTX))

    def test_site_falls_back_to_the_watch_when_the_row_has_none(self):
        body = self.body(row={"scnsNo": "012", "scnSseq": "1"})
        self.assertEqual(body["siteNo"], "0013")


# ── 선점 ───────────────────────────────────────────────────────────────────
class TestHoldApi(unittest.TestCase):
    def test_a_successful_hold_reports_the_ticket_number(self):
        session = FakeSession()
        out = booking.hold_api(session, dict(CTX))
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["mov_atkt_no"], "A1234567")
        self.assertEqual(out["seat_labels"], ["E7", "E8"])
        self.assertIsNotNone(out["hold_expires_at"])

    def test_it_sends_exactly_one_request(self):
        # 선점은 부작용이 있는 요청이다 — 재시도로 두 번 잡으면 안 된다.
        session = FakeSession()
        booking.hold_api(session, dict(CTX))
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(session.sent[0][0], watch.EP_SEAT_HOLD)

    def test_seats_are_picked_again_from_the_live_map(self):
        # 감지 때 고른 E7·E8이 팔리고 다른 자리만 남았다면 그쪽을 잡아야 한다.
        left = [seat("E", n, available=(n in (2, 3))) for n in range(1, 11)]
        session = FakeSession(seats=left)
        out = booking.hold_api(session, dict(CTX))
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["seat_labels"], ["E2", "E3"])

    def test_it_reads_the_map_for_the_showtime_it_means_to_hold(self):
        session = FakeSession()
        booking.hold_api(session, dict(CTX))
        self.assertEqual(session.seat_map_calls[0],
                         {"site_no": "0013", "scns_no": "012",
                          "ymd": "20260908", "scn_sseq": "1"})

    def test_a_block_that_vanished_stops_before_sending(self):
        gone = [seat("E", n, available=False) for n in range(1, 11)]
        session = FakeSession(seats=gone)
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertEqual(session.sent, [], "빈 좌석이 없는데 요청을 보냈다")
        self.assertIn("사라졌", out["error"])

    def test_a_wrong_showtime_is_never_sent(self):
        # 바디를 만드는 쪽이 ctx와 어긋나면 관문이 잡아야 한다.
        ctx = dict(CTX)
        session = FakeSession()
        original = booking.hold_body

        def sabotage(c, identity, chosen):
            body = original(c, identity, chosen)
            body["scnYmd"] = "20261231"      # 엉뚱한 날짜로 바꿔치기
            return body

        booking.hold_body = sabotage
        try:
            out = booking.hold_api(session, ctx)
        finally:
            booking.hold_body = original
        self.assertFalse(out["ok"])
        self.assertEqual(session.sent, [], "의도와 다른 요청이 나갔다")
        self.assertIn("상영일", out["error"])

    def test_a_rejection_repeats_what_cgv_said(self):
        session = FakeSession(response={
            "status": 422, "text": "",
            "body": {"statusCode": -1, "statusMessage": "존재하지 않는 좌석 위치 번호"}})
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertIn("존재하지 않는 좌석 위치 번호", out["error"])

    def test_a_non_zero_result_code_is_not_success(self):
        session = FakeSession(response={
            "status": 200, "text": "",
            "body": {"data": {"resultCode": "9", "resultMessage": "이미 선점된 좌석"}}})
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertIn("이미 선점된 좌석", out["error"])

    def test_an_unreadable_token_stops_before_the_map_is_read(self):
        session = FakeSession(identity=watch.TokenUnreadable("풀지 못했습니다"))
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertEqual(session.seat_map_calls, [])
        self.assertEqual(session.sent, [])

    def test_a_failure_still_names_the_seats_it_tried(self):
        # 이력에 남을 좌석이다 — 실패했다고 빈칸이 되면 안 된다.
        session = FakeSession(post_error=RuntimeError("연결이 끊겼습니다"))
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertEqual(out["seat_labels"], ["E7", "E8"])
        self.assertIn("연결이 끊겼습니다", out["error"])

    def test_an_unreadable_response_is_not_treated_as_held(self):
        session = FakeSession(response={"status": 502, "body": None,
                                        "text": "<html>bad gateway</html>"})
        out = booking.hold_api(session, dict(CTX))
        self.assertFalse(out["ok"])
        self.assertIn("502", out["error"])

    def test_wheelchair_seats_are_never_chosen(self):
        # 좌석맵은 이 자리를 일반석과 똑같이 내려주고, 누르면 팝업으로 막는다.
        # API 경로에는 그 팝업이 안 보이므로 고르지 않는 것이 유일한 방어다.
        rows = []
        for n in range(1, 11):
            s = seat("E", n, available=(n in (1, 2)))
            if n in (1, 2):
                s["seat_salfrm_cd"] = booking.seats_mod.WHEELCHAIR_SALFRM_CD
            rows.append(s)
        out = booking.hold_api(FakeSession(seats=rows), dict(CTX))
        self.assertFalse(out["ok"])


# ── 어느 방식으로 잡을지 ─────────────────────────────────────────────────────
class TestHoldModeSelection(unittest.TestCase):
    """감시가 고른 방식이 실제로 그 함수를 부르는지."""

    def test_api_watch_uses_the_api_path(self):
        self.assertIs(booking.hold_for({"hold_mode": "api"}), booking.hold_api)

    def test_the_default_is_still_the_screen(self):
        self.assertIs(booking.hold_for({}), booking.hold_block)
        self.assertIs(booking.hold_for({"hold_mode": "ui"}), booking.hold_block)

    def test_an_unknown_mode_falls_back_to_the_screen(self):
        # 오래된 행이나 손으로 넣은 값이 여기 걸린다 — 안전한 쪽으로 떨어져야 한다.
        for junk in ("", None, "API직접", "rest", 7):
            self.assertIs(booking.hold_for({"hold_mode": junk}),
                          booking.hold_block, f"{junk!r}를 api로 읽었다")

    def test_case_and_spacing_do_not_change_the_meaning(self):
        for text in ("API", " api ", "Api"):
            self.assertIs(booking.hold_for({"hold_mode": text}), booking.hold_api)


class TestTryAutoBookRoutesByMode(unittest.TestCase):
    """try_auto_book이 감시의 방식대로 부르는지 (주입 없이)."""

    def setUp(self):
        self.calls = []
        self.real_api, self.real_block = booking.hold_api, booking.hold_block

        def spy(name):
            def fn(session, ctx):
                self.calls.append(name)
                return {"ok": False, "error": "테스트", "seat_labels": []}
            return fn

        booking.hold_api, booking.hold_block = spy("api"), spy("ui")

    def tearDown(self):
        booking.hold_api, booking.hold_block = self.real_api, self.real_block

    def run_one(self, hold_mode):
        import store

        watch_row = {"id": 1, "owner_id": None, "auto_book": True,
                     "party_size": 2, "scn_ymd": "20260908", "rows": ["E"],
                     "hold_mode": hold_mode}
        row = {"scnsNo": "012", "scnSseq": "1", "siteNo": "0013",
               "scnsrtTm": "0900"}
        # DB를 건드리는 세 곳만 잠시 비워 둔다 — 여기서 보는 건 경로 선택뿐이다.
        saved = (store.active_hold, store.create_booking_attempt,
                 store.finish_booking_attempt, store.set_seat_watch)
        store.active_hold = lambda *a, **k: False
        store.create_booking_attempt = lambda *a, **k: 1
        store.finish_booking_attempt = lambda *a, **k: None
        store.set_seat_watch = lambda *a, **k: None
        try:
            booking.try_auto_book(FakeSession(), watch_row, row, ROW_E,
                                  site_no="0013", mov_no="30001323")
        finally:
            (store.active_hold, store.create_booking_attempt,
             store.finish_booking_attempt, store.set_seat_watch) = saved

    def test_an_api_watch_never_opens_a_screen(self):
        self.run_one("api")
        self.assertEqual(self.calls, ["api"])

    def test_a_plain_watch_still_drives_the_screen(self):
        self.run_one("ui")
        self.assertEqual(self.calls, ["ui"])


# ── 토큰에서 신원 읽기 ───────────────────────────────────────────────────────
class TestBookingIdentity(unittest.TestCase):
    """accessToken을 풀어 고객번호를 읽는다 (watch.booking_identity).

    **이 값은 그 토큰 안에만 있다.** 쿠키·localStorage·회원 API 어디에도 없어서
    (2026-09-07 실측), 화면을 몰지 않고 얻을 방법이 이것뿐이다. 그래서 못 읽으면
    조용히 넘어가지 않고 사유를 올린다 — 고객번호가 빠진 예매 요청은 보내지 않는다.
    """

    @staticmethod
    def token(claims: dict) -> str:
        """claims를 담은 accessToken 쿠키 값을 CGV와 같은 방식으로 만든다."""
        import base64
        import json
        import urllib.parse

        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)

        def seg(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        jwt = f"{seg({'alg': 'HS256'})}.{seg(claims)}.signature".encode()
        pad = 16 - len(jwt) % 16
        enc = Cipher(algorithms.AES(watch.CGV_TOKEN_KEY), modes.ECB()).encryptor()
        blob = enc.update(jwt + bytes([pad]) * pad) + enc.finalize()
        return urllib.parse.quote(base64.b64encode(blob).decode(), safe="")

    def test_it_reads_the_customer_number_and_grade(self):
        raw = self.token({"crerNo": "118012814", "cntCusgdCd": "02",
                          "userId": "tester", "userNm": "홍길동"})
        self.assertEqual(watch.booking_identity(raw),
                         {"cust_no": "118012814", "cusgd_cd": "02",
                          "user_id": "tester",
                          # 이름은 결제번호 요청(commonGetPayId)이 평문으로 싣는다.
                          "user_nm": "홍길동"})

    def test_a_missing_grade_reads_as_ordinary(self):
        # 등급은 넘겨짚어도 되는 값이다 — 요금은 CGV가 계산한다.
        raw = self.token({"crerNo": "118012814"})
        self.assertEqual(watch.booking_identity(raw)["cusgd_cd"], "01")

    def test_a_token_without_a_customer_number_is_refused(self):
        raw = self.token({"userId": "tester"})
        with self.assertRaises(watch.TokenUnreadable):
            watch.booking_identity(raw)

    def test_junk_is_refused_not_guessed(self):
        for junk in ("", "not-base64!!", "YWJjZGVm"):
            with self.assertRaises(watch.TokenUnreadable):
                watch.booking_identity(junk)

    def test_a_token_that_is_not_a_jwt_is_refused(self):
        import base64
        import urllib.parse

        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)

        plain = b"hello"
        pad = 16 - len(plain) % 16
        enc = Cipher(algorithms.AES(watch.CGV_TOKEN_KEY), modes.ECB()).encryptor()
        blob = enc.update(plain + bytes([pad]) * pad) + enc.finalize()
        raw = urllib.parse.quote(base64.b64encode(blob).decode(), safe="")
        with self.assertRaises(watch.TokenUnreadable):
            watch.booking_identity(raw)


if __name__ == "__main__":
    unittest.main()
