from __future__ import annotations

from collections.abc import Callable
import threading

from PySide6.QtCore import QObject, Signal

from deusex.core.events import (
    AppStartedEvent,
    AppStoppingEvent,
    AudioLevelEvent,
    ChatMessageEvent,
    CostUsageUpdatedEvent,
    ErrorOccurredEvent,
    EventBus,
    FastPathExecutedEvent,
    HotkeyTriggeredEvent,
    InCallStateChangedEvent,
    MemorySavedEvent,
    MicLevelEvent,
    MicMuteChangedEvent,
    ModuleStateChangedEvent,
    OrbStateChangedEvent,
    PrivacyModeChangedEvent,
    ProfileChangedEvent,
    SafetyConfirmationRequestedEvent,
    SafetyConfirmationResolvedEvent,
    ScheduledJobFiredEvent,
    ScheduledJobsUpdatedEvent,
    SettingsUpdatedEvent,
    SubtitleUpdatedEvent,
    VoiceCalibrationEvent,
    VoiceMuteChangedEvent,
    WakeWordDetectedEvent,
)


class QtEventBridge(QObject):
    """
    Único puente thread-safe entre `EventBus` (4 hilos) y `QtMainThread`.
    Las señales emitidas desde hilos secundarios son encoladas automáticamente por Qt
    hacia los slots de la interfaz sin tocar `asyncio` desde `QtMainThread`.
    """

    app_started = Signal(object)
    app_stopping = Signal(object)
    module_state_changed = Signal(object)
    settings_updated = Signal(object)
    privacy_mode_changed = Signal(bool)
    error_occurred = Signal(object)
    safety_confirmation_requested = Signal(object)
    safety_confirmation_resolved = Signal(object)
    scheduled_job_fired = Signal(object)
    scheduled_jobs_updated = Signal(int)
    fast_path_executed = Signal(object)
    chat_message_received = Signal(object)
    memory_saved = Signal(object)
    cost_usage_updated = Signal(object)
    profile_changed = Signal(object)
    voice_mute_changed = Signal(bool)
    mic_mute_changed = Signal(bool)
    in_call_state_changed = Signal(bool, object)
    hotkey_triggered = Signal(str, str)
    orb_state_changed = Signal(object)
    audio_level_changed = Signal(str, float)
    mic_level_changed = Signal(float, bool, object)
    subtitle_updated = Signal(object)
    wake_word_detected = Signal(str, str)
    voice_calibration_updated = Signal(object)

    def __init__(self, events: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._events = events
        self._lock = threading.RLock()
        self._detached = False
        self._unsubscribers: list[Callable[[], None]] = []
        self._attach()

    def _safe_emit(self, emitter: Callable[[], None]) -> None:
        with self._lock:
            if self._detached:
                return
            try:
                emitter()
            except RuntimeError:
                # El QObject ya fue destruido durante el cierre.
                self._detached = True

    def _attach(self) -> None:
        self._unsubscribers = [
            self._events.subscribe(
                AppStartedEvent,
                lambda e: self._safe_emit(lambda: self.app_started.emit(e)),
            ),
            self._events.subscribe(
                AppStoppingEvent,
                lambda e: self._safe_emit(lambda: self.app_stopping.emit(e)),
            ),
            self._events.subscribe(
                ModuleStateChangedEvent,
                lambda e: self._safe_emit(lambda: self.module_state_changed.emit(e)),
            ),
            self._events.subscribe(
                SettingsUpdatedEvent,
                lambda e: self._safe_emit(lambda: self.settings_updated.emit(e)),
            ),
            self._events.subscribe(
                PrivacyModeChangedEvent,
                lambda e: self._safe_emit(lambda: self.privacy_mode_changed.emit(e.enabled)),
            ),
            self._events.subscribe(
                ErrorOccurredEvent,
                lambda e: self._safe_emit(lambda: self.error_occurred.emit(e)),
            ),
            self._events.subscribe(
                SafetyConfirmationRequestedEvent,
                lambda e: self._safe_emit(lambda: self.safety_confirmation_requested.emit(e)),
            ),
            self._events.subscribe(
                SafetyConfirmationResolvedEvent,
                lambda e: self._safe_emit(lambda: self.safety_confirmation_resolved.emit(e)),
            ),
            self._events.subscribe(
                ScheduledJobFiredEvent,
                lambda e: self._safe_emit(lambda: self.scheduled_job_fired.emit(e)),
            ),
            self._events.subscribe(
                ScheduledJobsUpdatedEvent,
                lambda e: self._safe_emit(lambda: self.scheduled_jobs_updated.emit(e.pending_count)),
            ),
            self._events.subscribe(
                FastPathExecutedEvent,
                lambda e: self._safe_emit(lambda: self.fast_path_executed.emit(e)),
            ),
            self._events.subscribe(
                ChatMessageEvent,
                lambda e: self._safe_emit(lambda: self.chat_message_received.emit(e)),
            ),
            self._events.subscribe(
                MemorySavedEvent,
                lambda e: self._safe_emit(lambda: self.memory_saved.emit(e)),
            ),
            self._events.subscribe(
                CostUsageUpdatedEvent,
                lambda e: self._safe_emit(lambda: self.cost_usage_updated.emit(e)),
            ),
            self._events.subscribe(
                ProfileChangedEvent,
                lambda e: self._safe_emit(lambda: self.profile_changed.emit(e)),
            ),
            self._events.subscribe(
                VoiceMuteChangedEvent,
                lambda e: self._safe_emit(lambda: self.voice_mute_changed.emit(e.muted)),
            ),
            self._events.subscribe(
                MicMuteChangedEvent,
                lambda e: self._safe_emit(lambda: self.mic_mute_changed.emit(e.muted)),
            ),
            self._events.subscribe(
                InCallStateChangedEvent,
                lambda e: self._safe_emit(lambda: self.in_call_state_changed.emit(e.active, e.apps)),
            ),
            self._events.subscribe(
                HotkeyTriggeredEvent,
                lambda e: self._safe_emit(lambda: self.hotkey_triggered.emit(e.hotkey_id, e.combo)),
            ),
            self._events.subscribe(
                OrbStateChangedEvent,
                lambda e: self._safe_emit(lambda: self.orb_state_changed.emit(e)),
            ),
            self._events.subscribe(
                AudioLevelEvent,
                lambda e: self._safe_emit(lambda: self.audio_level_changed.emit(e.source, e.level)),
            ),
            self._events.subscribe(
                MicLevelEvent,
                lambda e: self._safe_emit(
                    lambda: self.mic_level_changed.emit(e.level, e.is_speech, e.device_index)
                ),
            ),
            self._events.subscribe(
                SubtitleUpdatedEvent,
                lambda e: self._safe_emit(lambda: self.subtitle_updated.emit(e)),
            ),
            self._events.subscribe(
                WakeWordDetectedEvent,
                lambda e: self._safe_emit(
                    lambda: self.wake_word_detected.emit(e.wake_word, e.triggered_by)
                ),
            ),
            self._events.subscribe(
                VoiceCalibrationEvent,
                lambda e: self._safe_emit(lambda: self.voice_calibration_updated.emit(e)),
            ),
        ]

    def detach(self) -> None:
        """Desuscribe todos los handlers del EventBus antes de cerrar la UI."""
        with self._lock:
            self._detached = True
            unsubs = list(self._unsubscribers)
            self._unsubscribers.clear()
        for unsub in unsubs:
            unsub()
