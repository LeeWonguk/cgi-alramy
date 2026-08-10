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
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
