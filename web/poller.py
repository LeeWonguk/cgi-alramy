#!/usr/bin/env python3
"""확인 사이클 스케줄러.

설정된 간격마다 watch.check_all()을 브라우저 워커에 투입한다. 예전에는 launchd가
매분 프로세스를 띄우고 watch.py --sweep이 그 1분을 쪼갰는데, 서버가 상주하게
되면서 그 우회로가 필요 없어졌다.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta

import seats
import store
import watch
from browser_worker import BrowserWorker

log = logging.getLogger("cgv-watch.poller")

# 영화·극장 목록을 며칠까지 묵혀 둘지. 신작은 하루 단위로 들어온다.
CATALOG_MAX_AGE_HOURS = 24


class Poller:
    def __init__(self, worker: BrowserWorker) -> None:
        self._worker = worker
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # 같은 순간에 사이클이 둘 돌지 않게 한다. 스케줄러와 "지금 확인" 버튼이
        # 겹칠 수 있어서, 프로세스 밖(CLI)을 막는 파일 락과 별개로 필요하다.
        self._cycle_lock = threading.Lock()

        self._lock = threading.Lock()  # 아래 상태값 보호
        self._next_check_at: datetime | None = None
        self._last_summary: dict | None = None

    # ── 수명 관리 ──
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="cgv-poller", daemon=True
        )
        self._thread.start()
        log.info("폴링 스케줄러를 시작했습니다 (간격 %d초)", self.interval())

    def stop(self, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._thread = None
        log.info("폴링 스케줄러를 종료했습니다")

    def interval(self) -> int:
        return max(store.MIN_INTERVAL_SECONDS,
                   int(store.get_setting("poll_interval_seconds", 30)))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "interval_seconds": self.interval(),
                "next_check_at": self._next_check_at,
                "last_summary": self._last_summary,
            }

    # ── 사이클 ──
    def run_cycle(self, trigger: str = "schedule") -> dict:
        """1회 확인. 겹치면 조용히 건너뛴다."""
        if not self._cycle_lock.acquire(blocking=False):
            log.info("확인이 이미 진행 중입니다 — %s 요청은 건너뜁니다", trigger)
            return {"skipped": True, "reason": "이미 확인 중입니다"}
        try:
            with watch.single_instance() as acquired:
                if not acquired:
                    log.info("다른 프로세스가 확인 중입니다 — 이번 차례는 건너뜁니다")
                    return {"skipped": True,
                            "reason": "다른 프로세스가 확인 중입니다"}
                return self._run_locked(trigger)
        finally:
            self._cycle_lock.release()

    def _run_locked(self, trigger: str) -> dict:
        cycle_id = store.start_cycle(trigger)
        try:
            # 타임아웃을 걸지 않는다. 워커가 작업을 하나씩만 처리하므로, 사이클이
            # 길어지면 다음 차례가 밀릴 뿐 요청이 겹쳐 쌓이지는 않는다.
            summary = self._worker.run(watch.check_all, label=f"cycle:{trigger}")
        except Exception as exc:  # noqa: BLE001 - 브라우저 기동 실패 등
            store.finish_cycle(cycle_id, ok=False, error=str(exc))
            watch.report_connect_failure(exc, dry_run=False)
            return {"error": str(exc)}

        store.finish_cycle(cycle_id, ok=True, **watch.summary_fields(summary))
        # 좌석 감시는 로그인이 필요해 무거우므로, 대상이 있을 때만 이어서 돈다.
        # 여기서 새는 예외로 방금 성공한 사이클을 실패로 만들지 않는다.
        self._run_seat_cycle(trigger)
        with self._lock:
            self._last_summary = summary
        return summary

    def _run_seat_cycle(self, trigger: str) -> None:
        """좌석 감시 한 바퀴. 같은 브라우저 세션을 재사용해 로그인·좌석을 본다."""
        try:
            if not store.seat_watches(enabled_only=True):
                return
            result = self._worker.run(seats.check_seat_watches,
                                      label=f"seats:{trigger}")
            log.info("좌석 감시 %s: 확인 %s건 · 알림 %s건", trigger,
                     result.get("watches_checked"), result.get("alerts_sent"))
        except Exception:  # noqa: BLE001 - 좌석 감시 실패로 메인 사이클을 망치지 않는다
            log.exception("좌석 감시 사이클에서 오류가 났습니다")

    # ── 내부 ──
    def _loop(self) -> None:
        # 서버를 띄운 직후 한 번 확인해 화면이 비어 있지 않게 한다.
        self._safe_cycle("startup")
        self._refresh_catalog_if_stale()

        while not self._stop.is_set():
            interval = self.interval()
            delay = self._delay_until_next_slot(interval)
            with self._lock:
                self._next_check_at = (datetime.now().astimezone()
                                       + timedelta(seconds=delay))
            if self._stop.wait(delay):
                break
            self._safe_cycle("schedule")
            self._refresh_catalog_if_stale()

        with self._lock:
            self._next_check_at = None

    def _safe_cycle(self, trigger: str) -> None:
        """사이클에서 새는 예외로 스케줄러 스레드가 죽지 않게 감싼다."""
        try:
            self.run_cycle(trigger)
        except Exception:  # noqa: BLE001
            log.exception("확인 사이클에서 예상치 못한 오류가 났습니다")

    def _refresh_catalog_if_stale(self) -> None:
        """영화·극장 목록이 낡았으면 다시 받는다.

        감시 대상이 모두 코드 캐시에 걸리면 확인 사이클은 목록 API를 **아예 부르지
        않는다**(watch.cached_ids). 그래서 자동으로 갱신될 계기가 없어 실제로 2주
        넘게 낡은 채로 있었다. 대상 추가 화면이 이 목록에서 영화를 고르므로,
        낡으면 신작을 아예 고를 수 없다.

        하루에 한 번이라 요청 2건이 늘 뿐이다. 실패는 삼킨다 — 목록이 낡는 것보다
        스케줄러가 죽는 게 나쁘다.
        """
        try:
            refreshed = store.catalog_refreshed_at()
            if refreshed is not None:
                age = datetime.now().astimezone() - refreshed
                if age < timedelta(hours=CATALOG_MAX_AGE_HOURS):
                    return
            result = self._worker.run(watch.refresh_catalog, label="catalog:auto")
            log.info("영화·극장 목록을 자동 갱신했습니다 (영화 %s · 극장 %s)",
                     result.get("movies"), result.get("sites"))
        except Exception:  # noqa: BLE001
            log.exception("목록 자동 갱신에 실패했습니다 — 다음 사이클에 다시 시도합니다")

    @staticmethod
    def _delay_until_next_slot(interval: int) -> float:
        """다음 확인 시각까지 남은 초.

        유닉스 에포크 기준으로 간격을 맞춘다. 간격이 60의 약수면(10·15·20·30·60)
        확인 시각이 매분 같은 자리에 고정된다. 예매 오픈은 정각·30분에 몰리는데
        확인 시각이 계속 밀리면 하필 오픈 직전에 확인하고 다음 차례까지 꽉
        기다리는 일이 생긴다.
        """
        now = time.time()
        return max(0.5, (math.floor(now / interval) + 1) * interval - now)
