#!/usr/bin/env python3
"""저장용 대칭 암호화. CGV 로그인 비밀번호를 되돌릴 수 있게 보관하는 데 쓴다.

이 프로젝트는 .env 로더·OAuth·웹훅을 모두 표준 라이브러리로 직접 구현하는 쪽을
택했지만, **되돌릴 수 있는 대칭 암호화만은 표준 라이브러리에 안전한 수단이 없다**
(hashlib은 단방향, stdlib에 AES가 없다). 비밀번호를 직접 XOR·자작 스킴으로
가리는 건 하면 안 되는 일이라, 이 한 곳에서만 검증된 `cryptography`(Fernet =
AES-128-CBC + HMAC)를 쓴다.

왜 해시가 아니라 암호화인가:
    CGV 로그인은 브라우저가 매번 원문 비밀번호를 SHA-256(hex)으로 해시해
    보낸다 (cgv_login 참고). 우리가 대신 로그인하려면 원문을 다시 손에 쥐어야
    하므로, 단방향 해시로는 안 되고 복호가 가능한 형태로 저장해야 한다.

키 관리 (store.session_secret과 같은 방식):
    1. .env의 CGV_CRED_KEY가 있으면 그 값을 키로 쓴다 (권장).
       키 만들기:  python3 -c "import secretbox; print(secretbox.generate_key())"
    2. 없으면 만들어 settings 표(cgv_cred_key)에 넣고 그 값을 쓴다. 서버를
       다시 띄워도 저장해 둔 비밀번호를 계속 열 수 있다.

    주의: 2번은 암호문과 키가 같은 DB에 있어 DB 자체가 새면 보호가 되지 않는다.
    실제 운영에서는 CGV_CRED_KEY를 .env(chmod 600)에 두는 1번을 쓰는 게 맞다.
    2번은 키 없이도 화면이 도는 개발 편의를 위한 뒷문이다.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from envfile import load_env

log = logging.getLogger("cgv-watch.secretbox")

ENV_KEY = "CGV_CRED_KEY"
SETTING_KEY = "cgv_cred_key"


class SecretBoxError(RuntimeError):
    """암호화·복호화가 실패했을 때. 호출자에게 보여줄 수 있는 오류."""


def generate_key() -> str:
    """새 Fernet 키(urlsafe base64) 문자열. .env의 CGV_CRED_KEY에 넣는다."""
    return Fernet.generate_key().decode("ascii")


def _key() -> bytes:
    """활성 암호화 키. .env → settings 순으로 찾고, 없으면 만들어 저장한다."""
    load_env()
    from_env = os.environ.get(ENV_KEY, "").strip()
    if from_env:
        return from_env.encode("ascii")

    # 지연 import — secretbox는 store보다 먼저 로드될 수 있다(순환 방지).
    import store

    stored = store.get_setting(SETTING_KEY)
    if stored:
        return str(stored).encode("ascii")

    key = generate_key()
    store.set_setting(SETTING_KEY, key)
    log.warning(
        "%s가 .env에 없어 암호화 키를 새로 만들어 DB에 저장했습니다. "
        "운영에서는 .env에 %s를 두는 편이 안전합니다.", ENV_KEY, ENV_KEY)
    # 경합으로 다른 프로세스가 먼저 넣었을 수 있으니 저장된 값을 다시 읽는다.
    return str(store.get_setting(SETTING_KEY) or key).encode("ascii")


def _fernet() -> Fernet:
    try:
        return Fernet(_key())
    except (ValueError, TypeError) as exc:
        raise SecretBoxError(
            f"{ENV_KEY}가 올바른 Fernet 키가 아닙니다. "
            f"secretbox.generate_key()로 다시 만드세요: {exc}") from exc


def encrypt(plaintext: str) -> bytes:
    """원문을 암호문(bytes)으로. DB의 bytea 컬럼에 그대로 넣는다."""
    if plaintext is None:
        raise SecretBoxError("암호화할 값이 없습니다")
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes | memoryview | str) -> str:
    """암호문을 원문으로 되돌린다. 키가 바뀌었으면 SecretBoxError."""
    if isinstance(token, memoryview):
        token = token.tobytes()
    if isinstance(token, str):
        token = token.encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise SecretBoxError(
            "비밀번호를 복호화하지 못했습니다 — 암호화 키가 바뀌었을 수 있습니다. "
            "해당 계정의 비밀번호를 다시 저장해야 합니다.") from exc
