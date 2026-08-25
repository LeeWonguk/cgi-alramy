#!/usr/bin/env python3
"""DB가 필요한 회귀 테스트.

**일회용 테스트 DB를 만들고 끝나면 지운다.** 운영 DB(cgv)는 건드리지 않는다 —
`DATABASE_URL`을 테스트 DB로 덮어쓴 뒤에 store를 import한다 (envfile.load_env는
`setdefault`라 이미 있는 값을 덮지 않으므로 순서가 중요하다).

Postgres에 붙을 수 없으면 전체를 건너뛴다.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import envfile  # noqa: E402

TEST_DB = "cgv_selftest"

# 실제 접속 정보를 그대로 쓰고 DB 이름만 바꾼다 — 계정·호스트가 달라도 따라간다.
envfile.load_env()
_real = os.environ.get("DATABASE_URL", "").strip() \
    or "postgresql://postgres:postgres@127.0.0.1:5432/cgv"

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

assert (conninfo_to_dict(_real).get("dbname") or "cgv") != TEST_DB
os.environ["DATABASE_URL"] = make_conninfo(_real, dbname=TEST_DB)
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8787")
os.environ["DEV_LOGIN"] = "0"          # 테스트가 개발모드 로그인을 열지 않게

import store  # noqa: E402

_ADMIN = make_conninfo(os.environ["DATABASE_URL"], dbname="postgres")
_skip_reason: str | None = None


def _drop_test_db() -> None:
    with psycopg.connect(_ADMIN, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
            .format(sql.Identifier(TEST_DB))
        )


def setUpModule() -> None:
    global _skip_reason
    try:
        _drop_test_db()          # 앞선 실행이 남긴 게 있으면 치우고 시작한다
        store.ensure_database()
        store.init_db()
    except Exception as exc:     # noqa: BLE001 - Postgres가 없는 환경
        _skip_reason = f"Postgres에 붙을 수 없습니다: {exc}"
        raise unittest.SkipTest(_skip_reason) from exc


def tearDownModule() -> None:
    if _skip_reason:
        return
    store.close()                # 풀을 닫아야 DROP DATABASE가 통한다
    _drop_test_db()


class DbCase(unittest.TestCase):
    def setUp(self) -> None:
        # 테스트 DB이므로 매번 비우고 시작한다.
        with store.pool().connection() as conn:
            conn.execute("delete from alerts")
            conn.execute("delete from watch_targets")
            conn.execute("delete from users")

    @staticmethod
    def make_user(name: str) -> dict:
        return store.login_user("local", name, nickname=name)


class TestAlertDedupeIsPerOwner(DbCase):
    """G1 회귀: 미전송 알림의 중복 판정이 소유자를 봐야 한다.

    config_error·connect_error는 target_id가 없고 dates도 비어 있다. 소유자를
    보지 않으면 B의 알림이 A의 행을 UPDATE해 A의 본문이 B 내용으로 바뀌고
    B의 이력은 아예 생기지 않는다.
    """

    def test_two_users_keep_separate_rows(self):
        a = self.make_user("owner")["id"]
        b = self.make_user("member")["id"]

        id_a = store.record_alert("config_error", "A의 설정 오류", owner_id=a)
        id_b = store.record_alert("config_error", "B의 설정 오류", owner_id=b)

        self.assertNotEqual(id_a, id_b,
                            "서로 다른 사용자의 알림이 한 행으로 뭉개졌다")

        bodies = {r["owner_id"]: r["body"] for r in store.recent_alerts(10)}
        self.assertEqual(bodies[a], "A의 설정 오류", "A의 본문이 덮어써졌다")
        self.assertEqual(bodies[b], "B의 설정 오류")

    def test_same_user_retry_reuses_the_row(self):
        # 웹훅이 죽어 있는 동안 30초마다 행이 쌓이면 이력이 못 쓰게 된다.
        a = self.make_user("owner")["id"]
        first = store.record_alert("config_error", "설정 오류", owner_id=a)
        second = store.record_alert("config_error", "설정 오류", owner_id=a)

        self.assertEqual(first, second)
        rows = store.recent_alerts(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 2)

    def test_delivered_rows_are_not_reused(self):
        a = self.make_user("owner")["id"]
        first = store.record_alert("config_error", "설정 오류", owner_id=a)
        store.mark_alert_delivered(first)
        second = store.record_alert("config_error", "설정 오류", owner_id=a)

        self.assertNotEqual(first, second,
                            "이미 보낸 알림을 재사용하면 이력이 사라진다")

    def test_owner_scoped_history_only_shows_own(self):
        a = self.make_user("owner")["id"]
        b = self.make_user("member")["id"]
        store.record_alert("config_error", "A의 설정 오류", owner_id=a)
        store.record_alert("config_error", "B의 설정 오류", owner_id=b)

        mine = store.recent_alerts(10, owner_id=b)
        self.assertEqual([r["body"] for r in mine], ["B의 설정 오류"])


class TestCheckNowIsOwnerOnly(DbCase):
    """G3 회귀: 사이클은 서버 전체를 돌리므로 소유자만 트리거할 수 있다."""

    @classmethod
    def setUpClass(cls) -> None:
        import web.app
        cls.app = web.app.create_app(start_background=False)
        # 실제로 사이클이 돌면 Chromium이 뜬다 — 호출 여부만 기록하는 스텁으로 바꾼다.
        cls.calls: list[str] = []

        class StubPoller:
            def run_cycle(self, trigger: str = "schedule") -> dict:
                cls.calls.append(trigger)
                return {"targets_checked": 0}

            def snapshot(self) -> dict:
                return {"running": False, "interval_seconds": 30,
                        "next_check_at": None, "last_summary": None}

        cls.app.extensions["cgv"]["poller"] = StubPoller()

    def setUp(self) -> None:
        super().setUp()
        type(self).calls.clear()
        self.client = self.app.test_client()

    def login_as(self, user_id: int) -> None:
        with self.client.session_transaction() as sess:
            sess["user_id"] = user_id

    def test_approved_member_is_denied(self):
        self.make_user("owner")                       # 첫 계정이 소유자
        member = self.make_user("member")
        store.set_user_status(member["id"], "approved")   # 승인 게이트는 통과시킨다

        self.login_as(member["id"])
        resp = self.client.post("/api/check-now")

        self.assertEqual(resp.status_code, 403)
        # 승인 대기 때문에 막힌 게 아니라 소유자 검사에서 막혀야 한다.
        self.assertEqual(resp.get_json()["error"], "소유자만 할 수 있습니다")
        self.assertEqual(self.calls, [], "막혔는데도 사이클이 돌았다")

    def test_owner_is_allowed(self):
        owner = self.make_user("owner")
        self.login_as(owner["id"])

        resp = self.client.post("/api/check-now")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.calls, ["manual"])

    def test_anonymous_is_unauthorized(self):
        self.make_user("owner")
        resp = self.client.post("/api/check-now")
        self.assertEqual(resp.status_code, 401)


class TestCgvAccountStore(DbCase):
    """CGV 자격증명 저장·상태 전이 (Phase 1).

    비밀번호는 암호화해 넣고, 화면용 조회에는 절대 실려 나가지 않으며, 원문은
    복호로만 되찾는다. 아이디·비밀번호가 바뀌면 상태가 unlinked로 되돌아간다.
    """

    def test_store_and_readback(self):
        uid = self.make_user("owner")["id"]

        account = store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")
        self.assertEqual(account["cgv_user_id"], "hayato5246")
        self.assertEqual(account["status"], "unlinked")
        # 화면용 조회에는 비밀번호(암호문 포함)가 없어야 한다.
        self.assertNotIn("password_enc", account)
        self.assertNotIn("password", account)

        # 원문은 복호로만 되찾는다.
        self.assertEqual(store.cgv_password(uid), "!Dnjsrnrl1")

    def test_password_stored_encrypted_not_plaintext(self):
        uid = self.make_user("owner")["id"]
        store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")

        with store.pool().connection() as conn:
            raw = conn.execute(
                "select password_enc from cgv_accounts where owner_id = %s", (uid,)
            ).fetchone()["password_enc"]
        blob = bytes(raw)
        self.assertNotIn(b"!Dnjsrnrl1", blob, "비밀번호가 평문으로 저장됐다")

    def test_update_resets_status_to_unlinked(self):
        uid = self.make_user("owner")["id"]
        store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")
        store.set_cgv_account_status(uid, "linked")
        self.assertEqual(store.cgv_account(uid)["status"], "linked")

        # 비밀번호를 바꾸면 이전 성공은 의미가 없어 unlinked로 돌아간다.
        again = store.set_cgv_account(uid, "hayato5246", "새로운비번")
        self.assertEqual(again["status"], "unlinked")
        self.assertIsNone(again["last_error"])
        self.assertEqual(store.cgv_password(uid), "새로운비번")

    def test_status_error_records_message(self):
        uid = self.make_user("owner")["id"]
        store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")

        store.set_cgv_account_status(uid, "error", error="CGV 서버 오류 (HTTP 500).")
        account = store.cgv_account(uid)
        self.assertEqual(account["status"], "error")
        self.assertEqual(account["last_error"], "CGV 서버 오류 (HTTP 500).")
        self.assertIsNone(account["last_login_at"])

        # 성공하면 오류가 지워지고 로그인 시각이 찍힌다.
        store.set_cgv_account_status(uid, "linked")
        account = store.cgv_account(uid)
        self.assertEqual(account["status"], "linked")
        self.assertIsNone(account["last_error"])
        self.assertIsNotNone(account["last_login_at"])

    def test_delete_and_absent(self):
        uid = self.make_user("owner")["id"]
        self.assertIsNone(store.cgv_account(uid))
        self.assertIsNone(store.cgv_password(uid))

        store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")
        self.assertTrue(store.delete_cgv_account(uid))
        self.assertIsNone(store.cgv_account(uid))
        self.assertFalse(store.delete_cgv_account(uid))

    def test_removed_when_user_deleted(self):
        # cgv_accounts.owner_id는 ON DELETE CASCADE — 사용자를 지우면 함께 사라진다.
        self.make_user("owner")                       # 첫 계정이 소유자
        member = self.make_user("member")["id"]
        store.set_cgv_account(member, "hayato5246", "!Dnjsrnrl1")

        self.assertTrue(store.delete_user(member))
        self.assertIsNone(store.cgv_account(member))

    def test_blank_fields_rejected(self):
        uid = self.make_user("owner")["id"]
        with self.assertRaises(ValueError):
            store.set_cgv_account(uid, "  ", "!Dnjsrnrl1")
        with self.assertRaises(ValueError):
            store.set_cgv_account(uid, "hayato5246", "")

    def test_session_token_roundtrip_and_clear(self):
        uid = self.make_user("owner")["id"]
        store.set_cgv_account(uid, "hayato5246", "!Dnjsrnrl1")
        self.assertIsNone(store.cgv_tokens(uid))

        tokens = {"accessToken": "aaa.bbb.ccc", "refresh_token": "r-123"}
        store.set_cgv_tokens(uid, tokens)
        self.assertEqual(store.cgv_tokens(uid), tokens)
        # 토큰 저장은 계정을 linked로 올린다.
        self.assertEqual(store.cgv_account(uid)["status"], "linked")

        # 토큰도 평문으로 저장되면 안 된다.
        with store.pool().connection() as conn:
            raw = conn.execute(
                "select session_enc from cgv_accounts where owner_id = %s", (uid,)
            ).fetchone()["session_enc"]
        self.assertNotIn(b"aaa.bbb.ccc", bytes(raw))

        store.clear_cgv_tokens(uid)
        self.assertIsNone(store.cgv_tokens(uid))


class TestSeatWatchStore(DbCase):
    """좌석 감시 저장·상태 (Phase 1)."""

    def test_add_list_delete(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산아이파크몰", "20260825",
                                 screen_types=["IMAX"], rows=["a", "B", "a"])
        self.assertEqual(w["scn_ymd"], "20260825")
        self.assertEqual(w["rows"], ["A", "B"])          # 정규화됨
        self.assertEqual(w["screen_types"], ["IMAX"])

        listed = store.seat_watches(owner_id=uid)
        self.assertEqual(len(listed), 1)

        self.assertTrue(store.delete_seat_watch(w["id"], owner_id=uid))
        self.assertEqual(store.seat_watches(owner_id=uid), [])

    def test_duplicate_reenables(self):
        uid = self.make_user("owner")["id"]
        a = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["A"])
        b = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["A"])
        self.assertEqual(a["id"], b["id"])               # 같은 조합은 한 행

    def test_min_consecutive_stored_and_updated(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 rows=["A"], min_consecutive=3)
        self.assertEqual(w["min_consecutive"], 3)
        # 같은 조합을 다시 추가하면 연속 조건이 갱신된다.
        again = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                     rows=["A"], min_consecutive=2)
        self.assertEqual(again["id"], w["id"])
        self.assertEqual(again["min_consecutive"], 2)

    def test_state_baseline_then_diff(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825")["id"]
        self.assertEqual(store.prev_seat_state(w), {})   # 첫 관측

        store.save_seat_state(w, {"001|5": ["A1", "A2"]})
        self.assertEqual(store.prev_seat_state(w), {"001|5": ["A1", "A2"]})

        store.save_seat_state(w, {"001|5": ["A1", "A2", "A3"]})
        self.assertEqual(store.prev_seat_state(w)["001|5"], ["A1", "A2", "A3"])

    def test_removed_when_user_deleted(self):
        self.make_user("owner")
        member = self.make_user("member")["id"]
        store.add_seat_watch(member, "오디세이", "용산", "20260825")
        self.assertTrue(store.delete_user(member))
        self.assertEqual(store.seat_watches(owner_id=member), [])

    def test_auto_book_fields_and_update(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 auto_book=True, party_size=2,
                                 ticket_spec={"adult": 2, "youth": 0})
        self.assertTrue(w["auto_book"])
        self.assertEqual(w["party_size"], 2)
        self.assertEqual(w["ticket_spec"], {"adult": 2})   # 0은 제거됨

        upd = store.set_seat_watch(w["id"], owner_id=uid, auto_book=False,
                                   party_size=3)
        self.assertFalse(upd["auto_book"])
        self.assertEqual(upd["party_size"], 3)

    def test_set_seat_watch_respects_owner(self):
        owner = self.make_user("owner")["id"]
        other = self.make_user("other")["id"]
        w = store.add_seat_watch(owner, "오디세이", "용산", "20260825")
        # 남의 감시는 못 바꾼다.
        store.set_seat_watch(w["id"], owner_id=other, enabled=False)
        self.assertTrue(store.seat_watch(w["id"])["enabled"])


class TestAutoBookOrchestration(DbCase):
    """booking.try_auto_book — 브라우저 없이 가짜 hold 함수로 오케스트레이션 검증."""

    @staticmethod
    def _seats(labels_avail):
        """A열 좌석 8개. labels_avail에 든 번호만 available."""
        out, x = [], 1
        for i in range(1, 9):
            out.append({"row": "A", "no": str(i), "label": f"A{i}",
                        "available": i in labels_avail, "kind": "", "zone": "",
                        "x_start": x, "x_end": x + 2, "left_pway": False,
                        "right_pway": False, "seat_loc_no": f"LOC{i}",
                        "sbord_no": "001", "seat_area_no": "001", "szone_no": "01001",
                        "stknd_cd": "27", "szone_kind_cd": "01", "seat_salfrm_cd": "01"})
            x += 2
        return out

    def _watch(self, uid, **kw):
        return store.add_seat_watch(uid, "오디세이", "용산", "20260825", **kw)

    def _row(self):
        return {"scnsNo": "001", "scnSseq": "5", "scnsrtTm": "2210", "siteNm": "용산"}

    def test_held_records_and_disables_watch(self):
        import booking
        from datetime import datetime, timedelta
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)
        exp = (datetime.now().astimezone() + timedelta(minutes=7))

        def fake_hold(session, ctx):
            self.assertEqual(ctx["seat_labels"], ["A4", "A5"])   # 가운데 2석
            return {"ok": True, "mov_atkt_no": "P013X", "hold_expires_at": exp,
                    "amount": 28000}

        out = booking.try_auto_book(None, w, self._row(), self._seats(set(range(1, 9))),
                                    mov_nm="오디세이", site_nm="용산", hold_fn=fake_hold)
        self.assertEqual(out["action"], "held")
        self.assertEqual(out["mov_atkt_no"], "P013X")
        # 이력에 held로 남고, 감시는 비활성화된다.
        att = store.booking_attempts(owner_id=uid)
        self.assertEqual(att[0]["status"], "held")
        self.assertEqual(att[0]["seat_labels"], ["A4", "A5"])
        self.assertFalse(store.seat_watch(w["id"])["enabled"])
        self.assertIsNotNone(store.active_hold(w["id"]))

    def test_failed_records_failure_keeps_watch(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)

        def fake_hold(session, ctx):
            return {"ok": False, "error": "선점 응답 없음"}

        out = booking.try_auto_book(None, w, self._row(), self._seats(set(range(1, 9))),
                                    mov_nm="오디세이", site_nm="용산", hold_fn=fake_hold)
        self.assertEqual(out["action"], "failed")
        self.assertEqual(store.booking_attempts(owner_id=uid)[0]["status"], "failed")
        self.assertTrue(store.seat_watch(w["id"])["enabled"])   # 유지
        self.assertIsNone(store.active_hold(w["id"]))

    def test_no_seats_when_block_too_small(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=3)
        # A1,A2만 available → 3연속 불가
        out = booking.try_auto_book(None, w, self._row(), self._seats({1, 2}),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=lambda s, c: {"ok": True})
        self.assertEqual(out["action"], "no_seats")
        self.assertEqual(store.booking_attempts(owner_id=uid), [])

    def test_skip_when_already_held(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=1)
        called = {"n": 0}

        def fake_hold(session, ctx):
            called["n"] += 1
            return {"ok": True, "mov_atkt_no": "X"}

        booking.try_auto_book(None, w, self._row(), self._seats({1}),
                              mov_nm="오디세이", site_nm="용산", hold_fn=fake_hold)
        # 두 번째 시도는 이미 held라 건너뛴다.
        out = booking.try_auto_book(None, w, self._row(), self._seats({1}),
                                    mov_nm="오디세이", site_nm="용산", hold_fn=fake_hold)
        self.assertEqual(out["action"], "skip")
        self.assertEqual(called["n"], 1)

    def test_off_when_auto_book_disabled(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=False, party_size=1)
        out = booking.try_auto_book(None, w, self._row(), self._seats({1}),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=lambda s, c: {"ok": True})
        self.assertEqual(out["action"], "skip")


if __name__ == "__main__":
    unittest.main()
