from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import contextlib
import ctypes
from dataclasses import dataclass
import io
import json
import logging
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Final, Protocol
import unicodedata
import wave

import httpx
import numpy as np
import numpy.typing as npt

from deusex.core.config import DeusExSettings, SecretsManager
from deusex.core.paths import AppPaths
from deusex.core.runtime import AudioInputThread
from deusex.modules.voice.context_gate import ContextGate
from deusex.modules.voice.dsp import DEFAULT_SAMPLE_RATE, FarFieldAudioConditioner
from deusex.modules.voice.phonetics import (
    WakePhoneticMatcher,
    strip_accents,
)
from deusex.modules.voice.phonetics import (
    clean_brand_articles as clean_brand_articles,
)
from deusex.modules.voice.voiceprint import OwnerVoiceprintGuard

logger = logging.getLogger(__name__)

DEFAULT_BLOCK_SAMPLES: Final[int] = 1_024  # 64 ms a 16 kHz
_SILENCE_COMMIT_WAKE_ONLY_SEC: Final[float] = 0.85
_SILENCE_COMMIT_COMPOUND_SEC: Final[float] = 1.15
_SILENCE_COMMIT_SEC: Final[float] = 1.15  # retrocompatibilidad
_INITIAL_WAKE_WAIT_SEC: Final[float] = 2.6
_MAX_COMMAND_SEC: Final[float] = 12.0
_MIC_LEVEL_INTERVAL_SEC: Final[float] = 0.06
_CAL_PROGRESS_INTERVAL_SEC: Final[float] = 0.15

_COMMAND_VERBS_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:enciende|apaga|sube|baja|pon|abre|cierra|silencia|activa|desactiva|busca|dime|como|cómo|que|qué)\b",
    re.IGNORECASE,
)
_LEADING_FILLER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:oye|hey|eh|por favor)\s+)+",
    re.IGNORECASE,
)

_VK_MAP: Final[dict[str, int]] = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "super": 0x5B,
    "space": 0x20,
    "espacio": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "caps": 0x14,
    "capslock": 0x14,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): ord(str(d)) for d in range(10)},
}

_NON_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[^\w\s]", re.IGNORECASE)
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class AudioInputDevice:
    """Dispositivo de micrófono disponible en el sistema."""

    index: int
    name: str
    channels: int
    default_sample_rate: float
    is_default: bool = False


def calc_rms_int16(pcm16: bytes) -> float:
    """Calcula el RMS en escala int16 [0.0, 32768.0] para un bloque de audio PCM16."""
    if not pcm16:
        return 0.0
    arr = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr * arr)))


@dataclass(frozen=True, slots=True)
class VoiceUtterance:
    """Transcripción capturada lista para evaluación contextual y enrutamiento."""

    transcript: str
    triggered_by: str  # wake_word | ptt | follow_up
    wake_word: str
    audio_samples: npt.NDArray[np.float32]
    avg_vocal_ratio: float
    wav_bytes: bytes = b""
    wav_peak: float = 0.0
    wav_duration_sec: float = 0.0
    fuzzy_wake_only: bool = False
    rms_int16: float = 0.0
    noise_floor_int16: float = 120.0


SP_VOCABULARY_PROMPT: Final[str] = (
    "Raphael, abre Spotify, cierra Spotify, pon YouTube en la tele, enciende la tele, "
    "apaga la tele, sube el volumen, baja el volumen, pon un temporizador, abre Discord, "
    "abre Chrome, abre el navegador, Chromecast, Xbox."
)


type GeminiAudioTranscriber = Callable[[bytes], Awaitable[str | None]] | Callable[[bytes, tuple[str, ...]], Awaitable[str | None]]

WHISPER_NATURAL_PROMPT: Final[str] = (
    "Comandos de voz en español con nombres de apps en inglés como YouTube, Spotify, Hearthstone o Discord."
)

_ACTION_VERBS: Final[frozenset[str]] = frozenset(
    {
        "abre", "abrir", "cierra", "cerrar", "pon", "poner", "enciende", "encender",
        "apaga", "apagar", "sube", "subir", "baja", "bajar", "busca", "buscar",
        "dime", "silencia", "activa", "desactiva", "puedes", "confirma", "confirmar",
        "cancela", "cancelar", "reproduce", "reproducir", "toca", "tocar",
        "pausa", "pausar", "reanuda", "reanudar", "siguiente", "anterior",
    }
)

_WHISPER_PHANTOMS: Final[tuple[str, ...]] = (
    "gracias por ver",
    "suscríbete",
    "suscribete",
    "subtítulos realizados",
    "subtitulos realizados",
    "transcripción por",
    "transcripcion por",
    "gracias por su atención",
    "gracias por su atencion",
)


def pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """Convierte un buffer PCM16 mono a un archivo WAV en memoria."""
    if not pcm16:
        return b""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(max(8_000, int(sample_rate)))
        wf.writeframes(pcm16)
    return buf.getvalue()


def prepare_wav_for_cloud_stt(
    pcm16: bytes,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> tuple[bytes, float, float]:
    """
    Prepara y normaliza linealmente el audio PCM16 para STT en nube:
    - Convierte a float32 [-1.0, 1.0].
    - No recorta silencios iniciales para no amputar plosivas (pon, cierra).
    - Limita únicamente silencio final excesivo con margen de 320 ms.
    - Calcula peak_before_norm = max(abs(audio)).
    - Aplica ganancia lineal suave únicamente si peak < 0.15 (hacia pico 0.45, tope 4.0x, cero np.tanh).
    - Empaqueta como WAV 16-bit mono en memoria.
    Retorna: (wav_bytes, peak_before_norm, duration_sec).
    """
    if not pcm16:
        return b"", 0.0, 0.0

    raw = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if raw.size == 0:
        return b"", 0.0, 0.0

    peak_before = float(np.max(np.abs(raw)))

    # Margen de 320 ms y umbral más bajo para silencio final
    margin_samples = round(0.32 * sample_rate)
    silence_thresh = 0.006

    voiced_idx = np.where(np.abs(raw) >= silence_thresh)[0]
    if voiced_idx.size > 0:
        end_idx = min(raw.size, int(voiced_idx[-1]) + 1 + margin_samples)
        trimmed = raw[:end_idx]
    else:
        trimmed = raw

    if trimmed.size == 0:
        trimmed = raw

    peak_trimmed = float(np.max(np.abs(trimmed)))
    # Ganancia lineal suave únicamente si la señal es muy baja (< 0.15)
    if peak_trimmed < 0.15 and peak_trimmed > 1e-4:
        gain = min(4.0, 0.45 / peak_trimmed)
        normalized = np.clip(trimmed * gain, -1.0, 1.0)
    else:
        normalized = trimmed

    pcm_out = (normalized * 32767.0).astype(np.int16).tobytes()
    wav_out = pcm16_to_wav_bytes(pcm_out, sample_rate)
    duration_out = float(normalized.size) / float(sample_rate)

    return wav_out, peak_before, duration_out


def _is_speech_recognition_available() -> bool:
    try:
        import speech_recognition  # noqa: F401
        return True
    except ImportError:
        return False


def _google_stt_sync(pcm16_bytes: bytes, sample_rate: int) -> str | None:
    """Reconoce audio PCM16 usando Google STT (es-CL con fallback a en-US)."""
    try:
        import speech_recognition as sr
    except ImportError:
        return None

    try:
        recognizer = sr.Recognizer()
        audio_data = sr.AudioData(pcm16_bytes, sample_rate, 2)
        try:
            text = recognizer.recognize_google(audio_data, language="es-CL")
            text_str = str(text).strip() if text else ""
            if text_str:
                return text_str
        except sr.UnknownValueError:
            pass

        try:
            text_en = recognizer.recognize_google(audio_data, language="en-US")
            text_en_str = str(text_en).strip() if text_en else ""
            if text_en_str:
                return text_en_str
        except sr.UnknownValueError:
            pass

        return None
    except Exception as exc:
        logger.debug("Google STT fallo/error: %s", exc)
        return None


async def _azure_stt_rest(
    wav_bytes: bytes,
    azure_key: str,
    azure_region: str = "eastus",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Reconoce audio WAV usando la API REST oficial de Azure Cognitive Services Speech (es-CL)."""
    clean_key = azure_key.strip()
    if not clean_key:
        return None

    url = (
        f"https://{azure_region.strip()}.stt.speech.microsoft.com/speech/recognition/conversation/"
        f"cognitiveservices/v1?language=es-CL&format=simple"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": clean_key,
        "Content-Type": f"audio/wav; codecs=audio/pcm; samplerate={sample_rate}",
    }
    manage_client = client is None
    c = client if client is not None else httpx.AsyncClient(timeout=3.5)
    try:
        resp = await c.post(url, headers=headers, content=wav_bytes, timeout=3.5)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("RecognitionStatus") == "Success":
                return str(data.get("DisplayText") or "").strip() or None
        return None
    except Exception as exc:
        logger.debug("Azure STT fallo/timeout: %s", exc)
        return None
    finally:
        if manage_client:
            await c.aclose()


async def _groq_whisper(
    wav_bytes: bytes,
    groq_key: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    clean_key = groq_key.strip()
    if not clean_key:
        return None
    manage_client = client is None
    c = client if client is not None else httpx.AsyncClient(timeout=3.5)
    try:
        files = {"file": ("command.wav", wav_bytes, "audio/wav")}
        data = {
            "model": "whisper-large-v3",
            "language": "es",
            "temperature": "0",
            "prompt": WHISPER_NATURAL_PROMPT,
        }
        headers = {"Authorization": f"Bearer {clean_key}"}
        resp = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
            timeout=3.5,
        )
        if resp.status_code == 200:
            text = resp.json().get("text", "").strip()
            return text or None
        return None
    except Exception as exc:
        logger.debug("Groq STT fallo/timeout: %s", exc)
        return None
    finally:
        if manage_client:
            await c.aclose()


async def _openai_whisper(
    wav_bytes: bytes,
    openai_key: str,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    clean_key = openai_key.strip()
    if not clean_key:
        return None
    manage_client = client is None
    c = client if client is not None else httpx.AsyncClient(timeout=4.5)
    try:
        files = {"file": ("command.wav", wav_bytes, "audio/wav")}
        data = {
            "model": "whisper-1",
            "language": "es",
            "temperature": "0",
            "prompt": WHISPER_NATURAL_PROMPT,
        }
        headers = {"Authorization": f"Bearer {clean_key}"}
        resp = await c.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            data=data,
            files=files,
            timeout=4.5,
        )
        if resp.status_code == 200:
            text = resp.json().get("text", "").strip()
            return text or None
        return None
    except Exception as exc:
        logger.debug("OpenAI STT fallo/timeout: %s", exc)
        return None
    finally:
        if manage_client:
            await c.aclose()


def choose_best_stt_candidate(
    candidates: list[tuple[str, str]],
    *,
    extra_vocabulary: tuple[str, ...] = (),
    fast_path_matcher: Callable[[str], bool] | None = None,
    wake_matcher: WakePhoneticMatcher | None = None,
) -> tuple[str, str, float] | None:
    """
    Árbitro determinista STT en <1 ms:
    Compara candidatos (texto, engine_id) puntuando según:
    - Prioridad nativa ES-CL (azure: +12, google: +8, gemini: +6, groq: +4, openai: +2).
    - +50 pts si calza directo en Fast-Path.
    - +40 pts si contiene nombre exacto de apps/marcas instaladas (len >= 4).
    - +25 pts si contiene wake-word del asistente.
    - +15 pts si inicia con verbo de acción válido en español.
    - -30 pts penalización por alucinación de Whisper (fantasmas, comas excesivas, Title-Case artificial).
    Retorna: (mejor_texto, mejor_motor, mejor_puntaje) o None si no hay candidatos válidos.
    """
    if not candidates:
        return None

    scored: list[tuple[float, str, str]] = []

    for raw_text, engine_id in candidates:
        text = raw_text.strip()
        if not text:
            continue

        cmd = text
        if wake_matcher is not None:
            _, extracted = wake_matcher.extract_command_post_wake(text)
            cmd = extracted or wake_matcher.normalize_stt_text(text)
        else:
            cmd = clean_brand_articles(text)

        # 1. Base tie-break por prioridad nativa ES-CL
        if "azure" in engine_id:
            score = 12.0
        elif "google" in engine_id:
            score = 8.0
        elif "gemini" in engine_id:
            score = 6.0
        elif "groq" in engine_id:
            score = 4.0
        elif "openai" in engine_id:
            score = 2.0
        else:
            score = 0.0

        # 2. +50 Fast-Path match
        if fast_path_matcher is not None and (fast_path_matcher(cmd) or fast_path_matcher(text)):
            score += 50.0

        # 3. +40 Apps / marcas en vocabulario
        norm_text = strip_accents(text.lower())
        cand_words = set(re.findall(r"\b[a-z0-9]+\b", norm_text))
        app_matched = False
        for app in extra_vocabulary:
            if len(app) >= 4:
                norm_app = strip_accents(app.lower()).strip()
                if " " in norm_app:
                    if re.search(rf"\b{re.escape(norm_app)}\b", norm_text):
                        app_matched = True
                        break
                elif norm_app in cand_words:
                    app_matched = True
                    break
        if app_matched:
            score += 40.0

        # 4. +25 Wake-word presente
        has_wake = False
        if wake_matcher is not None:
            det, _ = wake_matcher.detect_in_vosk(text)
            has_wake = det
        else:
            has_wake = bool(re.search(r"\b(raphael|rafael|rafa)\b", norm_text))
        if has_wake:
            score += 25.0

        # 5. +15 Verbo de acción español
        cmd_words = cmd.strip().lower().split()
        first_word = cmd_words[0] if cmd_words else ""
        if first_word in _ACTION_VERBS:
            score += 15.0

        # 6. -30 Filtro anti-alucinación Whisper
        lower_raw = text.lower()
        is_hallucination = any(ph in lower_raw for ph in _WHISPER_PHANTOMS)

        raw_words = text.split()
        if text.count(",") >= 2 and len(raw_words) < 7:
            is_hallucination = True

        if len(raw_words) <= 8:
            title_count = sum(
                1 for w in raw_words if len(w) > 1 and w[0].isupper() and w[1:].islower()
            )
            if title_count >= 3:
                is_hallucination = True

        if is_hallucination:
            score -= 30.0

        scored.append((score, text, engine_id))

    if not scored:
        return None

    # Ordenar por puntaje descendente
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_text, best_engine = scored[0]
    return best_text, best_engine, best_score


async def transcribe_wav_cascade(
    wav_bytes: bytes,
    *,
    pcm16_bytes: bytes = b"",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    secrets: SecretsManager | None = None,
    gemini_transcriber: GeminiAudioTranscriber | None = None,
    groq_api_key: str | None = None,
    openai_api_key: str | None = None,
    azure_speech_key: str | None = None,
    azure_region: str | None = None,
    client: httpx.AsyncClient | None = None,
    extra_vocabulary: tuple[str, ...] = (),
    fast_path_matcher: Callable[[str], bool] | None = None,
    wake_matcher: WakePhoneticMatcher | None = None,
    google_transcriber: Callable[[bytes], Awaitable[str | None]] | None = None,
) -> tuple[str, str] | None:
    """
    Árbitro STT en paralelo con respaldo inteligente en cascada:
    Fase 1 (Paralelo): Dispara simultáneamente Google STT (es-CL), Groq whisper-large-v3
    y Azure Speech (es-CL). Elige el mejor candidato con un árbitro determinista en <1 ms.
    Fase 2 (Respaldo): Si Fase 1 no produce candidato válido (o score < 10), prueba
    Gemini Audio WAV -> OpenAI whisper-1 -> None (Vosk local).
    """
    if not wav_bytes:
        return None

    pcm16_data = pcm16_bytes
    if not pcm16_data and len(wav_bytes) > 44 and wav_bytes[:4] == b"RIFF":
        pcm16_data = wav_bytes[44:]

    groq_key = groq_api_key or (secrets.get("GROQ_API_KEY") if secrets is not None else None)
    openai_key = openai_api_key or (secrets.get("OPENAI_API_KEY") if secrets is not None else None)
    azure_key = azure_speech_key or (secrets.get("AZURE_SPEECH_KEY") if secrets is not None else None)
    az_region: str = (
        azure_region
        or (secrets.get("AZURE_SPEECH_REGION") if secrets is not None else None)
        or "eastus"
    )

    # Fase 1: Disparo en paralelo de motores rápidos
    fast_coros: list[tuple[str, Awaitable[str | None]]] = []

    # 1. Google STT (inyectado o real vía speech_recognition)
    if google_transcriber is not None:
        fast_coros.append(("google:es-CL", google_transcriber(pcm16_data or wav_bytes)))
    elif pcm16_data and _is_speech_recognition_available():
        fast_coros.append(
            (
                "google:es-CL",
                asyncio.wait_for(
                    asyncio.to_thread(_google_stt_sync, pcm16_data, sample_rate),
                    timeout=3.5,
                ),
            )
        )

    # 2. Groq Whisper (modelo completo whisper-large-v3)
    if groq_key:
        fast_coros.append(("groq:whisper-large-v3", _groq_whisper(wav_bytes, groq_key, client)))

    # 3. Azure Speech (si está configurada la clave)
    if azure_key:
        fast_coros.append(("azure:es-CL", _azure_stt_rest(wav_bytes, azure_key, az_region, sample_rate, client)))

    candidates: list[tuple[str, str]] = []
    if fast_coros:
        raw_results = await asyncio.gather(*[coro for _, coro in fast_coros], return_exceptions=True)
        for (engine_id, _), res in zip(fast_coros, raw_results, strict=False):
            if isinstance(res, str) and res.strip():
                candidates.append((res.strip(), engine_id))

    if candidates:
        best = choose_best_stt_candidate(
            candidates,
            extra_vocabulary=extra_vocabulary,
            fast_path_matcher=fast_path_matcher,
            wake_matcher=wake_matcher,
        )
        if best is not None:
            best_text, best_engine, best_score = best
            summary = " | ".join(f"{engine}={text!r}" for text, engine in candidates)
            logger.info("STT paralelo: %s -> ganador=%s (score=%.0f)", summary, best_engine, best_score)
            if best_score > 0.0:
                return best_text, best_engine

    # Fase 2: Respaldo inteligente si Fase 1 falló o score <= 0
    # 2.1 Gemini Multimodal Audio
    if gemini_transcriber is not None:
        try:
            try:
                gemini_text = await asyncio.wait_for(
                    gemini_transcriber(wav_bytes, extra_vocabulary),  # type: ignore[call-arg]
                    timeout=5.5,
                )
            except TypeError:
                gemini_text = await asyncio.wait_for(
                    gemini_transcriber(wav_bytes),  # type: ignore[call-arg]
                    timeout=5.5,
                )
            if gemini_text and gemini_text.strip():
                return gemini_text.strip(), "gemini:audio"
        except Exception as exc:
            logger.debug("Gemini STT fallo/timeout: %s", exc)

    # 2.2 OpenAI Whisper
    if openai_key:
        try:
            openai_text = await _openai_whisper(wav_bytes, openai_key, client)
            if openai_text and openai_text.strip():
                return openai_text.strip(), "openai:whisper-1"
        except Exception as exc:
            logger.debug("OpenAI STT fallo/timeout: %s", exc)

    return None


class SpeechRecognizer(Protocol):
    """Contrato mínimo de reconocedor offline (Vosk real o fake en tests)."""

    def accept_waveform(self, pcm16: bytes) -> bool: ...
    def result_text(self) -> str: ...
    def partial_text(self) -> str: ...
    def final_result_text(self) -> str: ...
    def reset(self) -> None: ...


class KeyStatePoller(Protocol):
    """Contrato para sondeo no invasivo de teclas Push-to-Talk."""

    def is_combo_pressed(self, combo: str) -> bool: ...


def normalize_for_wake_match(text: str) -> str:
    """Normaliza tildes y puntuación para cotejar wake-words en español."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    without_accents = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    cleaned = _NON_WORD_RE.sub(" ", without_accents)
    return _SPACE_RE.sub(" ", cleaned).strip()


def extract_wake_word(text: str, wake_words: tuple[str, ...]) -> tuple[str | None, str]:
    """
    Busca una wake-word en el texto normalizado y devuelve `(wake_word_encontrada, resto_del_comando)`.
    """
    norm_text = normalize_for_wake_match(text)
    if not norm_text:
        return None, ""

    cands_set: set[str] = set()
    for w in wake_words:
        nw = normalize_for_wake_match(w)
        if not nw:
            continue
        cands_set.add(nw)
        f_variant = nw.replace("ph", "f")
        cands_set.add(f_variant)
        for base in (nw, f_variant):
            if base.endswith("el") and len(base) > 2:
                cands_set.add(f"{base[:-2]} el")

    candidates = sorted(cands_set, key=len, reverse=True)
    for cand in candidates:
        if not cand:
            continue
        pattern = re.compile(rf"(?:^|\s){re.escape(cand)}(?:\s+(.*)|$)")
        match = pattern.search(norm_text)
        if match is not None:
            remainder = (match.group(1) or "").strip()
            return cand, remainder
    return None, ""


def parse_hotkey_vks(combo: str) -> tuple[int, ...]:
    """Convierte un atajo textual (ej. 'Ctrl+Alt+V') a códigos VK de Windows."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    vks: list[int] = []
    for part in parts:
        vk = _VK_MAP.get(part)
        if vk is None:
            return ()
        if vk not in vks:
            vks.append(vk)
    return tuple(vks)


class Win32AsyncKeyPoller:
    """Implementación PTT sin hooks invasivos vía Win32 `GetAsyncKeyState`."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, ...]] = {}

    def is_combo_pressed(self, combo: str) -> bool:
        if sys.platform != "win32":
            return False
        vks = self._cache.get(combo)
        if vks is None:
            vks = parse_hotkey_vks(combo)
            self._cache[combo] = vks
        if not vks:
            return False

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        get_state = windll.user32.GetAsyncKeyState
        return all((int(get_state(vk)) & 0x8000) != 0 for vk in vks)


def is_vosk_model_present(model_dir: Path) -> bool:
    """Verifica que el modelo Vosk ES pre-empaquetado exista y contenga sus archivos clave."""
    if not model_dir.is_dir():
        return False
    has_am = (model_dir / "am" / "final.mdl").is_file() or (model_dir / "final.mdl").is_file()
    has_conf = (model_dir / "conf" / "model.conf").is_file() or (model_dir / "conf").is_dir()
    return has_am or has_conf


def list_input_devices() -> list[AudioInputDevice]:
    """Enumera los micrófonos de entrada disponibles vía `sounddevice`."""
    try:
        import sounddevice as sd
    except Exception:
        return []

    devices: list[AudioInputDevice] = []
    try:
        raw_list = sd.query_devices()
        default_pair = getattr(sd.default, "device", (None, None))
        default_in = default_pair[0] if isinstance(default_pair, (tuple, list)) and default_pair else None
        for idx, info in enumerate(raw_list):
            max_in = int(info.get("max_input_channels", 0) or 0)
            if max_in <= 0:
                continue
            devices.append(
                AudioInputDevice(
                    index=idx,
                    name=str(info.get("name", f"Micrófono {idx}")),
                    channels=max_in,
                    default_sample_rate=float(info.get("default_samplerate", DEFAULT_SAMPLE_RATE)),
                    is_default=(idx == default_in),
                )
            )
    except Exception as exc:
        logger.warning("No se pudieron enumerar los micrófonos de entrada: %s", exc)
    return devices


class _VoskRecognizerAdapter:
    """Adaptador tipado sobre `vosk.KaldiRecognizer`."""

    def __init__(
        self,
        model_dir: Path,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        *,
        model: Any | None = None,
        grammar_json: str | None = None,
    ) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        self._model = model if model is not None else vosk.Model(str(model_dir))
        if grammar_json:
            self._rec = vosk.KaldiRecognizer(self._model, float(sample_rate), grammar_json)
        else:
            self._rec = vosk.KaldiRecognizer(self._model, float(sample_rate))

    @property
    def model(self) -> Any:
        return self._model

    @staticmethod
    def _extract_json_field(raw: str, key: str) -> str:
        try:
            payload = json.loads(raw)
        except Exception:
            return ""
        val = payload.get(key, "")
        return str(val).strip() if isinstance(val, str) else ""

    def accept_waveform(self, pcm16: bytes) -> bool:
        return bool(self._rec.AcceptWaveform(pcm16))

    def result_text(self) -> str:
        return self._extract_json_field(str(self._rec.Result()), "text")

    def partial_text(self) -> str:
        return self._extract_json_field(str(self._rec.PartialResult()), "partial")

    def final_result_text(self) -> str:
        return self._extract_json_field(str(self._rec.FinalResult()), "text")

    def reset(self) -> None:
        self._rec.Reset()


class VoiceInputEngine:
    """
    Motor de escucha continua offline ejecutado exclusivamente en `AudioInputThread`:
    - Captura `sounddevice` -> `FarFieldAudioConditioner` -> Vosk ES.
    - Detección de wake-word (perfil + personalizadas), PTT Win32 (`GetAsyncKeyState`),
      ventana de seguimiento continuo (6 s) y calibración biométrica guiada (6 s).
    - Cero llamadas `asyncio.create_task()` desde el hilo de audio (Antídoto #5).
    """

    def __init__(
        self,
        *,
        paths: AppPaths,
        settings: DeusExSettings,
        audio_thread: AudioInputThread,
        conditioner: FarFieldAudioConditioner,
        voiceprint: OwnerVoiceprintGuard,
        context_gate: ContextGate,
        on_utterance: Callable[[VoiceUtterance], None],
        on_wake_word: Callable[[str, str], None] | None = None,
        on_mic_level: Callable[[float, bool, int | None], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        is_tts_speaking: Callable[[], bool] | None = None,
        on_calibration_event: Callable[[str, float, str], None] | None = None,
        on_capture_cancelled: Callable[[], None] | None = None,
        recognizer_factory: Callable[[Path, int], SpeechRecognizer] | None = None,
        key_poller: KeyStatePoller | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
    ) -> None:
        self._paths = paths
        self._settings = settings
        self._audio_thread = audio_thread
        self._conditioner = conditioner
        self._voiceprint = voiceprint
        self._context_gate = context_gate
        self._on_utterance = on_utterance
        self._on_wake_word = on_wake_word
        self._on_mic_level = on_mic_level
        self._on_barge_in = on_barge_in
        self._is_tts_speaking = is_tts_speaking
        self._on_calibration_event = on_calibration_event
        self._on_capture_cancelled = on_capture_cancelled
        self._recognizer_factory = recognizer_factory
        self._key_poller: KeyStatePoller = key_poller if key_poller is not None else Win32AsyncKeyPoller()
        self._sample_rate = max(8_000, int(sample_rate))

        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._worker_exited = threading.Event()
        self._worker_exited.set()

        self._running = False
        self._suspended = False
        self._wake_words: tuple[str, ...] = ("raphael", "deusex")
        self._phonetic_matcher = WakePhoneticMatcher(self._wake_words)
        self._recognizer: SpeechRecognizer | None = None
        self._bargein_recognizer: SpeechRecognizer | None = None
        self._active_device_index: int | None = settings.voice.input_device_index
        self._last_error: str | None = None

        # Ring buffer de 3.0 s (48 bloques de 1024 muestras = ~3.072 s) y VAD adaptativo
        self._ring_buffer: deque[bytes] = deque(maxlen=48)
        self._silence_feed_counter: int = 16
        self._noise_floor_int16: float = 120.0
        self._cal_noise_blocks: list[float] = []

        # Estado interno de captura de comando (solo manipulado desde el hilo de audio o tests)
        self._capturing = False
        self._capture_trigger = "wake_word"
        self._capture_wake_word = ""
        self._capture_started_at = 0.0
        self._capture_last_speech_at = 0.0
        self._capture_seed_text = ""
        self._capture_segments: list[str] = []
        self._capture_audio: list[npt.NDArray[np.float32]] = []
        self._capture_vocal_ratios: list[float] = []
        self._capture_pcm16: list[bytes] = []
        self._capture_last_partial = ""
        self._capture_fuzzy_only = False
        self._capture_max_rms = 0.0
        self._capture_ambient_rms = 120.0
        self._capture_umbral_voz = 130.0
        self._capture_umbral_corte = 108.0
        self._capture_had_post_wake_speech = False
        self._ptt_was_down = False
        self._last_mic_emit = 0.0
        self._last_cal_emit = 0.0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def matcher(self) -> WakePhoneticMatcher:
        with self._lock:
            return self._phonetic_matcher

    def set_wake_words(self, profile_wake_words: tuple[str, ...]) -> None:
        combined: list[str] = []
        for word in (*profile_wake_words, *self._settings.voice.custom_wake_words):
            norm = normalize_for_wake_match(word)
            if norm and norm not in combined:
                combined.append(norm)
        if not combined:
            combined = ["deusex"]
        with self._lock:
            self._wake_words = tuple(combined)
            self._phonetic_matcher = WakePhoneticMatcher(self._wake_words)
            self._bargein_recognizer = None

    @property
    def is_suspended(self) -> bool:
        with self._lock:
            return self._suspended

    def set_suspended(self, suspended: bool) -> None:
        with self._lock:
            self._suspended = bool(suspended)
            if self._suspended:
                self._reset_capture_state()

    def request_device_change(self, device_index: int | None) -> None:
        with self._lock:
            self._active_device_index = device_index
        if self.is_running:
            self.stop()
            self.start()

    def start_calibration(self, *, duration_sec: float = 6.0) -> bool:
        if self._settings.general.privacy_mode:
            return False
        self._voiceprint.start_calibration(duration_sec=duration_sec)
        self._last_cal_emit = 0.0
        if self._on_calibration_event is not None:
            self._on_calibration_event(
                "started",
                0.0,
                "Habla con naturalidad durante 6 segundos para calibrar tu huella vocal.",
            )
        return True

    def cancel_calibration(self) -> None:
        if self._voiceprint.is_calibrating:
            self._voiceprint.cancel_calibration()
            if self._on_calibration_event is not None:
                self._on_calibration_event("cancelled", 0.0, "Calibración vocal cancelada.")

    def _ensure_recognizer(self) -> SpeechRecognizer | None:
        with self._lock:
            if self._recognizer is not None:
                return self._recognizer

        if self._recognizer_factory is not None:
            rec = self._recognizer_factory(self._paths.vosk_es_dir, self._sample_rate)
            with self._lock:
                self._recognizer = rec
            return rec

        if not is_vosk_model_present(self._paths.vosk_es_dir):
            with self._lock:
                self._last_error = "Modelo Vosk ES no encontrado en USER_DATA_DIR/models/vosk-es."
            logger.warning(self._last_error)
            return None

        try:
            rec = _VoskRecognizerAdapter(self._paths.vosk_es_dir, self._sample_rate)
        except Exception as exc:
            with self._lock:
                self._last_error = f"No se pudo cargar Vosk ES: {exc}"
            logger.exception("Fallo cargando modelo Vosk ES")
            return None

        with self._lock:
            self._recognizer = rec
            self._last_error = None
        return rec

    def _ensure_bargein_recognizer(self, main_rec: SpeechRecognizer | None) -> SpeechRecognizer | None:
        with self._lock:
            if self._bargein_recognizer is not None:
                return self._bargein_recognizer
        if not is_vosk_model_present(self._paths.vosk_es_dir):
            return None
        try:
            grammar_json = self._phonetic_matcher.build_bargein_grammar_json()
            vosk_model = getattr(main_rec, "model", None) if main_rec is not None else None
            rec = _VoskRecognizerAdapter(
                self._paths.vosk_es_dir,
                self._sample_rate,
                model=vosk_model,
                grammar_json=grammar_json,
            )
            with self._lock:
                self._bargein_recognizer = rec
            return rec
        except Exception as exc:
            logger.debug("No se pudo crear reconocedor barge-in con gramática cerrada: %s", exc)
            return None

    def start(self) -> bool:
        """Arranca la escucha continua en `AudioInputThread` sin bloquear el llamador."""
        with self._lock:
            if self._running:
                return True
            self._stop_requested.clear()
            self._worker_exited.clear()
            self._running = True
            self._active_device_index = self._settings.voice.input_device_index

        self._conditioner.reset()
        self._audio_thread.set_worker(self._audio_worker)
        return True

    def stop(self, *, timeout_sec: float = 0.8) -> None:
        """Detiene el stream de micrófono y libera `AudioInputThread` en <1 s."""
        with self._lock:
            was_running = self._running
            self._running = False
        self._stop_requested.set()
        self._audio_thread.set_worker(None)
        if was_running:
            self._worker_exited.wait(timeout=timeout_sec)
        self._reset_capture_state()

    def _reset_capture_state(self) -> None:
        self._capturing = False
        self._capture_trigger = "wake_word"
        self._capture_wake_word = ""
        self._capture_started_at = 0.0
        self._capture_last_speech_at = 0.0
        self._capture_seed_text = ""
        self._capture_segments.clear()
        self._capture_audio.clear()
        self._capture_vocal_ratios.clear()
        self._capture_pcm16.clear()
        self._capture_last_partial = ""
        self._capture_fuzzy_only = False
        self._capture_max_rms = 0.0
        self._capture_had_post_wake_speech = False
        self._ptt_was_down = False

    def _begin_capture(
        self,
        *,
        trigger: str,
        wake_word: str,
        seed_text: str = "",
        fuzzy_only: bool = False,
        now: float,
    ) -> None:
        self._capturing = True
        self._capture_trigger = trigger
        self._capture_wake_word = wake_word
        self._capture_started_at = now
        self._capture_last_speech_at = now
        self._capture_seed_text = seed_text.strip()
        self._capture_segments.clear()
        self._capture_audio.clear()
        self._capture_vocal_ratios.clear()
        self._capture_last_partial = ""
        self._capture_fuzzy_only = fuzzy_only
        self._capture_max_rms = 0.0
        self._capture_had_post_wake_speech = bool(seed_text.strip())
        if trigger == "follow_up":
            self._context_gate.extend_follow_up_window(duration_sec=4.0)

        # Cálculo de ambiente y umbrales con histéresis a partir del ring buffer de 3.0 s
        blocks = list(self._ring_buffer)
        if blocks:
            rms_list = [calc_rms_int16(blk) for blk in blocks]
            ambient = float(np.percentile(rms_list, 25))
        else:
            ambient = self._noise_floor_int16

        ambient = max(95.0, min(165.0, ambient))
        umbral_voz = max(130.0, ambient * 1.5)
        umbral_corte = max(108.0, ambient * 1.2)

        self._capture_ambient_rms = ambient
        self._capture_umbral_voz = umbral_voz
        self._capture_umbral_corte = umbral_corte

        # Retroceder en el ring buffer al primer bloque donde RMS >= umbral_corte (-5 bloques extra)
        if blocks:
            found_idx: int | None = None
            for i, blk in enumerate(blocks):
                if calc_rms_int16(blk) >= umbral_corte:
                    found_idx = i
                    break
            start_idx = max(0, found_idx - 5) if found_idx is not None else max(0, len(blocks) - 10)
            self._capture_pcm16 = list(blocks[start_idx:])
        else:
            self._capture_pcm16 = []

        if self._on_wake_word is not None:
            with contextlib.suppress(Exception):
                self._on_wake_word(wake_word, trigger)

    def _commit_capture(self, recognizer: SpeechRecognizer | None) -> None:
        final_tail = ""
        if recognizer is not None:
            with contextlib.suppress(Exception):
                final_tail = recognizer.final_result_text()
                recognizer.reset()

        parts: list[str] = []
        if self._capture_seed_text:
            parts.append(self._capture_seed_text)
        parts.extend(seg for seg in self._capture_segments if seg)
        tail = final_tail or self._capture_last_partial
        if tail:
            prev_combined = _SPACE_RE.sub(" ", " ".join(parts)).strip()
            norm_prev = normalize_for_wake_match(prev_combined)
            norm_tail = normalize_for_wake_match(tail)
            if not norm_prev:
                parts.append(tail)
            elif norm_tail == norm_prev:
                pass
            elif norm_tail.startswith(norm_prev):
                parts = [tail]
            elif not norm_prev.endswith(norm_tail):
                parts.append(tail)

        full_text = _SPACE_RE.sub(" ", " ".join(parts)).strip()
        # Si era follow-up pero el audio/texto contiene la wake-word, promover a wake_word
        if self._capture_trigger == "follow_up":
            detected_w, _ = self._phonetic_matcher.detect_in_vosk(full_text)
            if detected_w:
                self._capture_trigger = "wake_word"
                self._capture_wake_word = (
                    self._wake_words[0] if self._wake_words else "raphael"
                )

        # Si la frase repite la wake-word al inicio (o variantes como 'rafa el'), limpiarla suavemente
        if self._capture_trigger == "wake_word" and self._capture_wake_word:
            wake_candidates = [self._capture_wake_word]
            norm_wake = normalize_for_wake_match(self._capture_wake_word)
            f_wake = norm_wake.replace("ph", "f")
            wake_candidates.append(f_wake)
            for base in (norm_wake, f_wake):
                if base.endswith("el") and len(base) > 2:
                    wake_candidates.append(f"{base[:-2]} el")
            for _ in range(3):
                found, remainder = extract_wake_word(full_text, tuple(wake_candidates))
                if found is not None and remainder:
                    full_text = remainder
                else:
                    break

        # Limpiar muletillas iniciales repetidas (oye, hey, eh, por favor)
        full_text = _LEADING_FILLER_RE.sub("", full_text).strip()

        # Limpiar prefijo suelto residual 'el ' o 'la ' solo si va seguido de un verbo de orden/pregunta
        match_art = re.match(r"^(?:el|la)\s+(.*)$", full_text, re.IGNORECASE)
        if match_art:
            after_art = match_art.group(1).strip()
            if _COMMAND_VERBS_RE.search(after_art):
                full_text = after_art

        audio_buf = (
            np.concatenate(self._capture_audio, axis=0)
            if self._capture_audio
            else np.zeros(0, dtype=np.float32)
        )
        if self._capture_vocal_ratios:
            avg_vocal = float(
                np.percentile(np.asarray(self._capture_vocal_ratios, dtype=np.float64), 75)
            )
        elif full_text:
            avg_vocal = 0.75
        else:
            avg_vocal = 0.0

        full_text = clean_brand_articles(full_text)
        pcm16_total = b"".join(self._capture_pcm16)
        if pcm16_total:
            audio_buf = np.frombuffer(pcm16_total, dtype=np.int16).astype(np.float32) / 32768.0
            wav_bytes, wav_peak, wav_dur = prepare_wav_for_cloud_stt(pcm16_total, self._sample_rate)
        else:
            wav_bytes, wav_peak, wav_dur = b"", 0.0, 0.0

        dur_sec = time.monotonic() - self._capture_started_at
        silence_time = time.monotonic() - self._capture_last_speech_at
        logger.info(
            "[WAKE] ambiente=%.0f | umbral_voz=%.0f | corte=%.0f | captura=%.1fs | fin=%s | rms=%.0f",
            self._capture_ambient_rms,
            self._capture_umbral_voz,
            self._capture_umbral_corte,
            dur_sec,
            "silencio" if silence_time >= 0.8 else "timeout",
            self._capture_max_rms,
        )

        trigger_used = self._capture_trigger
        if trigger_used == "follow_up":
            detected_w, _ = self._phonetic_matcher.detect_in_vosk(full_text)
            if detected_w:
                trigger_used = "wake_word"

        utterance = VoiceUtterance(
            transcript=full_text,
            triggered_by=trigger_used,
            wake_word=self._capture_wake_word,
            audio_samples=audio_buf,
            avg_vocal_ratio=avg_vocal,
            wav_bytes=wav_bytes,
            wav_peak=wav_peak,
            wav_duration_sec=wav_dur,
            fuzzy_wake_only=self._capture_fuzzy_only,
            rms_int16=self._capture_max_rms,
            noise_floor_int16=self._capture_ambient_rms,
        )
        self._reset_capture_state()

        if utterance.transcript:
            with contextlib.suppress(Exception):
                self._on_utterance(utterance)
        else:
            if self._on_capture_cancelled is not None:
                with contextlib.suppress(Exception):
                    self._on_capture_cancelled()

    def process_pcm_frame(
        self,
        raw_chunk: npt.NDArray[np.float32] | npt.NDArray[np.int16] | bytes,
        *,
        recognizer: SpeechRecognizer | None = None,
        now: float | None = None,
    ) -> None:
        """
        Procesa una trama de audio de forma determinista (usado por `_audio_worker` y pruebas offline).
        """
        ts = time.monotonic() if now is None else now
        if self._settings.general.privacy_mode:
            return
        with self._lock:
            if self._suspended:
                return
            wake_words = self._wake_words
            device_idx = self._active_device_index

        mult = self._context_gate.dynamic_vad_multiplier()
        frame = self._conditioner.process(raw_chunk, sensitivity_multiplier=mult)

        rec_pcm = frame.raw_hp_pcm16 if frame.raw_hp_pcm16 else frame.pcm16_bytes
        linear_f32 = (
            np.frombuffer(rec_pcm, dtype=np.int16).astype(np.float32) / 32768.0
            if rec_pcm
            else frame.samples_f32
        )
        rms_cur = calc_rms_int16(rec_pcm) if rec_pcm else 0.0

        if rec_pcm:
            self._ring_buffer.append(rec_pcm)

        # 1. Medidor de nivel de micrófono (throttled para no saturar Qt)
        if self._on_mic_level is not None and (ts - self._last_mic_emit) >= _MIC_LEVEL_INTERVAL_SEC:
            self._last_mic_emit = ts
            with contextlib.suppress(Exception):
                self._on_mic_level(frame.normalized_level, frame.is_speech, device_idx)

        # 2. Si hay calibración de huella vocal en curso (6 s), priorizar captura biométrica lineal sin tanh
        if self._voiceprint.is_calibrating:
            progress, ready = self._voiceprint.feed_calibration_chunk(linear_f32)
            if self._on_calibration_event is not None and (
                ready or (ts - self._last_cal_emit) >= _CAL_PROGRESS_INTERVAL_SEC
            ):
                self._last_cal_emit = ts
                self._on_calibration_event(
                    "progress",
                    progress,
                    f"Calibrando huella vocal... {round(progress * 100)}%",
                )
            if ready:
                ok = self._voiceprint.finalize_calibration()
                if self._on_calibration_event is not None:
                    self._on_calibration_event(
                        "completed" if ok else "failed",
                        1.0 if ok else 0.0,
                        (
                            "Huella vocal del propietario calibrada y guardada."
                            if ok
                            else "No se detectó suficiente señal vocal para calibrar."
                        ),
                    )
            return

        # 3. Push-to-Talk Win32 vía GetAsyncKeyState (cero hooks invasivos)
        ptt_down = (
            self._settings.voice.ptt_enabled
            and self._key_poller.is_combo_pressed(self._settings.voice.ptt_hotkey)
        )
        if ptt_down and not self._ptt_was_down:
            if self._is_tts_speaking is not None and self._is_tts_speaking() and self._on_barge_in is not None:
                with contextlib.suppress(Exception):
                    self._on_barge_in()
            if recognizer is not None:
                with contextlib.suppress(Exception):
                    recognizer.reset()
            self._begin_capture(trigger="ptt", wake_word="ptt", now=ts)
        elif not ptt_down and self._ptt_was_down and self._capturing and self._capture_trigger == "ptt":
            self._ptt_was_down = False
            self._commit_capture(recognizer)
            return
        self._ptt_was_down = ptt_down

        # 4. Ring Buffer y alimentación adaptativa a Vosk ES
        # Calibración de ruido ambiente inicial en primeros 12 bloques
        if len(self._cal_noise_blocks) < 12 and rms_cur > 0.0:
            self._cal_noise_blocks.append(rms_cur)
            if len(self._cal_noise_blocks) == 12:
                p35 = float(np.percentile(self._cal_noise_blocks, 35))
                self._noise_floor_int16 = max(95.0, min(165.0, p35))

        # Barge-in instantáneo si TTS está hablando y energía cruza umbral
        is_speaking = self._is_tts_speaking is not None and self._is_tts_speaking()
        if is_speaking:
            bargein_threshold = max(260.0, self._noise_floor_int16 * 2.3)
            if rms_cur >= bargein_threshold and rec_pcm:
                barge_rec = self._ensure_bargein_recognizer(recognizer)
                if barge_rec is not None:
                    try:
                        probe_b = ""
                        if barge_rec.accept_waveform(rec_pcm):
                            probe_b = barge_rec.result_text()
                        else:
                            probe_b = barge_rec.partial_text()
                        if probe_b:
                            detected_b, _ = self._phonetic_matcher.detect_in_vosk(probe_b)
                            if detected_b and self._on_barge_in is not None:
                                with contextlib.suppress(Exception):
                                    self._on_barge_in()
                    except Exception as exc:
                        logger.debug("Error evaluando barge-in con gramática: %s", exc)

        # Cola de alimentación a Vosk (hasta 16 bloques de silencio tras voz)
        is_voice_or_capture = (rms_cur >= self._capture_umbral_corte) or self._capturing or (rms_cur == 0.0)
        if is_voice_or_capture:
            self._silence_feed_counter = 16
            feed_vosk = True
        elif self._silence_feed_counter > 0:
            self._silence_feed_counter -= 1
            feed_vosk = True
        else:
            feed_vosk = False

        rec_text = ""
        partial_text = ""
        if recognizer is not None and rec_pcm and feed_vosk:
            try:
                if recognizer.accept_waveform(rec_pcm):
                    rec_text = recognizer.result_text()
                else:
                    partial_text = recognizer.partial_text()
            except Exception as exc:
                logger.debug("Error menor en reconocedor Vosk: %s", exc)

        # 5. Estado de captura activa de comando
        if self._capturing:
            if self._capture_trigger == "follow_up" and (
                frame.is_speech or rms_cur >= self._capture_umbral_corte
            ):
                self._context_gate.extend_follow_up_window(duration_sec=4.0)

            if rec_pcm:
                self._capture_pcm16.append(rec_pcm)
            if rms_cur > self._capture_max_rms:
                self._capture_max_rms = rms_cur
            if rms_cur >= self._capture_umbral_corte:
                self._capture_had_post_wake_speech = True

            if frame.is_speech or frame.normalized_level >= 0.06:
                self._capture_audio.append(frame.samples_f32)
            if frame.is_speech or frame.normalized_level >= 0.08:
                self._capture_vocal_ratios.append(frame.vocal_band_ratio)

            partial_changed = bool(partial_text and partial_text != self._capture_last_partial)
            if partial_text:
                self._capture_last_partial = partial_text

            if rec_text:
                self._capture_segments.append(rec_text)
                self._capture_last_speech_at = ts
                self._capture_had_post_wake_speech = True
            elif frame.is_speech or partial_changed or rms_cur >= self._capture_umbral_voz:
                self._capture_last_speech_at = ts
                self._capture_had_post_wake_speech = True

            elapsed = ts - self._capture_started_at
            silence_elapsed = ts - self._capture_last_speech_at
            has_content = bool(
                self._capture_seed_text
                or self._capture_segments
                or partial_text
                or self._capture_had_post_wake_speech
            )
            is_conf = (
                self._capture_trigger == "follow_up"
                and self._context_gate.is_follow_up_confirmation_active()
            )
            if is_conf:
                silence_limit = 0.85
            else:
                silence_limit = (
                    _SILENCE_COMMIT_COMPOUND_SEC if has_content else _SILENCE_COMMIT_WAKE_ONLY_SEC
                )
            if (elapsed >= _MAX_COMMAND_SEC) or (
                self._capture_trigger != "ptt" and silence_elapsed >= silence_limit
            ):
                self._commit_capture(recognizer)
            return

        # 6. Estado IDLE_WAKE: detección de wake-word o ventana de seguimiento continuo (6 s)
        probe_text = rec_text or partial_text
        if self._settings.voice.wake_word_enabled and probe_text:
            detected, fuzzy_only = self._phonetic_matcher.detect_in_vosk(probe_text)
            if detected:
                if self._is_tts_speaking is not None and self._is_tts_speaking() and self._on_barge_in is not None:
                    with contextlib.suppress(Exception):
                        self._on_barge_in()
                self._capture_last_partial = partial_text
                found_wake, remainder = extract_wake_word(probe_text, wake_words)
                active_wake = found_wake if found_wake is not None else wake_words[0]
                self._begin_capture(
                    trigger="wake_word",
                    wake_word=active_wake,
                    seed_text=remainder,
                    fuzzy_only=fuzzy_only,
                    now=ts,
                )
                if rec_pcm:
                    self._capture_pcm16.append(rec_pcm)
                if frame.is_speech or frame.normalized_level >= 0.06:
                    self._capture_audio.append(frame.samples_f32)
                if frame.is_speech or frame.normalized_level >= 0.08:
                    self._capture_vocal_ratios.append(frame.vocal_band_ratio)
                return

        # Ventana de seguimiento continuo (sin repetir wake-word)
        if (
            self._context_gate.is_follow_up_active()
            and not (self._is_tts_speaking is not None and self._is_tts_speaking())
        ):
            is_conf = self._context_gate.is_follow_up_confirmation_active()
            stricter_thresh = (
                self._capture_umbral_corte
                if is_conf
                else max(self._capture_umbral_voz * 1.35, 220.0)
            )
            should_start = (
                (rms_cur >= stricter_thresh and (frame.is_speech or is_conf))
                or bool(rec_text)
            )
            if should_start:
                self._begin_capture(
                    trigger="follow_up",
                    wake_word="",
                    seed_text=rec_text,
                    fuzzy_only=False,
                    now=ts,
                )
                if rec_pcm:
                    self._capture_pcm16.append(rec_pcm)
                if frame.is_speech or frame.normalized_level >= 0.06:
                    self._capture_audio.append(frame.samples_f32)
                if frame.is_speech or frame.normalized_level >= 0.08:
                    self._capture_vocal_ratios.append(frame.vocal_band_ratio)

    def _audio_worker(self, thread_stop_event: threading.Event) -> None:
        """Bucle ejecutado dentro de `AudioInputThread` con cierre determinista <0.5 s."""
        self._worker_exited.clear()
        pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=48)
        stream: Any = None
        try:
            recognizer = self._ensure_recognizer()
            try:
                import sounddevice as sd
            except Exception as exc:
                with self._lock:
                    self._last_error = f"sounddevice no disponible: {exc}"
                logger.warning(self._last_error)
                return

            def _sd_callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
                if status:
                    logger.debug("Estado stream sounddevice: %s", status)
                try:
                    pcm_queue.put_nowait(bytes(indata))
                except queue.Full:
                    with contextlib.suppress(queue.Empty):
                        pcm_queue.get_nowait()
                    with contextlib.suppress(queue.Full):
                        pcm_queue.put_nowait(bytes(indata))

            with self._lock:
                device_idx = self._active_device_index

            try:
                stream = sd.RawInputStream(
                    samplerate=self._sample_rate,
                    blocksize=DEFAULT_BLOCK_SAMPLES,
                    device=device_idx,
                    dtype="int16",
                    channels=1,
                    callback=_sd_callback,
                )
                stream.start()
                with contextlib.suppress(Exception):
                    dev_info = (
                        sd.query_devices(device_idx, "input")
                        if device_idx is not None
                        else sd.query_devices(kind="input")
                    )
                    mic_name = (
                        str(dev_info.get("name", f"device_{device_idx}"))
                        if isinstance(dev_info, dict)
                        else f"device_{device_idx}"
                    )
                    logger.info(
                        "Micrófono abierto con éxito: %r (índice=%s, freq=%d Hz)",
                        mic_name,
                        device_idx,
                        self._sample_rate,
                    )
            except Exception as exc:
                with self._lock:
                    self._last_error = f"No se pudo abrir el micrófono ({device_idx}): {exc}"
                logger.warning(self._last_error)
                return

            with self._lock:
                self._last_error = None

            while not thread_stop_event.is_set() and not self._stop_requested.is_set():
                try:
                    chunk = pcm_queue.get(timeout=0.05)
                except queue.Empty:
                    # Permitir commit por silencio aun si no llegaron muestras nuevas
                    if self._capturing:
                        now = time.monotonic()
                        has_content = bool(
                            self._capture_seed_text
                            or self._capture_segments
                            or self._capture_had_post_wake_speech
                        )
                        silence_limit = (
                            _SILENCE_COMMIT_COMPOUND_SEC
                            if has_content
                            else _SILENCE_COMMIT_WAKE_ONLY_SEC
                        )
                        if (now - self._capture_last_speech_at) >= silence_limit:
                            self._commit_capture(recognizer)
                    continue
                self.process_pcm_frame(chunk, recognizer=recognizer)
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.stop()
                with contextlib.suppress(Exception):
                    stream.close()
            self._worker_exited.set()
