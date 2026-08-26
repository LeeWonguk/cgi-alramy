#!/usr/bin/env python3
"""Postgres 저장소 — 감시 대상·관측 상태·알림·사이클 이력·영화/극장 목록.

예전에는 감시 대상이 config.toml에, 관측 결과가 state.json에 있었다. 웹에서
대상을 편집하려면 쓰기가 필요한데 tomllib은 읽기 전용이고 TOML을 다시 쓰면
파일의 주석 설명이 다 날아간다. 그래서 진짜 출처를 이 DB로 옮기고,
config.toml은 최초 1회 시드로만 쓴다 (migrate_legacy 참고).
"""

from __future__ import annotations

import atexit
import logging
import os
import secrets
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from envfile import load_env

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
CONFIG_PATH = ROOT / "config.toml"
LEGACY_STATE_PATH = ROOT / "state.json"

DEFAULT_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/cgv"
MIN_INTERVAL_SECONDS = 10  # 이보다 짧으면 CGV 쪽 부담이 커진다

log = logging.getLogger("cgv-watch.store")

# 설정 기본값. settings 표에 행이 없으면 이 값이 쓰인다.
DEFAULT_SETTINGS: dict[str, Any] = {
    "poll_interval_seconds": 30,
    "include_showtimes": True,
    "lookahead_days": 0,
    "headless": True,
    "default_screen_types": [],
    "session_recycle_minutes": 30,
}

# 브라우저가 하나뿐이고 스케줄러도 하나라 서버 전체에 하나씩만 있을 수 있는 값.
# 소유자만 바꾼다.
SERVER_SETTING_KEYS = ("poll_interval_seconds", "headless", "session_recycle_minutes")

# 사용자마다 다를 수 있는 값. 실제 값은 users 행에 있고, settings의 같은 키는
# **새 사용자를 만들 때의 초기값**으로만 쓰인다 (config.toml에서 시드된다).
USER_SETTING_KEYS = ("include_showtimes", "lookahead_days", "default_screen_types")

# 절대 API로 내보내지 않는 키 — 운영 상태값과 세션 서명 비밀키.
SECRET_SETTING_KEYS = ("config_error_signature", "global_fail_count",
                       "flask_secret_key")

# config.toml 키 -> settings 키. 시드할 때만 참조한다.
CONFIG_KEY_MAP = {
    "poll_interval_seconds": "poll_interval_seconds",
    "include_showtimes": "include_showtimes",
    "lookahead_days": "lookahead_days",
    "headless": "headless",
    "screen_types": "default_screen_types",
}


def _now() -> datetime:
    """타임스탬프는 앱 시계로 찍는다.

    DB가 컨테이너 안에 있으면 호스트와 시계가 어긋난다(실측 2초). 폴링 일정은
    앱 시계로 계산하는데 기록만 DB 시계로 남기면, 화면의 "N초 전"과 "다음 확인"
    카운트다운이 서로 다른 시계를 가리켜 어긋나 보인다.
    """
    return datetime.now().astimezone()


# ── 접속 ────────────────────────────────────────────────────────────────────
_pool: ConnectionPool | None = None


def dsn() -> str:
    load_env()
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DSN


def safe_dsn() -> str:
    """로그·API에 내보낼 접속 문자열 — 비밀번호를 가린다."""
    info = conninfo_to_dict(dsn())
    if info.get("password"):
        info["password"] = "***"
    return make_conninfo("", **info)


def ensure_database() -> None:
    """대상 DB가 없으면 만든다. 있으면 아무것도 하지 않는다.

    대상 DB에 그냥 붙어 보고 실패를 판별하는 방법은 쓰지 않는다 — 여러 주소로
    접속을 시도한 경우 psycopg가 오류를 합쳐 주면서 sqlstate가 사라져,
    "DB가 없다"와 "서버가 죽었다"를 구별할 수 없다. pg_database를 직접 본다.
    """
    dbname = conninfo_to_dict(dsn()).get("dbname") or "cgv"
    admin = make_conninfo(dsn(), dbname="postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (dbname,)
        ).fetchone()
        if exists:
            return
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    log.info("데이터베이스 '%s'를 새로 만들었습니다", dbname)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn(),
            min_size=1,
            max_size=8,
            timeout=10,
            open=True,
            kwargs={"row_factory": dict_row},
        )
        # 풀을 닫지 않고 인터프리터가 끝나면 psycopg_pool이 워커 스레드를
        # 못 멈췄다고 경고를 쏟는다. 진입점이 여러 개(CLI·서버)라 여기서 건다.
        atexit.register(close)
    return _pool


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_db() -> None:
    """DB·테이블을 만들고 설정을 시드한다. 여러 번 불러도 안전하다."""
    ensure_database()
    with pool().connection() as conn:
        # 파라미터가 없으면 psycopg3도 세미콜론으로 이어진 여러 문장을 받는다.
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    _seed_settings()


def health() -> dict:
    """접속·스키마 상태 요약. API의 헬스체크와 CLI 점검에 함께 쓴다."""
    with pool().connection() as conn:
        tables = conn.execute(
            "select count(*) as n from information_schema.tables"
            " where table_schema = 'public'"
        ).fetchone()["n"]
        targets = conn.execute("select count(*) as n from watch_targets").fetchone()["n"]
        version = conn.execute("show server_version").fetchone()["server_version"]
    return {"ok": True, "dsn": safe_dsn(), "server_version": version,
            "tables": tables, "targets": targets}


# ── 설정 ────────────────────────────────────────────────────────────────────
def read_config_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def _seed_settings() -> None:
    """비어 있는 설정 키만 채운다.

    ON CONFLICT DO NOTHING이라 웹에서 바꾼 값이 서버 재시작에 되돌려지지 않는다.
    """
    cfg = read_config_file()
    seed = dict(DEFAULT_SETTINGS)
    for cfg_key, setting_key in CONFIG_KEY_MAP.items():
        if cfg_key in cfg:
            seed[setting_key] = coerce_setting(setting_key, cfg[cfg_key])

    with pool().connection() as conn:
        with conn.cursor() as cur:
            for key, value in seed.items():
                cur.execute(
                    "insert into settings (key, value) values (%s, %s)"
                    " on conflict (key) do nothing",
                    (key, Json(value)),
                )


def coerce_setting(key: str, value: Any) -> Any:
    """웹·TOML에서 들어온 값을 설정의 타입으로 맞춘다."""
    if key == "poll_interval_seconds":
        return max(MIN_INTERVAL_SECONDS, int(value))
    if key == "session_recycle_minutes":
        return max(1, int(value))
    if key == "lookahead_days":
        return max(0, int(value))
    if key in ("include_showtimes", "headless"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key == "default_screen_types":
        return normalize_screen_types(value)
    return value


def normalize_screen_types(value: Any) -> list[str]:
    """screen_types = "IMAX" 처럼 문자열 하나로 쓴 경우도 받아준다."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(s).strip() for s in value if str(s).strip()]


def settings_all() -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    with pool().connection() as conn:
        for row in conn.execute("select key, value from settings").fetchall():
            out[row["key"]] = row["value"]
    return out


def get_setting(key: str, default: Any = None) -> Any:
    with pool().connection() as conn:
        row = conn.execute(
            "select value from settings where key = %s", (key,)
        ).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return row["value"]


def set_setting(key: str, value: Any) -> Any:
    value = coerce_setting(key, value)
    with pool().connection() as conn:
        conn.execute(
            "insert into settings (key, value, updated_at) values (%s, %s, %s)"
            " on conflict (key) do update"
            "   set value = excluded.value, updated_at = excluded.updated_at",
            (key, Json(value), _now()),
        )
    return value


def set_settings(mapping: dict[str, Any]) -> dict[str, Any]:
    """서버 공용 설정만 갱신하고 실제로 저장된 값을 돌려준다."""
    saved = {}
    for key, value in mapping.items():
        if key not in SERVER_SETTING_KEYS:
            continue
        saved[key] = set_setting(key, value)
    return saved


def server_settings() -> dict[str, Any]:
    """화면에 내보내도 되는 서버 설정만. 비밀키·운영 상태값은 뺀다."""
    everything = settings_all()
    return {key: everything[key] for key in SERVER_SETTING_KEYS}


def session_secret() -> str:
    """Flask 세션 서명 키.

    .env의 FLASK_SECRET_KEY가 있으면 그걸 쓰고, 없으면 만들어 DB에 넣어 둔다.
    서버를 다시 띄워도 로그인이 풀리지 않으면서 설정 파일을 손댈 일이 없다.
    """
    load_env()
    from_env = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if from_env:
        return from_env

    stored = get_setting("flask_secret_key")
    if stored:
        return str(stored)

    secret = secrets.token_urlsafe(48)
    with pool().connection() as conn:
        # 서버가 둘 이상 떠 있어도 먼저 넣은 쪽 값을 함께 쓰도록 DO NOTHING.
        conn.execute(
            "insert into settings (key, value, updated_at)"
            " values ('flask_secret_key', %s, %s) on conflict (key) do nothing",
            (Json(secret), _now()),
        )
    log.info("세션 서명 키를 새로 만들어 DB에 저장했습니다")
    return str(get_setting("flask_secret_key") or secret)


# 운영 상태값 — 예전 state.json의 `_config_error_signature`·`_global_fail_count`.
def config_error_signature() -> str | None:
    return get_setting("config_error_signature")


def set_config_error_signature(signature: str | None) -> None:
    with pool().connection() as conn:
        if signature is None:
            conn.execute("delete from settings where key = 'config_error_signature'")
        else:
            conn.execute(
                "insert into settings (key, value, updated_at)"
                " values ('config_error_signature', %s, %s)"
                " on conflict (key) do update"
                "   set value = excluded.value, updated_at = excluded.updated_at",
                (Json(signature), _now()),
            )


def bump_global_fail() -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "insert into settings (key, value, updated_at)"
            " values ('global_fail_count', '1'::jsonb, %s)"
            " on conflict (key) do update"
            "   set value = to_jsonb((settings.value)::int + 1),"
            "       updated_at = excluded.updated_at"
            " returning (value)::int as n",
            (_now(),),
        ).fetchone()
    return int(row["n"])


def clear_global_fail() -> None:
    with pool().connection() as conn:
        conn.execute("delete from settings where key = 'global_fail_count'")


# ── 사용자 ──────────────────────────────────────────────────────────────────
USER_COLUMNS = """
    id, provider, provider_user_id, nickname, email, profile_image, status,
    is_owner, webhook_url, webhook_kind, include_showtimes, lookahead_days,
    default_screen_types, created_at, last_login_at
"""

# 지원하는 웹훅 종류. 문구는 하나로 만들고 전송 직전에 서비스 문법으로 바꾼다
# (watch.send_webhook).
WEBHOOK_KINDS = ("slack", "discord")


def normalize_webhook_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in WEBHOOK_KINDS:
        raise ValueError(f"알 수 없는 웹훅 종류: {value!r} "
                         f"({' | '.join(WEBHOOK_KINDS)})")
    return kind


# 웹훅으로 인정하는 호스트. 서버가 사용자가 적어 준 주소로 직접 요청을 나가므로
# (watch.send_webhook), 아무 주소나 받으면 사설망·클라우드 메타데이터 주소를
# 대신 찔러 보는 통로가 된다 — 응답 코드와 본문 앞부분이 로그에 남고 화면의
# '테스트 전송' 성공/실패로도 새어 나간다. 보낼 곳은 어차피 둘뿐이라 막아 둔다.
WEBHOOK_HOSTS = {
    "slack": ("hooks.slack.com",),
    "discord": ("discord.com", "discordapp.com", "ptb.discord.com",
                "canary.discord.com"),
}


def normalize_webhook_url(value: Any) -> str | None:
    """사용자가 넣은 웹훅 주소를 검사한다. 빈 값이면 None(전역 웹훅으로 되돌림).

    https + 알려진 호스트만 통과시킨다. 어긋나면 ValueError — 호출자가 400으로
    돌려준다.
    """
    if value is None:
        return None
    url = str(value).strip()
    if not url:
        return None

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("웹훅 주소는 https:// 로 시작해야 합니다")
    host = (parsed.hostname or "").lower()
    allowed = [h for hosts in WEBHOOK_HOSTS.values() for h in hosts]
    # 정확히 일치하거나 그 도메인의 하위 호스트만 (evil-discord.com은 걸러진다).
    if not any(host == h or host.endswith("." + h) for h in allowed):
        raise ValueError(
            f"웹훅 주소로 쓸 수 없는 호스트입니다: {host or '(없음)'} "
            f"— Slack({WEBHOOK_HOSTS['slack'][0]}) 또는 "
            f"Discord({WEBHOOK_HOSTS['discord'][0]}) 주소만 됩니다")
    return url


def detect_webhook_kind(url: str | None) -> str | None:
    """웹훅 주소에서 종류를 알아낸다. 확실하지 않으면 None.

    사용자가 주소는 Discord 것으로 바꾸고 종류를 그대로 두는 실수가 흔하다.
    주소에 서비스가 적혀 있으므로 저장할 때 이 값으로 바로잡는다.
    """
    host = urlparse((url or "").strip()).hostname or ""
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return "discord"
    if host.endswith("slack.com"):
        return "slack"
    return None


def users() -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            f"select {USER_COLUMNS} from users order by is_owner desc, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def user(user_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            f"select {USER_COLUMNS} from users where id = %s", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def user_by_provider(provider: str, provider_user_id: str) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            f"select {USER_COLUMNS} from users"
            " where provider = %s and provider_user_id = %s",
            (provider, str(provider_user_id)),
        ).fetchone()
    return dict(row) if row else None


def owner() -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            f"select {USER_COLUMNS} from users where is_owner limit 1"
        ).fetchone()
    return dict(row) if row else None


def login_user(provider: str, provider_user_id: str, *, nickname: str | None = None,
               email: str | None = None, profile_image: str | None = None) -> dict:
    """로그인한 계정을 만들거나 갱신한다.

    users가 비어 있으면 첫 계정이 소유자가 되고 곧바로 승인된다. 그다음부터는
    'pending'으로 들어와 소유자의 승인을 기다린다 — 외부에 열어 두더라도
    아무나 감시 대상을 건드리지 못하게 하는 장치다.
    """
    stamp = _now()
    with pool().connection() as conn:
        first = conn.execute("select count(*) as n from users").fetchone()["n"] == 0
        row = conn.execute(
            f"insert into users (provider, provider_user_id, nickname, email,"
            f"   profile_image, status, is_owner, created_at, last_login_at)"
            f" values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" on conflict (provider, provider_user_id) do update set"
            f"   nickname = excluded.nickname, email = excluded.email,"
            f"   profile_image = excluded.profile_image,"
            f"   last_login_at = excluded.last_login_at"
            f" returning {USER_COLUMNS}",
            (provider, str(provider_user_id), nickname, email, profile_image,
             "approved" if first else "pending", first, stamp, stamp),
        ).fetchone()

    account = dict(row)
    if first:
        seed_user_settings(account["id"])
        adopted = adopt_orphan_data(account["id"])
        log.info("첫 로그인 계정을 소유자로 지정했습니다: %s (%s) — 기존 데이터 %s",
                 account["nickname"], provider, adopted)
        account = user(account["id"]) or account
    return account


def seed_user_settings(user_id: int) -> None:
    """새 사용자의 취향 설정을 settings의 시드값(config.toml 유래)으로 채운다."""
    defaults = settings_all()
    with pool().connection() as conn:
        conn.execute(
            "update users set include_showtimes = %s, lookahead_days = %s,"
            "   default_screen_types = %s where id = %s",
            (bool(defaults["include_showtimes"]), int(defaults["lookahead_days"]),
             normalize_screen_types(defaults["default_screen_types"]), user_id),
        )


def adopt_orphan_data(user_id: int) -> dict:
    """소유자 없이 남아 있던 감시 대상·알림을 넘겨준다.

    로그인을 붙이기 전에 만들어진 데이터가 주인 없이 떠 있게 되는데, 그대로 두면
    화면에서 영영 보이지 않는다. 첫 로그인 때 소유자에게 붙여 준다.
    """
    with pool().connection() as conn:
        targets_moved = conn.execute(
            "update watch_targets set owner_id = %s where owner_id is null",
            (user_id,),
        ).rowcount
        alerts_moved = conn.execute(
            "update alerts set owner_id = %s where owner_id is null", (user_id,)
        ).rowcount
    return {"targets": targets_moved, "alerts": alerts_moved}


def set_user_status(user_id: int, status: str) -> dict | None:
    if status not in ("pending", "approved", "blocked"):
        raise ValueError(f"알 수 없는 상태: {status}")
    with pool().connection() as conn:
        conn.execute("update users set status = %s where id = %s",
                     (status, user_id))
    return user(user_id)


def update_user(user_id: int, **fields: Any) -> dict | None:
    """사용자 취향 설정과 알림 웹훅을 갱신한다. 지정한 항목만 바뀐다.

    webhook_url을 새로 주면 주소에서 알아낸 종류로 webhook_kind를 덮어쓴다 —
    Slack 주소를 Discord로 표시해 두면 문법이 어긋난 문구가 나가기 때문이다.
    쓸 수 없는 주소(https가 아니거나 Slack·Discord가 아닌 호스트)면 ValueError.
    """
    fields = dict(fields)
    if fields.get("webhook_url"):
        # 먼저 검사한다 — 못 쓸 주소면 종류를 손대기 전에 ValueError로 끝난다.
        normalize_webhook_url(fields["webhook_url"])
        detected = detect_webhook_kind(fields["webhook_url"])
        if detected:
            fields["webhook_kind"] = detected

    allowed = {
        "webhook_url": normalize_webhook_url,
        "webhook_kind": normalize_webhook_kind,
        "include_showtimes": lambda v: coerce_setting("include_showtimes", v),
        "lookahead_days": lambda v: coerce_setting("lookahead_days", v),
        "default_screen_types": normalize_screen_types,
    }
    sets, params = [], []
    for key, coerce in allowed.items():
        if key in fields:
            sets.append(f"{key} = %s")
            params.append(coerce(fields[key]))
    if sets:
        params.append(user_id)
        with pool().connection() as conn:
            conn.execute(f"update users set {', '.join(sets)} where id = %s", params)
    return user(user_id)


def delete_user(user_id: int) -> bool:
    """사용자와 그 사람의 감시 대상을 함께 지운다 (watch_targets는 CASCADE).

    접근만 막고 데이터는 남기고 싶으면 set_user_status(..., 'blocked')를 쓴다.
    """
    with pool().connection() as conn:
        cur = conn.execute("delete from users where id = %s and not is_owner",
                           (user_id,))
        return cur.rowcount > 0


# ── CGV 로그인 자격증명 (Phase 1) ────────────────────────────────────────────
# 사용자별로 감시 대상 CGV 계정의 아이디·비밀번호를 보관한다. 비밀번호는 원문을
# 되찾을 수 있어야 로그인할 수 있으므로(cgv_login 참고) 되돌릴 수 있는 암호문으로
# 저장한다 — secretbox가 그 한 곳을 담당한다.
#
# 화면·API로 나가는 값에는 비밀번호를 절대 싣지 않는다. cgv_account_view가 상태와
# 아이디만 추려 내보내고, 원문 비밀번호는 cgv_password()로 로그인 직전에만 푼다.
CGV_ACCOUNT_COLUMNS = """
    owner_id, cgv_user_id, status, last_login_at, last_error, created_at, updated_at
"""


def set_cgv_account(owner_id: int, cgv_user_id: str, password: str) -> dict:
    """CGV 자격증명을 저장하거나 갱신한다. 비밀번호는 암호화해 넣는다.

    아이디나 비밀번호가 바뀌면 이전 로그인 성공 여부는 의미가 없어지므로
    status를 unlinked로 되돌리고 last_error를 지운다 — 다음 로그인 시도가
    새로 판정한다.
    """
    import secretbox

    cgv_user_id = (cgv_user_id or "").strip()
    if not cgv_user_id:
        raise ValueError("CGV 아이디가 비어 있습니다")
    if not password:
        raise ValueError("CGV 비밀번호가 비어 있습니다")

    enc = secretbox.encrypt(password)
    stamp = _now()
    with pool().connection() as conn:
        conn.execute(
            "insert into cgv_accounts"
            "   (owner_id, cgv_user_id, password_enc, status, last_error,"
            "    created_at, updated_at)"
            " values (%s, %s, %s, 'unlinked', null, %s, %s)"
            " on conflict (owner_id) do update set"
            "   cgv_user_id = excluded.cgv_user_id,"
            "   password_enc = excluded.password_enc,"
            "   status = 'unlinked', last_error = null,"
            "   updated_at = excluded.updated_at",
            (owner_id, cgv_user_id, enc, stamp, stamp),
        )
    return cgv_account(owner_id)


def cgv_account(owner_id: int) -> dict | None:
    """저장된 CGV 자격증명의 상태. 비밀번호(암호문)는 담지 않는다."""
    with pool().connection() as conn:
        row = conn.execute(
            f"select {CGV_ACCOUNT_COLUMNS} from cgv_accounts where owner_id = %s",
            (owner_id,),
        ).fetchone()
    return dict(row) if row else None


def cgv_password(owner_id: int) -> str | None:
    """저장된 원문 비밀번호를 복호화해 돌려준다. 로그인 직전에만 부른다.

    이 값은 메모리 밖으로 내보내지 않는다 — 로그를 남기거나 API로 실어 보내면
    안 된다. 저장된 계정이 없으면 None.
    """
    import secretbox

    with pool().connection() as conn:
        row = conn.execute(
            "select password_enc from cgv_accounts where owner_id = %s",
            (owner_id,),
        ).fetchone()
    if row is None:
        return None
    return secretbox.decrypt(row["password_enc"])


def set_cgv_account_status(owner_id: int, status: str,
                           *, error: str | None = None) -> dict | None:
    """로그인 시도 결과를 기록한다. linked면 성공 시각도 함께 남긴다.

    linked  — 로그인 성공. last_login_at을 지금으로, last_error를 지운다.
    error   — 로그인 실패. last_error에 사유를 남긴다.
    unlinked — 아직 시도 전(비밀번호 갱신 직후 등).
    """
    if status not in ("unlinked", "linked", "error"):
        raise ValueError(f"알 수 없는 상태: {status}")
    stamp = _now()
    with pool().connection() as conn:
        if status == "linked":
            conn.execute(
                "update cgv_accounts set status = 'linked', last_login_at = %s,"
                "   last_error = null, updated_at = %s where owner_id = %s",
                (stamp, stamp, owner_id),
            )
        else:
            conn.execute(
                "update cgv_accounts set status = %s, last_error = %s,"
                "   updated_at = %s where owner_id = %s",
                (status, error, stamp, owner_id),
            )
    return cgv_account(owner_id)


def delete_cgv_account(owner_id: int) -> bool:
    """저장된 CGV 자격증명을 지운다."""
    with pool().connection() as conn:
        cur = conn.execute(
            "delete from cgv_accounts where owner_id = %s", (owner_id,)
        )
        return cur.rowcount > 0


def set_cgv_tokens(owner_id: int, tokens: dict[str, str]) -> None:
    """로그인으로 받은 세션 쿠키를 암호화해 저장하고 status를 linked로 올린다.

    tokens는 accessToken·refresh_token 등을 담은 dict다. 로그인 직후·refresh
    직후에 부른다. 비어 있으면 세션을 지운다(clear_cgv_tokens와 같은 효과).
    """
    import json as _json

    import secretbox

    if not tokens:
        clear_cgv_tokens(owner_id)
        return
    enc = secretbox.encrypt(_json.dumps(tokens))
    stamp = _now()
    with pool().connection() as conn:
        conn.execute(
            "update cgv_accounts set session_enc = %s, status = 'linked',"
            "   last_login_at = %s, last_error = null, updated_at = %s"
            " where owner_id = %s",
            (enc, stamp, stamp, owner_id),
        )


def cgv_tokens(owner_id: int) -> dict[str, str] | None:
    """저장된 세션 쿠키를 복호화해 돌려준다. 없으면 None."""
    import json as _json

    import secretbox

    with pool().connection() as conn:
        row = conn.execute(
            "select session_enc from cgv_accounts where owner_id = %s", (owner_id,)
        ).fetchone()
    if row is None or row["session_enc"] is None:
        return None
    return _json.loads(secretbox.decrypt(row["session_enc"]))


def clear_cgv_tokens(owner_id: int) -> None:
    """저장된 세션 쿠키를 지운다 — refresh까지 만료돼 재로그인이 필요할 때."""
    with pool().connection() as conn:
        conn.execute(
            "update cgv_accounts set session_enc = null, updated_at = %s"
            " where owner_id = %s",
            (_now(), owner_id),
        )


# ── 좌석 감시 (Phase 1) ──────────────────────────────────────────────────────
SEAT_WATCH_COLUMNS = """
    id, owner_id, movie_query, site_query, scn_ymd, scn_time,
    scn_time_from, scn_time_to, screen_types, rows,
    min_consecutive, auto_book, party_size, ticket_spec, enabled, created_at
"""

# 자정을 넘긴 회차를 24시 이상으로 적는 CGV 표기의 상한. '2530' = 25:30 = 새벽 1:30.
# 하루치 상영표라 28시(= 새벽 4시)를 넘는 회차는 없다.
MAX_SHOWTIME_HOUR = 28


def normalize_scn_time(value) -> str:
    """상영 시간을 'HH:MM'으로 정규화. 비어 있으면 '' (= 모든 회차).

    '22:10'·'2210' 모두 받는다. CGV는 자정 넘김을 '24:30'·'2530'처럼 24~28시로도
    주므로 시(hour) 상한을 두지 않는다.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 3:
        return ""
    digits = digits[:4].rjust(4, "0") if len(digits) == 3 else digits[:4]
    return f"{digits[:2]}:{digits[2:4]}"


def hhmm_minutes(value) -> int | None:
    """'22:10'·'2210' → 자정부터의 분(1330). 읽을 수 없으면 None.

    CGV는 심야 회차를 '2530'처럼 24시 이상으로 준다. 그 표기를 그대로 살려
    분으로 펴 두면 '25:30 > 23:00' 비교가 자연스럽게 맞는다.
    """
    text = normalize_scn_time(value)
    if not text:
        return None
    hh, mm = int(text[:2]), int(text[3:5])
    if mm > 59 or hh > MAX_SHOWTIME_HOUR:
        return None
    return hh * 60 + mm


def normalize_time_range(start, end) -> tuple[str, str]:
    """상영 시간 범위를 ('HH:MM','HH:MM')으로. 한쪽이라도 비면 ('','').

    범위는 **양 끝을 포함**한다. 끝이 시작보다 이르면 자정을 넘긴 것으로 보고
    그대로 둔다 — 22:00~02:00은 seats 쪽에서 22:00~26:00으로 편다. 여기서 미리
    26:00으로 바꾸지 않는 건 사용자가 화면에 적은 값 그대로 되돌려 보여주기
    위해서다(<input type=time>은 26:00을 표시하지 못한다).
    """
    a, b = normalize_scn_time(start), normalize_scn_time(end)
    if not a or not b:
        return "", ""
    if hhmm_minutes(a) is None or hhmm_minutes(b) is None:
        raise ValueError("상영 시간 범위를 이해할 수 없습니다 (예: 18:00 ~ 23:30)")
    return a, b


def seat_watches(*, enabled_only: bool = False,
                 owner_id: int | None = None) -> list[dict]:
    """좌석 감시 목록. owner_id를 주면 그 사람 것만."""
    where, params = [], []
    if enabled_only:
        where.append("enabled")
    if owner_id is not None:
        where.append("owner_id = %s")
        params.append(owner_id)
    clause = (" where " + " and ".join(where)) if where else ""
    with pool().connection() as conn:
        rows = conn.execute(
            f"select {SEAT_WATCH_COLUMNS} from seat_watches{clause}"
            " order by scn_ymd, created_at",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def normalize_ticket_spec(spec) -> dict:
    """권종별 수량 dict로 정규화. {'adult':2,'youth':1} 형태, 음수·0은 제거."""
    if not spec:
        return {}
    if isinstance(spec, str):
        import json as _json
        try:
            spec = _json.loads(spec)
        except Exception:  # noqa: BLE001
            raise ValueError("권종 설정을 이해할 수 없습니다")
    if not isinstance(spec, dict):
        raise ValueError("권종 설정은 객체여야 합니다")
    out = {}
    for k, v in spec.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"권종 수량이 숫자가 아닙니다: {k}={v!r}")
        if n > 0:
            out[str(k)] = n
    return out


def add_seat_watch(owner_id: int | None, movie_query: str, site_query: str,
                   scn_ymd: str, screen_types=None, rows=None,
                   min_consecutive: int = 0, auto_book: bool = False,
                   party_size: int = 1, ticket_spec=None, scn_time="",
                   scn_time_from="", scn_time_to="") -> dict:
    """좌석 감시를 추가한다. 같은 조합이 있으면 옵션을 갱신해 돌려준다.

    회차 지정은 셋 중 하나다:
      · scn_time                    — 그 회차 하나만 (상영표가 이미 열렸을 때)
      · scn_time_from ~ scn_time_to — 그 시간대의 회차 (미상영 영화를 미리 걸 때)
      · 둘 다 비움                  — 그 날짜의 모든 회차
    scn_time이 있으면 그게 이긴다 — 회차를 콕 집어 놓고 범위를 함께 두는 건
    말이 안 되므로, 화면에서도 셋 중 하나만 고르게 되어 있다.
    """
    movie_query = (movie_query or "").strip()
    site_query = (site_query or "").strip()
    scn_ymd = (scn_ymd or "").strip()
    if not (movie_query and site_query and scn_ymd):
        raise ValueError("영화·극장·날짜는 비울 수 없습니다")
    types = normalize_screen_types(screen_types)
    row_filter = normalize_rows(rows)
    stime = normalize_scn_time(scn_time)
    tfrom, tto = normalize_time_range(scn_time_from, scn_time_to)
    if stime:
        tfrom = tto = ""      # 회차를 콕 집었으면 범위는 의미가 없다
    try:
        need = max(0, int(min_consecutive or 0))
    except (TypeError, ValueError):
        raise ValueError("연속 좌석 수는 숫자여야 합니다")
    try:
        party = max(1, int(party_size or 1))
    except (TypeError, ValueError):
        raise ValueError("인원수는 숫자여야 합니다")
    spec = normalize_ticket_spec(ticket_spec)
    with pool().connection() as conn:
        row = conn.execute(
            f"insert into seat_watches"
            f"   (owner_id, movie_query, site_query, scn_ymd, scn_time,"
            f"    scn_time_from, scn_time_to, screen_types, rows,"
            f"    min_consecutive, auto_book, party_size, ticket_spec)"
            f" values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            f" on conflict (owner_id, movie_query, site_query, scn_ymd, scn_time,"
            f"              scn_time_from, scn_time_to,"
            f"              screen_types, rows) do update set enabled = true,"
            f"              min_consecutive = excluded.min_consecutive,"
            f"              auto_book = excluded.auto_book,"
            f"              party_size = excluded.party_size,"
            f"              ticket_spec = excluded.ticket_spec"
            f" returning {SEAT_WATCH_COLUMNS}",
            (owner_id, movie_query, site_query, scn_ymd, stime, tfrom, tto,
             types, row_filter, need, bool(auto_book), party, Json(spec)),
        ).fetchone()
    return dict(row)


def set_seat_watch(seat_watch_id: int, owner_id: int | None = None,
                   **fields) -> dict | None:
    """좌석 감시의 옵션을 부분 갱신한다(enabled·auto_book·party_size·ticket_spec 등).

    owner_id를 주면 그 사람 소유일 때만 갱신한다.
    """
    allowed = {
        "enabled": lambda v: bool(v),
        "auto_book": lambda v: bool(v),
        "min_consecutive": lambda v: max(0, int(v or 0)),
        "party_size": lambda v: max(1, int(v or 1)),
        "ticket_spec": lambda v: Json(normalize_ticket_spec(v)),
    }
    sets, params = [], []
    for key, coerce in allowed.items():
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = %s")
            params.append(coerce(fields[key]))
    if not sets:
        return seat_watch(seat_watch_id)
    where = "id = %s"
    params.append(seat_watch_id)
    if owner_id is not None:
        where += " and owner_id = %s"
        params.append(owner_id)
    with pool().connection() as conn:
        conn.execute(f"update seat_watches set {', '.join(sets)} where {where}",
                     params)
    return seat_watch(seat_watch_id)


def seat_watch(seat_watch_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            f"select {SEAT_WATCH_COLUMNS} from seat_watches where id = %s",
            (seat_watch_id,),
        ).fetchone()
    return dict(row) if row else None


def delete_seat_watch(seat_watch_id: int, owner_id: int | None = None) -> bool:
    """좌석 감시를 지운다. owner_id를 주면 그 사람 것만 지운다."""
    sql_txt = "delete from seat_watches where id = %s"
    params: list = [seat_watch_id]
    if owner_id is not None:
        sql_txt += " and owner_id = %s"
        params.append(owner_id)
    with pool().connection() as conn:
        return conn.execute(sql_txt, params).rowcount > 0


def prev_seat_state(seat_watch_id: int) -> dict:
    """직전 관측의 회차별 빈좌석 집합. 없으면 빈 dict(= 첫 관측)."""
    with pool().connection() as conn:
        row = conn.execute(
            "select available from seat_watch_state where seat_watch_id = %s",
            (seat_watch_id,),
        ).fetchone()
    return (row["available"] if row else {}) or {}


def save_seat_state(seat_watch_id: int, available: dict,
                    *, error: str | None = None) -> None:
    """회차별 빈좌석 집합을 저장한다. error를 주면 last_error에 남긴다."""
    stamp = _now()
    with pool().connection() as conn:
        conn.execute(
            "insert into seat_watch_state"
            "   (seat_watch_id, available, last_ok, last_error, updated_at)"
            " values (%s, %s, %s, %s, %s)"
            " on conflict (seat_watch_id) do update set"
            "   available = excluded.available,"
            "   last_ok = coalesce(excluded.last_ok, seat_watch_state.last_ok),"
            "   last_error = excluded.last_error, updated_at = excluded.updated_at",
            (seat_watch_id, Json(available), None if error else stamp,
             error, stamp),
        )


def normalize_rows(rows) -> list[str]:
    """좌석 열 필터를 대문자·중복제거된 리스트로. seats.normalize_rows와 같은 규칙."""
    if not rows:
        return []
    if isinstance(rows, str):
        rows = rows.replace(",", " ").split()
    seen, out = set(), []
    for r in rows:
        key = str(r).strip().upper()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── 자동 예매(선점) 시도 이력 (Phase 1 auto-book) ────────────────────────────
BOOKING_COLUMNS = """
    id, seat_watch_id, owner_id, showtime_key, mov_nm, site_nm, scn_ymd,
    start_hhmm, seat_labels, seat_loc_nos, mov_atkt_no, amount, status,
    hold_expires_at, last_error, created_at, updated_at
"""


def create_booking_attempt(*, seat_watch_id: int | None, owner_id: int | None,
                           showtime_key: str, mov_nm: str, site_nm: str,
                           scn_ymd: str, start_hhmm: str, seat_labels: list[str],
                           seat_loc_nos: list[str]) -> int:
    """선점 시도를 pending으로 남기고 id를 돌려준다. 결과는 finish로 확정한다."""
    with pool().connection() as conn:
        row = conn.execute(
            "insert into booking_attempts"
            "  (seat_watch_id, owner_id, showtime_key, mov_nm, site_nm, scn_ymd,"
            "   start_hhmm, seat_labels, seat_loc_nos, status)"
            " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') returning id",
            (seat_watch_id, owner_id, showtime_key, mov_nm, site_nm, scn_ymd,
             start_hhmm, list(seat_labels or []), list(seat_loc_nos or [])),
        ).fetchone()
    return row["id"]


def finish_booking_attempt(attempt_id: int, status: str, *,
                           mov_atkt_no: str | None = None,
                           amount: int | None = None,
                           hold_expires_at: datetime | None = None,
                           seat_labels: list[str] | None = None,
                           error: str | None = None) -> None:
    """선점 시도 결과를 확정한다. status: held|failed|expired|cancelled.

    seat_labels를 주면 좌석도 덮어쓴다 — 시도를 열 때 적은 건 감지 시점의
    **후보**이고, 실제로 누른 좌석은 좌석맵에 도착해서 다시 고르기 때문이다
    (booking._select_block). 이력이 후보를 그대로 들고 있으면 사용자가 받은
    알림과 어긋난다.
    """
    if status not in ("held", "failed", "expired", "cancelled", "pending"):
        raise ValueError(f"알 수 없는 상태: {status}")
    sets = ["status=%s", "mov_atkt_no=%s", "amount=%s", "hold_expires_at=%s",
            "last_error=%s", "updated_at=%s"]
    params: list[Any] = [status, mov_atkt_no, amount, hold_expires_at, error,
                         _now()]
    if seat_labels is not None:
        sets.append("seat_labels=%s")
        params.append(list(seat_labels))
    params.append(attempt_id)
    with pool().connection() as conn:
        conn.execute(
            f"update booking_attempts set {', '.join(sets)} where id=%s", params)


def booking_attempts(*, owner_id: int | None = None, limit: int = 20) -> list[dict]:
    """선점 시도 이력. owner_id를 주면 그 사람 것만."""
    where, params = "", []
    if owner_id is not None:
        where = "where owner_id = %s"
        params.append(owner_id)
    params.append(limit)
    with pool().connection() as conn:
        rows = conn.execute(
            f"select {BOOKING_COLUMNS} from booking_attempts {where}"
            f" order by created_at desc limit %s", params).fetchall()
    return [dict(r) for r in rows]


def active_hold(seat_watch_id: int) -> dict | None:
    """아직 유효한(held, 만료 전) 선점이 있으면 돌려준다 — 중복 선점을 막는 데 쓴다."""
    with pool().connection() as conn:
        row = conn.execute(
            f"select {BOOKING_COLUMNS} from booking_attempts"
            " where seat_watch_id = %s and status = 'held'"
            "   and (hold_expires_at is null or hold_expires_at > now())"
            " order by created_at desc limit 1", (seat_watch_id,)).fetchone()
    return dict(row) if row else None


# ── 감시 대상 ───────────────────────────────────────────────────────────────
TARGET_COLUMNS = """
    t.id, t.owner_id, t.movie_query, t.site_query, t.screen_types, t.enabled,
    t.created_at,
    s.status, s.mov_no, s.site_no, s.mov_nm, s.site_nm,
    s.dates, s.matched_dates, s.screen_types as state_screen_types,
    s.fail_count, s.last_ok, s.last_error, s.updated_at,
    u.nickname as owner_nickname, u.status as owner_status,
    u.include_showtimes as owner_include_showtimes,
    u.lookahead_days as owner_lookahead_days,
    u.webhook_url as owner_webhook_url,
    u.webhook_kind as owner_webhook_kind
"""

TARGET_FROM = (" from watch_targets t"
               " left join watch_state s on s.target_id = t.id"
               " left join users u on u.id = t.owner_id")


def targets(*, owner_id: int | None = None,
            enabled_only: bool = False) -> list[dict]:
    """감시 대상 + 마지막 관측 상태 + 소유자 설정.

    owner_id를 주면 그 사용자 것만 돌려준다. 생략하면 전부 — **폴러 전용**이다.
    API 핸들러는 반드시 owner_id를 넘겨야 남의 감시가 새어 나가지 않는다.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("t.owner_id = %s")
        params.append(owner_id)
    if enabled_only:
        clauses.append("t.enabled")
        # 승인 대기·차단된 계정의 대상은 확인하지 않는다. 소유자가 아직 없는
        # 대상(로그인을 붙이기 전에 만든 것)은 계속 확인한다.
        clauses.append("(t.owner_id is null or u.status = 'approved')")
    where = f" where {' and '.join(clauses)}" if clauses else ""

    with pool().connection() as conn:
        rows = conn.execute(
            f"select {TARGET_COLUMNS}{TARGET_FROM}{where} order by t.id", params
        ).fetchall()
    return [dict(r) for r in rows]


def target(target_id: int, *, owner_id: int | None = None) -> dict | None:
    """대상 한 건. owner_id를 주면 그 사용자 것이 아닐 때 None을 돌려준다."""
    query = f"select {TARGET_COLUMNS}{TARGET_FROM} where t.id = %s"
    params: list[Any] = [target_id]
    if owner_id is not None:
        query += " and t.owner_id = %s"
        params.append(owner_id)
    with pool().connection() as conn:
        row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def add_target(movie_query: str, site_query: str, screen_types: Any = None,
               *, owner_id: int | None = None) -> dict | None:
    """감시 대상을 추가한다. 그 사용자에게 이미 있는 조합이면 None을 돌려준다."""
    with pool().connection() as conn:
        row = conn.execute(
            "insert into watch_targets (movie_query, site_query, screen_types,"
            "   owner_id)"
            " values (%s, %s, %s, %s)"
            " on conflict (owner_id, movie_query, site_query) do nothing"
            " returning id",
            (movie_query.strip(), site_query.strip(),
             normalize_screen_types(screen_types), owner_id),
        ).fetchone()
    return target(row["id"]) if row else None


def update_target(target_id: int, *, enabled: bool | None = None,
                  screen_types: Any = None) -> dict | None:
    sets, params = [], []
    if enabled is not None:
        sets.append("enabled = %s")
        params.append(bool(enabled))
    if screen_types is not None:
        sets.append("screen_types = %s")
        params.append(normalize_screen_types(screen_types))
    if sets:
        params.append(target_id)
        with pool().connection() as conn:
            conn.execute(
                f"update watch_targets set {', '.join(sets)} where id = %s", params
            )
    return target(target_id)


def delete_target(target_id: int) -> bool:
    with pool().connection() as conn:
        cur = conn.execute("delete from watch_targets where id = %s", (target_id,))
        return cur.rowcount > 0


# ── 관측 상태 ───────────────────────────────────────────────────────────────
def prev_state(row: dict) -> dict:
    """조인 결과를 예전 state.json 항목 형태로 되돌린다.

    watch.check_all이 이 dict를 그대로 비교에 쓴다. 관측 기록이 없으면 {} —
    "첫 관측"이라 알림 없이 기준선만 잡는다는 뜻이다.
    """
    if row.get("updated_at") is None:  # left join 미스 = 아직 확인한 적 없음
        return {}
    return {
        "status": row.get("status") or "unknown",
        "movNo": row.get("mov_no"),
        "siteNo": row.get("site_no"),
        "movNm": row.get("mov_nm"),
        "siteNm": row.get("site_nm"),
        "dates": list(row.get("dates") or []),
        "matched_dates": list(row.get("matched_dates") or []),
        "screen_types": list(row.get("state_screen_types") or []),
        "fail_count": int(row.get("fail_count") or 0),
    }


def save_state(target_id: int, *, mov_no: str, site_no: str, mov_nm: str,
               site_nm: str, dates: list[str], matched_dates: list[str],
               screen_types: list[str]) -> None:
    """정상 관측 결과를 저장한다. 실패 카운터는 0으로 되돌린다."""
    with pool().connection() as conn:
        conn.execute(
            "insert into watch_state (target_id, status, mov_no, site_no, mov_nm,"
            "   site_nm, dates, matched_dates, screen_types, fail_count, last_ok,"
            "   last_error, updated_at)"
            " values (%(id)s, 'tracking', %(mov_no)s, %(site_no)s, %(mov_nm)s,"
            "   %(site_nm)s, %(dates)s, %(matched)s, %(types)s, 0, %(now)s,"
            "   null, %(now)s)"
            " on conflict (target_id) do update set"
            "   status = 'tracking', mov_no = excluded.mov_no,"
            "   site_no = excluded.site_no, mov_nm = excluded.mov_nm,"
            "   site_nm = excluded.site_nm, dates = excluded.dates,"
            "   matched_dates = excluded.matched_dates,"
            "   screen_types = excluded.screen_types, fail_count = 0,"
            "   last_ok = excluded.last_ok, last_error = null,"
            "   updated_at = excluded.updated_at",
            {"id": target_id, "mov_no": mov_no, "site_no": site_no,
             "mov_nm": mov_nm, "site_nm": site_nm, "dates": list(dates),
             "matched": list(matched_dates), "types": list(screen_types),
             "now": _now()},
        )


def mark_not_open(target_id: int, screen_types: list[str]) -> None:
    """아직 예매가 열리지 않은 대상. 목록에 등장하는 순간이 곧 티켓 오픈이다."""
    with pool().connection() as conn:
        conn.execute(
            "insert into watch_state (target_id, status, screen_types, dates,"
            "   matched_dates, fail_count, last_ok, last_error, updated_at)"
            " values (%(id)s, 'not_open', %(types)s, '{}', '{}', 0, %(now)s,"
            "   null, %(now)s)"
            " on conflict (target_id) do update set"
            "   status = 'not_open', screen_types = excluded.screen_types,"
            "   dates = '{}', matched_dates = '{}', fail_count = 0,"
            "   last_ok = excluded.last_ok, last_error = null,"
            "   updated_at = excluded.updated_at",
            {"id": target_id, "types": list(screen_types), "now": _now()},
        )


def record_fail(target_id: int, error: str) -> int:
    """조회 실패를 기록하고 연속 실패 횟수를 돌려준다.

    날짜는 건드리지 않는다 — 다음 성공 때 그 사이 열린 날짜를 그대로 잡아야 한다.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "insert into watch_state (target_id, status, fail_count, last_error,"
            "   updated_at)"
            " values (%s, 'unknown', 1, %s, %s)"
            " on conflict (target_id) do update set"
            "   fail_count = watch_state.fail_count + 1,"
            "   last_error = excluded.last_error,"
            "   updated_at = excluded.updated_at"
            " returning fail_count",
            (target_id, error[:2000], _now()),
        ).fetchone()
    return int(row["fail_count"])


def reset_state(target_id: int) -> None:
    """기준선을 지운다 — 다음 확인은 알림 없이 현재 상태만 저장한다."""
    with pool().connection() as conn:
        conn.execute("delete from watch_state where target_id = %s", (target_id,))


# ── 시간표 캐시 ─────────────────────────────────────────────────────────────
def save_showtimes(target_id: int, ymd: str, payload: list[dict]) -> None:
    with pool().connection() as conn:
        conn.execute(
            "insert into showtimes (target_id, scn_ymd, payload, fetched_at)"
            " values (%s, %s, %s, %s)"
            " on conflict (target_id, scn_ymd) do update set"
            "   payload = excluded.payload, fetched_at = excluded.fetched_at",
            (target_id, ymd, Json(payload), _now()),
        )


def load_showtimes(target_id: int, ymd: str | None = None) -> dict[str, dict]:
    """{날짜: {payload, fetched_at}} 형태로 돌려준다."""
    query = ("select scn_ymd, payload, fetched_at from showtimes"
             " where target_id = %s")
    params: list[Any] = [target_id]
    if ymd:
        query += " and scn_ymd = %s"
        params.append(ymd)
    with pool().connection() as conn:
        rows = conn.execute(query + " order by scn_ymd", params).fetchall()
    return {r["scn_ymd"]: {"payload": r["payload"], "fetched_at": r["fetched_at"]}
            for r in rows}


def prune_showtimes(target_id: int, keep: list[str]) -> None:
    """더 이상 열려 있지 않은 날짜의 캐시를 지운다."""
    with pool().connection() as conn:
        if keep:
            conn.execute(
                "delete from showtimes where target_id = %s"
                " and not (scn_ymd = any(%s))",
                (target_id, list(keep)),
            )
        else:
            conn.execute("delete from showtimes where target_id = %s", (target_id,))


# ── 알림 이력 ───────────────────────────────────────────────────────────────
def record_alert(kind: str, body: str, *, target_id: int | None = None,
                 owner_id: int | None = None, mov_nm: str | None = None,
                 site_nm: str | None = None, dates: Any = (),
                 seat_watch_id: int | None = None) -> int:
    """알림을 이력에 남기고 id를 돌려준다.

    아직 못 보낸 **같은** 알림이 있으면 새 행을 만들지 않고 시도 횟수만 올린다.
    전송 실패 시 상태를 밀지 않는 설계라, 웹훅이 계속 죽어 있으면 30초마다
    똑같은 행이 쌓여 이력이 못 쓰게 된다.

    "같은 알림"에는 **소유자도 포함된다.** config_error·connect_error는
    target_id가 없고 dates도 비어 있어서, 소유자를 보지 않으면 사용자 A의
    미전송 알림을 B의 알림이 덮어써 A의 본문이 바뀌고 B의 이력은 생기지 않는다.
    한 행만 고르는 것도 같은 이유다 — UPDATE에 LIMIT을 못 걸어 서브쿼리로 잡는다.

    **좌석 감시도 포함된다.** 좌석 알림은 target_id가 없고 dates가 [scn_ymd]
    하나뿐이라, 같은 사람이 같은 날짜에 감시를 둘 걸면 서로를 덮어쓴다.
    """
    dates = list(dates or [])
    with pool().connection() as conn:
        pending = conn.execute(
            "update alerts set attempts = attempts + 1, body = %s, created_at = %s"
            " where id = ("
            "     select id from alerts"
            "      where not delivered and kind = %s and dates = %s"
            "        and target_id is not distinct from %s"
            "        and owner_id is not distinct from %s"
            "        and seat_watch_id is not distinct from %s"
            "      order by id limit 1)"
            " returning id",
            (body, _now(), kind, dates, target_id, owner_id, seat_watch_id),
        ).fetchone()
        if pending:
            return int(pending["id"])

        row = conn.execute(
            "insert into alerts (kind, target_id, owner_id, seat_watch_id,"
            "   mov_nm, site_nm, dates, body, created_at)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
            (kind, target_id, owner_id, seat_watch_id, mov_nm, site_nm, dates,
             body, _now()),
        ).fetchone()
    return int(row["id"])


def mark_alert_delivered(alert_id: int) -> None:
    with pool().connection() as conn:
        conn.execute(
            "update alerts set delivered = true, delivered_at = %s where id = %s",
            (_now(), alert_id),
        )


def recent_alerts(limit: int = 50, *, owner_id: int | None = None) -> list[dict]:
    """알림 이력. owner_id를 주면 그 사용자 것만 (운영 알림은 소유자가 없다)."""
    query = ("select id, created_at, kind, target_id, owner_id, seat_watch_id,"
             "   mov_nm, site_nm, dates, body, delivered, delivered_at, attempts"
             " from alerts")
    params: list[Any] = []
    if owner_id is not None:
        query += " where owner_id = %s"
        params.append(owner_id)
    params.append(max(1, min(limit, 500)))
    with pool().connection() as conn:
        rows = conn.execute(
            query + " order by created_at desc, id desc limit %s", params
        ).fetchall()
    return [dict(r) for r in rows]


# ── 사이클 이력 ─────────────────────────────────────────────────────────────
def start_cycle(trigger: str = "schedule") -> int:
    with pool().connection() as conn:
        row = conn.execute(
            "insert into poll_cycles (trigger, started_at) values (%s, %s)"
            " returning id", (trigger, _now())
        ).fetchone()
    return int(row["id"])


def finish_cycle(cycle_id: int, *, ok: bool, targets_checked: int = 0,
                 requests: int = 0, new_dates: int = 0,
                 error: str | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            "update poll_cycles set finished_at = %s, ok = %s,"
            "   targets_checked = %s, requests = %s, new_dates = %s, error = %s"
            " where id = %s",
            (_now(), ok, targets_checked, requests, new_dates,
             error[:2000] if error else None, cycle_id),
        )


def recent_cycles(limit: int = 50) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "select id, started_at, finished_at, ok, trigger, targets_checked,"
            "   requests, new_dates, error,"
            # extract(milliseconds ...)는 초 단위 필드만 보므로 1분을 넘기면
            # 값이 되돌아간다. 사이클이 길어질 수 있으니 epoch로 잰다.
            "   (extract(epoch from (finished_at - started_at)) * 1000)::int"
            "     as duration_ms"
            " from poll_cycles order by started_at desc, id desc limit %s",
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [dict(r) for r in rows]


def last_cycle() -> dict | None:
    """마지막으로 **끝난** 사이클.

    진행 중인 행을 돌려주면 화면 요약이 "0개 대상 · 소요 —"로 보여, 방금 확인이
    아무 일도 못 한 것처럼 읽힌다.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "select id, started_at, finished_at, ok, trigger, targets_checked,"
            "   requests, new_dates, error,"
            "   (extract(epoch from (finished_at - started_at)) * 1000)::int"
            "     as duration_ms"
            " from poll_cycles where finished_at is not null"
            " order by finished_at desc, id desc limit 1"
        ).fetchone()
    return dict(row) if row else None


# ── 영화·극장 목록 캐시 ─────────────────────────────────────────────────────
def replace_catalog_movies(movies: list[dict]) -> None:
    """CGV 영화 목록으로 캐시를 갈아끼운다. 목록에서 빠진 영화는 지운다."""
    stamp = _now()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("truncate catalog_movies")
        for m in movies:
            rate = m.get("atktRate")
            try:
                rate = float(rate) if rate not in (None, "") else None
            except (TypeError, ValueError):
                rate = None
            cur.execute(
                "insert into catalog_movies (mov_no, mov_nm, atkt_rate,"
                "   refreshed_at) values (%s, %s, %s, %s)"
                " on conflict (mov_no) do update set"
                "   mov_nm = excluded.mov_nm, atkt_rate = excluded.atkt_rate,"
                "   refreshed_at = excluded.refreshed_at",
                (m.get("movNo"), m.get("movNm"), rate, stamp),
            )


def replace_catalog_sites(sites: list[dict], regions: dict[str, str]) -> None:
    stamp = _now()
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("truncate catalog_sites")
        for s in sites:
            cur.execute(
                "insert into catalog_sites (site_no, site_nm, region,"
                "   refreshed_at) values (%s, %s, %s, %s)"
                " on conflict (site_no) do update set"
                "   site_nm = excluded.site_nm, region = excluded.region,"
                "   refreshed_at = excluded.refreshed_at",
                (s.get("siteNo"), s.get("siteNm"),
                 regions.get(s.get("regnGrpCd", "")), stamp),
            )


def catalog_movies() -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "select mov_no, mov_nm, atkt_rate, refreshed_at from catalog_movies"
            " order by atkt_rate desc nulls last, mov_nm"
        ).fetchall()
    return [dict(r) for r in rows]


def catalog_sites(region: str | None = None) -> list[dict]:
    query = "select site_no, site_nm, region, refreshed_at from catalog_sites"
    params: list[Any] = []
    if region:
        query += " where region = %s"
        params.append(region)
    with pool().connection() as conn:
        rows = conn.execute(query + " order by region nulls last, site_nm",
                            params).fetchall()
    return [dict(r) for r in rows]


def catalog_refreshed_at() -> datetime | None:
    """영화·극장 캐시 중 더 오래된 쪽의 시각. 둘 중 하나가 비면 None."""
    with pool().connection() as conn:
        row = conn.execute(
            "select least("
            "  (select min(refreshed_at) from catalog_movies),"
            "  (select min(refreshed_at) from catalog_sites)) as ts"
        ).fetchone()
    return row["ts"] if row else None


# ── 이관 ────────────────────────────────────────────────────────────────────
def migrate_legacy() -> dict:
    """config.toml의 [[watch]]와 state.json을 DB로 옮긴다 (1회용).

    기준선(dates·matched_dates)을 그대로 들고 가야 이관 직후 이미 열린 날짜
    전부가 "새 날짜"로 잡혀 Slack이 폭주하는 일을 막을 수 있다.
    """
    import json

    init_db()
    cfg = read_config_file()
    default_types = normalize_screen_types(cfg.get("screen_types"))

    added, states, skipped = 0, 0, []
    key_to_id: dict[str, int] = {}

    for entry in cfg.get("watch") or []:
        movie = str(entry.get("movie", "")).strip()
        sites = entry.get("sites") or []
        if not movie or not sites:
            skipped.append(f"movie/sites가 비어 있는 항목: {entry!r}")
            continue
        types = (normalize_screen_types(entry["screen_types"])
                 if "screen_types" in entry else default_types)
        for site in sites:
            site = str(site).strip()
            # 소유자가 아직 없는 대상은 유일 인덱스(owner_id, ...)에 NULL이 들어가
            # ON CONFLICT가 걸리지 않는다. 중복은 여기서 직접 막는다.
            existing = [t for t in targets()
                        if t["movie_query"] == movie and t["site_query"] == site]
            if existing:
                row = existing[0]
            else:
                row = add_target(movie, site, types)
                if row is None:
                    continue
                added += 1
            key_to_id[f"{movie}|{site}"] = row["id"]

    legacy = {}
    if LEGACY_STATE_PATH.exists():
        try:
            legacy = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            skipped.append(f"state.json을 읽지 못했습니다: {exc}")

    for key, prev in legacy.items():
        if key.startswith("_"):  # _config_error_signature 등 운영 상태값
            continue
        target_id = key_to_id.get(key)
        if target_id is None:
            skipped.append(f"config.toml에 없는 state 항목: {key}")
            continue
        if prev.get("status") == "not_open":
            mark_not_open(target_id, normalize_screen_types(prev.get("screen_types")))
            states += 1
            continue
        if not prev.get("movNo"):
            continue
        save_state(
            target_id,
            mov_no=prev["movNo"], site_no=prev["siteNo"],
            mov_nm=prev.get("movNm", ""), site_nm=prev.get("siteNm", ""),
            dates=prev.get("dates") or [],
            matched_dates=prev.get("matched_dates") or [],
            screen_types=normalize_screen_types(prev.get("screen_types")),
        )
        states += 1

    return {"targets_added": added, "states_imported": states, "skipped": skipped}


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    action = sys.argv[1] if len(sys.argv) > 1 else "init"
    if action == "init":
        init_db()
        print(health())
    elif action == "migrate":
        print(migrate_legacy())
    else:
        sys.exit(f"알 수 없는 동작: {action} (init | migrate)")
