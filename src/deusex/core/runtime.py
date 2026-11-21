from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import concurrent.futures
import contextlib
import ctypes
from dataclasses import dataclass
import logging
import queue
import sys
import threading
import time
from typing import Any, TypeVar

from deusex.core.events import EventBus

logger = logging.getLogger(__name__)

T = TypeVar("T")

_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1


class AsyncCoreThread:
    """Hilo #2: propietario exclusivo del bucle asyncio del núcleo de DeusEx v3."""

    def __init__(self, *, name: str = "AsyncCoreThread") -> None:
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                raise RuntimeError("AsyncCoreThread no está iniciado.")
            return self._loop

    @property
    def thread_id(self) -> int | None:
        with self._lock:
            return self._thread.ident if self._thread is not None else None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return (
                self._thread is not None
                and self._thread.is_alive()
                and self._loop is not None
                and self._loop.is_running()
            )

    def start(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run_loop, name=self._name, daemon=True)
            self._thread.start()

        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("AsyncCoreThread no inició dentro del tiempo límite.")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception as exc:
                logger.warning("Error menor durante drenado de AsyncCoreThread: %s", exc)
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                with self._lock:
                    self._loop = None

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        with self._lock:
            if self._loop is None or self._loop.is_closed() or not self._loop.is_running():
                coro.close()
                raise RuntimeError("AsyncCoreThread no está activo para recibir corrutinas.")
            return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run_sync(self, coro: Coroutine[Any, Any, T], *, timeout: float = 10.0) -> T:
        return self.submit(coro).result(timeout=timeout)

    def stop(self, timeout: float = 1.5) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread

        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

        with self._lock:
            self._thread = None


@dataclass(slots=True)
class _ComWorkItem:
    fn: Callable[[], Any]
    future: concurrent.futures.Future[Any]


class ComStaThread:
    """Hilo #3: Single-Threaded Apartment (STA) exclusivo para COM, pycaw, SAPI y UIA."""

    def __init__(self, *, name: str = "ComStaThread") -> None:
        self._name = name
        self._queue: queue.Queue[_ComWorkItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._com_initialized = False

    @property
    def thread_id(self) -> int | None:
        with self._lock:
            return self._thread.ident if self._thread is not None else None

    @property
    def com_initialized(self) -> bool:
        return self._com_initialized

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._worker_loop, name=self._name, daemon=True)
            self._thread.start()

        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("ComStaThread no inició dentro del tiempo límite.")

    def _init_com(self) -> bool:
        if sys.platform != "win32":
            return False
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        hr = int(windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
        if hr in (_S_OK, _S_FALSE):
            return True
        logger.warning("CoInitializeEx devolvió HRESULT=0x%08X en ComStaThread", hr & 0xFFFFFFFF)
        return False

    def _uninit_com(self) -> None:
        if sys.platform != "win32":
            return
        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            windll.ole32.CoUninitialize()

    def _worker_loop(self) -> None:
        com_ok = self._init_com()
        self._com_initialized = com_ok
        self._ready.set()
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if item.future.set_running_or_notify_cancel():
                    try:
                        result = item.fn()
                    except BaseException as exc:
                        item.future.set_exception(exc)
                    else:
                        item.future.set_result(result)
        finally:
            if com_ok:
                self._uninit_com()
                self._com_initialized = False

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> concurrent.futures.Future[T]:
        if not self.is_running:
            raise RuntimeError("ComStaThread no está activo.")
        fut: concurrent.futures.Future[T] = concurrent.futures.Future()
        self._queue.put(_ComWorkItem(fn=lambda: fn(*args, **kwargs), future=fut))
        return fut

    def run_sync(self, fn: Callable[..., T], *args: Any, timeout: float = 5.0, **kwargs: Any) -> T:
        return self.submit(fn, *args, **kwargs).result(timeout=timeout)

    async def run_async(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        fut = self.submit(fn, *args, **kwargs)
        return await asyncio.wrap_future(fut)

    def stop(self, timeout: float = 1.0) -> None:
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._queue.put(None)
        if thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None


class AudioInputThread:
    """Hilo #4: hilo dedicado para captura continua de micrófono y Vosk offline (Rebanada 4)."""

    def __init__(self, *, name: str = "AudioInputThread") -> None:
        self._name = name
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_event = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._worker: Callable[[threading.Event], None] | None = None

    @property
    def thread_id(self) -> int | None:
        with self._lock:
            return self._thread.ident if self._thread is not None else None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("AudioInputThread no inició dentro del tiempo límite.")

    def set_worker(self, worker: Callable[[threading.Event], None] | None) -> None:
        """Asigna o limpia la función de captura (sin crear hilos extra)."""
        with self._lock:
            self._worker = worker
            if worker is not None:
                self._active_event.set()
            else:
                self._active_event.clear()

    def _run(self) -> None:
        self._ready.set()
        while not self._stop_event.is_set():
            if not self._active_event.wait(timeout=0.15):
                continue
            if self._stop_event.is_set():
                break
            with self._lock:
                worker = self._worker
            if worker is None:
                self._active_event.clear()
                continue
            try:
                worker(self._stop_event)
            except Exception:
                logger.exception("Error en AudioInputThread; pausando captura para estabilidad.")
                self._active_event.clear()

    def stop(self, timeout: float = 0.8) -> None:
        self._stop_event.set()
        self._active_event.set()
        with self._lock:
            thread = self._thread
            self._worker = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None


class RuntimeServices:
    """Coordina los hilos de ejecución secundarios y garantiza cierre determinista <3 s."""

    def __init__(self, events: EventBus) -> None:
        self.events = events
        self.async_core = AsyncCoreThread()
        self.com_sta = ComStaThread()
        self.audio_input = AudioInputThread()
        self._shutdown_hooks: list[Callable[[], None]] = []
        self._lock = threading.RLock()
        self._started = False

    @property
    def is_started(self) -> bool:
        return self._started

    def register_shutdown_hook(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._shutdown_hooks.append(hook)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self.async_core.start()
            self.events.bind_loop(self.async_core.loop)
            self.com_sta.start()
            self.audio_input.start()
            self._started = True

    def submit_async(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return self.async_core.submit(coro)

    def shutdown(self, *, total_budget_sec: float = 2.5) -> float:
        """Apaga los 3 hilos secundarios y ejecuta hooks registrados en <3 s."""
        started_at = time.monotonic()
        with self._lock:
            hooks = list(reversed(self._shutdown_hooks))
            self._shutdown_hooks.clear()

        for hook in hooks:
            try:
                hook()
            except Exception:
                logger.exception("Error en hook de apagado %r", hook)

        self.audio_input.stop(timeout=min(0.6, total_budget_sec * 0.25))
        self.events.drain(timeout=min(0.5, total_budget_sec * 0.20))
        self.events.unbind_loop()
        self.async_core.stop(timeout=min(1.0, total_budget_sec * 0.40))
        self.com_sta.stop(timeout=min(0.6, total_budget_sec * 0.25))

        with self._lock:
            self._started = False

        elapsed = time.monotonic() - started_at
        logger.info("RuntimeServices detenido limpiamente en %.3f s", elapsed)
        return elapsed
