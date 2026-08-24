#!/usr/bin/env bash
# CGV 알림기 웹 서버를 launchd LaunchAgent로 등록합니다.
#
# 확인 간격은 이제 DB 설정값이라 웹의 '설정' 탭에서 바꿉니다 —
# 간격을 바꿀 때 이 스크립트를 다시 실행할 필요는 없습니다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.lwg.cgv-web"
OLD_LABEL="com.lwg.cgv-watch"   # 매분 watch.py를 띄우던 예전 job
AGENTS="$HOME/Library/LaunchAgents"

# ── 필요한 패키지가 다 있는 python 찾기 ────────────────────────────────────
# launchd는 사용자 셸 PATH를 물려받지 않기 때문에 절대경로가 필요하다.
PY=""
for cand in \
  "$(command -v python3 2>/dev/null || true)" \
  /opt/homebrew/Caskroom/miniforge/base/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3
do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  if "$cand" -c "import playwright, flask, psycopg, psycopg_pool, waitress" 2>/dev/null; then
    PY="$cand"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "오류: 필요한 패키지가 모두 설치된 python3을 찾지 못했습니다." >&2
  echo "      pip install -r requirements.txt" >&2
  echo "      playwright install chromium" >&2
  exit 1
fi
echo "python:   $PY"

if [ ! -f "$ROOT/.env" ]; then
  echo "경고: .env가 없습니다 — Slack 알림도, 로그인도 동작하지 않습니다." >&2
  echo "      cp .env.example .env && chmod 600 .env" >&2
fi

# 로그인 수단이 하나도 없으면 아무도 들어갈 수 없다. 미리 짚어 준다.
if ! "$PY" -c "
import sys, auth
sys.exit(0 if any(p.configured() for p in auth.PROVIDERS.values()) else 1)" 2>/dev/null; then
  echo "경고: 네이버·카카오 로그인 키가 없습니다 — 아무도 로그인할 수 없습니다." >&2
  echo "      .env의 NAVER_CLIENT_ID / KAKAO_REST_API_KEY를 채우세요." >&2
  echo "      콜백 주소는 로그인 화면에 그대로 표시됩니다." >&2
fi

if [ ! -f "$ROOT/web/static/index.html" ]; then
  echo "경고: 프론트엔드가 빌드되지 않았습니다 — 화면이 안내 문구만 보입니다." >&2
  echo "      cd frontend && npm install && npm run build" >&2
fi

# ── DB 준비 확인 ───────────────────────────────────────────────────────────
if ! "$PY" -c "import store; store.init_db()" 2>/dev/null; then
  echo "오류: Postgres에 접속할 수 없습니다." >&2
  echo "      DATABASE_URL을 확인하세요 (.env)." >&2
  exit 1
fi
echo "DB:       $("$PY" -c "import store; print(store.safe_dsn())")"

mkdir -p "$ROOT/logs" "$AGENTS"

# 예전 job과 현재 job을 모두 내린다.
# 예전 워처가 살아 있으면 서버와 같은 DB를 두고 경합한다 — 반드시 걷어낸다.
# `$LABEL.*.plist`는 job을 초 위치마다 나눠 깔던 시절의 잔재를 지우는 용도다.
for old in "$AGENTS/$OLD_LABEL.plist" "$AGENTS/$OLD_LABEL".*.plist \
           "$AGENTS/$LABEL.plist" "$AGENTS/$LABEL".*.plist; do
  [ -e "$old" ] || continue
  name="$(basename "$old" .plist)"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  rm -f "$old"
  echo "내렸습니다: $name"
done

# plist 생성은 파이썬에 맡긴다. 만들어진 경로를 stdout으로 돌려주고 요약은 stderr로.
PLIST="$("$PY" "$ROOT/make_plists.py" "$ROOT" "$PY" "$AGENTS" "$LABEL")"
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "등록 완료:  $PLIST"
echo "대시보드:  http://127.0.0.1:8787"
echo "공개 주소:  $("$PY" -c "import auth; print(auth.public_base_url())")"
echo "확인:      launchctl list | grep cgv"
echo "로그:      tail -f $ROOT/logs/watch.log"
echo "해제:      ./uninstall.sh"
echo
echo "처음 로그인한 계정이 소유자가 됩니다. 그 뒤 계정은 소유자 승인이 필요합니다."
