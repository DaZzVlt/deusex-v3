from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import threading
import time
from typing import Final

import numpy as np
import numpy.typing as npt

from deusex.core.config import DeusExSettings
from deusex.modules.voice.ducking import AudioSessionSnapshot, SelectiveAudioDucker
from deusex.modules.voice.voiceprint import OwnerVoiceprintGuard

logger = logging.getLogger(__name__)

CONTEXTUAL_SILENCE_TOKEN: Final[str] = "[SILENCIO_CONTEXTUAL]"
_SESSION_CACHE_TTL_SEC: Final[float] = 1.25
_MULTIMEDIA_VAD_MULTIPLIER: Final[float] = 1.65

_FILLER_TRANSCRIPTS: Final[frozenset[str]] = frozenset(
    {
        "eh",
        "emm",
        "mmm",
        "mm",
        "aja",
        "ajá",
        "ok",
        "okay",
        "okey",
        "si",
        "sí",
        "no",
        "ya",
        "bueno",
        "claro",
        "jaja",
        "jajaja",
        "hola",
        "oye",
        "mmmh",
        "uf",
        "ay",
        "ehh",
    }
)

_CONFIRMATION_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "sí",
        "si",
        "no",
        "confirmo",
        "confirmar",
        "confirma",
        "confirmado",
        "aprobar",
        "aprueba",
        "aprobado",
        "adelante",
        "dale",
        "hazlo",
        "procede",
        "proceder",
        "ejecuta",
        "ejecutar",
        "cancelar",
        "cancela",
        "cancelado",
        "rechazar",
        "rechaza",
        "rechazado",
        "anular",
        "anula",
        "anulado",
        "mejor no",
        "detén",
        "deten",
        "detente",
        "para",
        "alto",
    }
)

_THIRD_PERSON_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(estaba hablando con|le dije a|ese asistente|el asistente ese)\b", re.IGNORECASE),
    re.compile(r"\b(escuchaste eso|mira lo que|te digo que|le voy a decir)\b", re.IGNORECASE),
    re.compile(r"\b(habla solo|se activ[oó] solo|creo que escuch[oó])\b", re.IGNORECASE),
)

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_PUNCT_STRIP_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\sáéíóúüñ¿?¡!]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ContextDecision:
    """Decisión estructurada del escudo contextual multicapa."""

    allow: bool
    reason: str
    text_only: bool
    is_follow_up: bool
    multimedia_active: bool
    in_call_active: bool
    cleaned_transcript: str
    voiceprint_similarity: float | None = None


class ContextGate:
    """
    Escudo contextual multicapa para la entrada de voz:
    - Eleva sensibilidad dinámica cuando `is_multimedia_active` es True vía `pycaw`.
    - Filtra ruido residual, muletillas y conversaciones en tercera persona (`is_noise_or_third_person`).
    - Gestiona Modo Privacidad y Modo "En Llamada" (desactiva seguimiento continuo 6 s y fuerza solo texto).
    - Gestiona ventana de seguimiento continuo y marcador `[SILENCIO_CONTEXTUAL]`.
    """

    def __init__(
        self,
        settings: DeusExSettings,
        ducker: SelectiveAudioDucker,
        voiceprint: OwnerVoiceprintGuard,
    ) -> None:
        self._settings = settings
        self._ducker = ducker
        self._voiceprint = voiceprint
        self._lock = threading.RLock()
        self._cached_snapshot = AudioSessionSnapshot()
        self._follow_up_deadline: float = 0.0
        self._follow_up_is_confirmation: bool = False
        self._last_logged_in_call: bool | None = None

    def get_audio_snapshot(self, *, force_refresh: bool = False) -> AudioSessionSnapshot:
        """Obtiene snapshot de sesiones Windows con caché corta para no saturar ComStaThread."""
        now = time.monotonic()
        with self._lock:
            cached = self._cached_snapshot
            if not force_refresh and (now - cached.captured_at) < _SESSION_CACHE_TTL_SEC:
                return cached

        fresh = self._ducker.inspect_sessions_sync()
        with self._lock:
            self._cached_snapshot = fresh
        return fresh

    def is_multimedia_active(self, *, force_refresh: bool = False) -> bool:
        return self.get_audio_snapshot(force_refresh=force_refresh).is_multimedia_active

    def is_in_call_mode(self, *, force_refresh: bool = False) -> bool:
        """True si el usuario activó `in_call_text_only` o si hay una app de llamada activa."""
        snap = self.get_audio_snapshot(force_refresh=force_refresh)
        in_call = bool(self._settings.voice.in_call_text_only or snap.is_call_active)
        if self._last_logged_in_call != in_call:
            self._last_logged_in_call = in_call
            logger.info(
                "Transición Modo En Llamada: active=%s (in_call_text_only=%s, apps=%s)",
                in_call,
                self._settings.voice.in_call_text_only,
                list(snap.active_call_apps),
            )
        return in_call

    def dynamic_vad_multiplier(self) -> float:
        """Eleva umbral VAD automáticamente cuando hay música o video reproduciéndose."""
        snap = self.get_audio_snapshot()
        return _MULTIMEDIA_VAD_MULTIPLIER if snap.is_multimedia_active else 1.0

    def open_follow_up_window(
        self,
        *,
        duration_sec: float | None = None,
        force_for_confirmation: bool = False,
    ) -> bool:
        """Abre la ventana de seguimiento continuo (6 s por defecto), bloqueada en Privacidad/Llamada (salvo confirmación de seguridad)."""
        if self._settings.general.privacy_mode:
            self.close_follow_up_window()
            return False
        if not force_for_confirmation and self.is_in_call_mode():
            self.close_follow_up_window()
            return False

        window_sec = (
            float(duration_sec)
            if duration_sec is not None
            else float(self._settings.voice.follow_up_window_sec)
        )
        if window_sec <= 0.0:
            self.close_follow_up_window()
            return False

        with self._lock:
            self._follow_up_deadline = time.monotonic() + window_sec
            self._follow_up_is_confirmation = force_for_confirmation
        return True

    def extend_follow_up_window(self, *, duration_sec: float = 4.0) -> None:
        """Extiende la ventana de seguimiento si el usuario sigue hablando activamente."""
        with self._lock:
            if self._follow_up_deadline > 0.0:
                self._follow_up_deadline = max(
                    self._follow_up_deadline, time.monotonic() + float(duration_sec)
                )

    def close_follow_up_window(self) -> None:
        with self._lock:
            self._follow_up_deadline = 0.0
            self._follow_up_is_confirmation = False

    def is_follow_up_active(self, *, force_for_confirmation: bool = False) -> bool:
        if self._settings.general.privacy_mode:
            return False
        with self._lock:
            is_conf = self._follow_up_is_confirmation or force_for_confirmation
            if not is_conf and self.is_in_call_mode():
                return False
            return time.monotonic() < self._follow_up_deadline

    def is_follow_up_confirmation_active(self) -> bool:
        """Indica si la ventana de seguimiento abierta corresponde específicamente a una confirmación de SafetyGate."""
        if self._settings.general.privacy_mode:
            return False
        with self._lock:
            return self._follow_up_is_confirmation and (time.monotonic() < self._follow_up_deadline)

    @staticmethod
    def is_contextual_silence_response(text: str) -> bool:
        """Detecta si el modelo decidió guardar silencio contextual en una ventana de seguimiento."""
        return CONTEXTUAL_SILENCE_TOKEN in text.upper()

    @staticmethod
    def normalize_transcript(transcript: str) -> str:
        collapsed = _WHITESPACE_RE.sub(" ", transcript.strip())
        return collapsed

    def is_noise_or_third_person(
        self,
        transcript: str,
        *,
        is_follow_up: bool = False,
        vocal_band_ratio: float = 1.0,
        multimedia_active: bool = False,
        allow_short_confirmation: bool = False,
    ) -> bool:
        """Filtra ruido no vocal, muletillas muy cortas o diálogo en tercera persona."""
        cleaned = self.normalize_transcript(transcript)
        if not cleaned:
            return True

        stripped = _PUNCT_STRIP_RE.sub("", cleaned).strip().lower()
        if allow_short_confirmation and stripped in _CONFIRMATION_PHRASES:
            return False

        # Relación vocal demasiado baja sugiere música/ruido de altavoz
        min_ratio = 0.50 if multimedia_active else 0.35
        if vocal_band_ratio < min_ratio:
            return True

        if len(stripped) < 2:
            return True

        words = stripped.split()
        real_words = [w for w in words if len(w) >= 2 and w not in _FILLER_TRANSCRIPTS]
        if not real_words:
            return True

        # En ventana de seguimiento continuo sin wake-word, exigir más sustancia y descartar 3ª persona
        if is_follow_up and not allow_short_confirmation:
            if len(stripped) < 4:
                return True
            if sum(len(w) for w in real_words) < 4:
                return True
            for pattern in _THIRD_PERSON_PATTERNS:
                if pattern.search(cleaned):
                    return True

        return False

    def evaluate_utterance(
        self,
        transcript: str,
        *,
        triggered_by: str,
        audio_samples: npt.NDArray[np.float32] | npt.NDArray[np.int16] | bytes | None = None,
        vocal_band_ratio: float = 1.0,
        allow_short_confirmation: bool = False,
    ) -> ContextDecision:
        """
        Evalúa una intervención de voz completa (`triggered_by`: "wake_word", "ptt" o "follow_up").
        """
        cleaned = self.normalize_transcript(transcript)

        if self._settings.general.privacy_mode:
            self.close_follow_up_window()
            return ContextDecision(
                allow=False,
                reason="privacy_mode",
                text_only=True,
                is_follow_up=False,
                multimedia_active=False,
                in_call_active=False,
                cleaned_transcript="",
            )

        snap = self.get_audio_snapshot()
        in_call = self._settings.voice.in_call_text_only or snap.is_call_active
        text_only = in_call or self._settings.voice.tts_muted
        is_follow_up = triggered_by == "follow_up"

        if is_follow_up and (
            (in_call and not allow_short_confirmation)
            or not self.is_follow_up_active(force_for_confirmation=allow_short_confirmation)
        ):
            return ContextDecision(
                allow=False,
                reason="follow_up_blocked_in_call" if in_call else "follow_up_expired",
                text_only=text_only,
                is_follow_up=True,
                multimedia_active=snap.is_multimedia_active,
                in_call_active=in_call,
                cleaned_transcript="",
            )

        if self.is_noise_or_third_person(
            cleaned,
            is_follow_up=is_follow_up,
            vocal_band_ratio=vocal_band_ratio,
            multimedia_active=snap.is_multimedia_active,
            allow_short_confirmation=allow_short_confirmation,
        ):
            return ContextDecision(
                allow=False,
                reason="noise_or_third_person",
                text_only=text_only,
                is_follow_up=is_follow_up,
                multimedia_active=snap.is_multimedia_active,
                in_call_active=in_call,
                cleaned_transcript="",
            )

        # Biometría vocal opcional (omitida en PTT explícito; más estricta si hay multimedia activa)
        vp_sim: float | None = None
        if (
            self._settings.voice.voiceprint_enabled
            and triggered_by != "ptt"
            and audio_samples is not None
        ):
            threshold_override = (
                min(0.70, self._voiceprint.threshold + 0.03) if snap.is_multimedia_active else None
            )
            vp = self._voiceprint.verify(
                audio_samples,
                enabled=True,
                threshold_override=threshold_override,
            )
            vp_sim = vp.similarity
            if not vp.accepted:
                return ContextDecision(
                    allow=False,
                    reason=vp.reason,
                    text_only=text_only,
                    is_follow_up=is_follow_up,
                    multimedia_active=snap.is_multimedia_active,
                    in_call_active=in_call,
                    cleaned_transcript="",
                    voiceprint_similarity=vp_sim,
                )

        return ContextDecision(
            allow=True,
            reason="accepted",
            text_only=text_only,
            is_follow_up=is_follow_up,
            multimedia_active=snap.is_multimedia_active,
            in_call_active=in_call,
            cleaned_transcript=cleaned,
            voiceprint_similarity=vp_sim,
        )
