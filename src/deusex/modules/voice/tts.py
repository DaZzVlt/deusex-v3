from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Final, Protocol
import uuid

from deusex.core.config import DeusExSettings
from deusex.core.paths import AppPaths
from deusex.core.runtime import ComStaThread
from deusex.modules.voice.ducking import SelectiveAudioDucker

logger = logging.getLogger(__name__)

DEFAULT_EDGE_VOICE: Final[str] = "es-CL-CatalinaNeural"
_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_CLEAN_RE: Final[re.Pattern[str]] = re.compile(r"[`*_#>~\[\]]+")
_TEMP_RE: Final[re.Pattern[str]] = re.compile(r"(\d+)(?:\.\d+)?\s*°\s*C\b", re.IGNORECASE)
_GB_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\.(\d+)\s*(?:GB|GigaBytes|gigas)\b", re.IGNORECASE
)
_GB_INTEGER_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\s*(?:GB|GigaBytes|gigas)\b", re.IGNORECASE
)
_MB_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\.(\d+)\s*(?:MB|MegaBytes|megas)\b", re.IGNORECASE
)
_MB_INTEGER_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\s*(?:MB|MegaBytes|megas)\b", re.IGNORECASE
)
_QUOTES_RE: Final[re.Pattern[str]] = re.compile(r"[«»“”\"']+")
_BULLET_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_EMOJI_RE: Final[re.Pattern[str]] = re.compile(
    r"[\U00010000-\U0010ffff]|[\u2600-\u26ff\u2700-\u27bf]",
    flags=re.UNICODE,
)
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class WordTiming:
    """Marca temporal de palabra devuelta por `edge-tts` (`WordBoundary`)."""

    offset_ms: float
    duration_ms: float
    text: str


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    """Audio sintetizado en MP3 junto con sus marcas `WordBoundary`."""

    audio_bytes: bytes
    word_timings: tuple[WordTiming, ...]
    voice: str


@dataclass(frozen=True, slots=True)
class TTSResult:
    """Resultado de una solicitud de habla."""

    ok: bool
    engine: str  # edge-tts | sapi | muted | empty
    cancelled: bool = False
    error: str | None = None


class EdgeTTSSynthesizer(Protocol):
    """Protocolo inyectable para pruebas offline de síntesis Edge-TTS."""

    async def synthesize(self, text: str, *, voice: str, rate_str: str) -> SynthesizedSpeech: ...


class AudioPlaybackBackend(Protocol):
    """Protocolo inyectable para reproducción de audio MP3 con soporte de cancelación."""

    def play_file(
        self,
        file_path: Path,
        *,
        on_finished: Callable[[bool], None],
    ) -> bool: ...

    def stop(self) -> None: ...


class SapiBackend(Protocol):
    """Protocolo inyectable para fallback SAPI en `ComStaThread`."""

    def speak_async_start(self, text: str, *, rate: float) -> bool: ...
    def is_speaking(self) -> bool: ...
    def purge(self) -> None: ...


def sanitize_text_for_speech(text: str) -> str:
    """Limpia Markdown, URLs, caracteres de control y humaniza unidades/números para lectura en voz alta."""
    if not text:
        return ""
    # 1. URLs
    t = _URL_RE.sub("enlace web", text)
    # 2. Bullets de listas a pausas suaves
    t = _BULLET_RE.sub(". ", t)
    # 3. Unidades y temperaturas
    t = _TEMP_RE.sub(r"\1 grados", t)
    t = _GB_DECIMAL_RE.sub(r"\1 coma \2 gigas", t)
    t = _GB_INTEGER_RE.sub(r"\1 gigas", t)
    t = _MB_DECIMAL_RE.sub(r"\1 coma \2 megas", t)
    t = _MB_INTEGER_RE.sub(r"\1 megas", t)
    # 4. Comillas ornamentales y Markdown
    t = _QUOTES_RE.sub("", t)
    t = _MARKDOWN_CLEAN_RE.sub(" ", t)
    # 5. Emojis
    t = _EMOJI_RE.sub("", t)
    # 6. Espacios en blanco
    return _WHITESPACE_RE.sub(" ", t).strip()


def format_edge_rate(rate: float) -> str:
    """Convierte multiplicador 0.5-2.0 al formato de `edge-tts` (ej. '+15%', '-10%')."""
    clamped = max(0.5, min(2.0, float(rate)))
    delta_pct = round((clamped - 1.0) * 100.0)
    return f"{delta_pct:+d}%"


def map_rate_to_sapi(rate: float) -> int:
    """Mapea multiplicador 0.5-2.0 al rango entero SAPI (-10 a +10)."""
    clamped = max(0.5, min(2.0, float(rate)))
    if clamped >= 1.0:
        return max(0, min(10, round((clamped - 1.0) * 6.0)))
    return max(-10, min(0, round((clamped - 1.0) * 10.0)))


class _DefaultEdgeTTSSynthesizer:
    """Implementación real con `edge-tts` capturando audio y eventos `WordBoundary`."""

    async def synthesize(self, text: str, *, voice: str, rate_str: str) -> SynthesizedSpeech:
        import edge_tts

        communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate_str)
        audio_parts: list[bytes] = []
        timings: list[WordTiming] = []

        async for chunk in communicate.stream():
            chunk_type = str(chunk.get("type", ""))
            if chunk_type == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray)):
                    audio_parts.append(bytes(data))
            elif chunk_type == "WordBoundary":
                offset_ticks = float(chunk.get("offset", 0) or 0)
                duration_ticks = float(chunk.get("duration", 0) or 0)
                word_txt = str(chunk.get("text", "") or "").strip()
                if word_txt:
                    timings.append(
                        WordTiming(
                            offset_ms=offset_ticks / 10_000.0,
                            duration_ms=max(20.0, duration_ticks / 10_000.0),
                            text=word_txt,
                        )
                    )

        return SynthesizedSpeech(
            audio_bytes=b"".join(audio_parts),
            word_timings=tuple(timings),
            voice=voice,
        )


class _QtMultimediaPlaybackBackend:
    """
    Reproductor thread-safe sobre `PySide6.QtMultimedia.QMediaPlayer`.
    Vive en el hilo de `QCoreApplication` para respetar el modelo de 4 hilos sin crashes.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._host: Any = None
        self._on_finished: Callable[[bool], None] | None = None

    def _ensure_host(self) -> Any:
        with self._lock:
            if self._host is not None:
                return self._host

        try:
            from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception:
            return None

        app = QCoreApplication.instance()
        if app is None:
            return None

        backend_ref = self

        class _PlayerHost(QObject):
            play_requested = Signal(str)
            stop_requested = Signal()

            def __init__(self) -> None:
                super().__init__()
                self._audio_out: Any = None
                self._player: Any = None
                self.play_requested.connect(self._do_play)
                self.stop_requested.connect(self._do_stop)

            def _init_player(self) -> None:
                if self._player is not None:
                    return
                self._audio_out = QAudioOutput(self)
                self._audio_out.setVolume(1.0)
                self._player = QMediaPlayer(self)
                self._player.setAudioOutput(self._audio_out)
                self._player.mediaStatusChanged.connect(self._on_media_status)
                self._player.errorOccurred.connect(self._on_error)

            def _do_play(self, file_path: str) -> None:
                self._init_player()
                self._player.stop()
                self._player.setSource(QUrl.fromLocalFile(file_path))
                self._player.play()

            def _do_stop(self) -> None:
                if self._player is not None:
                    self._player.stop()
                    self._player.setSource(QUrl())

            def _on_media_status(self, status: Any) -> None:
                end_status = QMediaPlayer.MediaStatus.EndOfMedia
                invalid_status = QMediaPlayer.MediaStatus.InvalidMedia
                if status == end_status:
                    backend_ref._notify_finished(True)
                elif status == invalid_status:
                    backend_ref._notify_finished(False)

            def _on_error(self, _error: Any, _msg: str = "") -> None:
                backend_ref._notify_finished(False)

        host = _PlayerHost()
        host.moveToThread(app.thread())
        with self._lock:
            self._host = host
        return host

    def _notify_finished(self, ok: bool) -> None:
        with self._lock:
            cb = self._on_finished
            self._on_finished = None
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(ok)

    def play_file(
        self,
        file_path: Path,
        *,
        on_finished: Callable[[bool], None],
    ) -> bool:
        host = self._ensure_host()
        if host is None:
            return False
        with self._lock:
            self._on_finished = on_finished
        try:
            host.play_requested.emit(str(file_path))
            return True
        except RuntimeError:
            with self._lock:
                self._host = None
                self._on_finished = None
            return False

    def stop(self) -> None:
        with self._lock:
            host = self._host
            cb = self._on_finished
            self._on_finished = None
        if host is not None:
            with contextlib.suppress(RuntimeError):
                host.stop_requested.emit()
        if cb is not None:
            with contextlib.suppress(Exception):
                cb(False)


class _Win32SapiBackend:
    """Backend SAPI nativo para Windows ejecutado estrictamente en `ComStaThread`."""

    def __init__(self) -> None:
        self._voice: Any = None

    def _ensure_voice(self) -> Any:
        if sys.platform != "win32":
            return None
        if self._voice is not None:
            return self._voice
        try:
            import comtypes.client

            self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
            return self._voice
        except Exception as exc:
            logger.warning("SAPI.SpVoice no disponible: %s", exc)
            return None

    def speak_async_start(self, text: str, *, rate: float) -> bool:
        voice = self._ensure_voice()
        if voice is None:
            return False
        try:
            voice.Rate = map_rate_to_sapi(rate)
            # SVSFlagsAsync (1) | SVSFPurgeBeforeSpeak (2) == 3
            voice.Speak(text, 3)
            return True
        except Exception as exc:
            logger.warning("Error iniciando SAPI.SpVoice: %s", exc)
            return False

    def is_speaking(self) -> bool:
        if self._voice is None:
            return False
        try:
            # RunningState == 2 indica reproducción activa en SAPI
            return int(self._voice.Status.RunningState) == 2
        except Exception:
            return False

    def purge(self) -> None:
        if self._voice is None:
            return
        with contextlib.suppress(Exception):
            # SVSFlagsAsync | SVSFPurgeBeforeSpeak con texto vacío corta de inmediato
            self._voice.Speak("", 3)


class VoiceTTSEngine:
    """
    Motor de síntesis de voz de DeusEx v3:
    - Edge-TTS principal con reproducción vía `QtMultimedia` y seguimiento `WordBoundary`.
    - Fallback automático a SAPI en `ComStaThread` si falla la red o el reproductor.
    - Integración garantizada con `SelectiveAudioDucker` y cancelación instantánea (*barge-in*).
    """

    def __init__(
        self,
        *,
        paths: AppPaths,
        settings: DeusExSettings,
        com_sta: ComStaThread,
        ducker: SelectiveAudioDucker,
        on_subtitle_word: Callable[[str, str, bool, bool], None] | None = None,
        on_tts_level: Callable[[float], None] | None = None,
        edge_synthesizer: EdgeTTSSynthesizer | None = None,
        playback_backend: AudioPlaybackBackend | None = None,
        sapi_backend: SapiBackend | None = None,
    ) -> None:
        self._paths = paths
        self._settings = settings
        self._com_sta = com_sta
        self._ducker = ducker
        self._on_subtitle_word = on_subtitle_word
        self._on_tts_level = on_tts_level
        self._edge: EdgeTTSSynthesizer = (
            edge_synthesizer if edge_synthesizer is not None else _DefaultEdgeTTSSynthesizer()
        )
        self._player: AudioPlaybackBackend = (
            playback_backend if playback_backend is not None else _QtMultimediaPlaybackBackend()
        )
        self._sapi: SapiBackend = sapi_backend if sapi_backend is not None else _Win32SapiBackend()

        self._tts_cache_dir = self._paths.cache_dir / "tts"
        self._tts_cache_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._speaking = False
        self._cancel_event = threading.Event()
        self._active_generation = 0
        self._speak_async_lock = asyncio.Lock()

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def resolve_voice_name(self, *, profile_id: str, profile_default_voice: str = "") -> str:
        override = self._settings.voice.voice_by_profile.get(profile_id, "").strip()
        if override:
            return override
        if profile_default_voice.strip():
            return profile_default_voice.strip()
        return DEFAULT_EDGE_VOICE

    def cancel(self) -> None:
        """Cancelación inmediata (*barge-in* o apagado rápido <1 s)."""
        with self._lock:
            self._active_generation += 1
            self._cancel_event.set()
            was_speaking = self._speaking
            self._speaking = False

        with contextlib.suppress(Exception):
            self._player.stop()

        if was_speaking or self._com_sta.is_running:
            with contextlib.suppress(Exception):
                if self._com_sta.is_running:
                    self._com_sta.run_sync(self._sapi.purge, timeout=0.4)
                else:
                    self._sapi.purge()

        self._ducker.restore_sync()
        self._emit_level(0.0)
        self._emit_subtitle("", "", True, True)

    def _emit_subtitle(self, full_text: str, word: str, is_final: bool, clear: bool) -> None:
        if self._on_subtitle_word is not None:
            with contextlib.suppress(Exception):
                self._on_subtitle_word(full_text, word, is_final, clear)

    def _emit_level(self, level: float) -> None:
        if self._on_tts_level is not None:
            with contextlib.suppress(Exception):
                self._on_tts_level(max(0.0, min(1.0, float(level))))

    async def _drive_subtitles_and_level(
        self,
        clean_text: str,
        word_timings: tuple[WordTiming, ...],
        done_event: asyncio.Event,
        generation: int,
    ) -> None:
        started_at = time.monotonic()
        if word_timings:
            accumulated: list[str] = []
            for idx, wt in enumerate(word_timings):
                if self._is_cancelled(generation) or done_event.is_set():
                    break
                target_sec = wt.offset_ms / 1000.0
                while not done_event.is_set() and not self._is_cancelled(generation):
                    elapsed = time.monotonic() - started_at
                    if elapsed >= target_sec:
                        break
                    # Envolvente suave de nivel visual mientras avanza el habla
                    pulse = 0.35 + 0.35 * abs(math.sin(elapsed * 14.0))
                    self._emit_level(pulse)
                    await asyncio.sleep(min(0.04, max(0.01, target_sec - elapsed)))
                if self._is_cancelled(generation):
                    break
                accumulated.append(wt.text)
                self._emit_subtitle(
                    " ".join(accumulated),
                    wt.text,
                    idx == len(word_timings) - 1,
                    False,
                )
                self._emit_level(0.75)

            while not done_event.is_set() and not self._is_cancelled(generation):
                elapsed = time.monotonic() - started_at
                self._emit_level(0.30 + 0.30 * abs(math.sin(elapsed * 12.0)))
                await asyncio.sleep(0.05)
        else:
            words = clean_text.split()
            word_step = max(0.12, 0.24 / max(0.5, self._settings.voice.tts_rate))
            accumulated = []
            for idx, word in enumerate(words):
                if self._is_cancelled(generation) or done_event.is_set():
                    break
                accumulated.append(word)
                self._emit_subtitle(" ".join(accumulated), word, idx == len(words) - 1, False)
                self._emit_level(0.65)
                await asyncio.sleep(word_step)
            while not done_event.is_set() and not self._is_cancelled(generation):
                self._emit_level(0.40)
                await asyncio.sleep(0.05)

    def _is_cancelled(self, generation: int) -> bool:
        with self._lock:
            return self._cancel_event.is_set() or generation != self._active_generation

    async def _play_edge_speech(
        self,
        clean_text: str,
        speech: SynthesizedSpeech,
        generation: int,
    ) -> bool:
        if not speech.audio_bytes:
            return False

        tmp_file = self._tts_cache_dir / f"tts_{uuid.uuid4().hex}.mp3"
        loop = asyncio.get_running_loop()
        done_event = asyncio.Event()
        playback_ok = False

        def _on_done(ok: bool) -> None:
            nonlocal playback_ok
            playback_ok = ok
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(done_event.set)

        try:
            tmp_file.write_bytes(speech.audio_bytes)
            started = self._player.play_file(tmp_file, on_finished=_on_done)
            if not started:
                return False

            subtitle_task = asyncio.create_task(
                self._drive_subtitles_and_level(clean_text, speech.word_timings, done_event, generation)
            )
            try:
                while not done_event.is_set():
                    if self._is_cancelled(generation):
                        self._player.stop()
                        done_event.set()
                        break
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(done_event.wait(), timeout=0.05)
            finally:
                subtitle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await subtitle_task

            return bool(playback_ok and not self._is_cancelled(generation))
        finally:
            with contextlib.suppress(OSError):
                tmp_file.unlink(missing_ok=True)

    async def _speak_sapi_fallback(self, clean_text: str, generation: int) -> bool:
        rate = self._settings.voice.tts_rate
        if self._com_sta.is_running:
            started = bool(
                await self._com_sta.run_async(
                    self._sapi.speak_async_start, clean_text, rate=rate
                )
            )
        else:
            started = self._sapi.speak_async_start(clean_text, rate=rate)

        if not started:
            return False

        done_event = asyncio.Event()
        subtitle_task = asyncio.create_task(
            self._drive_subtitles_and_level(clean_text, (), done_event, generation)
        )
        try:
            max_wait = max(5.0, min(60.0, len(clean_text) * 0.14))
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                if self._is_cancelled(generation):
                    if self._com_sta.is_running:
                        await self._com_sta.run_async(self._sapi.purge)
                    else:
                        self._sapi.purge()
                    return False
                if self._com_sta.is_running:
                    still_talking = bool(await self._com_sta.run_async(self._sapi.is_speaking))
                else:
                    still_talking = self._sapi.is_speaking()
                if not still_talking:
                    break
                await asyncio.sleep(0.06)
            return not self._is_cancelled(generation)
        finally:
            done_event.set()
            subtitle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subtitle_task

    async def speak(
        self,
        text: str,
        *,
        profile_id: str = "raphael",
        profile_default_voice: str = "",
        allow_ducking: bool = True,
    ) -> TTSResult:
        """Sintetiza y reproduce texto con Edge-TTS + ducking y fallback transparente a SAPI."""
        if self._settings.voice.tts_muted:
            return TTSResult(ok=True, engine="muted")

        clean_text = sanitize_text_for_speech(text)
        if not clean_text:
            return TTSResult(ok=True, engine="empty")

        # Si ya había una locución previa, interrumpirla limpiamente (barge-in)
        if self.is_speaking:
            self.cancel()

        async with self._speak_async_lock:
            with self._lock:
                self._active_generation += 1
                generation = self._active_generation
                self._cancel_event.clear()
                self._speaking = True

            voice_name = self.resolve_voice_name(
                profile_id=profile_id,
                profile_default_voice=profile_default_voice,
            )
            rate_str = format_edge_rate(self._settings.voice.tts_rate)
            ducked = False
            try:
                if allow_ducking and not self._settings.voice.in_call_text_only:
                    await self._ducker.duck(level_pct=self._settings.voice.ducking_level_pct)
                    ducked = True

                # 1. Intento principal: Edge-TTS + QtMultimedia
                edge_error: str | None = None
                try:
                    speech = await self._edge.synthesize(
                        clean_text,
                        voice=voice_name,
                        rate_str=rate_str,
                    )
                    if self._is_cancelled(generation):
                        return TTSResult(ok=False, engine="edge-tts", cancelled=True)
                    if await self._play_edge_speech(clean_text, speech, generation):
                        return TTSResult(ok=True, engine="edge-tts")
                    if self._is_cancelled(generation):
                        return TTSResult(ok=False, engine="edge-tts", cancelled=True)
                    edge_error = "Reproductor QtMultimedia no disponible; activando fallback SAPI."
                except Exception as exc:
                    edge_error = str(exc)
                    logger.warning("Edge-TTS falló (%s); usando fallback SAPI en ComStaThread.", exc)

                if self._is_cancelled(generation):
                    return TTSResult(ok=False, engine="edge-tts", cancelled=True)

                # 2. Fallback automático: SAPI en ComStaThread
                sapi_ok = await self._speak_sapi_fallback(clean_text, generation)
                if self._is_cancelled(generation):
                    return TTSResult(ok=False, engine="sapi", cancelled=True)
                if sapi_ok:
                    return TTSResult(ok=True, engine="sapi")
                return TTSResult(ok=False, engine="sapi", error=edge_error or "SAPI no disponible.")
            finally:
                with self._lock:
                    if generation == self._active_generation:
                        self._speaking = False
                if ducked:
                    await self._ducker.restore()
                self._emit_level(0.0)
                self._emit_subtitle("", "", True, True)
