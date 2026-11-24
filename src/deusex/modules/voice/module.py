from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
import importlib.util
import logging
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Final

from deusex.core.contracts import (
    BaseModule,
    DiagnosticItem,
    DiagnosticSeverity,
    FastPathRule,
    ModuleContext,
    ModuleState,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from deusex.core.events import (
    AudioLevelEvent,
    InCallStateChangedEvent,
    MicLevelEvent,
    MicMuteChangedEvent,
    OrbStateChangedEvent,
    PrivacyModeChangedEvent,
    ProfileChangedEvent,
    SafetyConfirmationRequestedEvent,
    SafetyConfirmationResolvedEvent,
    SettingsUpdatedEvent,
    SubtitleUpdatedEvent,
    VoiceCalibrationEvent,
    VoiceMuteChangedEvent,
    WakeWordDetectedEvent,
)
from deusex.core.permissions import Permission
from deusex.modules.voice.context_gate import ContextDecision, ContextGate
from deusex.modules.voice.dsp import FarFieldAudioConditioner
from deusex.modules.voice.ducking import AudioSessionBackend, SelectiveAudioDucker
from deusex.modules.voice.phonetics import (
    WakePhoneticMatcher,
    clean_brand_articles,
    is_third_person_wake_mention,
    strip_accents,
)
from deusex.modules.voice.stt import (
    AudioInputDevice,
    KeyStatePoller,
    SpeechRecognizer,
    VoiceInputEngine,
    VoiceUtterance,
    is_vosk_model_present,
    list_input_devices,
)
from deusex.modules.voice.tts import (
    AudioPlaybackBackend,
    EdgeTTSSynthesizer,
    SapiBackend,
    TTSResult,
    VoiceTTSEngine,
)
from deusex.modules.voice.voiceprint import OwnerVoiceprintGuard

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceCommandReply:
    """Respuesta simplificada devuelta por el orquestador cerebral para ser locutada."""

    text: str
    spoken_text: str | None = None
    should_speak: bool = True


@dataclass(frozen=True, slots=True)
class ProfileVoiceInfo:
    """Metadatos vocales del perfil activo inyectados sin acoplamiento circular."""

    profile_id: str = "raphael"
    wake_words: tuple[str, ...] = ("raphael", "deusex")
    default_voice: str = "es-CL-CatalinaNeural"
    accent_hex: str = "#38BDF8"


type VoiceCommandCallback = Callable[[str, bool], Awaitable[VoiceCommandReply | None]]
type ProfileInfoProvider = Callable[[], ProfileVoiceInfo]
type AudioTranscriber = Callable[[bytes], Awaitable[tuple[str, str] | None]]
type FastPathMatcher = Callable[[str], bool]
type SafetyResolver = Callable[[str, bool, str], bool]

_SAFETY_APPROVAL_WORDS: Final[frozenset[str]] = frozenset(
    {
        "sí", "si", "confirmo", "confirmar", "confirma",
        "aprobar", "aprueba", "adelante", "dale", "hazlo",
        "aceptar", "acepto", "ok", "claro",
    }
)

_SAFETY_APPROVAL_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "sí confirmo", "si confirmo", "confirmo la acción", "confirmo la accion",
        "confirma la acción", "confirma la accion", "sí hazlo", "si hazlo",
        "sí adelante", "si adelante", "claro que sí", "claro que si",
    }
)

_SAFETY_REJECTION_WORDS: Final[frozenset[str]] = frozenset(
    {
        "no", "cancela", "cancelar", "rechaza", "rechazar",
        "anular", "anula", "mejor no", "olvídalo", "olvidalo", "detente",
    }
)

_SAFETY_REJECTION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "no cancela", "cancela la acción", "cancela la accion", "no lo hagas",
        "mejor no lo hagas", "déjalo", "dejalo",
    }
)


def _check_safety_intent(text: str) -> tuple[bool, bool]:
    """Retorna (is_approval, is_rejection) evaluando texto limpio y normalizado."""
    norm = strip_accents(text.lower().strip())
    if not norm:
        return False, False
    if norm in _SAFETY_APPROVAL_WORDS or norm in _SAFETY_APPROVAL_PHRASES:
        return True, False
    if norm in _SAFETY_REJECTION_WORDS or norm in _SAFETY_REJECTION_PHRASES:
        return False, True
    words = set(re.findall(r"\b[a-z0-9]+\b", norm))
    if words & _SAFETY_APPROVAL_WORDS:
        return True, False
    if words & _SAFETY_REJECTION_WORDS:
        return False, True
    return False, False


def _format_safety_spoken_prompt(event: SafetyConfirmationRequestedEvent) -> str:
    """Genera una pregunta hablada concisa y natural, sin jerga de código ni paréntesis."""
    summary = event.summary_es or event.args_preview or event.tool_name
    cleaned = re.sub(r"\(.*?\)", "", summary).strip()
    cleaned = re.sub(r"[a-zA-Z]:\\[^\s]+", lambda m: Path(m.group(0)).name, cleaned)
    cleaned = re.sub(r"/(?:[^\s]+/)+([^\s]+)", r"\1", cleaned)
    first_clause = re.split(r"(?:\.\s+|\n|;)", cleaned)[0].strip()
    first_clause = re.sub(r"\s+", " ", first_clause).strip().rstrip(".").strip()
    if first_clause:
        first_clause = first_clause[0].lower() + first_clause[1:]
    else:
        first_clause = "ejecutar esta acción"
    return f"¿Confirmo {first_clause}? Di sí o no."


class VoiceModule(BaseModule):
    """
    Módulo enchufable de Voz y Orbe (Rebanada 4):
    - Escucha continua Vosk ES + DSP + PTT Win32 en `AudioInputThread`.
    - Escudo contextual multicapa + biometría vocal 26D + ducking selectivo en `ComStaThread`.
    - Síntesis Edge-TTS + `WordBoundary` + SAPI fallback + *barge-in* instantáneo.
    - Permisos estrictos: `Permission.VOICE_LISTEN` y `Permission.VOICE_SPEAK` (Antídoto #3).
    """

    module_id = "voice"
    display_name = "Voz y Orbe"
    description = (
        "Escucha offline Vosk ES, PTT Win32, escudo contextual, biometría vocal 26D, "
        "Edge-TTS con subtítulos sincronizados y ducking selectivo."
    )
    required_permissions = (Permission.VOICE_LISTEN, Permission.VOICE_SPEAK)

    def __init__(
        self,
        *,
        on_voice_command: VoiceCommandCallback | None = None,
        profile_info_provider: ProfileInfoProvider | None = None,
        on_set_voice_muted: Callable[[bool], None] | None = None,
        on_save_settings: Callable[[str], None] | None = None,
        audio_session_backend: AudioSessionBackend | None = None,
        edge_synthesizer: EdgeTTSSynthesizer | None = None,
        playback_backend: AudioPlaybackBackend | None = None,
        sapi_backend: SapiBackend | None = None,
        recognizer_factory: Callable[[Any, int], SpeechRecognizer] | None = None,
        key_poller: KeyStatePoller | None = None,
        auto_start_capture: bool = True,
        audio_transcriber: AudioTranscriber | None = None,
        fast_path_matcher: FastPathMatcher | None = None,
        vocabulary_provider: Callable[[], tuple[str, ...]] | None = None,
        on_resolve_safety: SafetyResolver | None = None,
    ) -> None:
        super().__init__()
        self._on_voice_command = on_voice_command
        self._profile_info_provider = profile_info_provider
        self._on_set_voice_muted = on_set_voice_muted
        self._on_save_settings = on_save_settings
        self._audio_session_backend = audio_session_backend
        self._edge_synthesizer = edge_synthesizer
        self._playback_backend = playback_backend
        self._sapi_backend = sapi_backend
        self._recognizer_factory = recognizer_factory
        self._key_poller = key_poller
        self._auto_start_capture = auto_start_capture
        self._audio_transcriber = audio_transcriber
        self._fast_path_matcher = fast_path_matcher
        self._vocabulary_provider = vocabulary_provider
        self._on_resolve_safety = on_resolve_safety
        self._pending_confirmation: SafetyConfirmationRequestedEvent | None = None

        self._ctx: ModuleContext | None = None
        self._conditioner: FarFieldAudioConditioner | None = None
        self._voiceprint: OwnerVoiceprintGuard | None = None
        self._ducker: SelectiveAudioDucker | None = None
        self._context_gate: ContextGate | None = None
        self._stt: VoiceInputEngine | None = None
        self._tts: VoiceTTSEngine | None = None
        self._unsubscribers: list[Callable[[], None]] = []
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._lock = threading.RLock()
        self._utterance_lock = asyncio.Lock()
        self._post_tts_cooldown_until: float = 0.0
        self._orb_state = "idle"
        self._last_call_active: bool | None = None
        self._last_call_apps: tuple[str, ...] | None = None

    @property
    def voiceprint(self) -> OwnerVoiceprintGuard | None:
        return self._voiceprint

    @property
    def context_gate(self) -> ContextGate | None:
        return self._context_gate

    @property
    def ducker(self) -> SelectiveAudioDucker | None:
        return self._ducker

    @property
    def stt(self) -> VoiceInputEngine | None:
        return self._stt

    @property
    def tts(self) -> VoiceTTSEngine | None:
        return self._tts

    def bind_callbacks(
        self,
        *,
        on_voice_command: VoiceCommandCallback | None = None,
        profile_info_provider: ProfileInfoProvider | None = None,
        on_set_voice_muted: Callable[[bool], None] | None = None,
        on_save_settings: Callable[[str], None] | None = None,
        audio_transcriber: AudioTranscriber | None = None,
        fast_path_matcher: FastPathMatcher | None = None,
        on_resolve_safety: SafetyResolver | None = None,
    ) -> None:
        if on_voice_command is not None:
            self._on_voice_command = on_voice_command
        if profile_info_provider is not None:
            self._profile_info_provider = profile_info_provider
        if on_set_voice_muted is not None:
            self._on_set_voice_muted = on_set_voice_muted
        if on_save_settings is not None:
            self._on_save_settings = on_save_settings
        if audio_transcriber is not None:
            self._audio_transcriber = audio_transcriber
        if fast_path_matcher is not None:
            self._fast_path_matcher = fast_path_matcher
        if on_resolve_safety is not None:
            self._on_resolve_safety = on_resolve_safety

    def _current_profile_info(self) -> ProfileVoiceInfo:
        if self._profile_info_provider is not None:
            with contextlib.suppress(Exception):
                return self._profile_info_provider()
        return ProfileVoiceInfo()

    def _emit_orb_state(
        self,
        state: str,
        *,
        visible: bool | None = None,
        preview_sec: float = 0.0,
    ) -> None:
        if self._ctx is None:
            return
        prof = self._current_profile_info()
        if self._ctx.settings.general.privacy_mode:
            effective_state = "privacy"
            effective_visible = False
        else:
            effective_state = state
            if visible is not None:
                effective_visible = visible
            elif effective_state == "idle":
                effective_visible = self._ctx.settings.ui.orb_visibility == "always_subtle"
            else:
                effective_visible = True

        self._orb_state = effective_state
        self._ctx.events.publish(
            OrbStateChangedEvent(
                state=effective_state,
                visible=effective_visible,
                accent_hex=prof.accent_hex,
                preview_sec=preview_sec,
            )
        )

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self._conditioner = FarFieldAudioConditioner()
        self._voiceprint = OwnerVoiceprintGuard(ctx.paths.user_data_dir)
        self._ducker = SelectiveAudioDucker(
            ctx.runtime.com_sta,
            default_level_pct=ctx.settings.voice.ducking_level_pct,
            backend=self._audio_session_backend,
            smooth_ramp=self._audio_session_backend is None,
        )
        self._context_gate = ContextGate(
            ctx.settings,
            self._ducker,
            self._voiceprint,
        )
        self._tts = VoiceTTSEngine(
            paths=ctx.paths,
            settings=ctx.settings,
            com_sta=ctx.runtime.com_sta,
            ducker=self._ducker,
            on_subtitle_word=self._on_tts_subtitle,
            on_tts_level=self._on_tts_level,
            edge_synthesizer=self._edge_synthesizer,
            playback_backend=self._playback_backend,
            sapi_backend=self._sapi_backend,
        )
        self._stt = VoiceInputEngine(
            paths=ctx.paths,
            settings=ctx.settings,
            audio_thread=ctx.runtime.audio_input,
            conditioner=self._conditioner,
            voiceprint=self._voiceprint,
            context_gate=self._context_gate,
            on_utterance=self._on_utterance_from_audio_thread,
            on_wake_word=self._on_wake_word_from_audio_thread,
            on_mic_level=self._on_mic_level_from_audio_thread,
            on_barge_in=self._on_barge_in_from_audio_thread,
            is_tts_speaking=lambda: self._tts.is_speaking if self._tts is not None else False,
            on_calibration_event=self._on_calibration_from_audio_thread,
            on_capture_cancelled=self._on_capture_cancelled_from_audio_thread,
            recognizer_factory=self._recognizer_factory,
            key_poller=self._key_poller,
        )

        prof = self._current_profile_info()
        self._stt.set_wake_words(prof.wake_words)
        is_suspended = ctx.settings.general.privacy_mode or ctx.settings.voice.mic_muted
        self._stt.set_suspended(is_suspended)

        self._unsubscribers = [
            ctx.events.subscribe(PrivacyModeChangedEvent, self._on_privacy_changed),
            ctx.events.subscribe(VoiceMuteChangedEvent, self._on_voice_mute_changed),
            ctx.events.subscribe(MicMuteChangedEvent, self._on_mic_mute_changed),
            ctx.events.subscribe(ProfileChangedEvent, self._on_profile_changed),
            ctx.events.subscribe(SettingsUpdatedEvent, self._on_settings_updated),
            ctx.events.subscribe(SafetyConfirmationRequestedEvent, self._on_safety_confirmation_requested),
            ctx.events.subscribe(SafetyConfirmationResolvedEvent, self._on_safety_confirmation_resolved),
        ]

        if self._auto_start_capture and ctx.settings.is_permission_enabled(Permission.VOICE_LISTEN):
            self._stt.start()

        self.check_and_publish_call_state()
        poll_task = asyncio.create_task(self._poll_call_state_loop())
        with self._lock:
            self._active_tasks.add(poll_task)
        poll_task.add_done_callback(lambda t: self._active_tasks.discard(t))

        self._emit_orb_state("privacy" if ctx.settings.general.privacy_mode else "idle")
        if self._auto_start_capture and not is_vosk_model_present(ctx.paths.vosk_es_dir) and self._recognizer_factory is None:
            self.set_state(ModuleState.DEGRADED, "Modelo Vosk ES no instalado en USER_DATA_DIR/models/vosk-es.")
        else:
            self.set_state(ModuleState.ACTIVE)

    async def stop(self) -> None:
        """Detiene escucha, TTS, ducking y tareas pendientes limpiamente en <1 s."""
        for unsub in list(self._unsubscribers):
            with contextlib.suppress(Exception):
                unsub()
        self._unsubscribers.clear()

        if self._stt is not None:
            self._stt.cancel_calibration()
            self._stt.stop(timeout_sec=0.6)

        if self._tts is not None:
            self._tts.cancel()

        if self._ducker is not None:
            self._ducker.stop()

        if self._context_gate is not None:
            self._context_gate.close_follow_up_window()

        self._pending_confirmation = None

        with self._lock:
            tasks = list(self._active_tasks)
            self._active_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=0.4)

        self._emit_orb_state("idle", visible=False)
        self.set_state(ModuleState.DISABLED)

    # ------------------------------------------------------------------
    # Reacciones a eventos del núcleo
    # ------------------------------------------------------------------
    def _on_privacy_changed(self, event: PrivacyModeChangedEvent) -> None:
        if self._stt is not None:
            is_mic_muted = bool(self._ctx and self._ctx.settings.voice.mic_muted)
            self._stt.set_suspended(event.enabled or is_mic_muted)
            if event.enabled:
                self._stt.cancel_calibration()
        if event.enabled:
            if self._context_gate is not None:
                self._context_gate.close_follow_up_window()
            if self._tts is not None:
                self._tts.cancel()
            self._emit_orb_state("privacy", visible=False)
        else:
            self._emit_orb_state("idle")

    def _on_voice_mute_changed(self, event: VoiceMuteChangedEvent) -> None:
        if event.muted and self._tts is not None:
            self._tts.cancel()

    def _on_mic_mute_changed(self, event: MicMuteChangedEvent) -> None:
        if self._stt is not None:
            is_privacy = bool(self._ctx and self._ctx.settings.general.privacy_mode)
            self._stt.set_suspended(event.muted or is_privacy)
            if event.muted:
                self._stt.cancel_calibration()

    def check_and_publish_call_state(self, *, force_refresh: bool = False) -> bool:
        """Inspecciona sesiones de audio Windows y publica InCallStateChangedEvent si hay cambios."""
        if self._ctx is None or self._context_gate is None:
            return False
        snap = self._context_gate.get_audio_snapshot(force_refresh=force_refresh)
        in_call = bool(self._ctx.settings.voice.in_call_text_only or snap.is_call_active)
        if self._last_call_active != in_call or self._last_call_apps != snap.active_call_apps:
            self._last_call_active = in_call
            self._last_call_apps = snap.active_call_apps
            logger.info(
                "Transición Modo En Llamada: active=%s apps=%s",
                in_call,
                list(snap.active_call_apps),
            )
            self._ctx.events.publish(
                InCallStateChangedEvent(
                    active=in_call,
                    apps=snap.active_call_apps,
                )
            )
        return in_call

    async def _poll_call_state_loop(self) -> None:
        """Sondeo periódico ultraligero (cada 2.5 s) para detectar entrada o salida de llamadas."""
        while True:
            try:
                await asyncio.sleep(2.5)
                self.check_and_publish_call_state()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Error menor en sondeo de estado de llamada: %s", exc)

    def _on_profile_changed(self, _event: ProfileChangedEvent) -> None:
        prof = self._current_profile_info()
        if self._stt is not None:
            self._stt.set_wake_words(prof.wake_words)
        self._emit_orb_state(self._orb_state)

    def _on_settings_updated(self, event: SettingsUpdatedEvent) -> None:
        if self._ctx is None or event.section not in ("all", "voice", "ui", "permissions"):
            return
        if self._ducker is not None:
            self._ducker.set_default_level_pct(self._ctx.settings.voice.ducking_level_pct)
        if self._stt is not None:
            prof = self._current_profile_info()
            self._stt.set_wake_words(prof.wake_words)
        if self._ctx.settings.voice.in_call_text_only and self._context_gate is not None:
            self._context_gate.close_follow_up_window()
        self.check_and_publish_call_state(force_refresh=True)
        if event.section == "ui":
            self._emit_orb_state(self._orb_state)

    def _on_safety_confirmation_requested(self, event: SafetyConfirmationRequestedEvent) -> None:
        self._pending_confirmation = event
        if self._ctx is None or self._ctx.settings.general.privacy_mode:
            return

        can_speak = (
            self._tts is not None
            and self._ctx.settings.is_permission_enabled(Permission.VOICE_SPEAK)
            and not self._ctx.settings.voice.tts_muted
            and not (self._context_gate is not None and self._context_gate.is_in_call_mode())
        )
        prompt = _format_safety_spoken_prompt(event)
        if can_speak:
            async def _speak_and_listen() -> None:
                await self.speak_text(prompt)
                self._post_tts_cooldown_until = 0.0
                if self._context_gate is not None:
                    self._context_gate.open_follow_up_window(
                        duration_sec=min(20.0, float(event.timeout_sec)),
                        force_for_confirmation=True,
                    )
                self._emit_orb_state("listening", visible=True)

            task = asyncio.create_task(_speak_and_listen())
            with self._lock:
                self._active_tasks.add(task)
            task.add_done_callback(lambda t: self._active_tasks.discard(t))
        else:
            if self._context_gate is not None:
                self._context_gate.open_follow_up_window(
                    duration_sec=min(20.0, float(event.timeout_sec)),
                    force_for_confirmation=True,
                )
            self._emit_orb_state("listening", visible=True)

    def _on_safety_confirmation_resolved(self, event: SafetyConfirmationResolvedEvent) -> None:
        if self._pending_confirmation and self._pending_confirmation.confirmation_id == event.confirmation_id:
            self._pending_confirmation = None
            if self._context_gate is not None:
                self._context_gate.close_follow_up_window()
            self._emit_orb_state("idle")

    # ------------------------------------------------------------------
    # Callbacks desde AudioInputThread (Antídoto #5: cero create_task directo)
    # ------------------------------------------------------------------
    def _on_mic_level_from_audio_thread(
        self,
        level: float,
        is_speech: bool,
        device_index: int | None,
    ) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(
            MicLevelEvent(level=level, is_speech=is_speech, device_index=device_index)
        )
        if self._orb_state in ("listening", "calibrating"):
            self._ctx.events.publish(AudioLevelEvent(source="mic", level=level))

    def _on_wake_word_from_audio_thread(self, wake_word: str, triggered_by: str) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(
            WakeWordDetectedEvent(wake_word=wake_word, triggered_by=triggered_by)
        )
        self._emit_orb_state("listening", visible=True)

    def _on_barge_in_from_audio_thread(self) -> None:
        if self._tts is not None:
            self._tts.cancel()

    def _on_calibration_from_audio_thread(self, status: str, progress: float, message: str) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(
            VoiceCalibrationEvent(status=status, progress=progress, message=message)
        )
        if status in ("started", "progress"):
            self._emit_orb_state("calibrating", visible=True)
        else:
            self._emit_orb_state("idle")

    def _on_capture_cancelled_from_audio_thread(self) -> None:
        self._emit_orb_state("idle")

    def _on_utterance_from_audio_thread(self, utterance: VoiceUtterance) -> None:
        """Enruta la transcripción a `AsyncCoreThread` de forma segura (Antídoto #5)."""
        if self._ctx is None or not self._ctx.runtime.async_core.is_running:
            return
        self._ctx.runtime.submit_async(self.handle_utterance(utterance))

    def _on_tts_subtitle(self, text: str, word: str, is_final: bool, clear: bool) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(
            SubtitleUpdatedEvent(text=text, word=word, is_final=is_final, clear=clear)
        )

    def _on_tts_level(self, level: float) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(AudioLevelEvent(source="tts", level=level))

    # ------------------------------------------------------------------
    # Pipeline de voz en AsyncCoreThread
    # ------------------------------------------------------------------
    async def handle_utterance(self, utterance: VoiceUtterance) -> ContextDecision:
        """Evalúa el escudo contextual, llama al cerebro y sintetiza respuesta si corresponde."""
        if self._ctx is None or self._context_gate is None:
            return ContextDecision(
                allow=False,
                reason="module_not_started",
                text_only=True,
                is_follow_up=False,
                multimedia_active=False,
                in_call_active=False,
                cleaned_transcript="",
            )

        if not self._ctx.settings.is_permission_enabled(Permission.VOICE_LISTEN):
            self._emit_orb_state("idle")
            return ContextDecision(
                allow=False,
                reason="permission_denied",
                text_only=True,
                is_follow_up=False,
                multimedia_active=False,
                in_call_active=False,
                cleaned_transcript="",
            )

        prof = self._current_profile_info()
        matcher = WakePhoneticMatcher(prof.wake_words)

        # ------------------------------------------------------------------
        # Desbloqueo de SafetyGate: evaluar confirmación/rechazo ANTES de self._utterance_lock
        # ------------------------------------------------------------------
        pending = self._pending_confirmation
        if pending is not None:
            _, cmd_vosk = matcher.extract_command_post_wake(utterance.transcript)
            eval_text = cmd_vosk or matcher.normalize_stt_text(utterance.transcript)
            is_appr, is_rej = _check_safety_intent(eval_text)

            # Si no es concluyente en Vosk y hay audio disponible, probar STT nube rápido (<3.5 s)
            if not (is_appr or is_rej) and self._audio_transcriber is not None and len(utterance.wav_bytes) >= 4000:
                try:
                    cloud_res = await asyncio.wait_for(
                        self._audio_transcriber(utterance.wav_bytes),
                        timeout=3.5,
                    )
                    if cloud_res is not None:
                        refined_text, _ = cloud_res
                        _, cmd_cloud = matcher.extract_command_post_wake(refined_text)
                        eval_text = cmd_cloud or matcher.normalize_stt_text(refined_text)
                        is_appr, is_rej = _check_safety_intent(eval_text)
                except Exception as exc:
                    logger.debug("STT nube rápido para confirmación falló o timeout: %s", exc)

            if is_appr or is_rej:
                self._pending_confirmation = None
                approved = is_appr
                logger.info(
                    "SafetyGate resuelto por voz: approved=%s (%r)",
                    approved,
                    eval_text,
                )
                if self._tts is not None:
                    self._tts.cancel()
                if self._on_resolve_safety is not None:
                    self._on_resolve_safety(pending.confirmation_id, approved, "voice")
                reply_msg = "Confirmado." if approved else "Cancelado."
                can_speak = (
                    self._ctx.settings.is_permission_enabled(Permission.VOICE_SPEAK)
                    and not self._ctx.settings.general.privacy_mode
                    and not self._ctx.settings.voice.tts_muted
                )
                if can_speak:
                    await self.speak_text(reply_msg)
                self._emit_orb_state("idle")
                return ContextDecision(
                    allow=True,
                    reason="safety_resolved",
                    text_only=False,
                    is_follow_up=False,
                    multimedia_active=False,
                    in_call_active=False,
                    cleaned_transcript=eval_text,
                )

        # ------------------------------------------------------------------
        # Flujo normal de comandos bajo candado exclusivo
        # ------------------------------------------------------------------
        async with self._utterance_lock:
            # Si llegó como follow_up pero el texto contiene la wake-word, promover a wake_word
            detected_wake, _ = matcher.detect_in_vosk(utterance.transcript)
            effective_trigger = (
                "wake_word"
                if (utterance.triggered_by == "follow_up" and detected_wake)
                else utterance.triggered_by
            )

            # Escudo de mención en 3ª persona sobre la wake-word (ej: "hablando de Rafael", "modificando a Rafael")
            if effective_trigger == "wake_word" and is_third_person_wake_mention(
                utterance.transcript, matcher.variants
            ):
                logger.info(
                    "Descartando mención en 3ª persona del wake word en transcripción local: %r",
                    utterance.transcript,
                )
                self._emit_orb_state("idle")
                return ContextDecision(
                    allow=False,
                    reason="third_person_mention",
                    text_only=True,
                    is_follow_up=False,
                    multimedia_active=False,
                    in_call_active=False,
                    cleaned_transcript="",
                )

            has_pending = self._pending_confirmation is not None
            decision = self._context_gate.evaluate_utterance(
                utterance.transcript,
                triggered_by=effective_trigger,
                audio_samples=utterance.audio_samples,
                vocal_band_ratio=utterance.avg_vocal_ratio,
                allow_short_confirmation=has_pending,
            )

            # Enfriamiento post-TTS para ignorar ecos en follow-up (salvo confirmaciones de seguridad activas)
            if (
                not has_pending
                and decision.is_follow_up
                and time.monotonic() < self._post_tts_cooldown_until
            ):
                logger.debug(
                    "Descartando follow-up recibido durante cooldown post-TTS (eco): %r",
                    utterance.transcript,
                )
                self._emit_orb_state("idle")
                return ContextDecision(
                    allow=False,
                    reason="post_tts_cooldown",
                    text_only=decision.text_only,
                    is_follow_up=True,
                    multimedia_active=decision.multimedia_active,
                    in_call_active=decision.in_call_active,
                    cleaned_transcript="",
                    voiceprint_similarity=decision.voiceprint_similarity,
                )

            sim_info = (
                f" (sim={decision.voiceprint_similarity:.2f})"
                if decision.voiceprint_similarity is not None
                else ""
            )
            logger.info(
                "Voz recibida (%s%s): %r -> allow=%s (%s)",
                effective_trigger,
                sim_info,
                utterance.transcript,
                decision.allow,
                decision.reason,
            )
            if not decision.allow:
                self._emit_orb_state("idle")
                return decision

            # Respaldo secundario para SafetyGate si se recibió durante la adquisición del candado
            if self._pending_confirmation is not None:
                is_appr, is_rej = _check_safety_intent(decision.cleaned_transcript)
                if is_appr or is_rej:
                    pending = self._pending_confirmation
                    self._pending_confirmation = None
                    approved = is_appr
                    logger.info(
                        "Resolviendo SafetyGate por voz: confirmation_id=%s, approved=%s (%s)",
                        pending.confirmation_id,
                        approved,
                        decision.cleaned_transcript,
                    )
                    if self._tts is not None:
                        self._tts.cancel()
                    if self._on_resolve_safety is not None:
                        self._on_resolve_safety(pending.confirmation_id, approved, "voice")
                    reply_msg = "Confirmado." if approved else "Cancelado."
                    can_speak = (
                        not decision.text_only
                        and self._ctx.settings.is_permission_enabled(Permission.VOICE_SPEAK)
                        and not self._ctx.settings.general.privacy_mode
                        and not self._ctx.settings.voice.tts_muted
                    )
                    if can_speak:
                        await self.speak_text(reply_msg, allow_ducking=not decision.in_call_active)
                    self._emit_orb_state("idle")
                    return decision

            self._emit_orb_state("thinking", visible=True)
            effective_transcript = clean_brand_articles(decision.cleaned_transcript)

            # Pipeline Híbrido:
            # 1. 0 tokens en Fast-Path exacto si Vosk coincide
            is_fast_path = (
                self._fast_path_matcher is not None
                and self._fast_path_matcher(effective_transcript)
            )
            if is_fast_path:
                logger.info(
                    "Fast-Path exacto detectado en Vosk: %r (0 tokens, omitiendo STT nube)",
                    effective_transcript,
                )
            elif (
                self._audio_transcriber is not None
                and len(utterance.wav_bytes) >= 4000
                and not self._ctx.settings.general.privacy_mode
            ):
                try:
                    logger.info(
                        "Invocando STT híbrido (dur=%.2fs, peak=%.2f, fuzzy=%s, rms=%.0f) para audio de %d bytes",
                        utterance.wav_duration_sec,
                        utterance.wav_peak,
                        utterance.fuzzy_wake_only,
                        utterance.rms_int16,
                        len(utterance.wav_bytes),
                    )
                    cascade_result = await asyncio.wait_for(
                        self._audio_transcriber(utterance.wav_bytes),
                        timeout=8.0,
                    )
                    if cascade_result is not None:
                        refined_text, engine_used = cascade_result
                        refined_text = refined_text.strip()
                        if refined_text:
                            if effective_trigger == "wake_word" and is_third_person_wake_mention(
                                refined_text, matcher.variants
                            ):
                                logger.info(
                                    "Descartando mención en 3ª persona confirmada por STT nube: %r",
                                    refined_text,
                                )
                                self._emit_orb_state("idle")
                                return ContextDecision(
                                    allow=False,
                                    reason="third_person_mention",
                                    text_only=decision.text_only,
                                    is_follow_up=decision.is_follow_up,
                                    multimedia_active=decision.multimedia_active,
                                    in_call_active=decision.in_call_active,
                                    cleaned_transcript="",
                                    voiceprint_similarity=decision.voiceprint_similarity,
                                )
                            ok_wake, candidate = matcher.extract_command_post_wake(
                                refined_text,
                                fuzzy_only=utterance.fuzzy_wake_only,
                                rms_int16=utterance.rms_int16,
                                noise_floor=utterance.noise_floor_int16,
                            )
                            if not ok_wake:
                                logger.info(
                                    "Descartando activación difusa no confirmada por STT nube ni energía vocal: %r (STT nube=%r)",
                                    decision.cleaned_transcript,
                                    refined_text,
                                )
                                self._emit_orb_state("idle")
                                return ContextDecision(
                                    allow=False,
                                    reason="false_fuzzy_wake",
                                    text_only=decision.text_only,
                                    is_follow_up=decision.is_follow_up,
                                    multimedia_active=decision.multimedia_active,
                                    in_call_active=decision.in_call_active,
                                    cleaned_transcript="",
                                    voiceprint_similarity=decision.voiceprint_similarity,
                                )
                            if candidate:
                                logger.info(
                                    "STT híbrido (%s, dur=%.2fs, peak=%.2f): %r -> %r",
                                    engine_used,
                                    utterance.wav_duration_sec,
                                    utterance.wav_peak,
                                    decision.cleaned_transcript,
                                    candidate,
                                )
                                effective_transcript = candidate
                except Exception as exc:
                    logger.warning("Fallo o timeout en STT híbrido remoto: %s", exc)
            else:
                # Si no se llamó a STT nube, validar activación difusa local
                if utterance.fuzzy_wake_only and utterance.triggered_by == "wake_word":
                    ok_wake, candidate = matcher.extract_command_post_wake(
                        effective_transcript,
                        fuzzy_only=True,
                        rms_int16=utterance.rms_int16,
                        noise_floor=utterance.noise_floor_int16,
                    )
                    if not ok_wake:
                        logger.info(
                            "Descartando activación difusa local sin suficiente señal: %r (rms=%.0f)",
                            effective_transcript,
                            utterance.rms_int16,
                        )
                        self._emit_orb_state("idle")
                        return ContextDecision(
                            allow=False,
                            reason="false_fuzzy_wake",
                            text_only=decision.text_only,
                            is_follow_up=decision.is_follow_up,
                            multimedia_active=decision.multimedia_active,
                            in_call_active=decision.in_call_active,
                            cleaned_transcript="",
                            voiceprint_similarity=decision.voiceprint_similarity,
                        )
                    if candidate:
                        effective_transcript = candidate

            try:
                reply: VoiceCommandReply | None = None
                if self._on_voice_command is not None:
                    cmd_timeout = max(
                        25.0,
                        float(self._ctx.settings.security.safety_timeout_sec) + 10.0,
                    )
                    try:
                        reply = await asyncio.wait_for(
                            self._on_voice_command(effective_transcript, decision.is_follow_up),
                            timeout=cmd_timeout,
                        )
                    except TimeoutError:
                        logger.warning(
                            "Timeout (%.0fs) procesando comando de voz: %r",
                            cmd_timeout,
                            effective_transcript,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Error procesando comando de voz %r: %s",
                            effective_transcript,
                            exc,
                        )

                if reply is None:
                    self._emit_orb_state("idle")
                    return decision

                raw_output = reply.spoken_text if reply.spoken_text is not None else reply.text
                if self._context_gate.is_contextual_silence_response(raw_output):
                    self._context_gate.close_follow_up_window()
                    self._emit_orb_state("idle")
                    return decision

                can_speak = (
                    reply.should_speak
                    and not decision.text_only
                    and self._ctx.settings.is_permission_enabled(Permission.VOICE_SPEAK)
                    and not self._ctx.settings.general.privacy_mode
                )
                if can_speak and raw_output.strip():
                    await self.speak_text(raw_output, allow_ducking=not decision.in_call_active)

                # Abrir ventana de seguimiento de 6 s salvo Modo Privacidad o Modo En Llamada
                if not decision.in_call_active and not self._ctx.settings.general.privacy_mode:
                    self._context_gate.open_follow_up_window()
            finally:
                if not (self._tts is not None and self._tts.is_speaking):
                    self._emit_orb_state("idle")
            return decision

    async def speak_text(self, text: str, *, allow_ducking: bool = True) -> TTSResult:
        if self._ctx is None or self._tts is None:
            return TTSResult(ok=False, engine="empty", error="Módulo de voz no inicializado.")
        if not self._ctx.settings.is_permission_enabled(Permission.VOICE_SPEAK):
            return TTSResult(ok=False, engine="muted", error="Permiso VOICE_SPEAK desactivado.")

        prof = self._current_profile_info()
        self._emit_orb_state("speaking", visible=True)
        try:
            return await self._tts.speak(
                text,
                profile_id=prof.profile_id,
                profile_default_voice=prof.default_voice,
                allow_ducking=allow_ducking,
            )
        finally:
            self._post_tts_cooldown_until = (
                0.0 if self._pending_confirmation is not None else time.monotonic() + 0.65
            )
            self._emit_orb_state("idle")

    def trigger_orb_preview(self, *, duration_sec: float = 4.0) -> None:
        """Emite vista previa fija de 4 segundos para probar estilos del Orbe en Ajustes."""
        self._emit_orb_state("preview", visible=True, preview_sec=max(1.0, float(duration_sec)))

    def list_microphones(self) -> list[AudioInputDevice]:
        return list_input_devices()

    def start_voiceprint_calibration(self, *, duration_sec: float = 6.0) -> bool:
        if self._stt is None:
            return False
        return self._stt.start_calibration(duration_sec=duration_sec)

    def cancel_voiceprint_calibration(self) -> None:
        if self._stt is not None:
            self._stt.cancel_calibration()

    def set_input_device(self, device_index: int | None) -> None:
        if self._ctx is None:
            return
        self._ctx.settings.voice.input_device_index = device_index
        if self._on_save_settings is not None:
            self._on_save_settings("voice")
        if self._stt is not None:
            self._stt.request_device_change(device_index)

    # ------------------------------------------------------------------
    # Herramientas tipadas (Permission.VOICE_LISTEN / Permission.VOICE_SPEAK)
    # ------------------------------------------------------------------
    def get_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="voice_speak",
                module_id=self.module_id,
                description="Pronuncia un mensaje en voz alta usando Edge-TTS o SAPI fallback.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Texto a pronunciar."},
                    },
                    "required": ["text"],
                },
                permission=Permission.VOICE_SPEAK,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_speak,
                blocked_in_privacy=False,
            ),
            ToolSpec(
                name="voice_set_mute",
                module_id=self.module_id,
                description="Silencia o reactiva la respuesta hablada (modo solo texto).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "muted": {"type": "boolean", "description": "True para silenciar voz."},
                    },
                    "required": ["muted"],
                },
                permission=Permission.VOICE_SPEAK,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_set_mute,
                blocked_in_privacy=False,
            ),
            ToolSpec(
                name="voice_set_in_call_mode",
                module_id=self.module_id,
                description="Activa o desactiva el Modo En Llamada (sin ducking ni seguimiento continuo).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["enabled"],
                },
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_set_in_call_mode,
                blocked_in_privacy=False,
            ),
            ToolSpec(
                name="voice_start_calibration",
                module_id=self.module_id,
                description="Inicia la calibración guiada de 6 segundos de la huella vocal del propietario.",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_start_calibration,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="voice_list_microphones",
                module_id=self.module_id,
                description="Lista los micrófonos de entrada detectados en el PC.",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_list_microphones,
                blocked_in_privacy=False,
            ),
        ]

    async def _tool_speak(self, args: dict[str, Any]) -> ToolResult:
        text = str(args.get("text", "")).strip()
        if not text:
            return ToolResult(ok=False, summary="Debes indicar un texto para hablar.", error_code="empty_text")
        res = await self.speak_text(text)
        return ToolResult(
            ok=res.ok,
            summary="Mensaje pronunciado correctamente." if res.ok else (res.error or "No se pudo hablar."),
            data={"engine": res.engine, "cancelled": res.cancelled},
            error_code=None if res.ok else "tts_failed",
        )

    async def _tool_set_mute(self, args: dict[str, Any]) -> ToolResult:
        muted = bool(args.get("muted", True))
        if self._on_set_voice_muted is not None:
            self._on_set_voice_muted(muted)
        elif self._ctx is not None:
            self._ctx.settings.voice.tts_muted = muted
            self._ctx.events.publish(VoiceMuteChangedEvent(muted=muted))
        if muted and self._tts is not None:
            self._tts.cancel()
        msg = "Voz silenciada (modo solo texto)." if muted else "Voz reactivada."
        return ToolResult(ok=True, summary=msg, data={"muted": muted}, spoken_text="" if muted else msg)

    async def _tool_set_in_call_mode(self, args: dict[str, Any]) -> ToolResult:
        enabled = bool(args.get("enabled", True))
        if self._ctx is None:
            return ToolResult(ok=False, summary="Módulo de voz no iniciado.", error_code="not_started")
        self._ctx.settings.voice.in_call_text_only = enabled
        if enabled and self._context_gate is not None:
            self._context_gate.close_follow_up_window()
        if self._on_save_settings is not None:
            self._on_save_settings("voice")
        self.check_and_publish_call_state(force_refresh=True)
        msg = (
            "Modo En Llamada activado: respuestas solo texto y sin ducking de llamadas."
            if enabled
            else "Modo En Llamada desactivado."
        )
        return ToolResult(ok=True, summary=msg, data={"in_call_text_only": enabled})

    async def _tool_start_calibration(self, _args: dict[str, Any]) -> ToolResult:
        ok = self.start_voiceprint_calibration(duration_sec=6.0)
        if not ok:
            return ToolResult(
                ok=False,
                summary="No se pudo iniciar la calibración vocal (verifica privacidad y micrófono).",
                error_code="calibration_unavailable",
            )
        return ToolResult(
            ok=True,
            summary="Calibración vocal iniciada: habla con normalidad durante 6 segundos.",
            data={"duration_sec": 6.0},
        )

    async def _tool_list_microphones(self, _args: dict[str, Any]) -> ToolResult:
        devices = self.list_microphones()
        items = [
            {
                "index": d.index,
                "name": d.name,
                "channels": d.channels,
                "default": d.is_default,
            }
            for d in devices
        ]
        summary = (
            f"Se detectaron {len(items)} micrófonos de entrada."
            if items
            else "No se detectaron micrófonos de entrada."
        )
        return ToolResult(ok=True, summary=summary, data={"devices": items})

    # ------------------------------------------------------------------
    # Reglas Fast-Path locales (<50 ms, 0 tokens)
    # ------------------------------------------------------------------
    def get_fast_path_rules(self) -> list[FastPathRule]:
        return [
            FastPathRule(
                rule_id="voice.mute_on",
                module_id=self.module_id,
                priority=20,
                pattern=re.compile(
                    r"^(?:silencia(?:r)?\s+(?:la\s+)?voz|c[aá]llate|deja\s+de\s+hablar|modo\s+solo\s+texto)$",
                    re.IGNORECASE,
                ),
                permission=Permission.VOICE_SPEAK,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_mute_on,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="voice.mute_off",
                module_id=self.module_id,
                priority=20,
                pattern=re.compile(
                    r"^(?:activa(?:r)?\s+(?:la\s+)?voz|vuelve\s+a\s+hablar|quita(?:r)?\s+el\s+silencio\s+de\s+voz)$",
                    re.IGNORECASE,
                ),
                permission=Permission.VOICE_SPEAK,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_mute_off,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="voice.in_call_on",
                module_id=self.module_id,
                priority=22,
                pattern=re.compile(
                    r"^(?:activa(?:r)?\s+(?:el\s+)?modo\s+en\s+llamada|estoy\s+en\s+llamada)$",
                    re.IGNORECASE,
                ),
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_in_call_on,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="voice.in_call_off",
                module_id=self.module_id,
                priority=22,
                pattern=re.compile(
                    r"^(?:desactiva(?:r)?\s+(?:el\s+)?modo\s+en\s+llamada|termin[eé]\s+la\s+llamada|salir\s+de\s+modo\s+llamada)$",
                    re.IGNORECASE,
                ),
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_in_call_off,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="voice.calibrate",
                module_id=self.module_id,
                priority=25,
                pattern=re.compile(
                    r"^(?:calibra(?:r)?\s+mi\s+voz|calibrar\s+huella\s+vocal)$",
                    re.IGNORECASE,
                ),
                permission=Permission.VOICE_LISTEN,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_calibrate,
                blocked_in_privacy=True,
                earcon="confirm",
            ),
        ]

    async def _fp_mute_on(self, _m: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_set_mute({"muted": True})

    async def _fp_mute_off(self, _m: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_set_mute({"muted": False})

    async def _fp_in_call_on(self, _m: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_set_in_call_mode({"enabled": True})

    async def _fp_in_call_off(self, _m: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_set_in_call_mode({"enabled": False})

    async def _fp_calibrate(self, _m: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_start_calibration({})

    # ------------------------------------------------------------------
    # Diagnósticos del Doctor
    # ------------------------------------------------------------------
    def diagnose(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = []

        # 1. Modelo Vosk ES
        vosk_dir = self._ctx.paths.vosk_es_dir if self._ctx is not None else None
        if vosk_dir is not None and is_vosk_model_present(vosk_dir):
            items.append(
                DiagnosticItem(
                    code="voice.vosk_model",
                    area="Voz",
                    title="Modelo offline Vosk ES",
                    severity=DiagnosticSeverity.OK,
                    message="Modelo de reconocimiento en español disponible localmente.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="voice.vosk_model",
                    area="Voz",
                    title="Modelo offline Vosk ES",
                    severity=DiagnosticSeverity.ERROR,
                    message="Falta el modelo Vosk ES en USER_DATA_DIR/models/vosk-es.",
                    remediation="Ejecuta `uv run python scripts/setup_runtimes.py` para descargar Vosk ES.",
                    auto_fixable=False,
                )
            )

        # 2. Micrófonos de entrada
        mics = self.list_microphones()
        if mics:
            items.append(
                DiagnosticItem(
                    code="voice.microphone",
                    area="Voz",
                    title="Micrófono de entrada",
                    severity=DiagnosticSeverity.OK,
                    message=f"{len(mics)} micrófono(s) detectado(s) por sounddevice.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="voice.microphone",
                    area="Voz",
                    title="Micrófono de entrada",
                    severity=DiagnosticSeverity.WARNING,
                    message="No se detectaron dispositivos de captura de audio activos.",
                    remediation="Conecta un micrófono y verifica permisos de privacidad en Windows 11.",
                )
            )

        # 3. Edge-TTS
        has_edge = importlib.util.find_spec("edge_tts") is not None
        items.append(
            DiagnosticItem(
                code="voice.edge_tts",
                area="Voz",
                title="Síntesis Edge-TTS",
                severity=DiagnosticSeverity.OK if has_edge else DiagnosticSeverity.ERROR,
                message="edge-tts instalado y listo." if has_edge else "Paquete edge-tts no encontrado.",
                remediation="" if has_edge else "Ejecuta `uv sync` para restaurar dependencias.",
            )
        )

        # 4. Fallback SAPI
        has_sapi = sys.platform == "win32" and importlib.util.find_spec("comtypes") is not None
        items.append(
            DiagnosticItem(
                code="voice.sapi_fallback",
                area="Voz",
                title="Respaldo offline SAPI (Windows)",
                severity=DiagnosticSeverity.OK if has_sapi else DiagnosticSeverity.WARNING,
                message=(
                    "Respaldo SAPI COM STA listo para modo sin conexión."
                    if has_sapi
                    else "SAPI solo está disponible en Windows con comtypes."
                ),
            )
        )

        # 5. Huella vocal si está habilitada
        if (
            self._ctx is not None
            and self._ctx.settings.voice.voiceprint_enabled
            and self._voiceprint is not None
            and not self._voiceprint.is_enrolled
        ):
            items.append(
                DiagnosticItem(
                    code="voice.voiceprint_pending",
                    area="Voz",
                    title="Calibración de huella vocal",
                    severity=DiagnosticSeverity.WARNING,
                    message="La biometría vocal está activada pero aún no has calibrado tu voz.",
                    remediation="Usa el botón 'Calibrar huella vocal (6 s)' en Ajustes -> Voz y Orbe.",
                )
            )

        return items
