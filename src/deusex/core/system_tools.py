from __future__ import annotations

from contextlib import suppress
import ctypes
from ctypes import wintypes
import io
import logging
import math
import struct
import sys
import threading
import wave

from deusex.core.events import EventBus, HotkeyTriggeredEvent
from deusex.core.paths import AppPaths

logger = logging.getLogger(__name__)

# Constantes Win32 RegisterHotKey
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

_SPECIAL_VK_MAP: dict[str, int] = {
    "SPACE": 0x20,
    "ESPACIO": 0x20,
    "TAB": 0x09,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    **{f"F{n}": 0x6F + n for n in range(1, 13)},
}


def parse_hotkey_combo(combo: str) -> tuple[int, int]:
    """Convierte combinaciones como 'Alt+Space' o 'Ctrl+Shift+P' a (modifiers, vk)."""
    parts = [p.strip().upper() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"Atajo global inválido (requiere modificador + tecla): {combo!r}")

    modifiers = _MOD_NOREPEAT
    key_token: str | None = None

    for token in parts:
        if token in ("ALT",):
            modifiers |= _MOD_ALT
        elif token in ("CTRL", "CONTROL"):
            modifiers |= _MOD_CONTROL
        elif token in ("SHIFT", "MAYUS"):
            modifiers |= _MOD_SHIFT
        elif token in ("WIN", "SUPER", "META"):
            modifiers |= _MOD_WIN
        else:
            if key_token is not None:
                raise ValueError(f"Atajo con múltiples teclas principales: {combo!r}")
            key_token = token

    if key_token is None or modifiers == _MOD_NOREPEAT:
        raise ValueError(f"Atajo global incompleto: {combo!r}")

    if key_token in _SPECIAL_VK_MAP:
        vk = _SPECIAL_VK_MAP[key_token]
    elif len(key_token) == 1 and key_token.isalnum():
        vk = ord(key_token)
    else:
        raise ValueError(f"Tecla no soportada en atajo global: {key_token!r}")

    return modifiers, vk


def _synthesize_tone_wav(notes: tuple[tuple[float, float], ...], *, volume: float = 0.28) -> bytes:
    """Sintetiza un micro-sonido WAV PCM 16-bit en memoria con envolvente suave (sin clicks)."""
    sample_rate = 22050
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)

        frames = bytearray()
        for freq_hz, duration_sec in notes:
            total_samples = max(1, int(sample_rate * duration_sec))
            if freq_hz <= 0.0:
                frames.extend(b"\x00\x00" * total_samples)
                continue
            fade_samples = max(1, min(total_samples // 3, int(sample_rate * 0.012)))
            for i in range(total_samples):
                env = 1.0
                if i < fade_samples:
                    env = i / fade_samples
                elif i > total_samples - fade_samples:
                    env = (total_samples - i) / fade_samples
                phase = 2.0 * math.pi * freq_hz * (i / sample_rate)
                wave_val = 0.80 * math.sin(phase) + 0.20 * math.sin(2.0 * phase)
                sample = wave_val * volume * env
                clamped = max(-1.0, min(1.0, sample))
                frames.extend(struct.pack("<h", int(clamped * 32767)))
        wav_file.writeframes(bytes(frames))
    return buf.getvalue()


_DEFAULT_EARCON_NOTES: dict[str, tuple[tuple[float, float], ...]] = {
    "confirm": ((587.33, 0.065), (880.0, 0.085)),
    "wake": ((523.25, 0.06), (659.25, 0.06), (783.99, 0.09)),
    "timer": (
        (0.0, 0.05),
        (880.0, 0.14),
        (1174.66, 0.14),
        (1318.51, 0.22),
        (0.0, 0.14),
        (880.0, 0.14),
        (1174.66, 0.14),
        (1318.51, 0.28),
    ),
    "error": ((311.13, 0.10), (233.08, 0.14)),
    "privacy_on": ((659.25, 0.07), (440.0, 0.10)),
    "privacy_off": ((440.0, 0.07), (659.25, 0.10)),
    "notify": ((698.46, 0.08), (880.0, 0.11)),
}

_EARCON_VOLUMES: dict[str, float] = {
    "timer": 0.82,
    "notify": 0.48,
    "error": 0.45,
}


class EarconPlayer:
    """Reproductor instantáneo de micro-sonidos WAV (assets/earcons/ o síntesis en memoria)."""

    def __init__(self, paths: AppPaths, *, muted: bool = False) -> None:
        self._paths = paths
        self._muted = muted
        self._lock = threading.RLock()
        self._cache: dict[str, bytes] = {}
        self._active_buffer: bytes | None = None

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def get_wav_bytes(self, name: str) -> bytes:
        with self._lock:
            cached = self._cache.get(name)
            if cached is not None:
                return cached

            wav_path = self._paths.earcons_dir / f"{name}.wav"
            if wav_path.is_file():
                data = wav_path.read_bytes()
            else:
                notes = _DEFAULT_EARCON_NOTES.get(name, _DEFAULT_EARCON_NOTES["confirm"])
                vol = _EARCON_VOLUMES.get(name, 0.28)
                data = _synthesize_tone_wav(notes, volume=vol)

            self._cache[name] = data
            return data

    def play(self, name: str) -> bool:
        if self._muted or sys.platform != "win32":
            return False
        try:
            import winsound

            wav_bytes = self.get_wav_bytes(name)
            with self._lock:
                # Mantener referencia viva mientras winsound reproduce en modo SND_ASYNC.
                self._active_buffer = wav_bytes
                winsound.PlaySound(
                    wav_bytes,
                    winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            return True
        except Exception as exc:
            logger.debug("No se pudo reproducir earcon '%s': %s", name, exc)
            return False


class GlobalHotkeyManager:
    """Gestor nativo Win32 RegisterHotKey (cero hooks invasivos de teclado, cierre <50 ms)."""

    def __init__(self, events: EventBus) -> None:
        self._events = events
        self._lock = threading.RLock()
        self._desired: dict[str, str] = {}
        self._id_to_meta: dict[int, tuple[str, str]] = {}
        self._registered_ids: set[int] = set()
        self._thread: threading.Thread | None = None
        self._win_thread_id: int | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def registered_hotkeys(self) -> dict[str, str]:
        with self._lock:
            return {
                hotkey_id: combo
                for numeric_id, (hotkey_id, combo) in self._id_to_meta.items()
                if numeric_id in self._registered_ids
            }

    def add_hotkey(self, hotkey_id: str, combo: str) -> None:
        # Valida la sintaxis inmediatamente aunque no haya arrancado el hilo.
        parse_hotkey_combo(combo)
        with self._lock:
            self._desired[hotkey_id] = combo

    def start(self, timeout: float = 1.5) -> None:
        if sys.platform != "win32":
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested.clear()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._win32_loop,
                name="Win32HotkeyThread",
                daemon=True,
            )
            self._thread.start()

        self._ready.wait(timeout=timeout)

    def _win32_loop(self) -> None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            self._ready.set()
            return

        user32 = windll.user32
        kernel32 = windll.kernel32
        self._win_thread_id = int(kernel32.GetCurrentThreadId())

        # Forzar creación de la cola de mensajes del hilo antes de señalar _ready.
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        with self._lock:
            desired_copy = list(self._desired.items())
            self._id_to_meta.clear()
            self._registered_ids.clear()

        for idx, (hotkey_id, combo) in enumerate(desired_copy, start=1):
            try:
                mods, vk = parse_hotkey_combo(combo)
                ok = bool(user32.RegisterHotKey(None, idx, mods, vk))
                if ok:
                    with self._lock:
                        self._id_to_meta[idx] = (hotkey_id, combo)
                        self._registered_ids.add(idx)
                else:
                    logger.warning(
                        "No se pudo registrar atajo global %s (%s): posiblemente ocupado por otra app.",
                        hotkey_id,
                        combo,
                    )
            except Exception as exc:
                logger.warning("Atajo global inválido %s (%s): %s", hotkey_id, combo, exc)

        self._ready.set()

        try:
            while not self._stop_requested.is_set():
                ret = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if ret <= 0:
                    break
                if msg.message == _WM_HOTKEY:
                    numeric_id = int(msg.wParam)
                    with self._lock:
                        meta = self._id_to_meta.get(numeric_id)
                    if meta is not None:
                        hotkey_id, combo = meta
                        self._events.publish(
                            HotkeyTriggeredEvent(hotkey_id=hotkey_id, combo=combo)
                        )
        finally:
            with self._lock:
                reg_ids = list(self._registered_ids)
                self._registered_ids.clear()
            for numeric_id in reg_ids:
                with suppress(Exception):
                    user32.UnregisterHotKey(None, numeric_id)
            self._win_thread_id = None

    def stop(self, timeout: float = 1.0) -> None:
        self._stop_requested.set()
        with self._lock:
            thread = self._thread
            tid = self._win_thread_id

        if sys.platform == "win32" and tid is not None:
            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                with suppress(Exception):
                    windll.user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

        with self._lock:
            self._thread = None
