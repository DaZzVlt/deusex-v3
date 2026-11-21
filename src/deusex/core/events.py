from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import concurrent.futures
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import logging
import threading
from typing import Any, TypeVar
import uuid

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base inmutable para todos los eventos de dominio de DeusEx v3."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class AppStartedEvent(DomainEvent):
    reconciled_modules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppStoppingEvent(DomainEvent):
    reason: str = "user_request"


@dataclass(frozen=True, slots=True)
class ModuleStateChangedEvent(DomainEvent):
    module_id: str = ""
    old_state: str = ""
    new_state: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SettingsUpdatedEvent(DomainEvent):
    section: str = "all"


@dataclass(frozen=True, slots=True)
class PrivacyModeChangedEvent(DomainEvent):
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ErrorOccurredEvent(DomainEvent):
    source: str = ""
    message_es: str = ""
    technical_detail: str = ""


E = TypeVar("E", bound=DomainEvent)


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    call_attr = getattr(fn, "__call__", None)  # noqa: B004
    return inspect.iscoroutinefunction(call_attr)


@dataclass(frozen=True, slots=True)
class _Subscription:
    event_type: type[DomainEvent]
    handler: Callable[[Any], Any]
    is_async: bool


class EventBus:
    """
    Bus de eventos thread-safe con Antídoto #5:
    toda corrutina suscrita se despacha estrictamente al event loop de `AsyncCoreThread`.
    Jamás depende de un event loop corriendo en QtMainThread, ComStaThread ni AudioInputThread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: list[_Subscription] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_futures: set[concurrent.futures.Future[Any]] = set()
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_loop_bound(self) -> bool:
        with self._lock:
            return self._loop is not None and not self._loop.is_closed()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def unbind_loop(self) -> None:
        with self._lock:
            self._loop = None

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], None] | Callable[[E], Awaitable[None]],
    ) -> Callable[[], None]:
        sub = _Subscription(
            event_type=event_type,
            handler=handler,
            is_async=_is_async_callable(handler),
        )
        with self._lock:
            self._subscriptions.append(sub)

        def _unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._subscriptions.remove(sub)

        return _unsubscribe

    def _matching_subscriptions(self, event: DomainEvent) -> tuple[list[_Subscription], asyncio.AbstractEventLoop | None]:
        with self._lock:
            matched = [sub for sub in self._subscriptions if isinstance(event, sub.event_type)]
            loop = self._loop
        return matched, loop

    async def _invoke_async_safe(self, handler: Callable[[Any], Awaitable[Any]], event: DomainEvent) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "Error en handler asíncrono %r para evento %s",
                handler,
                type(event).__name__,
            )

    def _track_future(self, fut: concurrent.futures.Future[Any]) -> None:
        with self._lock:
            self._pending_futures.add(fut)

        def _done(done_fut: concurrent.futures.Future[Any]) -> None:
            with self._lock:
                self._pending_futures.discard(done_fut)
            if not done_fut.cancelled():
                exc = done_fut.exception()
                if exc is not None:
                    logger.error("Excepción no controlada en futuro de EventBus: %s", exc)

        fut.add_done_callback(_done)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        with self._lock:
            self._pending_tasks.add(task)

        def _done(done_task: asyncio.Task[Any]) -> None:
            with self._lock:
                self._pending_tasks.discard(done_task)
            if not done_task.cancelled():
                exc = done_task.exception()
                if exc is not None:
                    logger.error("Excepción no controlada en tarea de EventBus: %s", exc)

        task.add_done_callback(_done)

    def publish(self, event: DomainEvent) -> None:
        """Publica un evento de forma segura desde cualquiera de los 4 hilos."""
        matched, loop = self._matching_subscriptions(event)
        if not matched:
            return

        running_loop: asyncio.AbstractEventLoop | None
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        for sub in matched:
            if not sub.is_async:
                try:
                    sub.handler(event)
                except Exception:
                    logger.exception(
                        "Error en handler síncrono %r para evento %s",
                        sub.handler,
                        type(event).__name__,
                    )
                continue

            if loop is None or loop.is_closed() or not loop.is_running():
                logger.warning(
                    "Evento %s omitido para handler async %r: AsyncCoreThread no está activo.",
                    type(event).__name__,
                    sub.handler,
                )
                continue

            coro = self._invoke_async_safe(sub.handler, event)
            if running_loop is loop:
                task = loop.create_task(coro)
                self._track_task(task)
            else:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                self._track_future(fut)

    async def publish_and_wait(self, event: DomainEvent, timeout: float = 5.0) -> None:  # noqa: ASYNC109
        """Publica un evento y espera a que completen todos sus handlers síncronos y asíncronos."""
        matched, loop = self._matching_subscriptions(event)
        if not matched:
            return

        current_loop = asyncio.get_running_loop()
        awaitables: list[Awaitable[Any]] = []

        for sub in matched:
            if not sub.is_async:
                try:
                    sub.handler(event)
                except Exception:
                    logger.exception(
                        "Error en handler síncrono %r para evento %s",
                        sub.handler,
                        type(event).__name__,
                    )
                continue

            coro = self._invoke_async_safe(sub.handler, event)
            if loop is not None and loop.is_running() and loop is not current_loop:
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                awaitables.append(asyncio.wrap_future(fut))
            else:
                awaitables.append(coro)

        if awaitables:
            await asyncio.wait_for(
                asyncio.gather(*awaitables, return_exceptions=True),
                timeout=timeout,
            )

    def drain(self, timeout: float = 1.0) -> None:
        """Espera brevemente a que finalicen futuros despachados entre hilos antes de cerrar."""
        with self._lock:
            futures = list(self._pending_futures)
        if not futures:
            return
        concurrent.futures.wait(futures, timeout=timeout)
