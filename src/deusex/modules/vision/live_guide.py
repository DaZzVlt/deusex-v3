from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging
import threading
import uuid

from deusex.core.events import EventBus, LiveGuideStateChangedEvent, ScreenCapturedEvent
from deusex.modules.vision.capture import (
    CapturedFrame,
    MonitorSelector,
    ScreenCaptureService,
)

logger = logging.getLogger(__name__)


class LiveGuideState(StrEnum):
    OFF = "off"
    ACTIVE = "active"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class LiveGuideSnapshot:
    state: LiveGuideState
    session_id: str
    monitor: MonitorSelector
    turns_count: int
    captures_count: int
    last_capture_id: str | None = None


class LiveGuideController:
    """
    Controlador de Modo Guía Live estrictamente por turnos:
    - Sin timers ni bucles en segundo plano.
    - En estado `off` o `paused`: 0 capturas y 0 tokens garantizados.
    - Captura puntual exclusivamente cuando llega un turno del usuario estando en `active`.
    """

    def __init__(
        self,
        capture_service: ScreenCaptureService,
        events: EventBus | None = None,
    ) -> None:
        self._capture_service = capture_service
        self._events = events
        self._lock = threading.RLock()
        self._state: LiveGuideState = LiveGuideState.OFF
        self._session_id: str = ""
        self._monitor: MonitorSelector = "active"
        self._resolved_monitor_index: int = 0
        self._turns_count: int = 0
        self._captures_count: int = 0
        self._last_capture_id: str | None = None

    def bind_events(self, events: EventBus | None) -> None:
        with self._lock:
            self._events = events

    @property
    def state(self) -> LiveGuideState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state == LiveGuideState.ACTIVE

    def snapshot(self) -> LiveGuideSnapshot:
        with self._lock:
            return LiveGuideSnapshot(
                state=self._state,
                session_id=self._session_id,
                monitor=self._monitor,
                turns_count=self._turns_count,
                captures_count=self._captures_count,
                last_capture_id=self._last_capture_id,
            )

    def _publish_state(self) -> None:
        with self._lock:
            events = self._events
            ev = LiveGuideStateChangedEvent(
                state=self._state.value,
                session_id=self._session_id,
                monitor_index=self._resolved_monitor_index,
                turns_count=self._turns_count,
                captures_count=self._captures_count,
            )
        if events is not None:
            events.publish(ev)

    def start(
        self,
        *,
        monitor: MonitorSelector = "active",
        privacy_mode: bool = False,
    ) -> LiveGuideSnapshot:
        if privacy_mode:
            raise PermissionError(
                "No se puede iniciar el Modo Guía Live mientras el Modo Privacidad está activo."
            )
        with self._lock:
            if self._state == LiveGuideState.OFF:
                self._session_id = uuid.uuid4().hex[:12]
                self._turns_count = 0
                self._captures_count = 0
                self._last_capture_id = None
            self._monitor = monitor
            self._resolved_monitor_index = monitor if isinstance(monitor, int) else 0
            self._state = LiveGuideState.ACTIVE
        self._publish_state()
        logger.info("Guía Live activada (sesión=%s, monitor=%s).", self._session_id, monitor)
        return self.snapshot()

    def pause(self) -> LiveGuideSnapshot:
        with self._lock:
            if self._state == LiveGuideState.OFF:
                return self.snapshot()
            changed = self._state != LiveGuideState.PAUSED
            self._state = LiveGuideState.PAUSED
        if changed:
            self._publish_state()
            logger.info("Guía Live pausada (0 capturas y 0 tokens mientras dure la pausa).")
        return self.snapshot()

    def resume(self, *, privacy_mode: bool = False) -> LiveGuideSnapshot:
        if privacy_mode:
            raise PermissionError(
                "No se puede reanudar el Modo Guía Live mientras el Modo Privacidad está activo."
            )
        with self._lock:
            if self._state == LiveGuideState.OFF:
                return self.start(monitor=self._monitor, privacy_mode=privacy_mode)
            changed = self._state != LiveGuideState.ACTIVE
            self._state = LiveGuideState.ACTIVE
        if changed:
            self._publish_state()
            logger.info("Guía Live reanudada (sesión=%s).", self._session_id)
        return self.snapshot()

    def stop(self) -> LiveGuideSnapshot:
        with self._lock:
            was_running = self._state != LiveGuideState.OFF
            self._state = LiveGuideState.OFF
            self._session_id = ""
            self._resolved_monitor_index = 0
            self._turns_count = 0
            self._captures_count = 0
            self._last_capture_id = None
        if was_running:
            self._publish_state()
            logger.info("Guía Live detenida.")
        return self.snapshot()

    def handle_privacy_changed(self, enabled: bool) -> bool:
        """Pausa automáticamente la Guía Live al entrar en Modo Privacidad."""
        if not enabled:
            return False
        with self._lock:
            if self._state != LiveGuideState.ACTIVE:
                return False
            self._state = LiveGuideState.PAUSED
        self._publish_state()
        logger.info("Guía Live pausada automáticamente por Modo Privacidad.")
        return True

    async def capture_for_turn(self, *, privacy_mode: bool = False) -> CapturedFrame | None:
        """
        Captura un único frame para el turno actual del usuario.
        Devuelve `None` (0 capturas, 0 tokens) si la Guía Live está en `off`, `paused` o Privacidad.
        """
        with self._lock:
            if self._state != LiveGuideState.ACTIVE:
                return None
            monitor = self._monitor
            expected_session = self._session_id

        if privacy_mode:
            self.handle_privacy_changed(True)
            return None

        frame = await self._capture_service.capture(
            monitor=monitor,
            privacy_mode=privacy_mode,
            source="live_guide",
        )

        with self._lock:
            if self._state != LiveGuideState.ACTIVE or self._session_id != expected_session:
                return None
            self._turns_count += 1
            self._captures_count += 1
            self._resolved_monitor_index = frame.monitor_index
            self._last_capture_id = frame.capture_id
            events = self._events

        if events is not None:
            events.publish(
                ScreenCapturedEvent(
                    capture_id=frame.capture_id,
                    monitor_index=frame.monitor_index,
                    width=frame.width,
                    height=frame.height,
                    mime_type=frame.mime_type,
                    size_bytes=frame.size_bytes,
                    source="live_guide",
                )
            )
        self._publish_state()
        return frame
