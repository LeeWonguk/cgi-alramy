#!/usr/bin/env python3
"""네이버·카카오 소셜 로그인 (OAuth 2.0 authorization code).

provider가 둘뿐이고 흐름도 표준 authorization code라, Authlib 같은 라이브러리를
새로 들이지 않고 기존 `send_slack`(watch.py)처럼 urllib 표준 라이브러리로 처리한다.

신원은 **(provider, provider_user_id)** 로 잡는다. 카카오는 이메일이 선택 동의라
사용자가 거부하면 아예 오지 않고, 이메일을 키로 쓰면 같은 이메일을 쓰는 다른
provider 계정과 뭉개진다. 이메일·닉네임은 표시용으로만 쓴다.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from envfile import load_env

log = logging.getLogger("cgv-watch.auth")

HTTP_TIMEOUT = 15


class AuthError(RuntimeError):
    """로그인 흐름에서 사용자에게 보여줄 수 있는 오류."""


@dataclass(frozen=True)
class Profile:
    provider: str
    provider_user_id: str
    nickname: str | None = None
    email: str | None = None
    profile_image: str | None = None


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    profile_url: str
    client_id_env: str
    client_secret_env: str
    scope: str | None
    parse_profile: Callable[[dict], Profile]
    # 네이버는 토큰 요청도 쿼리스트링으로 받는다. 카카오는 form POST만 받는다.
    token_via_post: bool = True
    secret_required: bool = True

    def client_id(self) -> str:
        load_env()
        return os.environ.get(self.client_id_env, "").strip()

    def client_secret(self) -> str:
        load_env()
        return os.environ.get(self.client_secret_env, "").strip()

    def configured(self) -> bool:
        if not self.client_id():
            return False
        return bool(self.client_secret()) if self.secret_required else True

    def missing_keys(self) -> list[str]:
        missing = []
        if not self.client_id():
            missing.append(self.client_id_env)
        if self.secret_required and not self.client_secret():
            missing.append(self.client_secret_env)
        return missing


def _parse_naver(payload: dict) -> Profile:
    # {"resultcode":"00","message":"success","response":{"id":..., "email":..., ...}}
    data = payload.get("response") or {}
    if not data.get("id"):
        raise AuthError("네이버 프로필에 id가 없습니다")
    return Profile(
        provider="naver",
        provider_user_id=str(data["id"]),
        nickname=data.get("nickname") or data.get("name"),
        email=data.get("email"),
        profile_image=data.get("profile_image"),
    )


def _parse_kakao(payload: dict) -> Profile:
    # {"id":..., "kakao_account":{"email":...,"profile":{"nickname":...}}, ...}
    if not payload.get("id"):
        raise AuthError("카카오 프로필에 id가 없습니다")
    account = payload.get("kakao_account") or {}
    profile = account.get("profile") or {}
    properties = payload.get("properties") or {}
    return Profile(
        provider="kakao",
        provider_user_id=str(payload["id"]),
        nickname=profile.get("nickname") or properties.get("nickname"),
        # 이메일은 선택 동의라 없을 수 있다 — 없어도 로그인은 성립한다.
        email=account.get("email"),
        profile_image=(profile.get("profile_image_url")
                       or properties.get("profile_image")),
    )


PROVIDERS: dict[str, Provider] = {
    "naver": Provider(
        name="naver",
        label="네이버",
        authorize_url="https://nid.naver.com/oauth2.0/authorize",
        token_url="https://nid.naver.com/oauth2.0/token",
        profile_url="https://openapi.naver.com/v1/nid/me",
        client_id_env="NAVER_CLIENT_ID",
        client_secret_env="NAVER_CLIENT_SECRET",
        scope=None,
        parse_profile=_parse_naver,
        token_via_post=False,
    ),
    "kakao": Provider(
        name="kakao",
        label="카카오",
        authorize_url="https://kauth.kakao.com/oauth/authorize",
        token_url="https://kauth.kakao.com/oauth/token",
        profile_url="https://kapi.kakao.com/v2/user/me",
        client_id_env="KAKAO_REST_API_KEY",
        client_secret_env="KAKAO_CLIENT_SECRET",
        scope=None,
        parse_profile=_parse_kakao,
        # 카카오 Client Secret은 콘솔에서 켰을 때만 필요하다.
        secret_required=False,
    ),
}


def get_provider(name: str) -> Provider:
    provider = PROVIDERS.get((name or "").lower())
    if provider is None:
        raise AuthError(f"지원하지 않는 로그인 방식입니다: {name}")
    return provider


def public_base_url() -> str:
    """리다이렉트 URI의 기준.

    요청 헤더가 아니라 설정값에서 만든다 — provider에 등록한 값과 **문자열이
    정확히 같아야** 하므로 프록시 헤더에 따라 흔들릴 여지를 남기지 않는다.
    """
    load_env()
    base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    return (base or "http://localhost:8787").rstrip("/")


def redirect_uri(provider: Provider) -> str:
    return f"{public_base_url()}/api/auth/{provider.name}/callback"


def new_state() -> str:
    """CSRF 방어용 state. 세션에 넣어 두고 콜백에서 대조한다."""
    return secrets.token_urlsafe(24)


def authorize_url(provider: Provider, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri(provider),
        "state": state,
    }
    if provider.scope:
        params["scope"] = provider.scope
    return f"{provider.authorize_url}?{urllib.parse.urlencode(params)}"


def _request_json(url: str, *, data: bytes | None = None,
                  headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AuthError(f"{url.split('?')[0]} 응답 {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError(f"{url.split('?')[0]} 호출 실패: {exc}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise AuthError(f"응답을 JSON으로 읽지 못했습니다: {body[:200]}") from exc


def exchange_code(provider: Provider, code: str, state: str) -> str:
    """인가 코드를 액세스 토큰으로 바꾼다."""
    params: dict[str, Any] = {
        "grant_type": "authorization_code",
        "client_id": provider.client_id(),
        "redirect_uri": redirect_uri(provider),
        "code": code,
        "state": state,
    }
    secret = provider.client_secret()
    if secret:
        params["client_secret"] = secret

    if provider.token_via_post:
        payload = _request_json(
            provider.token_url,
            data=urllib.parse.urlencode(params).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    else:
        payload = _request_json(
            f"{provider.token_url}?{urllib.parse.urlencode(params)}"
        )

    token = payload.get("access_token")
    if not token:
        # 네이버는 HTTP 200에 error/error_description을 담아 보낸다.
        reason = payload.get("error_description") or payload.get("error") or payload
        raise AuthError(f"{provider.label} 토큰 발급 실패: {reason}")
    return str(token)


def fetch_profile(provider: Provider, access_token: str) -> Profile:
    payload = _request_json(
        provider.profile_url, headers={"Authorization": f"Bearer {access_token}"}
    )
    return provider.parse_profile(payload)


def login_flow(provider: Provider, code: str, state: str) -> Profile:
    """콜백에서 받은 코드로 프로필까지 한 번에 가져온다."""
    token = exchange_code(provider, code, state)
    profile = fetch_profile(provider, token)
    log.info("%s 로그인 성공: %s (%s)", provider.label, profile.nickname,
             profile.provider_user_id)
    return profile


def available() -> list[dict]:
    """화면에 띄울 로그인 수단 목록. 키가 없는 provider도 이유와 함께 알려준다."""
    return [
        {
            "provider": p.name,
            "label": p.label,
            "configured": p.configured(),
            "missing": p.missing_keys(),
            "login_url": f"/api/auth/{p.name}/login",
        }
        for p in PROVIDERS.values()
    ]
