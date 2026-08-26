"""`.env`를 환경변수로 읽어들인다.

watch.py와 store.py가 함께 쓴다. store를 import하는 watch에서 이 함수를 정의하면
순환 import가 되므로 별 모듈로 뺐다.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"

_loaded = False


def load_env(force: bool = False) -> None:
    """.env를 읽어 환경변수에 채운다 (이미 설정된 값은 덮지 않는다).

    여러 번 불러도 파일은 한 번만 읽는다 — 서버·CLI 양쪽에서 진입점이 여러 개다.
    """
    global _loaded
    if _loaded and not force:
        return
    _loaded = True

    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `export FOO=bar`도 받는다 — .env를 `source` 해서 쓰던 사람이 그대로
        # 옮겨 오면 키 이름이 'export FOO'가 되어 조용히 무시된다.
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[7:].lstrip()
            if "=" not in line:
                continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), _unquote(value.strip()))


def _unquote(value: str) -> str:
    """양끝을 감싼 따옴표만 벗긴다.

    `.strip("'\\"")`은 짝이 맞는지 보지 않아서, 비밀번호가 따옴표로 끝나면
    그 글자를 먹어 버린다. 값 안의 `#`은 건드리지 않는다 — 웹훅 URL이나
    비밀번호에 그대로 들어 있을 수 있어, 주석으로 보고 자르면 값이 망가진다.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value
