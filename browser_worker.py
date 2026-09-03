#!/usr/bin/env python3
"""브라우저를 소유하는 단일 스레드와 작업 큐.

Playwright의 sync API는 **세션을 만든 스레드에서만** 쓸 수 있다. Flask 요청
스레드가 CgvSession을 직접 만지면 조용히 깨진다. 그래서 브라우저를 소유하는
워커 스레드 하나를 두고, CGV로 나가는 모든 요청을 큐로 직렬화한다.

    폴링 스케줄러 ─┐
                   ├─→ queue ─→ 워커 스레드 (CgvSession 1개 상주)
    Flask 요청 ────┘              └─ 결과는 Future로 돌려준다

세션을 상주시키므로 확인 사이클마다 Chromium을 새로 띄우던 1~2초가 사라진다.
대신 오래 떠 있는 브라우저가 메모리를 물고 늘어지므로 일정 시간마다 재기동한다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import store
from watch import CgvSession

log = logging.getLogger("cgv-watch.worker")

# 큐에 들어가는 종료 신호. 값 비교로 쓰므로 별 객체면 된다.
_SHUTDOWN = object()


@dataclass
class Job:
    fn: Callable[[CgvSession], Any]
    label: str
    future: Future = field(default_factory=Future)


class BrowserWorker:
    """CGV 세션을 상주시키며 작업을 하나씩 처리하는 스레드."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._session: CgvSession | None = None
        self._stopping = threading.Event()

        self._lock = threading.Lock()  # 아래 상태값 보호 — 대시보드가 읽는다
        self._current: str | None = None
        self._jobs_done = 0
        self._session_started: datetime | None = None
        self._session_jobs = 0
        self._last_error: str | None = None
        # 결제 때문에 재활용을 미루는 중인지. 로그를 한 번만 남기려고 든다 —
        # 판정은 작업마다 도니까(3초에 한 번) 매번 찍으면 그것만 수백 줄이 된다.
        self._recycle_deferred = False

    # ── 수명 관리 ──
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="cgv-browser", daemon=True
        )
        self._thread.start()
        log.info("브라우저 워커를 시작했습니다")

    def stop(self, timeout: float = 20.0) -> None:
        if self._thread is None:
            return
        self._stopping.set()
        self._queue.put(_SHUTDOWN)
        self._thread.join(timeout=timeout)
        self._thread = None
        log.info("브라우저 워커를 종료했습니다")

    # ── 작업 투입 ──
    def submit(self, fn: Callable[[CgvSession], Any], *, label: str) -> Future:
        """작업을 큐에 넣고 Future를 돌려준다. fn은 워커 스레드에서 실행된다."""
        if self._stopping.is_set():
            raise RuntimeError("워커가 종료 중입니다")
        job = Job(fn=fn, label=label)
        self._queue.put(job)
        return job.future

    def run(self, fn: Callable[[CgvSession], Any], *, label: str,
            timeout: float | None = None) -> Any:
        """작업을 넣고 결과를 기다린다. 작업 안에서 난 예외는 그대로 올라온다."""
        return self.submit(fn, label=label).result(timeout=timeout)

    # ── 상태 ──
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "current_job": self._current,
                "queue_depth": self._queue.qsize(),
                "jobs_done": self._jobs_done,
                "session_open": self._session is not None,
                "session_started_at": self._session_started,
                "session_jobs": self._session_jobs,
                "last_error": self._last_error,
            }

    # ── 내부 ──
    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                break
            self._execute(item)  # type: ignore[arg-type]
        self._close_session()

    def _execute(self, job: Job) -> None:
        if not job.future.set_running_or_notify_cancel():
            return
        with self._lock:
            self._current = job.label

        try:
            session = self._ensure_session()
        except Exception as exc:  # noqa: BLE001 - 브라우저 기동 실패 종류가 다양
            self._note_error(f"세션 준비 실패: {exc}")
            job.future.set_exception(exc)
            with self._lock:
                self._current = None
            return

        started = time.monotonic()
        try:
            result = job.fn(session)
        except Exception as exc:  # noqa: BLE001 - 작업 예외는 호출자에게 넘긴다
            self._note_error(f"{job.label}: {exc}")
            # 개별 API 실패로 브라우저를 버리면 손해다. 정말 죽었을 때만 닫는다.
            if not session.is_alive():
                log.warning("세션이 죽어 있어 닫습니다 (%s)", job.label)
                self._close_session()
            job.future.set_exception(exc)
        else:
            job.future.set_result(result)
            log.debug("%s 완료 (%.2f초)", job.label, time.monotonic() - started)
        finally:
            with self._lock:
                self._current = None
                self._jobs_done += 1
                self._session_jobs += 1

    def _ensure_session(self) -> CgvSession:
        if self._session is not None:
            if self._session_expired() or not self._session.is_alive():
                log.info("브라우저 세션을 다시 띄웁니다")
                self._close_session()

        if self._session is None:
            headless = bool(store.get_setting("headless", True))
            session = CgvSession(headless=headless)
            session.__enter__()  # 컨텍스트 매니저를 수동으로 열어 계속 들고 있는다
            with self._lock:
                self._session = session
                self._session_started = datetime.now().astimezone()
                self._session_jobs = 0
            log.info("브라우저 세션을 띄웠습니다 (headless=%s)", headless)
        return self._session

    def _session_expired(self) -> bool:
        """오래 떠 있던 세션은 갈아준다 — 메모리 누적과 좀비 프로세스를 끊는다.

        **결제가 진행 중이면 미룬다.** 재활용은 브라우저를 통째로 닫으므로 결제창을
        띄워 둔 탭도 함께 죽는다. 카카오페이 승인은 그 창이 받아 CGV에 넘겨야 예매가
        확정되니, 닫히면 **돈은 나가고 좌석은 안 잡힌다.**

        실측(2026-09-03)으로 두 번 그렇게 잃었다. 결제 링크를 보낸 뒤 15:19:18 →
        15:29:57(10분 39초 뒤), 16:59:21 → 17:00:06(**45초 뒤**)에 재활용이 돌았다.
        30분 주기와 결제 시한(15분)이 겹치는 건 우연이 아니라 흔한 일이다.

        미루는 시간은 결제 시한까지로 묶여 있다(paying_pages의 deadline). 그 시각이
        지나면 has_live_payment가 False가 되어 다음 판정에서 정상적으로 갈린다.
        """
        if self._session_started is None:
            return False
        minutes = int(store.get_setting("session_recycle_minutes", 30))
        age = (datetime.now().astimezone() - self._session_started).total_seconds()
        if age < minutes * 60:
            return False
        if self._session is not None and self._session.has_live_payment():
            if not self._recycle_deferred:
                self._recycle_deferred = True
                log.info("결제가 진행 중이라 브라우저 세션 재활용을 미룹니다 — "
                         "지금 닫으면 카카오페이 승인이 CGV까지 가지 않습니다.")
            return False
        if self._recycle_deferred:
            self._recycle_deferred = False
            log.info("결제 시한이 지나 미뤄 둔 브라우저 세션 재활용을 진행합니다.")
        return True

    def _close_session(self) -> None:
        session = self._session
        with self._lock:
            self._session = None
            self._session_started = None
            self._session_jobs = 0
        if session is not None:
            try:
                session.__exit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - 정리 실패는 로그만
                log.warning("세션 정리 중 오류: %s", exc)

    def _note_error(self, message: str) -> None:
        log.warning("%s", message)
        with self._lock:
            self._last_error = message
