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
import unittest.mock
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

    def test_another_watch_on_the_same_showtime_does_not_hold_again(self):
        """같은 회차를 보는 감시가 둘이면 선점도 둘 나간다 — 결제도 두 번이다.

        감시 유일성이 rows·seat_num_from/to까지 포함해서, 같은 (영화·극장·날짜·
        시각)에 열만 다르게 건 감시 쌍이 실제로 만들어진다. 감시 id로만 중복을
        보면 서로를 못 보고 둘 다 통과한다.
        """
        import booking
        uid = self.make_user("owner")["id"]
        first = self._watch(uid, auto_book=True, party_size=2, rows=["A"])
        second = self._watch(uid, auto_book=True, party_size=2, rows=["A"],
                             seat_num_from=1, seat_num_to=6)
        self.assertNotEqual(first["id"], second["id"])

        held = booking.try_auto_book(
            None, first, self._row(), self._seats(set(range(1, 9))),
            mov_nm="오디세이", site_nm="용산",
            hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"})
        self.assertEqual(held["action"], "held")

        def must_not_run(session, ctx):
            self.fail("같은 회차인데 두 번째 선점이 나갔습니다")

        out = booking.try_auto_book(
            None, second, self._row(), self._seats(set(range(1, 9))),
            mov_nm="오디세이", site_nm="용산", hold_fn=must_not_run)
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "already held")

    def test_a_live_hold_outlives_the_watch_that_made_it(self):
        """감시를 지우면 seat_watch_id가 NULL이 된다(이력은 남긴다) — 그래도 찾아야 한다."""
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2, rows=["A"])
        booking.try_auto_book(
            None, w, self._row(), self._seats(set(range(1, 9))),
            mov_nm="오디세이", site_nm="용산",
            hold_fn=lambda s, c: {"ok": True, "mov_atkt_no": "P1"})
        self.assertTrue(store.delete_seat_watch(w["id"], owner_id=uid))
        self.assertIsNone(store.booking_attempts(owner_id=uid)[0]["seat_watch_id"])

        ident = {"owner_id": uid, "showtime_key": "001|5",
                 "scn_ymd": "20260825", "site_nm": "용산"}
        self.assertIsNotNone(store.active_hold(w["id"], **ident))
        # showtime_key는 하루 안에서만 유일하다 — 날짜·극장이 다르면 남의 선점이다.
        self.assertIsNone(store.active_hold(w["id"], **{**ident, "scn_ymd": "20260826"}))
        self.assertIsNone(store.active_hold(w["id"], **{**ident, "site_nm": "왕십리"}))

    def test_a_successful_payment_protects_the_tab_it_opened(self):
        """결제창이 뜬 탭을 워밍 풀에 남겨 두면 같은 날짜의 다른 감시가 덮는다."""
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, auto_pay=True, party_size=1)
        detached = []

        class Sess:
            def detach_booking_page(self, key, keep_seconds):
                detached.append((key, keep_seconds))
                return True

        def fake_hold(session, ctx):
            ctx["_page"] = "결제탭"           # hold_block이 하는 일
            return {"ok": True, "mov_atkt_no": "P1"}

        def fake_pay(session, ctx, *, method):
            return {"ok": True, "method": method, "pay_url": "https://kakao/1",
                    "pay_expires_at": None, "amount": 15000, "error": ""}

        out = booking.try_auto_book(
            Sess(), w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", mov_no="30001323", site_no="0013",
            hold_fn=fake_hold, pay_fn=fake_pay)

        self.assertEqual(out["action"], "held")
        self.assertEqual(detached, [("30001323|0013|20260825",
                                     booking.PAY_PAGE_KEEP_SECONDS)])

    def test_a_failed_payment_leaves_the_warm_pool_alone(self):
        """결제창이 안 떴으면 지킬 것도 없다 — 괜히 탭만 버리면 프리워밍이 죽는다."""
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, auto_pay=True, party_size=1)
        detached = []

        class Sess:
            def detach_booking_page(self, key, keep_seconds):
                detached.append(key)
                return True

        def fake_hold(session, ctx):
            ctx["_page"] = "결제탭"
            return {"ok": True, "mov_atkt_no": "P1"}

        def fake_pay(session, ctx, *, method):
            return {"ok": False, "method": method, "pay_url": None,
                    "pay_expires_at": None, "amount": None,
                    "error": "카카오페이 결제창이 뜨지 않았습니다"}

        out = booking.try_auto_book(
            Sess(), w, self._row(), self._seats({4}), mov_nm="오디세이",
            site_nm="용산", hold_fn=fake_hold, pay_fn=fake_pay)

        self.assertEqual(out["action"], "held")   # 선점은 유효하다
        self.assertEqual(detached, [])

    def test_a_watch_deleted_mid_cycle_is_not_booked(self):
        """사이클이 도는 동안 감시를 지우면 선점하지 않는다.

        사이클은 시작할 때 감시 목록을 한 번 읽고 그 스냅샷으로 돈다. 그동안
        사용자가 감시를 지웠다는 건 그 좌석을 원하지 않는다는 뜻이다 — 그런데도
        잡으면 자동 결제까지 이어져 돈이 나간다.
        """
        import booking
        uid = self.make_user("owner")["id"]
        w = self._watch(uid, auto_book=True, party_size=2)
        self.assertTrue(store.delete_seat_watch(w["id"], owner_id=uid))

        def must_not_run(session, ctx):
            self.fail("지워진 감시로 선점이 나갔습니다")

        out = booking.try_auto_book(
            None, w, self._row(), self._seats(set(range(1, 9))),
            mov_nm="오디세이", site_nm="용산", hold_fn=must_not_run)
        self.assertEqual(out["action"], "skip")
        self.assertEqual(out["reason"], "watch deleted")

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
        session.budget = watch.RateBudget()
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

    def test_compact_seats_are_inflated_back(self):
        """좌석을 값 배열로 받아 원래 모양으로 되돌린다.

        키 이름이 좌석마다(624석 × 회차 수) 반복되는 게 전송 비용의 대부분이라
        브라우저에서 떼고 보낸다 — 실측 6.6MB → 1.9MB, 전송 1760ms → 829ms.
        되돌린 결과가 원본과 다르면 감시가 조용히 틀린 좌석을 본다.
        """
        import seats as seats_mod

        fields = seats_mod.SEAT_FIELDS
        values = ["H", "13", "Y", "일반", "일반존", "일반", 1, 3, "N", "N",
                  "L1", "1", "1", "1", "27", "01", "01"]
        s, _ = self.make_session({
            "/a": self.ok({"items": [{"seats": [values, values]}]})})
        out = s.get_json_many(["/a"], seat_fields=fields)[0]
        seats_back = out["data"]["items"][0]["seats"]
        self.assertEqual(seats_back, [dict(zip(fields, values))] * 2)
        # parse_seats가 그대로 읽을 수 있어야 한다
        self.assertEqual(len(seats_mod.parse_seats(out["data"])), 2)

    def test_without_seat_fields_the_payload_is_untouched(self):
        s, _ = self.make_session({"/a": self.ok({"items": [{"seats": [{"x": 1}]}]})})
        out = s.get_json_many(["/a"])[0]
        self.assertEqual(out["data"]["items"][0]["seats"], [{"x": 1}])

    def test_an_odd_shape_is_left_alone(self):
        """모양이 예상과 다르면 건드리지 않는다 — 조용히 뭉개면 안 된다."""
        s, _ = self.make_session({"/a": self.ok({"items": [{"seats": "이상함"}]})})
        out = s.get_json_many(["/a"], seat_fields=["a"])[0]
        self.assertEqual(out["data"]["items"][0]["seats"], "이상함")

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
        s.budget = watch.RateBudget()

        def boom(script, arg):
            raise RuntimeError("페이지가 갈아 끼워지는 중")

        page = type("P", (), {"evaluate": staticmethod(boom)})()
        s._spaces = watch.OrderedDict()
        s._current = None
        s._spaces[None] = watch._OwnerSpace(object(), page)
        self.assertEqual(s.get_json_many(["/a", "/b"]), [None, None])


class TestRateBudget(unittest.TestCase):
    """분당 몇 건까지 보낼 수 있는지를 스스로 찾는다.

    CGV의 한도는 공개돼 있지 않다. 실측으로 205/분은 무사했고 820/분에서 429가
    났으니 그 사이 어딘가다. 안전한 값을 미리 못박으면 실제 한계가 그보다 높을
    때 공연히 덜 보게 되고, 낮으면 계속 거절당한다 — 그래서 올려 보고 맞는다.
    """

    def test_it_hands_out_only_what_is_left(self):
        b = watch.RateBudget(limit=10)
        self.assertEqual(b.take(4), 4)
        self.assertEqual(b.allowance(), 6)
        self.assertEqual(b.take(99), 6, "남은 만큼만 줘야 한다")
        self.assertEqual(b.take(1), 0)

    def test_being_refused_halves_the_limit(self):
        b = watch.RateBudget(limit=200)
        b.penalize()
        self.assertEqual(b.limit, 100)

    def test_the_floor_never_raises_the_limit(self):
        """거절당했는데 오히려 더 보내면 안 된다."""
        b = watch.RateBudget(limit=20)
        b.penalize()
        self.assertLessEqual(b.limit, 20)

    def test_a_refusal_also_spends_the_window(self):
        # 방금 거절당했으니 남은 예산이 있어도 쉬어야 한다.
        b = watch.RateBudget(limit=100)
        b.penalize()
        self.assertEqual(b.allowance(), 0)

    def test_quiet_time_probes_upward(self):
        b = watch.RateBudget(limit=100)
        b._last_probe = 0.0          # 충분히 조용했던 것으로
        b.relax()
        self.assertGreater(b.limit, 100)

    def test_probing_respects_the_ceiling(self):
        b = watch.RateBudget(limit=watch.RATE_CEILING)
        b._last_probe = 0.0
        b.relax()
        self.assertEqual(b.limit, watch.RATE_CEILING)

    def test_probing_does_not_happen_too_soon(self):
        b = watch.RateBudget(limit=100)
        b.relax()
        self.assertEqual(b.limit, 100, "쉬지도 않고 바로 올렸다")

    def test_the_window_slides(self):
        b = watch.RateBudget(limit=5)
        b.take(5)
        self.assertEqual(b.allowance(), 0)
        b._sent.clear()              # 창이 지난 것으로
        self.assertEqual(b.allowance(), 5)


class TestThrottling(unittest.TestCase):
    """429는 사고고, 예산 소진은 정상이다 — 둘을 구분한다."""

    def make_session(self, replies, limit=1000):
        session = watch.CgvSession.__new__(watch.CgvSession)
        session.requests = 0
        session.budget = watch.RateBudget(limit=limit)
        self.calls = []

        def evaluate(script, arg):
            if isinstance(arg, dict) and "paths" in arg:
                self.calls.append(list(arg["paths"]))
                return [replies.get(p, {"status": 0}) for p in arg["paths"]]
            self.calls.append([arg])
            return replies.get(arg, {"status": 0, "text": ""})

        page = type("P", (), {"evaluate": staticmethod(evaluate)})()
        session._spaces = watch.OrderedDict()
        session._current = None
        session._spaces[None] = watch._OwnerSpace(object(), page)
        return session

    def test_a_429_is_not_retried(self):
        """예전에는 2초·4초 백오프로 세 번을 더 보냈다 — 세 배로 때리는 짓이다."""
        s = self.make_session({"/a": {"status": 429, "text": ""}})
        with self.assertRaises(watch.Throttled):
            s.get_json("/a")
        self.assertEqual(len(self.calls), 1, "429를 받고도 다시 보냈다")

    def test_a_429_lowers_the_limit(self):
        s = self.make_session({"/a": {"status": 429, "text": ""}}, limit=200)
        with self.assertRaises(watch.Throttled):
            s.get_json("/a")
        self.assertLess(s.budget.limit, 200)

    def test_a_429_stops_the_rest_of_the_batch(self):
        s = self.make_session({"/p0": {"status": 429}})
        paths = [f"/p{i}" for i in range(watch.SEAT_MAP_BATCH * 3)]
        out = s.get_json_many(paths)
        self.assertEqual(len(out), len(paths), "결과 길이는 유지돼야 한다")
        self.assertEqual(len(self.calls), 1, "거절당한 뒤에도 계속 보냈다")

    def test_running_out_of_budget_is_not_an_error(self):
        """예산 소진은 우리가 아낀 것이다 — 거절당한 것과 다르다."""
        good = {"status": 200, "text": '{"statusCode": 0, "data": {}}'}
        s = self.make_session({"/a": good}, limit=1)
        s.get_json("/a")
        with self.assertRaises(watch.RateLimited):
            s.get_json("/b")

    def test_a_batch_beyond_the_budget_is_deferred_not_dropped(self):
        s = self.make_session({}, limit=3)
        out = s.get_json_many([f"/p{i}" for i in range(10)])
        self.assertEqual(len(out), 10, "미룬 몫도 자리를 지켜야 한다")
        self.assertEqual(sum(len(c) for c in self.calls), 3,
                         "예산보다 많이 보냈다")

    def test_no_budget_means_no_request(self):
        s = self.make_session({}, limit=0)
        self.assertEqual(s.get_json_many(["/a", "/b"]), [None, None])
        self.assertEqual(self.calls, [], "예산이 없는데 보냈다")

    def test_the_batch_stays_small_enough(self):
        """묶음을 키웠다가 429를 받고 되돌렸다 — 다시 키우면 같은 일이 난다."""
        self.assertLessEqual(watch.SEAT_MAP_BATCH, 8)


class TestADeletedWatchDoesNotCrashTheCycle(DbCase):
    """사이클 도중 감시가 지워지는 경합 (store.watch_was_deleted).

    사이클은 시작할 때 감시 목록을 한 번 읽고(store.seat_watches) 3초간 그
    스냅샷으로 돈다. 그동안 웹에서 감시를 지우면 뒤이은 쓰기가 없는 id를 가리켜
    FK 위반이 되고, 예전에는 그 예외가 사이클 밖으로 나가 **바퀴가 통째로
    죽었다.** 실측(2026-09-02 10:10, 지난 날짜 감시를 정리하던 중):

      ForeignKeyViolation: ... "booking_attempts_seat_watch_id_fkey"
      DETAIL:  Key (seat_watch_id)=(65) is not present in table "seat_watches".

    고장이 아니라 정상적인 경합이므로 조용히 넘어가야 한다.
    """

    def gone_watch(self):
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825")
        self.assertTrue(store.delete_seat_watch(w["id"], owner_id=uid))
        return uid, w["id"]

    def test_saving_state_for_a_gone_watch_is_not_an_error(self):
        _, wid = self.gone_watch()
        self.assertFalse(store.save_seat_state(wid, {"001|5": ["A1"]}))

    def test_saving_an_error_for_a_gone_watch_is_not_an_error(self):
        _, wid = self.gone_watch()
        self.assertFalse(store.save_seat_state(wid, {}, error="시간표 조회 실패"))

    def test_a_live_watch_still_saves(self):
        """그물이 정상 경로를 삼키지 않는지 — 이게 없으면 조용히 아무것도 안 남는다."""
        uid = self.make_user("owner")["id"]
        w = store.add_seat_watch(uid, "오디세이", "용산", "20260825")
        self.assertTrue(store.save_seat_state(w["id"], {"001|5": ["A1"]}))
        self.assertEqual(store.prev_seat_state(w["id"]), {"001|5": ["A1"]})

    def test_a_booking_attempt_for_a_gone_watch_returns_none(self):
        uid, wid = self.gone_watch()
        self.assertIsNone(store.create_booking_attempt(
            seat_watch_id=wid, owner_id=uid, showtime_key="001|5",
            mov_nm="오디세이", site_nm="용산", scn_ymd="20260825",
            start_hhmm="22:10", seat_labels=["A1"], seat_loc_nos=["L1"]))

    def test_an_alert_for_a_gone_watch_is_still_recorded(self):
        """선점 성공처럼 돈과 좌석이 걸린 알림을 버리면 안 된다 — 연결만 끊는다."""
        uid, wid = self.gone_watch()
        alert_id = store.record_alert(
            "book_held", "🎫 좌석 선점 완료", owner_id=uid,
            mov_nm="오디세이", site_nm="용산", dates=["20260825"],
            seat_watch_id=wid)
        self.assertIsNotNone(alert_id)
        row = next(a for a in store.recent_alerts(limit=5) if a["id"] == alert_id)
        self.assertIsNone(row["seat_watch_id"], "없는 감시를 가리키고 있다")
        self.assertIn("선점 완료", row["body"])

    def test_a_real_foreign_key_problem_is_not_swallowed(self):
        """seat_watch_id와 무관한 FK 위반은 그대로 올라가야 한다."""
        import psycopg

        other = psycopg.errors.ForeignKeyViolation(
            'insert or update on table "alerts" violates foreign key '
            'constraint "alerts_owner_id_fkey"')
        self.assertFalse(store.watch_was_deleted(other))
        self.assertFalse(store.watch_was_deleted(RuntimeError("아무 오류")))


class TestOneWatchCannotKillTheCycle(DbCase):
    """감시 하나에서 예외가 나도 남은 감시는 확인돼야 한다.

    예전에는 `for w in group` 루프에 그물이 없어서, 예외가 나가면
    check_seat_watches가 통째로 죽고 남은 감시들은 확인조차 되지 않았다.
    실측(2026-09-02 10:10)으로 사이클 도중 감시를 지웠을 때 FK 위반이 그렇게
    바퀴를 죽였다. 그 경합은 store 쪽에서 조용히 넘기게 고쳤지만, 여기 그물이
    없으면 다음번 뜻밖의 예외에 같은 일이 난다.
    """

    class Sess:
        def __init__(self):
            self.budget = type("B", (), {"relax": lambda self: None})()

        def allowance(self):
            return 100

        def use(self, owner_id):
            pass

    def setUp(self):
        super().setUp()
        import cgv_login
        import seats
        self.seats = seats
        self._patches = [
            unittest.mock.patch.object(cgv_login, "ensure_logged_in",
                                       lambda oid, s: True),
            unittest.mock.patch.object(seats, "_prefetch_seat_maps",
                                       lambda *a, **k: {}),
        ]
        for pt in self._patches:
            pt.start()
            self.addCleanup(pt.stop)

    def test_a_raising_watch_does_not_stop_the_others(self):
        uid = self.make_user("owner")["id"]
        first = store.add_seat_watch(uid, "오디세이", "용산", "20260825")
        second = store.add_seat_watch(uid, "오디세이", "용산", "20260826")
        seen = []

        def one(session, catalog, w, *a, **k):
            seen.append(w["id"])
            if w["id"] == first["id"]:
                raise RuntimeError("뜻밖의 고장")
            return 3

        with unittest.mock.patch.object(self.seats, "_check_one_seat_watch", one):
            summary = self.seats.check_seat_watches(self.Sess())

        self.assertEqual(sorted(seen), sorted([first["id"], second["id"]]),
                         "터진 감시 뒤로 나머지를 안 봤다")
        self.assertEqual(summary["watches_checked"], 2)
        self.assertEqual(summary["alerts_sent"], 3, "성공한 감시의 알림이 사라졌다")

    def test_throttling_still_stops_the_whole_cycle(self):
        """이건 사이클 전체 신호다 — 삼키고 계속하면 CGV를 계속 때린다."""
        import watch as watch_mod
        uid = self.make_user("owner")["id"]
        store.add_seat_watch(uid, "오디세이", "용산", "20260825")
        store.add_seat_watch(uid, "오디세이", "용산", "20260826")

        def throttled(session, catalog, w, *a, **k):
            raise watch_mod.Throttled("CGV가 요청을 거절했습니다 (HTTP 429)")

        with unittest.mock.patch.object(self.seats, "_check_one_seat_watch",
                                        throttled):
            with self.assertRaises(watch_mod.Throttled):
                self.seats.check_seat_watches(self.Sess())


class TestThePayingTabLeavesTheWarmPool(unittest.TestCase):
    """CgvSession.detach_booking_page — 결제창이 뜬 탭을 지키는 부분.

    실측(2026-09-03 13:36): 결제 링크가 나간 4초 뒤 같은 탭이
    `예매 화면을 20260904로 바로 열었습니다`로 덮였다. 9/4 감시가 셋이었고
    warm_key는 (영화·극장·날짜)라 셋이 한 탭을 공유했다 — 선점에 성공한 감시만
    꺼지고, 남은 둘의 프리워밍이 그 탭을 집어 간 것이다.
    """

    class Tab:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def evaluate(self, _script):
            if self.closed:
                raise RuntimeError("닫힌 탭이다")
            return 1

        def close(self):
            self.closed = True

    def make_session(self):
        opened: list = []
        Tab = self.Tab

        class Ctx:
            def new_page(self):
                tab = Tab(f"탭{len(opened)}")
                opened.append(tab)
                return tab

        session = watch.CgvSession.__new__(watch.CgvSession)
        session._spaces = watch.OrderedDict()
        session._current = None
        session._browser = object()
        space = watch._OwnerSpace(Ctx(), None)
        session._spaces[None] = space
        return session, space, opened

    def test_the_kept_tab_is_not_closed_and_not_handed_out_again(self):
        s, space, opened = self.make_session()
        paying = s.booking_page("K")

        self.assertTrue(s.detach_booking_page("K", 600))
        self.assertNotIn("K", space.booking_pages, "풀에 남아 있으면 또 집어 간다")
        self.assertFalse(paying.closed, "지켜야 할 결제 탭을 닫아 버렸다")

        # 같은 키로 다시 부르면 **새 탭**이 열려야 한다 — 남은 감시의 프리워밍은
        # 계속 돌아야 하고, 그렇다고 결제창을 줘서는 안 된다.
        again = s.booking_page("K")
        self.assertIsNot(again, paying)
        self.assertFalse(paying.closed)
        self.assertEqual(len(opened), 2)

    def test_the_kept_tab_is_closed_once_its_time_is_up(self):
        s, space, _ = self.make_session()
        paying = s.booking_page("K")
        s.detach_booking_page("K", -1)          # 시한이 이미 지난 것으로
        s.booking_page("다른키")                 # 이때 정리된다
        self.assertTrue(paying.closed, "시한이 지난 결제 탭이 남았다")
        self.assertEqual(space.paying_pages, [])

    def test_detaching_what_is_not_there_is_not_an_error(self):
        s, _, _ = self.make_session()
        self.assertFalse(s.detach_booking_page("없는키", 600))

    def test_kept_tabs_do_not_pile_up_forever(self):
        s, space, _ = self.make_session()
        for i in range(watch.PAYING_PAGE_LIMIT + 2):
            s.booking_page(f"K{i}")
            s.detach_booking_page(f"K{i}", 600)
        self.assertLessEqual(len(space.paying_pages), watch.PAYING_PAGE_LIMIT)

    def test_a_live_payment_is_visible_across_every_space(self):
        """재활용은 컨텍스트를 통째로 닫는다 — 남의 공간 결제창까지 죽는다."""
        s, space, _ = self.make_session()
        self.assertFalse(s.has_live_payment(), "지키는 탭이 없는데 있다고 한다")

        s.booking_page("K")
        s.detach_booking_page("K", 600)
        self.assertTrue(s.has_live_payment())

        # 다른 소유자 공간으로 옮겨도 보여야 한다.
        other = watch._OwnerSpace(space.context, None)
        s._spaces[7] = other
        s._current = 7
        self.assertTrue(s.has_live_payment(), "남의 공간 결제창을 못 봤다")

    def test_a_payment_whose_time_is_up_is_not_live(self):
        s, _, _ = self.make_session()
        s.booking_page("K")
        s.detach_booking_page("K", -1)
        self.assertFalse(s.has_live_payment())

    def test_a_deliberate_teardown_takes_the_kept_tabs_too(self):
        """일부러 버리는 자리에서 이것만 남기면 아무도 정리하지 않는 탭이 된다."""
        s, space, _ = self.make_session()
        paying = s.booking_page("K")
        s.detach_booking_page("K", 600)
        s.close_booking_pages()
        self.assertTrue(paying.closed)
        self.assertEqual(space.paying_pages, [])


class TestTheRecycleWaitsForThePayment(unittest.TestCase):
    """정기 브라우저 재활용이 결제 진행 중인 세션을 갈아버리면 안 된다.

    재활용은 메모리 누적을 끊으려고 30분마다 브라우저를 통째로 다시 띄운다. 그런데
    결제 시한은 15분이라 두 창이 겹치는 일이 흔하다 — 실측(2026-09-03)으로 결제
    링크를 보낸 뒤 10분 39초와 **45초** 뒤에 각각 재활용이 돌아 두 건을 잃었다.
    결제창이 죽으면 카카오페이 승인이 CGV까지 가지 않아 돈만 나간다.
    """

    class Session:
        def __init__(self, live):
            self.live = live

        def has_live_payment(self):
            return self.live

    def worker(self, *, age_minutes, live_payment):
        import browser_worker
        from datetime import datetime, timedelta

        w = browser_worker.BrowserWorker.__new__(browser_worker.BrowserWorker)
        w._session = self.Session(live_payment)
        w._session_started = (datetime.now().astimezone()
                              - timedelta(minutes=age_minutes))
        w._recycle_deferred = False

        real = browser_worker.store.get_setting
        browser_worker.store.get_setting = lambda key, default=None: (
            30 if key == "session_recycle_minutes" else real(key, default))
        self.addCleanup(setattr, browser_worker.store, "get_setting", real)
        return w

    def test_an_old_session_with_nothing_in_flight_is_recycled(self):
        self.assertTrue(self.worker(age_minutes=31, live_payment=False)
                        ._session_expired())

    def test_a_young_session_is_left_alone(self):
        self.assertFalse(self.worker(age_minutes=1, live_payment=False)
                         ._session_expired())

    def test_a_payment_in_flight_defers_the_recycle(self):
        w = self.worker(age_minutes=31, live_payment=True)
        self.assertFalse(w._session_expired(), "결제 중인데 브라우저를 갈았다")
        self.assertTrue(w._recycle_deferred)
        # 판정은 작업마다 돈다 — 두 번째부터는 로그를 다시 남기지 않는다.
        self.assertFalse(w._session_expired())

    def test_the_recycle_happens_once_the_payment_window_closes(self):
        """미루기가 영구적이면 안 된다 — 시한이 지나면 갈려야 한다."""
        w = self.worker(age_minutes=31, live_payment=True)
        self.assertFalse(w._session_expired())
        w._session.live = False
        self.assertTrue(w._session_expired())
        self.assertFalse(w._recycle_deferred)

    def test_a_session_that_never_started_is_not_expired(self):
        w = self.worker(age_minutes=31, live_payment=False)
        w._session_started = None
        self.assertFalse(w._session_expired())


class TestTheTwoTabPoolsStayApart(unittest.TestCase):
    """미리 진행해 둔 탭(2층)은 1층 폴백과 따로 센다.

    1층(날짜 단위)은 상영표가 그려진 채 쉬는 **폴백**이다. 2층이 무효가 되면
    거기로 돌아가 0.2초에 회차를 누른다. 한 LRU에 섞으면 회차 수만큼 늘어나는
    2층이 1층을 축출해, 그 폴백이 사라지고 선점마다 딥링크 6.2초를 다시 문다
    (커밋 ad83cbb가 기록한 절벽과 같은 모양이다).
    """

    class Tab:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def evaluate(self, _script):
            if self.closed:
                raise RuntimeError("닫힌 탭이다")
            return 1

        def close(self):
            self.closed = True

    def make_session(self):
        opened = []
        Tab = self.Tab

        class Ctx:
            def new_page(self):
                tab = Tab(f"탭{len(opened)}")
                opened.append(tab)
                return tab

        s = watch.CgvSession.__new__(watch.CgvSession)
        s._spaces = watch.OrderedDict()
        s._current = None
        s._browser = object()
        space = watch._OwnerSpace(Ctx(), None)
        s._spaces[None] = space
        return s, space, opened

    def test_the_same_key_gives_different_tabs_per_pool(self):
        s, _, _ = self.make_session()
        self.assertIsNot(s.booking_page("K"), s.advanced_page("K"))

    def test_second_tier_churn_does_not_evict_the_fallback(self):
        """이게 두 풀로 나눈 이유 전부다."""
        s, space, _ = self.make_session()
        fallback = s.booking_page("날짜키")
        for i in range(watch.ADVANCED_PAGE_LIMIT + 3):
            s.advanced_page(f"회차{i}")
        self.assertIs(s.booking_page("날짜키"), fallback, "폴백이 쫓겨났다")
        self.assertFalse(fallback.closed)
        self.assertLessEqual(len(space.advanced_pages),
                             watch.ADVANCED_PAGE_LIMIT)

    def test_dropping_an_advanced_tab_clears_its_record(self):
        s, space, _ = self.make_session()
        page = s.advanced_page("K")
        s.set_advanced_stage("K", "VISITOR")
        self.assertEqual(s.advanced_stage("K")["state"], "VISITOR")

        self.assertTrue(s.drop_advanced_page("K"))
        self.assertTrue(page.closed)
        self.assertIsNone(s.advanced_stage("K"))
        self.assertNotIn("K", space.advanced_pages)
        self.assertFalse(s.drop_advanced_page("K"))

    def test_a_stage_without_a_tab_is_never_recorded(self):
        """탭이 없는데 "준비됐다"고 우기는 기록이 남으면 그게 엉뚱한 선점이 된다."""
        s, _, _ = self.make_session()
        s.set_advanced_stage("없는키", "PARTY_SET")
        self.assertIsNone(s.advanced_stage("없는키"))

    def test_the_payment_tab_is_found_in_either_pool(self):
        """선점이 2층 탭에서 났으면 결제창도 거기 있다 — 여기서 키를 틀리면 돈이 나간다."""
        s, space, _ = self.make_session()
        paying = s.advanced_page("진행키")
        s.set_advanced_stage("진행키", "VISITOR")

        self.assertTrue(s.detach_booking_page("진행키", 600))
        self.assertFalse(paying.closed, "지켜야 할 결제 탭을 닫아 버렸다")
        self.assertNotIn("진행키", space.advanced_pages)
        self.assertIsNone(s.advanced_stage("진행키"))
        self.assertIsNot(s.advanced_page("진행키"), paying)

    def test_a_deliberate_teardown_closes_both_pools(self):
        s, space, _ = self.make_session()
        first = s.booking_page("날짜키")
        second = s.advanced_page("진행키")
        s.set_advanced_stage("진행키", "VISITOR")

        s.close_booking_pages()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(space.advance_state, {})

    def test_a_dead_advanced_tab_is_replaced_and_forgotten(self):
        s, _, _ = self.make_session()
        page = s.advanced_page("K")
        s.set_advanced_stage("K", "VISITOR")
        page.closed = True                     # 브라우저가 탭을 잃었다
        self.assertIsNot(s.advanced_page("K"), page)
        self.assertIsNone(s.advanced_stage("K"), "죽은 탭의 기록이 남았다")


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
