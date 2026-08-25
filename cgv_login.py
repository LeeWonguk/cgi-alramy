#!/usr/bin/env python3
"""CGV 계정 로그인 오케스트레이션 (Phase 1).

로그인 자체는 CgvSession.login_cgv가 cgv.co.kr/mem/login 페이지를 몰아 처리한다
(비밀번호 암호화·바디 구성은 페이지 JS가, 숫자 캡차는 fillText 후킹이 담당).
이 모듈은 그 위에서 **저장된 세션을 최대한 재사용**하는 순서를 정한다:

    1. 저장된 세션 쿠키가 있으면 되살려(restore) 그대로 쓴다 — 캡차 없음.
    2. accessToken이 만료됐으면 refresh_token으로 갱신한다 — 캡차 없음.
    3. refresh까지 만료됐으면 저장된 아이디·비밀번호로 다시 로그인한다 — 캡차.

캡차가 필요한 3번은 브라우저를 실제로 몰아야 하므로, 이 함수들은 모두
browser_worker 스레드에서(세션을 소유한 스레드에서) 실행돼야 한다:

    worker.run(lambda s: cgv_login.ensure_logged_in(owner_id, s), label="cgv-login")
"""

from __future__ import annotations

import logging

import store
from watch import CgvSession, LoginError

log = logging.getLogger("cgv-watch.cgv-login")


def ensure_logged_in(owner_id: int, session: CgvSession) -> bool:
    """세션이 이 소유자의 CGV 계정으로 로그인된 상태가 되도록 보장한다.

    저장된 세션→refresh→재로그인 순으로 시도하고, 성공하면 갱신된 세션 쿠키를
    저장한 뒤 True를 돌려준다. 저장된 계정이 없거나 끝내 실패하면 False.
    이미 로그인돼 있으면 아무것도 하지 않는다.
    """
    if session.logged_in():
        return True

    account = store.cgv_account(owner_id)
    if account is None:
        log.info("owner %s: 저장된 CGV 계정이 없습니다", owner_id)
        return False

    # 1) 저장된 세션 쿠키 되살리기
    tokens = store.cgv_tokens(owner_id)
    if tokens:
        session.restore_tokens(tokens)
        if session.logged_in():
            # 되살린 accessToken이 실제로 유효한지는 refresh로 확정할 필요는 없다 —
            # 좌석 조회가 401이면 그때 재로그인한다. 여기서는 쿠키 존재로 충분하다.
            return True

    # 2) refresh로 accessToken 갱신 (refresh_token 쿠키가 살아 있으면 성공)
    if tokens and session.refresh_session():
        store.set_cgv_tokens(owner_id, session.session_tokens())
        return True

    # 3) 저장된 아이디·비밀번호로 재로그인 (캡차)
    password = store.cgv_password(owner_id)
    if password is None:
        store.set_cgv_account_status(owner_id, "error",
                                     error="저장된 비밀번호가 없습니다")
        return False
    try:
        fresh = session.login_cgv(account["cgv_user_id"], password)
    except LoginError as exc:
        log.warning("owner %s: CGV 재로그인 실패 — %s", owner_id, exc)
        store.clear_cgv_tokens(owner_id)
        store.set_cgv_account_status(owner_id, "error", error=str(exc))
        return False

    store.set_cgv_tokens(owner_id, fresh)
    return True


def login_now(owner_id: int, session: CgvSession) -> bool:
    """저장된 세션을 무시하고 캡차부터 새로 로그인한다.

    사용자가 '지금 로그인'을 눌렀을 때처럼, 자격증명이 여전히 유효한지 즉시
    확인하고 싶을 때 쓴다. 성공하면 세션을 저장하고 True.
    """
    account = store.cgv_account(owner_id)
    password = store.cgv_password(owner_id)
    if account is None or password is None:
        return False
    try:
        fresh = session.login_cgv(account["cgv_user_id"], password)
    except LoginError as exc:
        log.warning("owner %s: CGV 로그인 실패 — %s", owner_id, exc)
        store.set_cgv_account_status(owner_id, "error", error=str(exc))
        return False
    store.set_cgv_tokens(owner_id, fresh)
    return True
