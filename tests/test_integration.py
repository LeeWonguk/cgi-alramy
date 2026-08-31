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
import watch  # noqa: E402

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

    def test_seat_number_range_round_trips(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 rows=["H"], seat_num_from=13, seat_num_to=32)
        self.assertEqual((w["seat_num_from"], w["seat_num_to"]), (13, 32))
        self.assertEqual(store.seat_watch(w["id"])["seat_num_from"], 13)

    def test_a_reversed_range_is_stored_corrected(self):
        """32~13으로 적어도 13~32로 저장된다 — 아니면 아무 좌석도 안 걸린다."""
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 seat_num_from=32, seat_num_to=13)
        self.assertEqual((w["seat_num_from"], w["seat_num_to"]), (13, 32))

    def test_no_range_defaults_to_unlimited(self):
        """예전 감시는 컬럼이 0이라 전 번호를 본다 — 동작이 바뀌면 안 된다."""
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["H"])
        self.assertEqual((w["seat_num_from"], w["seat_num_to"]), (0, 0))

    def test_watches_differing_only_by_range_are_separate(self):
        # 같은 열을 보되 앞구역·뒷구역을 따로 걸 수 있어야 한다.
        uid = self.make_user("owner")["id"]
        a = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 rows=["H"], seat_num_from=13, seat_num_to=32)
        b = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 rows=["H"], seat_num_from=1, seat_num_to=12)
        self.assertNotEqual(a["id"], b["id"])

    def test_the_range_can_be_edited_afterwards(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["H"])
        out = store.set_seat_watch(w["id"], owner_id=uid,
                                   seat_num_from=13, seat_num_to=32)
        self.assertEqual((out["seat_num_from"], out["seat_num_to"]), (13, 32))

    def test_duplicate_reenables(self):
        uid = self.make_user("owner")["id"]
        a = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["A"])
        b = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["A"])
        self.assertEqual(a["id"], b["id"])               # 같은 조합은 한 행

    def test_scn_time_normalized_and_distinct(self):
        # 정규화: '2210'·'22:10' 모두 'HH:MM'으로.
        self.assertEqual(store.normalize_scn_time("2210"), "22:10")
        self.assertEqual(store.normalize_scn_time("22:10"), "22:10")
        self.assertEqual(store.normalize_scn_time(""), "")
        self.assertEqual(store.normalize_scn_time("2530"), "25:30")  # 자정 넘김

        uid = self.make_user("owner")["id"]
        allw = store.add_seat_watch(uid, "오디세이", "용산", "20260825")
        t2210 = store.add_seat_watch(uid, "오디세이", "용산", "20260825", scn_time="2210")
        t1900 = store.add_seat_watch(uid, "오디세이", "용산", "20260825", scn_time="19:00")
        # 시간이 다르면 별개의 감시다.
        ids = {allw["id"], t2210["id"], t1900["id"]}
        self.assertEqual(len(ids), 3)
        self.assertEqual(t2210["scn_time"], "22:10")
        self.assertEqual(allw["scn_time"], "")

    def test_time_range_stored_and_distinct(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 scn_time_from="1800", scn_time_to="23:30")
        self.assertEqual((w["scn_time_from"], w["scn_time_to"]),
                         ("18:00", "23:30"))
        self.assertEqual(w["scn_time"], "")

        # 시간대가 다르면 별개의 감시다.
        other = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                     scn_time_from="09:00", scn_time_to="12:00")
        self.assertNotEqual(w["id"], other["id"])

        # 같은 시간대를 다시 추가하면 같은 행이다.
        again = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                     scn_time_from="18:00", scn_time_to="23:30")
        self.assertEqual(w["id"], again["id"])

    def test_exact_time_clears_the_range(self):
        # 회차를 콕 집으면 시간대는 의미가 없다 — 둘이 함께 저장되면 안 된다.
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 scn_time="22:10",
                                 scn_time_from="09:00", scn_time_to="12:00")
        self.assertEqual(w["scn_time"], "22:10")
        self.assertEqual((w["scn_time_from"], w["scn_time_to"]), ("", ""))

    def test_half_open_range_is_dropped(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825",
                                 scn_time_from="18:00")
        self.assertEqual((w["scn_time_from"], w["scn_time_to"]), ("", ""))

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

    def test_auto_pay_requests_payment_and_keeps_the_link(self):
        """auto_pay를 켜면 선점에 이어 결제를 요청하고 링크를 남긴다."""
        import booking
        from datetime import datetime, timedelta
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, auto_pay=True, party_size=1)
        link = "https://online-pay.kakaopay.com/pay/r1/abc123"
        pay_exp = datetime.now().astimezone() + timedelta(minutes=15)
        seen = {}

        def fake_pay(session, ctx, *, method):
            seen["method"] = method
            seen["seats"] = list(ctx["seat_labels"])
            return {"ok": True, "method": method, "pay_url": link,
                    "pay_expires_at": pay_exp, "amount": 15000, "error": ""}

        out = booking.try_auto_book(
            None, w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"},
            pay_fn=fake_pay)

        self.assertEqual(out["action"], "held")
        self.assertEqual(out["pay_url"], link)
        self.assertEqual(seen["method"], "kakaopay")
        att = store.booking_attempts(owner_id=uid)[0]
        self.assertEqual(att["status"], "held")
        self.assertEqual(att["pay_url"], link)
        self.assertEqual(att["pay_method"], "kakaopay")
        self.assertEqual(att["amount"], 15000)   # 금액은 결제 화면에서 읽는다
        self.assertIsNone(att["pay_error"])

    def test_payment_failure_does_not_undo_the_hold(self):
        """결제 요청이 실패해도 좌석은 잡혀 있다 — 사람이 손으로 마칠 수 있다."""
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, auto_pay=True, party_size=1)

        def fake_pay(session, ctx, *, method):
            return {"ok": False, "method": method, "pay_url": None,
                    "pay_expires_at": None, "amount": None,
                    "error": "카카오페이 결제창이 뜨지 않았습니다"}

        out = booking.try_auto_book(
            None, w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"},
            pay_fn=fake_pay)

        self.assertEqual(out["action"], "held")
        self.assertIsNone(out["pay_url"])
        self.assertIn("결제창", out["pay_error"])
        att = store.booking_attempts(owner_id=uid)[0]
        self.assertEqual(att["status"], "held")          # held 그대로
        self.assertIn("결제창", att["pay_error"])
        self.assertIsNotNone(store.active_hold(w["id"]))  # 중복 선점도 여전히 막힌다

    def test_a_raising_pay_function_is_caught(self):
        """결제 쪽 예외가 선점 성공까지 실패로 만들면 안 된다."""
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, auto_pay=True, party_size=1)

        def boom(session, ctx, *, method):
            raise RuntimeError("브라우저가 죽었다")

        out = booking.try_auto_book(
            None, w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"},
            pay_fn=boom)
        self.assertEqual(out["action"], "held")
        self.assertIn("브라우저가 죽었다", out["pay_error"])

    def test_pay_is_not_attempted_when_auto_pay_is_off(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=1)
        called = {"n": 0}

        def fake_pay(session, ctx, *, method):
            called["n"] += 1
            return {"ok": True}

        booking.try_auto_book(
            None, w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"},
            pay_fn=fake_pay)
        self.assertEqual(called["n"], 0)
        self.assertIsNone(store.booking_attempts(owner_id=uid)[0]["pay_url"])

    def test_records_the_seats_the_hold_actually_took(self):
        # hold는 좌석맵에 도착해서 좌석을 다시 고른다. 이력과 알림이 감지 때의
        # **후보**를 그대로 적으면, 사용자가 받은 문구와 실제 잡힌 자리가 어긋난다.
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)

        def fake_hold(session, ctx):
            self.assertEqual(ctx["seat_labels"], ["A4", "A5"])   # 후보
            return {"ok": True, "mov_atkt_no": "P013X",
                    "seat_labels": ["A7", "A8"]}                 # 실제로 고른 것

        out = booking.try_auto_book(None, w, self._row(),
                                    self._seats(set(range(1, 9))),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=fake_hold)
        self.assertEqual(out["seats"], ["A7", "A8"])
        self.assertEqual(store.booking_attempts(owner_id=uid)[0]["seat_labels"],
                         ["A7", "A8"])

    def test_failed_hold_records_what_it_tried(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)

        def fake_hold(session, ctx):
            return {"ok": False, "error": "그 사이 팔렸습니다",
                    "seat_labels": ["A7", "A8"]}

        out = booking.try_auto_book(None, w, self._row(),
                                    self._seats(set(range(1, 9))),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=fake_hold)
        self.assertEqual(out["seats"], ["A7", "A8"])
        self.assertEqual(store.booking_attempts(owner_id=uid)[0]["seat_labels"],
                         ["A7", "A8"])

    def test_hold_without_seat_labels_keeps_the_candidate(self):
        # 좌석맵에 닿기도 전에 죽으면 후보가 곧 '시도한 좌석'이다.
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)
        out = booking.try_auto_book(None, w, self._row(),
                                    self._seats(set(range(1, 9))),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=lambda s, c: {"ok": False,
                                                          "error": "UI 구동 실패"})
        self.assertEqual(out["seats"], ["A4", "A5"])
        self.assertEqual(store.booking_attempts(owner_id=uid)[0]["seat_labels"],
                         ["A4", "A5"])

    def test_off_when_auto_book_disabled(self):
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=False, party_size=1)
        out = booking.try_auto_book(None, w, self._row(), self._seats({1}),
                                    mov_nm="오디세이", site_nm="용산",
                                    hold_fn=lambda s, c: {"ok": True})
        self.assertEqual(out["action"], "skip")


class FakeCookieJar:
    """Playwright BrowserContext의 쿠키 부분만 흉내낸다."""

    def __init__(self) -> None:
        self.jar: dict[str, str] = {}

    def cookies(self):
        return [{"name": k, "value": v} for k, v in self.jar.items()]

    def clear_cookies(self, name=None):
        if name is None:
            self.jar.clear()
        else:
            self.jar.pop(name, None)

    def add_cookies(self, cookies):
        for c in cookies:
            self.jar[c["name"]] = c["value"]


class TestBatchedJson(unittest.TestCase):
    """좌석맵을 묶어 받는다 — 실패한 항목만 빠지고 나머지는 살아야 한다.

    실패를 통째로 올리면 배치가 인증 복구까지 떠안게 된다. 그 자리를 None으로
    두면 호출자가 개별 경로로 다시 받고, 401 복구(_AuthGuard)는 지금처럼
    거기서 일어난다.
    """

    def make_session(self, replies):
        """evaluate가 정해진 답을 주는 세션. replies는 경로 → 항목 dict."""
        session = watch.CgvSession.__new__(watch.CgvSession)
        session.requests = 0
        calls = []

        def evaluate(script, arg):
            calls.append(list(arg["paths"]))
            return [replies.get(p, {"status": 0}) for p in arg["paths"]]

        page = type("P", (), {"evaluate": staticmethod(evaluate)})()
        session._spaces = watch.OrderedDict()
        session._current = None
        session._spaces[None] = watch._OwnerSpace(object(), page)
        return session, calls

    def ok(self, payload=None):
        return {"status": 200, "json": {"statusCode": 0,
                                        "data": payload or {"items": []}}}

    def test_good_items_come_back_in_order(self):
        s, _ = self.make_session({
            "/a": self.ok({"items": ["A"]}), "/b": self.ok({"items": ["B"]})})
        out = s.get_json_many(["/a", "/b"])
        self.assertEqual([o["data"]["items"] for o in out], [["A"], ["B"]])

    def test_a_failed_item_becomes_none_and_the_rest_survive(self):
        s, _ = self.make_session({"/a": self.ok(), "/c": self.ok()})
        out = s.get_json_many(["/a", "/b", "/c"])   # /b는 답이 없다
        self.assertIsNotNone(out[0])
        self.assertIsNone(out[1])
        self.assertIsNotNone(out[2])

    def test_a_401_is_left_for_the_individual_path(self):
        """배치가 인증을 떠안지 않는다 — None으로 두면 개별 경로가 복구한다."""
        s, _ = self.make_session({"/a": {"status": 401}})
        self.assertEqual(s.get_json_many(["/a"]), [None])

    def test_an_api_level_error_is_not_mistaken_for_success(self):
        # HTTP는 200인데 statusCode가 오류인 경우 — get_json과 같은 기준이다.
        s, _ = self.make_session(
            {"/a": {"status": 200, "json": {"statusCode": "9999",
                                            "statusMessage": "오류"}}})
        self.assertEqual(s.get_json_many(["/a"]), [None])

    def test_unparsable_json_is_not_mistaken_for_success(self):
        s, _ = self.make_session({"/a": {"status": 200, "bad": "<html>"}})
        self.assertEqual(s.get_json_many(["/a"]), [None])

    def test_requests_are_chunked(self):
        """한 메시지가 커지는 것과 CGV로 한꺼번에 나가는 것을 함께 막는다."""
        n = watch.SEAT_MAP_BATCH * 2 + 3
        s, calls = self.make_session({})
        s.get_json_many([f"/p{i}" for i in range(n)])
        self.assertEqual(len(calls), 3, "묶음이 쪼개지지 않았다")
        for chunk in calls:
            self.assertLessEqual(len(chunk), watch.SEAT_MAP_BATCH)

    def test_an_empty_list_does_not_touch_the_browser(self):
        s, calls = self.make_session({})
        self.assertEqual(s.get_json_many([]), [])
        self.assertEqual(calls, [])

    def test_every_request_is_counted(self):
        s, _ = self.make_session({})
        s.get_json_many([f"/p{i}" for i in range(5)])
        self.assertEqual(s.requests, 5, "부하 집계에서 빠지면 안 된다")

    def test_a_dead_batch_falls_back_instead_of_dying(self):
        s = watch.CgvSession.__new__(watch.CgvSession)
        s.requests = 0

        def boom(script, arg):
            raise RuntimeError("페이지가 갈아 끼워지는 중")

        page = type("P", (), {"evaluate": staticmethod(boom)})()
        s._spaces = watch.OrderedDict()
        s._current = None
        s._spaces[None] = watch._OwnerSpace(object(), page)
        self.assertEqual(s.get_json_many(["/a", "/b"]), [None, None])


class TestOwnerSpacesAreIsolated(unittest.TestCase):
    """소유자마다 BrowserContext를 따로 둔다 — 브라우저 없이 구조만 시험한다.

    예전에는 컨텍스트가 하나뿐이라 사용자가 둘이면 사이클마다 쿠키를 비우고
    다시 로그인했고, 그때마다 **미리 띄워 둔 예매 탭을 전부 닫아야 했다**
    (앞사람 화면으로 선점하면 안 되므로). 그래서 사용자가 둘 이상이면
    프리워밍이 한 번도 살아남지 못하고 선점마다 딥링크 6.2초를 다시 물었다.
    """

    def make_session(self):
        """_new_space만 가짜로 바꾼 세션. 나머지 로직은 진짜 것을 쓴다."""
        session = watch.CgvSession.__new__(watch.CgvSession)
        session._spaces = watch.OrderedDict()
        session._current = None
        session._browser = object()          # 열려 있는 것처럼
        made = []

        def new_space():
            page = type("P", (), {"evaluate": lambda self, s: 1})()
            page.context = FakeCookieJar()
            space = watch._OwnerSpace(page.context, page)
            made.append(space)
            return space

        session._new_space = new_space
        session._spaces[None] = new_space()
        return session, made

    def test_each_owner_gets_its_own_space(self):
        s, made = self.make_session()
        s.use(1)
        s.use(2)
        self.assertEqual(len(s._spaces), 3, "기본 공간 + 소유자 둘")
        self.assertIsNot(s._spaces[1], s._spaces[2])

    def test_switching_back_keeps_the_prewarmed_tabs(self):
        """이게 이 변경의 핵심이다 — 전환이 프리워밍을 죽이지 않아야 한다."""
        s, _ = self.make_session()
        s.use(1)
        s._space.booking_pages["a"] = object()
        s.use(2)
        self.assertEqual(len(s._space.booking_pages), 0, "새 공간은 비어 있다")
        s.use(1)
        self.assertEqual(len(s._space.booking_pages), 1, "돌아오니 탭이 사라졌다")

    def test_the_login_owner_is_per_space(self):
        s, _ = self.make_session()
        s.use(1)
        s.mark_logged_in(1)
        s.use(2)
        self.assertIsNone(s.logged_in_owner, "남의 공간 주인이 새어 나왔다")
        s.use(1)
        self.assertEqual(s.logged_in_owner, 1)

    def test_marking_the_wrong_owner_is_refused(self):
        """공간을 안 고르고 로그인하면 남의 계정으로 선점하게 된다 — 막는다."""
        s, _ = self.make_session()
        s.use(1)
        s.mark_logged_in(1)
        with self.assertRaises(RuntimeError) as caught:
            s.mark_logged_in(2)          # use(2) 없이
        self.assertIn("use(2)", str(caught.exception))

    def test_reusing_the_same_owner_costs_nothing(self):
        s, made = self.make_session()
        s.use(1)
        before = len(made)
        for _ in range(10):
            s.use(1)
        self.assertEqual(len(made), before, "같은 공간인데 다시 만들었다")

    def test_spaces_do_not_pile_up_forever(self):
        s, _ = self.make_session()
        for owner in range(1, watch.OWNER_SPACE_LIMIT + 4):
            s.use(owner)
        self.assertLessEqual(len(s._spaces), watch.OWNER_SPACE_LIMIT)

    def test_the_default_space_is_never_evicted(self):
        """로그인 없는 일(날짜 감시)이 쓰는 공간이다 — 닫으면 매번 홈부터 연다."""
        s, _ = self.make_session()
        for owner in range(1, watch.OWNER_SPACE_LIMIT + 4):
            s.use(owner)
        self.assertIn(None, s._spaces)


class FakeSession:
    """브라우저 없는 CgvSession.

    쿠키를 다루는 메서드는 **진짜 CgvSession의 것을 그대로 빌려 쓴다** — 소유자
    판정이 거기 들어 있으므로, 흉내낸 구현으로 바꾸면 정작 검증하려던 로직이
    테스트에서 빠져나간다.
    """

    logged_in = watch.CgvSession.logged_in
    logged_in_as = watch.CgvSession.logged_in_as
    mark_logged_in = watch.CgvSession.mark_logged_in
    clear_session_cookies = watch.CgvSession.clear_session_cookies
    session_tokens = watch.CgvSession.session_tokens
    restore_tokens = watch.CgvSession.restore_tokens
    # 소유자 공간을 거쳐 풀리는 값들도 진짜 것을 빌린다 — 흉내내면 정작
    # 검증하려던 격리가 테스트에서 빠져나간다.
    _page = watch.CgvSession._page
    logged_in_owner = watch.CgvSession.logged_in_owner

    def __init__(self) -> None:
        page = type("P", (), {})()
        page.context = FakeCookieJar()
        # 진짜와 같은 모양의 공간 하나. use()를 부르지 않고 들어오는 경로를
        # 흉내내는 셈이라, cgv_login의 안전망(_detach)이 그대로 시험된다.
        self._space = watch._OwnerSpace(page.context, page)
        self.logins: list[str] = []
        self.refresh_ok = False

    def refresh_session(self) -> bool:
        if not self.refresh_ok:
            return False
        self._page.context.jar["accessToken"] = "refreshed"
        return True

    def login_cgv(self, user_id, password, *, timeout_ms=20_000):
        self.logins.append(user_id)
        self._page.context.jar.update({"accessToken": f"tok-{user_id}",
                                       "refresh_token": f"ref-{user_id}"})
        return self.session_tokens()


class TestCgvSessionIsPerOwner(DbCase):
    """회귀: 브라우저 세션 하나를 여러 사용자가 나눠 쓴다.

    `logged_in()`은 accessToken 쿠키가 **있는지**만 본다. 그 값만 보고 통과시키면
    앞사람의 로그인으로 뒷사람의 좌석을 조회하고, 자동 예매까지 앞사람 계정으로
    나간다. 세션이 **누구의** 것인지를 봐야 한다.
    """

    def link(self, name: str, cgv_id: str) -> int:
        uid = self.make_user(name)["id"]
        store.set_cgv_account(uid, cgv_id, f"pw-{cgv_id}")
        store.set_cgv_tokens(uid, {"accessToken": f"tok-{cgv_id}",
                                   "refresh_token": f"ref-{cgv_id}"})
        return uid

    def test_second_owner_does_not_inherit_the_first_session(self):
        import cgv_login

        a = self.link("owner", "alice")
        b = self.link("member", "bob")
        session = FakeSession()

        self.assertTrue(cgv_login.ensure_logged_in(a, session))
        self.assertEqual(session._page.context.jar["accessToken"], "tok-alice")

        self.assertTrue(cgv_login.ensure_logged_in(b, session))
        self.assertEqual(session.logged_in_owner, b)
        self.assertEqual(session._page.context.jar["accessToken"], "tok-bob",
                         "B의 좌석을 A의 로그인으로 조회하고 있다")

    def test_same_owner_twice_does_not_relogin(self):
        import cgv_login

        a = self.link("owner", "alice")
        session = FakeSession()
        cgv_login.ensure_logged_in(a, session)
        cgv_login.ensure_logged_in(a, session)
        self.assertEqual(session.logins, [], "같은 사람인데 다시 로그인했다")

    def test_recover_never_writes_another_owners_tokens(self):
        import cgv_login

        a = self.link("owner", "alice")
        b = self.link("member", "bob")
        session = FakeSession()
        cgv_login.ensure_logged_in(a, session)      # 세션은 A의 것
        session.refresh_ok = True                   # refresh는 성공할 수 있다

        cgv_login.recover_session(b, session)

        self.assertEqual(store.cgv_tokens(a)["accessToken"], "tok-alice",
                         "A의 저장된 세션이 손상됐다")
        self.assertNotEqual(store.cgv_tokens(b)["accessToken"], "refreshed",
                            "A의 세션을 refresh해 B의 행에 저장했다")
        self.assertEqual(session.logins, ["bob"],
                         "B로 다시 로그인했어야 한다")
        self.assertEqual(session.logged_in_owner, b)

    def test_login_now_clears_a_stale_session_first(self):
        import cgv_login

        a = self.link("owner", "alice")
        b = self.link("member", "bob")
        session = FakeSession()
        cgv_login.ensure_logged_in(a, session)

        self.assertTrue(cgv_login.login_now(b, session))
        # A의 refresh_token이 남아 있으면 B의 세션에 섞여 저장된다.
        self.assertEqual(store.cgv_tokens(b),
                         {"accessToken": "tok-bob", "refresh_token": "ref-bob"})

    def test_clearing_keeps_non_session_cookies(self):
        # 통째로 지우면 Cloudflare 통과 흔적까지 날아가 날짜 확인이 403을 맞는다.
        session = FakeSession()
        session._page.context.jar.update({"accessToken": "t", "cf_clearance": "cf"})
        session.clear_session_cookies()
        self.assertEqual(session._page.context.jar, {"cf_clearance": "cf"})
        self.assertIsNone(session.logged_in_owner)


class TestAlertDedupeIsPerSeatWatch(DbCase):
    """회귀: 미전송 알림의 중복 판정이 좌석 감시도 봐야 한다.

    좌석 알림은 target_id가 없고 dates가 [scn_ymd] 하나뿐이다. 한 사람이 같은
    날짜에 감시를 둘 걸면(A열·B열) 서로의 미전송 행을 덮어써 한쪽 알림이
    조용히 사라진다.
    """

    def watches(self):
        uid = self.make_user("owner")["id"]
        a = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["A"])
        b = store.add_seat_watch(uid, "오디세이", "용산", "20260825", rows=["B"])
        return uid, a["id"], b["id"]

    def test_two_watches_same_date_keep_separate_rows(self):
        uid, a, b = self.watches()
        first = store.record_alert("seat_open", "A열 빈자리", owner_id=uid,
                                   dates=["20260825"], seat_watch_id=a)
        second = store.record_alert("seat_open", "B열 빈자리", owner_id=uid,
                                    dates=["20260825"], seat_watch_id=b)

        self.assertNotEqual(first, second)
        bodies = {r["body"] for r in store.recent_alerts(owner_id=uid)}
        self.assertEqual(bodies, {"A열 빈자리", "B열 빈자리"})

    def test_same_watch_retry_reuses_the_row(self):
        uid, a, _ = self.watches()
        first = store.record_alert("seat_open", "A열 빈자리", owner_id=uid,
                                   dates=["20260825"], seat_watch_id=a)
        again = store.record_alert("seat_open", "A열 빈자리(재시도)", owner_id=uid,
                                   dates=["20260825"], seat_watch_id=a)

        self.assertEqual(first, again)
        rows = store.recent_alerts(owner_id=uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], 2)

    def test_history_carries_the_watch_id(self):
        uid, a, _ = self.watches()
        store.record_alert("seat_open", "A열 빈자리", owner_id=uid,
                           dates=["20260825"], seat_watch_id=a)
        self.assertEqual(store.recent_alerts(owner_id=uid)[0]["seat_watch_id"], a)

    def test_deleting_the_watch_keeps_the_history(self):
        uid, a, _ = self.watches()
        store.record_alert("seat_open", "A열 빈자리", owner_id=uid,
                           dates=["20260825"], seat_watch_id=a)
        store.delete_seat_watch(a, owner_id=uid)
        rows = store.recent_alerts(owner_id=uid)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["seat_watch_id"])


class TestSchemaStaysIdempotent(DbCase):
    """schema.sql은 서버가 뜰 때마다 통째로 실행된다 — 두 번째부터 깨지면 안 된다.

    2026-08-31 배포에서 이렇게 죽었다:

        ERROR: could not create unique index "seat_watches_uniq_idx"
        DETAIL: Key (…, scn_time, screen_types, rows)=(…) is duplicated.

    '만들고 → 나중에 DROP'을 한 파일에 같이 두면, 다음 기동에서 또 만들려 든다.
    그때는 새 인덱스 기준으로만 구별되는 행이 이미 들어와 있어 좁은 인덱스가
    만들어지지 않는다. 전체가 한 트랜잭션이라 **스키마가 통째로 롤백되고**,
    앱은 db_error를 안은 채 떠서 모든 API가 401을 냈다.
    """

    def test_rerunning_the_schema_survives_rows_only_a_new_index_can_tell_apart(self):
        uid = self.make_user("schema")["id"]
        # 좌석 번호 범위만 다른 두 행 — 예전 좁은 인덱스로는 '중복'이다.
        a = store.add_seat_watch(uid, "오디세이", "용산", "20260905",
                                 screen_types=["IMAX"], rows=["H", "I"],
                                 seat_num_from=13, seat_num_to=32)
        b = store.add_seat_watch(uid, "오디세이", "용산", "20260905",
                                 screen_types=["IMAX"], rows=["H", "I"])
        self.assertNotEqual(a["id"], b["id"], "새 인덱스라면 두 행이어야 한다")

        store.init_db()      # 재기동 — 여기서 터지면 로그인이 통째로 죽는다
        self.assertIsNotNone(store.seat_watch(a["id"]), "롤백되면 안 된다")

    def test_only_the_current_unique_index_is_created(self):
        """지나간 이름을 다시 만들면 같은 사고가 반복된다."""
        store.init_db()
        with store.pool().connection() as conn:
            names = {r["indexname"] for r in conn.execute(
                "select indexname from pg_indexes "
                "where tablename = 'seat_watches'").fetchall()}
        self.assertIn("seat_watches_uniq_seatnum_idx", names)
        for gone in ("seat_watches_uniq_idx", "seat_watches_uniq_range_idx"):
            self.assertNotIn(gone, names, f"{gone}가 되살아났다")

    def test_the_schema_file_never_creates_a_superseded_index(self):
        """소스 차원에서 못박는다 — DROP되는 이름을 CREATE하면 안 된다."""
        import re

        sql = (ROOT / "schema.sql").read_text(encoding="utf-8")
        created = set(re.findall(r"^CREATE UNIQUE INDEX IF NOT EXISTS (\w+)",
                                 sql, re.M))
        dropped = set(re.findall(r"^DROP INDEX IF EXISTS (\w+)", sql, re.M))
        both = created & dropped
        self.assertFalse(both, f"같은 파일에서 만들고 지우는 인덱스: {both}")


class TestDbErrorRecovers(DbCase):
    """DB가 늦게 뜨면 앱이 '떠 있지만 아무도 로그인 못 하는' 상태로 굳었다.

    2026-08-31 실측: 네이버 로그인이 끝까지 성공하고(state 검증까지 통과) 세션
    쿠키도 멀쩡한데 /api/me가 계속 401이었다. before_request가 **시작할 때 한 번
    정해진** db_error를 보고 세션을 아예 읽지 않았기 때문이다. compose에서
    Postgres가 몇 초 늦게 뜨는 것만으로 이 상태가 되고, 로그에는 401만 남는다.
    """

    def make_app(self, *, fail_until: int, background=False):
        """앞의 fail_until번은 DB 접속이 실패하는 앱을 만든다."""
        import web.app

        self.web = web.app
        calls = {"n": 0}
        real_init, real_secret = store.init_db, store.session_secret

        def fake_init():
            calls["n"] += 1
            if calls["n"] <= fail_until:
                raise RuntimeError("connection refused")
            return real_init()

        store.init_db = fake_init
        self.addCleanup(setattr, store, "init_db", real_init)
        try:
            app = self.web.create_app(start_background=background)
        finally:
            store.init_db = fake_init      # create_app 밖에서도 계속 가짜를 쓴다
        return app, calls

    def test_a_late_database_is_picked_up(self):
        app, _ = self.make_app(fail_until=1)
        self.assertIsNotNone(self.web.parts(app)["db_error"], "실패가 기록돼야 한다")

        self.web.parts(app)["db_next_try"] = 0.0        # 쿨다운을 지난 것으로
        self.assertIsNone(self.web.db_ready(app), "DB가 살았는데 못 붙었다")
        self.assertIsNone(self.web.parts(app)["db_error"])

    def test_the_session_key_returns_to_the_stored_one(self):
        """임시 랜덤 키를 그대로 두면 다음 재기동 때 또 전원 로그아웃된다."""
        app, _ = self.make_app(fail_until=1)
        temp_key = app.secret_key
        self.web.parts(app)["db_next_try"] = 0.0
        self.web.db_ready(app)
        self.assertNotEqual(app.secret_key, temp_key)
        self.assertEqual(app.secret_key, store.session_secret())

    def test_a_still_dead_database_is_reported_not_hidden(self):
        app, _ = self.make_app(fail_until=99)
        self.web.parts(app)["db_next_try"] = 0.0
        err = self.web.db_ready(app)
        self.assertIsNotNone(err)
        self.assertIn("connection refused", err)

    def test_the_first_request_retries_right_away(self):
        """DB가 1초 뒤에 떴다면 첫 요청에서 바로 되살아나야 한다."""
        app, calls = self.make_app(fail_until=99)
        before = calls["n"]
        self.web.db_ready(app)
        self.assertEqual(calls["n"], before + 1, "첫 요청이 시도조차 안 했다")

    def test_retries_are_rate_limited(self):
        """정말 죽어 있는 동안 매 요청마다 붙어 보면 전 요청이 느려진다."""
        app, calls = self.make_app(fail_until=99)
        self.web.db_ready(app)          # 첫 시도는 바로 나간다(위 테스트)
        after_first = calls["n"]
        for _ in range(50):
            self.web.db_ready(app)
        self.assertEqual(calls["n"], after_first, "쿨다운 안에서 또 시도했다")

        self.web.parts(app)["db_next_try"] = 0.0
        self.web.db_ready(app)
        self.assertEqual(calls["n"], after_first + 1,
                         "쿨다운이 지나면 한 번은 시도해야 한다")

    def test_a_healthy_app_never_retries(self):
        app, calls = self.make_app(fail_until=0)
        self.assertIsNone(self.web.parts(app)["db_error"])
        before = calls["n"]
        for _ in range(100):
            self.assertIsNone(self.web.db_ready(app))
        self.assertEqual(calls["n"], before, "멀쩡한데 다시 붙어 봤다")

    def test_background_work_starts_on_recovery(self):
        """되살아나도 감시가 안 돌면 반쪽이다 — 화면만 열리고 알림은 안 온다."""
        app, _ = self.make_app(fail_until=1, background=True)
        info = self.web.parts(app)
        started = []
        info["worker"].start = lambda: started.append("worker")
        info["poller"].start = lambda: started.append("poller")

        info["db_next_try"] = 0.0
        self.web.db_ready(app)
        self.assertEqual(started, ["worker", "poller"])

    def test_the_session_is_read_again_after_recovery(self):
        """이게 원래 증상이다 — 쿠키는 멀쩡한데 401만 나오던 것."""
        app, _ = self.make_app(fail_until=1)
        uid = self.make_user("late-db")["id"]
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = uid

        self.web.parts(app)["db_next_try"] = 0.0
        res = client.get("/api/me")
        self.assertEqual(res.status_code, 200, "복구 뒤에도 401이면 고쳐진 게 없다")
        self.assertEqual(res.get_json()["user"]["id"], uid)


class TestHealthIsReachableWithoutLogin(DbCase):
    """헬스체크는 로그인할 수 없는 쪽이 부른다 — 대신 내용을 권한에 따라 자른다."""

    @classmethod
    def setUpClass(cls) -> None:
        import web.app
        cls.app = web.app.create_app(start_background=False)

    def setUp(self) -> None:
        super().setUp()
        self.client = self.app.test_client()

    def test_anonymous_gets_a_bare_ok(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIs(payload["ok"], True)
        # 접속 문자열·워커 내부는 익명에게 나가면 안 된다.
        self.assertEqual(set(payload), {"ok"})

    def test_owner_gets_the_detail(self):
        owner = self.make_user("owner")
        with self.client.session_transaction() as sess:
            sess["user_id"] = owner["id"]
        payload = self.client.get("/api/health").get_json()
        self.assertIn("worker", payload)
        self.assertIn("db", payload)

    def test_member_gets_the_bare_form(self):
        self.make_user("owner")
        member = self.make_user("member")
        store.set_user_status(member["id"], "approved")
        with self.client.session_transaction() as sess:
            sess["user_id"] = member["id"]
        self.assertEqual(set(self.client.get("/api/health").get_json()), {"ok"})


if __name__ == "__main__":
    unittest.main()
