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


if __name__ == "__main__":
    unittest.main()
