#!/usr/bin/env python3
"""Flask 앱 — REST API와 Svelte 정적 파일.

    python3 -m web.app          # 127.0.0.1:8787 (waitress)

CGV로 나가는 모든 요청은 browser_worker의 단일 스레드를 거친다. 요청 스레드가
Playwright 세션을 직접 만지면 깨지기 때문이다.

로그인은 네이버·카카오 소셜 로그인(auth.py)이고, 키가 없을 때는 개발용 로컬
계정으로도 들어올 수 있다. 데이터는 어느 쪽이든 사용자별로 나뉜다.
API 핸들러는 반드시 `me()["id"]`로 범위를 좁혀야 남의 감시가 새어 나가지 않는다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `python web/app.py`로 띄워도 동작하게
    sys.path.insert(0, str(ROOT))

from flask import (Flask, g, jsonify, redirect, request,  # noqa: E402
                   send_from_directory, session)
from flask.json.provider import DefaultJSONProvider  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

import auth  # noqa: E402
import store  # noqa: E402
import watch  # noqa: E402
from browser_worker import BrowserWorker  # noqa: E402
from envfile import load_env  # noqa: E402
from web.poller import Poller  # noqa: E402

STATIC_DIR = ROOT / "web" / "static"
LOG_PATH = ROOT / "logs" / "watch.log"

DEFAULT_HOST = "127.0.0.1"  # TLS 종료는 앞단 프록시(터널·nginx)에 맡긴다
DEFAULT_PORT = 8787  # 5000은 macOS AirPlay Receiver와 충돌한다

LOOKUP_TIMEOUT = 90.0  # 즉석 조회는 앞선 확인 사이클을 기다릴 수 있다
# CGV 로그인은 페이지 로딩·캡차 렌더·리다이렉트까지 기다려야 해 더 넉넉히 준다.
LOGIN_TIMEOUT = 120.0

log = logging.getLogger("cgv-watch.web")


class IsoJSONProvider(DefaultJSONProvider):
    """datetime을 ISO 8601로 낸다 — 기본값(RFC 822)은 JS에서 다루기 번거롭다."""

    def default(self, o):
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def create_app(start_background: bool = True) -> Flask:
    load_env()
    app = Flask(__name__, static_folder=None)
    app.json = IsoJSONProvider(app)

    # TLS를 앞단이 끊으므로 Flask는 자기가 http로 서비스된다고 착각한다.
    # 프록시가 준 실제 스킴·호스트를 신뢰해야 링크와 쿠키가 어긋나지 않는다.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db_error: str | None = None
    try:
        store.init_db()
        app.secret_key = store.session_secret()
    except Exception as exc:  # noqa: BLE001 - 접속 실패 종류가 여러 가지다
        db_error = f"{exc}"
        app.secret_key = os.urandom(32)  # DB 없이도 앱은 떠서 오류를 보여준다
        log.error("DB에 접속할 수 없습니다: %s", exc)
        log.error("DATABASE_URL을 확인하세요 (현재: %s)", store.safe_dsn())

    # 쿠키: OAuth 콜백이 최상위 GET으로 돌아올 때 실려야 하므로 Lax.
    # Strict면 방금 발급한 state를 콜백에서 못 읽어 로그인이 무한 반복된다.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=auth.public_base_url().startswith("https://"),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
    )

    # config.toml의 [[watch]]는 --migrate에서만 읽는다. 파일에 남아 있으면
    # "고쳤는데 왜 반영이 안 되지" 하고 헤매기 쉬우니 시작할 때 한 번 짚어준다.
    if store.read_config_file().get("watch"):
        log.warning("config.toml에 [[watch]] 항목이 남아 있습니다 — 이제 무시됩니다. "
                    "감시 대상은 웹 화면이나 `watch.py --migrate`로 관리하세요.")

    # 비밀번호 없이 들어올 수 있는 상태다. 조용히 열려 있으면 위험하니 알린다.
    local_login = auth.local_login_state()
    if local_login["enabled"]:
        log.warning("개발모드: 로컬 계정 로그인이 열려 있습니다 (%s). "
                    "비밀번호가 없으므로 외부에 공개된 주소에서는 .env에 "
                    "DEV_LOGIN=0을 두세요.", local_login["reason"])

    worker = BrowserWorker()
    poller = Poller(worker)
    app.extensions["cgv"] = {"worker": worker, "poller": poller,
                             "db_error": db_error}

    if start_background and db_error is None:
        worker.start()
        poller.start()

    register_auth(app)
    register_api(app)
    register_static(app)
    return app


def parts(app: Flask) -> dict:
    return app.extensions["cgv"]


# ── 헬퍼 ────────────────────────────────────────────────────────────────────
def fail(message: str, status: int = 400):
    return jsonify({"error": message}), status


def body() -> dict:
    return request.get_json(silent=True) or {}


def int_arg(name: str, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(request.args.get(name, default)), maximum))
    except (TypeError, ValueError):
        return default


def me() -> dict | None:
    """현재 로그인 사용자. before_request가 요청마다 한 번만 조회해 둔다."""
    return getattr(g, "user", None)


def user_view(row: dict, *, full: bool = False) -> dict:
    """사용자 한 명을 화면용으로. provider_user_id 같은 내부 값은 빼고 낸다."""
    view = {
        "id": row["id"],
        "provider": row["provider"],
        "nickname": row["nickname"],
        "email": row["email"],
        "profile_image": row["profile_image"],
        "status": row["status"],
        "is_owner": row["is_owner"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }
    if full:
        view["settings"] = {
            "include_showtimes": row["include_showtimes"],
            "lookahead_days": row["lookahead_days"],
            "default_screen_types": row["default_screen_types"],
        }
        # 웹훅 전체를 돌려주지 않는다 — 설정 여부와 종류만 알면 화면은 충분하다.
        view["has_webhook"] = bool(row["webhook_url"])
        view["webhook_kind"] = row["webhook_kind"] or "slack"
    return view


def cgv_account_view(row: dict | None) -> dict:
    """저장된 CGV 계정 상태를 화면용으로. 비밀번호·세션 토큰은 절대 싣지 않는다."""
    if row is None:
        return {"linked": False, "status": "none"}
    return {
        "linked": True,
        "cgv_user_id": row["cgv_user_id"],
        "status": row["status"],            # unlinked | linked | error
        "last_login_at": row["last_login_at"],
        "last_error": row["last_error"],
    }


def seat_watch_view(row: dict) -> dict:
    """좌석 감시 한 건을 화면용으로."""
    return {
        "id": row["id"],
        "movie_query": row["movie_query"],
        "site_query": row["site_query"],
        "scn_ymd": row["scn_ymd"],
        "screen_types": row["screen_types"] or [],
        "rows": row["rows"] or [],
        "min_consecutive": row["min_consecutive"] or 0,
        "enabled": row["enabled"],
        "created_at": row["created_at"],
    }


def target_view(row: dict, showtimes: dict[str, dict] | None = None) -> dict:
    """감시 대상 한 건을 화면용 모양으로 정리한다."""
    view = {
        "id": row["id"],
        "movie_query": row["movie_query"],
        "site_query": row["site_query"],
        "screen_types": row["screen_types"],
        "enabled": row["enabled"],
        "created_at": row["created_at"],
        "status": row["status"] or "unknown",
        "mov_no": row["mov_no"],
        "site_no": row["site_no"],
        "mov_nm": row["mov_nm"] or row["movie_query"],
        "site_nm": row["site_nm"] or row["site_query"],
        "dates": row["dates"] or [],
        "matched_dates": row["matched_dates"] or [],
        "fail_count": row["fail_count"] or 0,
        "last_ok": row["last_ok"],
        "last_error": row["last_error"],
        "updated_at": row["updated_at"],
    }
    # 필터를 쓰면 비교 기준이 matched_dates다 — 화면도 같은 기준으로 보여준다.
    view["tracked_dates"] = (view["matched_dates"] if row["screen_types"]
                             else view["dates"])
    if showtimes is not None:
        view["showtimes"] = [
            {"date": ymd,
             "fetched_at": entry["fetched_at"],
             "groups": watch.group_showtimes(entry["payload"])}
            for ymd, entry in sorted(showtimes.items())
        ]
    return view


# ── 로그인 ──────────────────────────────────────────────────────────────────
def register_auth(app: Flask) -> None:
    @app.before_request
    def load_and_guard():
        """세션에서 사용자를 읽고, 로그인이 필요한 경로를 막는다.

        게이트를 여기 한 곳에 두어 새 API를 추가할 때 인증을 빠뜨릴 수 없게 한다.
        """
        g.user = None
        if parts(app)["db_error"] is None:
            user_id = session.get("user_id")
            if user_id:
                g.user = store.user(user_id)
                if g.user is None:      # 지워진 계정의 오래된 쿠키
                    session.clear()

        path = request.path
        if not path.startswith("/api/"):
            return None                 # SPA 껍데기는 누구나 (데이터는 API로만)
        if path.startswith("/api/auth/"):
            return None                 # 로그인 흐름 자체

        if g.user is None:
            return fail("로그인이 필요합니다", 401)
        if path == "/api/me":
            return None                 # 승인 대기 중에도 자기 상태는 볼 수 있다
        if g.user["status"] != "approved":
            return fail("소유자의 승인을 기다리는 중입니다", 403)
        return None

    @app.get("/api/auth/providers")
    def auth_providers():
        return jsonify({"providers": auth.available(),
                        "base_url": auth.public_base_url(),
                        "local": auth.local_login_state()})

    @app.post("/api/auth/local/login")
    def auth_local_login():
        """개발용 로컬 계정 로그인 — 이름만 받는다 (비밀번호 없음).

        열려 있는지는 요청마다 다시 확인한다. .env를 고쳐 껐다면 그 뒤로는
        바로 막혀야 한다.
        """
        state = auth.local_login_state()
        if not state["enabled"]:
            return fail(f"로컬 계정 로그인이 꺼져 있습니다 — {state['reason']}", 403)
        if parts(app)["db_error"]:
            return fail(f"DB 접속 실패: {parts(app)['db_error']}", 503)

        try:
            profile = auth.local_profile(str(body().get("name", "")))
        except auth.AuthError as exc:
            return fail(str(exc))

        account = store.login_user(
            profile.provider, profile.provider_user_id,
            nickname=profile.nickname,
        )
        session.clear()
        session["user_id"] = account["id"]
        session.permanent = True
        # 비밀번호 없는 로그인이라 누가 언제 들어왔는지는 로그에 남겨 둔다.
        log.warning("로컬 계정 로그인: %s (#%d, 요청 %s)", account["nickname"],
                    account["id"], request.remote_addr)
        return jsonify({"user": user_view(account, full=True)})

    @app.get("/api/auth/<provider_name>/login")
    def auth_login(provider_name: str):
        try:
            provider = auth.get_provider(provider_name)
        except auth.AuthError as exc:
            return fail(str(exc), 404)
        if not provider.configured():
            return fail(
                f"{provider.label} 로그인이 설정되지 않았습니다 — "
                f".env에 {', '.join(provider.missing_keys())}를 채우세요", 503)

        state = auth.new_state()
        session[f"oauth_state_{provider.name}"] = state
        return redirect(auth.authorize_url(provider, state))

    @app.get("/api/auth/<provider_name>/callback")
    def auth_callback(provider_name: str):
        try:
            provider = auth.get_provider(provider_name)
        except auth.AuthError as exc:
            return fail(str(exc), 404)

        # 사용자가 동의 화면에서 취소한 경우도 여기로 돌아온다.
        if request.args.get("error"):
            return login_failed(request.args.get("error_description")
                                or request.args["error"])

        expected = session.pop(f"oauth_state_{provider.name}", None)
        state = request.args.get("state", "")
        if not expected or state != expected:
            return login_failed("로그인 요청이 만료됐거나 위조됐습니다. 다시 시도해 주세요.")

        code = request.args.get("code", "")
        if not code:
            return login_failed("인가 코드를 받지 못했습니다.")

        try:
            profile = auth.login_flow(provider, code, state)
        except auth.AuthError as exc:
            log.warning("%s 로그인 실패: %s", provider.label, exc)
            return login_failed(str(exc))

        account = store.login_user(
            profile.provider, profile.provider_user_id,
            nickname=profile.nickname, email=profile.email,
            profile_image=profile.profile_image,
        )
        session.clear()
        session["user_id"] = account["id"]
        session.permanent = True
        return redirect("/")

    def login_failed(reason: str):
        """로그인 실패도 화면으로 돌려보낸다 — JSON을 브라우저에 띄우지 않는다."""
        from urllib.parse import quote

        return redirect(f"/?login_error={quote(reason[:300])}")

    @app.post("/api/auth/logout")
    def auth_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/me")
    def whoami():
        user = me()
        return jsonify({
            "user": user_view(user, full=True),
            "owner_exists": store.owner() is not None,
        })


# ── API ─────────────────────────────────────────────────────────────────────
def register_api(app: Flask) -> None:
    @app.errorhandler(Exception)
    def on_error(exc):  # noqa: ANN001
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc
        log.exception("API 처리 중 오류")
        return jsonify({"error": str(exc)}), 500

    def owner_only():
        """소유자 전용 동작 앞에 둔다. 통과하면 None."""
        return None if me()["is_owner"] else fail("소유자만 할 수 있습니다", 403)

    @app.get("/api/health")
    def health():
        info = parts(app)
        payload = {
            "db_error": info["db_error"],
            "worker": info["worker"].snapshot(),
            "poller": info["poller"].snapshot(),
        }
        if info["db_error"]:
            payload["db"] = {"ok": False, "dsn": store.safe_dsn()}
            return jsonify(payload), 503
        payload["db"] = store.health()
        return jsonify(payload)

    @app.get("/api/dashboard")
    def dashboard():
        info = parts(app)
        if info["db_error"]:
            return fail(f"DB 접속 실패: {info['db_error']}", 503)

        user = me()
        rows = store.targets(owner_id=user["id"])
        views = [target_view(row, store.load_showtimes(row["id"])) for row in rows]
        payload = {
            "targets": views,
            "settings": store.server_settings(),
            "poller": info["poller"].snapshot(),
            "alerts": store.recent_alerts(5, owner_id=user["id"]),
            "server_time": datetime.now().astimezone(),
        }
        # 사이클 이력과 워커 내부 상태는 서버 전체 정보라 소유자에게만 보인다.
        if user["is_owner"]:
            payload["worker"] = info["worker"].snapshot()
            payload["last_cycle"] = store.last_cycle()
        return jsonify(payload)

    # ── 감시 대상 ──
    @app.get("/api/targets")
    def list_targets():
        return jsonify([target_view(row)
                        for row in store.targets(owner_id=me()["id"])])

    @app.post("/api/targets")
    def create_targets():
        data = body()
        movie = str(data.get("movie", "")).strip()
        sites = data.get("sites") or ([data["site"]] if data.get("site") else [])
        screen_types = store.normalize_screen_types(data.get("screen_types"))
        if not movie:
            return fail("영화명이 비어 있습니다")
        if not sites:
            return fail("극장을 하나 이상 지정하세요")

        created, duplicates = [], []
        for site in sites:
            site = str(site).strip()
            if not site:
                continue
            row = store.add_target(movie, site, screen_types, owner_id=me()["id"])
            if row is None:
                duplicates.append(site)
            else:
                created.append(target_view(row))
        status = 201 if created else 409
        return jsonify({"created": created, "duplicates": duplicates}), status

    @app.patch("/api/targets/<int:target_id>")
    def patch_target(target_id: int):
        data = body()
        if store.target(target_id, owner_id=me()["id"]) is None:
            return fail("없는 감시 대상입니다", 404)
        row = store.update_target(
            target_id,
            enabled=data.get("enabled"),
            screen_types=(store.normalize_screen_types(data["screen_types"])
                          if "screen_types" in data else None),
        )
        return jsonify(target_view(row))

    @app.delete("/api/targets/<int:target_id>")
    def remove_target(target_id: int):
        doomed = store.target(target_id, owner_id=me()["id"])
        if doomed is None or not store.delete_target(target_id):
            return fail("없는 감시 대상입니다", 404)
        # 관측 기준선까지 함께 사라지는 되돌릴 수 없는 동작이다. 무엇이 언제
        # 지웠는지 로그에 남겨 둔다 — 대상이 조용히 사라지면 원인을 찾을 길이 없다.
        log.warning("감시 대상 삭제: #%d %s · %s (%s, 요청 %s)",
                    target_id, doomed["movie_query"], doomed["site_query"],
                    me()["nickname"], request.remote_addr)
        return jsonify({"deleted": target_id})

    @app.post("/api/targets/<int:target_id>/reset")
    def reset_target(target_id: int):
        if store.target(target_id, owner_id=me()["id"]) is None:
            return fail("없는 감시 대상입니다", 404)
        store.reset_state(target_id)
        return jsonify({"reset": target_id,
                        "note": "다음 확인은 알림 없이 기준선만 저장합니다"})

    # ── 영화·극장 목록 ──
    @app.get("/api/catalog")
    def catalog():
        movies = store.catalog_movies()
        sites = store.catalog_sites(request.args.get("region") or None)
        regions = sorted({s["region"] for s in store.catalog_sites() if s["region"]})
        return jsonify({"movies": movies, "sites": sites, "regions": regions,
                        "refreshed_at": store.catalog_refreshed_at()})

    @app.post("/api/catalog/refresh")
    def refresh_catalog():
        try:
            result = parts(app)["worker"].run(
                watch.refresh_catalog, label="catalog", timeout=LOOKUP_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"목록을 받아오지 못했습니다: {exc}", 502)
        return jsonify({**result, "refreshed_at": store.catalog_refreshed_at()})

    # ── 즉석 조회 ──
    @app.post("/api/lookup")
    def lookup_dates():
        data = body()
        mov_no, site_no = str(data.get("mov_no", "")), str(data.get("site_no", ""))
        if not mov_no or not site_no:
            return fail("mov_no와 site_no가 필요합니다")
        try:
            dates = parts(app)["worker"].run(
                lambda cgv: cgv.bookable_dates(site_no, mov_no),
                label=f"lookup:{mov_no}@{site_no}", timeout=LOOKUP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"조회 실패: {exc}", 502)
        return jsonify({"dates": dates})

    @app.post("/api/lookup/showtimes")
    def lookup_showtimes():
        data = body()
        mov_no, site_no = str(data.get("mov_no", "")), str(data.get("site_no", ""))
        ymd = str(data.get("date", ""))
        wanted = store.normalize_screen_types(data.get("screen_types"))
        if not (mov_no and site_no and ymd):
            return fail("mov_no·site_no·date가 필요합니다")
        try:
            rows = parts(app)["worker"].run(
                lambda cgv: cgv.showtimes(site_no, mov_no, ymd),
                label=f"showtimes:{ymd}", timeout=LOOKUP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"시간표 조회 실패: {exc}", 502)
        if wanted:
            rows = [r for r in rows if watch.matches_screen_types(r, wanted)]
        return jsonify({"date": ymd, "count": len(rows),
                        "groups": watch.group_showtimes(rows)})

    @app.get("/api/showtimes")
    def cached_showtimes():
        try:
            target_id = int(request.args["target_id"])
        except (KeyError, ValueError):
            return fail("target_id가 필요합니다")
        if store.target(target_id, owner_id=me()["id"]) is None:
            return fail("없는 감시 대상입니다", 404)
        cached = store.load_showtimes(target_id, request.args.get("date") or None)
        return jsonify([
            {"date": ymd, "fetched_at": entry["fetched_at"],
             "groups": watch.group_showtimes(entry["payload"])}
            for ymd, entry in sorted(cached.items())
        ])

    # ── 이력 ──
    @app.get("/api/alerts")
    def alerts():
        return jsonify(store.recent_alerts(int_arg("limit", 50, 500),
                                           owner_id=me()["id"]))

    @app.get("/api/cycles")
    def cycles():
        # 사이클은 서버 전체가 한 번에 도는 것이라 개인 이력이 아니다.
        denied = owner_only()
        return denied or jsonify(store.recent_cycles(int_arg("limit", 50, 500)))

    @app.get("/api/logs")
    def logs():
        denied = owner_only()  # 로그에는 모든 사용자의 감시가 섞여 있다
        if denied:
            return denied
        lines = int_arg("lines", 200, 2000)
        if not LOG_PATH.exists():
            return jsonify({"lines": [], "note": "아직 로그 파일이 없습니다"})
        # 로그는 최대 5MB까지 커진다. 끝부분만 읽어 마지막 N줄을 낸다.
        with LOG_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", errors="replace")
        return jsonify({"lines": tail.splitlines()[-lines:]})

    # ── 사용자 관리 (소유자 전용) ──
    @app.get("/api/users")
    def list_users():
        denied = owner_only()
        return denied or jsonify([user_view(u, full=True) for u in store.users()])

    @app.patch("/api/users/<int:user_id>")
    def patch_user(user_id: int):
        denied = owner_only()
        if denied:
            return denied
        if store.user(user_id) is None:
            return fail("없는 사용자입니다", 404)
        data = body()
        if "status" in data:
            try:
                store.set_user_status(user_id, str(data["status"]))
            except ValueError as exc:
                return fail(str(exc))
        return jsonify(user_view(store.user(user_id), full=True))

    @app.delete("/api/users/<int:user_id>")
    def remove_user(user_id: int):
        denied = owner_only()
        if denied:
            return denied
        if user_id == me()["id"]:
            return fail("자기 계정은 지울 수 없습니다")
        if not store.delete_user(user_id):
            return fail("없는 사용자이거나 소유자입니다", 404)
        log.warning("사용자 삭제: #%d (감시 대상도 함께 사라집니다)", user_id)
        return jsonify({"deleted": user_id})

    # ── 동작 ──
    # ── CGV 계정 로그인 (Phase 1) ──
    @app.get("/api/cgv-account")
    def get_cgv_account():
        """내 CGV 계정 연동 상태. 비밀번호·토큰은 나오지 않는다."""
        return jsonify(cgv_account_view(store.cgv_account(me()["id"])))

    @app.put("/api/cgv-account")
    def put_cgv_account():
        """CGV 아이디·비밀번호를 저장한다. 저장 즉시 로그인을 시도해 유효성을 확인."""
        data = body()
        cgv_id = str(data.get("cgv_user_id", "")).strip()
        password = str(data.get("password", ""))
        if not cgv_id or not password:
            return fail("아이디와 비밀번호를 모두 입력하세요")
        try:
            store.set_cgv_account(me()["id"], cgv_id, password)
        except ValueError as exc:
            return fail(str(exc))

        # 저장된 자격증명으로 바로 로그인해 본다(캡차 포함). 실패해도 자격증명은
        # 저장돼 있으니, 나중에 CGV 상태가 나아지면 다음 좌석 사이클이 재시도한다.
        owner_id = me()["id"]
        try:
            ok = parts(app)["worker"].run(
                lambda s: __import__("cgv_login").login_now(owner_id, s),
                label="cgv-login", timeout=LOGIN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 브라우저 기동 실패 등
            log.warning("CGV 로그인 확인 실패: %s", exc)
            ok = False
        return jsonify({"account": cgv_account_view(store.cgv_account(owner_id)),
                        "logged_in": bool(ok)})

    @app.delete("/api/cgv-account")
    def delete_cgv_account():
        """저장된 CGV 계정을 지운다 (좌석 감시는 로그인이 없으면 확인되지 않는다)."""
        store.delete_cgv_account(me()["id"])
        return jsonify({"deleted": True})

    # ── 좌석 감시 (Phase 1) ──
    @app.get("/api/seat-watches")
    def list_seat_watches():
        return jsonify([seat_watch_view(row)
                        for row in store.seat_watches(owner_id=me()["id"])])

    @app.post("/api/seat-watches")
    def create_seat_watch():
        data = body()
        movie = str(data.get("movie", "")).strip()
        site = str(data.get("site", "")).strip()
        scn_ymd = str(data.get("scn_ymd", "")).strip()
        if not (movie and site and scn_ymd):
            return fail("영화·극장·날짜를 모두 지정하세요")
        try:
            row = store.add_seat_watch(
                me()["id"], movie, site, scn_ymd,
                screen_types=data.get("screen_types"), rows=data.get("rows"),
                min_consecutive=data.get("min_consecutive", 0))
        except ValueError as exc:
            return fail(str(exc))
        return jsonify(seat_watch_view(row)), 201

    @app.delete("/api/seat-watches/<int:watch_id>")
    def remove_seat_watch(watch_id: int):
        if not store.delete_seat_watch(watch_id, owner_id=me()["id"]):
            return fail("없는 좌석 감시입니다", 404)
        return jsonify({"deleted": watch_id})

    @app.post("/api/check-now")
    def check_now():
        # 사이클은 **모든 사용자의 대상**을 한 바퀴 확인한다 — 한 사람의 버튼이
        # 서버 전체를 움직이므로 소유자 전용이다. (/api/cycles·/api/logs·
        # PATCH /api/settings 와 같은 기준.) 사용자별로 자기 대상만 확인하게
        # 하려면 check_all에 소유자 스코프를 넣어야 한다 — 그때 다시 열면 된다.
        denied = owner_only()
        return denied or jsonify(parts(app)["poller"].run_cycle("manual"))

    @app.get("/api/settings")
    def get_settings():
        """서버 공용 설정. 운영 상태값·세션 비밀키는 절대 나가지 않는다."""
        return jsonify(store.server_settings())

    @app.patch("/api/settings")
    def patch_settings():
        # 브라우저와 스케줄러가 하나씩뿐이라 서버 전체에 영향을 준다.
        denied = owner_only()
        if denied:
            return denied
        try:
            saved = store.set_settings(body())
        except (TypeError, ValueError) as exc:
            return fail(f"설정 값을 이해할 수 없습니다: {exc}")
        return jsonify({"saved": saved, "settings": store.server_settings()})

    @app.patch("/api/me/settings")
    def patch_my_settings():
        """내 취향 설정과 알림 웹훅. 웹훅은 빈 문자열이면 전역 웹훅으로 되돌린다."""
        data = body()
        fields = {k: data[k] for k in
                  ("include_showtimes", "lookahead_days", "default_screen_types",
                   "webhook_url", "webhook_kind") if k in data}
        try:
            updated = store.update_user(me()["id"], **fields)
        except (TypeError, ValueError) as exc:
            return fail(f"설정 값을 이해할 수 없습니다: {exc}")
        return jsonify(user_view(updated, full=True))

    @app.post("/api/test-notify")
    def test_notify():
        """내 웹훅으로 테스트 메시지. 어디로 보냈는지도 함께 낸다."""
        url, kind = watch.resolve_webhook(me().get("webhook_url"),
                                          me().get("webhook_kind"))
        ok = watch.send_webhook(
            "✅ *CGV 알림기 연결 테스트*\n"
            "이 메시지가 보이면 알림이 정상 동작합니다.",
            webhook_url=url, kind=kind,
        )
        return jsonify({
            "sent": ok,
            "kind": kind,
            "label": watch.WEBHOOK_LABELS.get(kind, kind),
            # 개인 웹훅이 없으면 .env의 전역 웹훅으로 나갔다는 뜻이다.
            "personal": bool((me().get("webhook_url") or "").strip()),
        }), (200 if ok else 502)


# ── 정적 파일 ───────────────────────────────────────────────────────────────
def register_static(app: Flask) -> None:
    index = STATIC_DIR / "index.html"

    @app.get("/")
    def home():
        if not index.exists():
            return (
                "<h1>프론트엔드가 아직 빌드되지 않았습니다</h1>"
                "<pre>cd frontend && npm install && npm run build</pre>"
                "<p>개발 중이라면 <code>npm run dev</code>(5173)를 쓰세요. "
                "API는 <code>/api/dashboard</code>에서 동작합니다.</p>",
                200,
                {"Content-Type": "text/html; charset=utf-8"},
            )
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/<path:filename>")
    def assets(filename: str):
        # SPA라 알 수 없는 경로는 index.html로 넘긴다 (API는 위에서 이미 처리).
        if (STATIC_DIR / filename).is_file():
            return send_from_directory(STATIC_DIR, filename)
        return home()


def main() -> int:
    parser = argparse.ArgumentParser(description="CGV 알림기 웹 서버")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-poll", action="store_true",
                        help="폴링·브라우저 없이 API만 띄웁니다 (개발용)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    watch.setup_logging(args.verbose)
    app = create_app(start_background=not args.no_poll)

    from waitress import serve

    log.info("http://%s:%d 에서 대기합니다 (공개 주소: %s)",
             args.host, args.port, auth.public_base_url())
    try:
        serve(app, host=args.host, port=args.port, threads=8,
              ident="cgv-watch")
    except KeyboardInterrupt:
        pass
    finally:
        info = parts(app)
        info["poller"].stop()
        info["worker"].stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
