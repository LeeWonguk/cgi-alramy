# syntax=docker/dockerfile:1.7

# ── 프론트엔드 빌드 ────────────────────────────────────────────────
# vite가 ../web/static으로 산출물을 내보내므로 (frontend/vite.config.js:9)
# frontend/ 아래에서 빌드하고 나면 /build/web/static이 생긴다.
FROM node:20-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci
COPY frontend/ ./frontend/
RUN cd frontend && npm run build


# ── 런타임 ────────────────────────────────────────────────────────
# requirements.txt의 조합을 실제로 확인한 인터프리터가 3.13이다 — 맞춰 둔다.
FROM python:3.13-slim-bookworm AS runtime

# TZ: 컨테이너 기본은 UTC다. 로그·알림 시각·`_now()`가 전부 한국 시각으로
# 읽혀야 화면의 "N초 전"과 사람이 보는 시계가 어긋나지 않는다. (선점 만료처럼
# 틀리면 동작이 바뀌는 값은 booking.KST로 따로 못박아 두었으니, 이건 표시용
# 안전장치다.)
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Seoul

# tini: waitress가 SIGTERM에 깨끗이 내려가도록 PID 1을 대신 잡는다.
# tzdata: TZ가 실제로 먹으려면 /usr/share/zoneinfo가 있어야 한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.txt
# 파이썬 의존성 — 레이어 캐시 활용을 위해 소스 복사 전에 설치.
# 버전의 출처는 requirements.txt 하나뿐이다. 여기에 범위를 또 적으면 안 된다.
RUN pip install -r requirements.txt

# 브라우저 바이너리는 **playwright를 고정한 뒤에** 받는다. 리비전이 파이썬 패키지
# 버전에 매여 있어서, 받고 나서 playwright를 갈아끼우면 컨테이너 안의 chromium이
# 맞지 않아 "Executable doesn't exist"로 죽는다.
RUN playwright install --with-deps chromium
# 앱 소스와 프론트엔드 산출물.
COPY . .
COPY --from=frontend /build/web/static ./web/static

# logs/는 볼륨으로 마운트되지만, 마운트 전 컨테이너 시작 순간에도 존재해야 한다.
RUN mkdir -p /app/logs

EXPOSE 8787

ENTRYPOINT ["/usr/bin/tini", "--"]
# 컨테이너 안에서 0.0.0.0 바인딩은 안전하다 — 외부로는 nginx만 노출된다.
CMD ["python3", "-m", "web.app", "--host", "0.0.0.0", "--port", "8787"]
