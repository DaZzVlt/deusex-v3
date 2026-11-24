from __future__ import annotations

import contextlib
from dataclasses import dataclass
import logging
import os
import sys
import threading
import time
from typing import Any, Final, Protocol

from deusex.core.runtime import ComStaThread

logger = logging.getLogger(__name__)

# Aplicaciones de comunicación/llamada excluidas del ducking y reconocidas para Modo En Llamada
CALL_APP_TOKENS: Final[tuple[str, ...]] = (
    "discord",
    "teams",
    "ms-teams",
    "zoom",
    "skype",
    "google meet",
    "meet",
    "slack",
    "webex",
    "whatsapp",
    "telegram",
    "facetime",
)

_ACTIVE_PEAK_THRESHOLD: Final[float] = 0.012
_RAMP_STEPS: Final[int] = 4
_RAMP_STEP_DELAY_SEC: Final[float] = 0.025


@dataclass(frozen=True, slots=True)
class AudioSessionInfo:
    """Instantánea inmutable de una sesión de audio activa en Windows."""

    session_key: str
    process_name: str
    display_name: str
    pid: int
    volume: float
    muted: bool
    is_active: bool
    peak: float
    is_capture: bool = False


@dataclass(frozen=True, slots=True)
class AudioSessionSnapshot:
    """Resumen de actividad multimedia y de llamadas en el sistema."""

    is_multimedia_active: bool = False
    is_call_active: bool = False
    active_call_apps: tuple[str, ...] = ()
    active_media_apps: tuple[str, ...] = ()
    captured_at: float = 0.0


class AudioSessionBackend(Protocol):
    """Contrato inyectable para pruebas offline y backend real pycaw en ComStaThread."""

    def list_sessions(self) -> list[AudioSessionInfo]: ...
    def set_session_volume(self, session_key: str, volume: float) -> bool: ...


def is_call_session(process_name: str, display_name: str = "") -> bool:
    """Determina si una sesión pertenece a una app de llamada (Discord, Teams, Zoom, Skype, Meet)."""
    proc = process_name.strip().lower()
    if proc.endswith(".exe"):
        proc = proc[:-4]
    disp = display_name.strip().lower()

    for token in CALL_APP_TOKENS:
        if token in ("meet", "google meet"):
            if "google meet" in disp or proc == "meet" or "meet.google.com" in disp:
                return True
            continue
        if token in proc or token in disp:
            return True
    return False


class _Win32PycawBackend:
    """Backend real de sesiones de audio Windows vía pycaw (ejecutar solo en ComStaThread)."""

    def _enumerate_raw(self) -> list[tuple[str, Any, bool]]:
        if sys.platform != "win32":
            return []
        try:
            from pycaw.pycaw import AudioUtilities
        except Exception:
            return []

        result: list[tuple[str, Any, bool]] = []

        # 1. Sesiones de altavoz (eRender)
        with contextlib.suppress(Exception):
            raw_sessions = AudioUtilities.GetAllSessions()
            for idx, session in enumerate(raw_sessions):
                proc = getattr(session, "Process", None)
                pid = int(getattr(proc, "pid", 0) or 0)
                pname = str(getattr(proc, "name", lambda: "")() or "") if proc is not None else ""
                key = f"{pid}:{pname.lower()}:{idx}"
                result.append((key, session, False))

        # 2. Sesiones de captura de micrófono (eCapture) para detección infalible de llamadas activas
        with contextlib.suppress(Exception):
            from pycaw.pycaw import AudioSession, IAudioSessionControl2, IAudioSessionManager2

            mic = AudioUtilities.GetMicrophone()
            if mic is not None:
                mgr = mic.Activate(IAudioSessionManager2._iid_, 7, None)
                if mgr is not None:
                    mgr2 = mgr.QueryInterface(IAudioSessionManager2)
                    enumerator = mgr2.GetSessionEnumerator()
                    count = int(enumerator.GetCount())
                    for idx in range(count):
                        try:
                            ctl = enumerator.GetSession(idx)
                            ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                            session = AudioSession(ctl2)
                            proc = getattr(session, "Process", None)
                            pid = int(getattr(proc, "pid", 0) or 0)
                            pname = str(getattr(proc, "name", lambda: "")() or "") if proc is not None else ""
                            key = f"cap:{pid}:{pname.lower()}:{idx}"
                            result.append((key, session, True))
                        except Exception:
                            continue

        return result

    def list_sessions(self) -> list[AudioSessionInfo]:
        sessions: list[AudioSessionInfo] = []
        for key, raw, is_capture in self._enumerate_raw():
            try:
                proc = getattr(raw, "Process", None)
                pid = int(getattr(proc, "pid", 0) or 0)
                pname = str(proc.name()) if proc is not None else ""
                display = str(getattr(raw, "DisplayName", "") or "")
                state = int(getattr(raw, "State", 0) or 0)
                vol_iface = getattr(raw, "SimpleAudioVolume", None)
                volume = float(vol_iface.GetMasterVolume()) if vol_iface is not None else 1.0
                muted = bool(vol_iface.GetMute()) if vol_iface is not None else False

                peak = 0.0
                with contextlib.suppress(Exception):
                    from pycaw.pycaw import IAudioMeterInformation

                    ctl = getattr(raw, "_ctl", None)
                    if ctl is not None:
                        meter = ctl.QueryInterface(IAudioMeterInformation)
                        peak = float(meter.GetPeakValue())

                sessions.append(
                    AudioSessionInfo(
                        session_key=key,
                        process_name=pname,
                        display_name=display,
                        pid=pid,
                        volume=max(0.0, min(1.0, volume)),
                        muted=muted,
                        is_active=(state == 1),
                        peak=max(0.0, peak),
                        is_capture=is_capture,
                    )
                )
            except Exception:
                continue
        return sessions

    def set_session_volume(self, session_key: str, volume: float) -> bool:
        clamped = max(0.0, min(1.0, float(volume)))
        for key, raw, is_capture in self._enumerate_raw():
            if is_capture or key != session_key:
                continue
            try:
                vol_iface = getattr(raw, "SimpleAudioVolume", None)
                if vol_iface is None:
                    return False
                vol_iface.SetMasterVolume(clamped, None)
                return True
            except Exception:
                return False
        return False


class SelectiveAudioDucker:
    """
    Atenuador selectivo de volumen multimedia durante TTS vía `pycaw` en `ComStaThread`.
    - Excluye el propio proceso de DeusEx y todas las apps de llamada (Discord, Teams, Zoom, Skype, Meet).
    - Garantiza restauración total e idempotente en `restore()` / `stop()` o ante fallos.
    """

    def __init__(
        self,
        com_sta: ComStaThread | None = None,
        *,
        default_level_pct: int = 22,
        backend: AudioSessionBackend | None = None,
        own_pid: int | None = None,
        smooth_ramp: bool = True,
    ) -> None:
        self._com_sta = com_sta
        self._default_level_pct = max(0, min(100, int(default_level_pct)))
        self._backend: AudioSessionBackend = backend if backend is not None else _Win32PycawBackend()
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        self._smooth_ramp = smooth_ramp
        self._lock = threading.RLock()
        self._saved_volumes: dict[str, float] = {}
        self._is_ducked = False

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return self._is_ducked

    def set_default_level_pct(self, level_pct: int) -> None:
        with self._lock:
            self._default_level_pct = max(0, min(100, int(level_pct)))

    def _run_on_sta_sync(self, fn: Any, *args: Any, timeout_sec: float = 1.5) -> Any:
        if self._com_sta is None or not self._com_sta.is_running:
            return fn(*args)
        if threading.get_ident() == self._com_sta.thread_id:
            return fn(*args)
        return self._com_sta.run_sync(fn, *args, timeout=timeout_sec)

    async def _run_on_sta_async(self, fn: Any, *args: Any) -> Any:
        if self._com_sta is None or not self._com_sta.is_running:
            return fn(*args)
        return await self._com_sta.run_async(fn, *args)

    def _inspect_sessions_impl(self) -> AudioSessionSnapshot:
        sessions = self._backend.list_sessions()
        call_apps: list[str] = []
        media_apps: list[str] = []

        for sess in sessions:
            if sess.pid == self._own_pid:
                continue

            # Si es sesión de captura de micrófono de una app de llamadas y está activa:
            # Detecta llamada activa en Discord/Teams aunque el interlocutor esté en silencio
            if sess.is_capture:
                if sess.is_active and is_call_session(sess.process_name, sess.display_name):
                    label = sess.process_name or sess.display_name or f"pid:{sess.pid}"
                    if label not in call_apps:
                        call_apps.append(label)
                continue

            if sess.muted or sess.volume <= 0.001:
                continue

            actively_sounding = sess.peak >= _ACTIVE_PEAK_THRESHOLD or (
                sess.is_active and sess.peak == 0.0 and sess.process_name != ""
            )
            if not actively_sounding:
                continue

            label = sess.process_name or sess.display_name or f"pid:{sess.pid}"
            if is_call_session(sess.process_name, sess.display_name):
                if label not in call_apps:
                    call_apps.append(label)
            elif label not in media_apps:
                media_apps.append(label)

        return AudioSessionSnapshot(
            is_multimedia_active=len(media_apps) > 0,
            is_call_active=len(call_apps) > 0,
            active_call_apps=tuple(call_apps),
            active_media_apps=tuple(media_apps),
            captured_at=time.monotonic(),
        )

    def inspect_sessions_sync(self) -> AudioSessionSnapshot:
        try:
            snap: AudioSessionSnapshot = self._run_on_sta_sync(self._inspect_sessions_impl)
            return snap
        except Exception as exc:
            logger.debug("No se pudo inspeccionar sesiones de audio en ComStaThread: %s", exc)
            return AudioSessionSnapshot(captured_at=time.monotonic())

    async def inspect_sessions(self) -> AudioSessionSnapshot:
        try:
            snap: AudioSessionSnapshot = await self._run_on_sta_async(self._inspect_sessions_impl)
            return snap
        except Exception as exc:
            logger.debug("No se pudo inspeccionar sesiones de audio async: %s", exc)
            return AudioSessionSnapshot(captured_at=time.monotonic())

    def _duck_impl(self, level_pct: int) -> int:
        target_factor = max(0.0, min(1.0, float(level_pct) / 100.0))
        sessions = self._backend.list_sessions()
        plan: list[tuple[str, float, float]] = []

        with self._lock:
            for sess in sessions:
                if sess.is_capture:
                    continue
                if sess.pid == self._own_pid:
                    continue
                if sess.muted or not sess.is_active:
                    continue
                if is_call_session(sess.process_name, sess.display_name):
                    continue

                original_vol = self._saved_volumes.get(sess.session_key, sess.volume)
                target_vol = min(original_vol, max(0.02, original_vol * target_factor))
                if target_vol >= sess.volume - 0.005:
                    continue

                if sess.session_key not in self._saved_volumes:
                    self._saved_volumes[sess.session_key] = original_vol
                plan.append((sess.session_key, sess.volume, target_vol))

            if plan:
                self._is_ducked = True

        if not plan:
            return 0

        steps = _RAMP_STEPS if self._smooth_ramp else 1
        for step in range(1, steps + 1):
            frac = step / float(steps)
            for key, start_v, end_v in plan:
                interp = start_v + (end_v - start_v) * frac
                with contextlib.suppress(Exception):
                    self._backend.set_session_volume(key, interp)
            if self._smooth_ramp and step < steps:
                time.sleep(_RAMP_STEP_DELAY_SEC)

        return len(plan)

    def _restore_impl(self) -> int:
        with self._lock:
            saved = dict(self._saved_volumes)
            self._saved_volumes.clear()
            self._is_ducked = False

        if not saved:
            return 0

        restored = 0
        for key, original_vol in saved.items():
            try:
                if self._backend.set_session_volume(key, original_vol):
                    restored += 1
            except Exception as exc:
                logger.warning("Fallo al restaurar volumen de sesión %s: %s", key, exc)
        return restored

    async def duck(self, *, level_pct: int | None = None) -> int:
        """Atenúa sesiones multimedia no excluidas de forma suave en ComStaThread."""
        pct = self._default_level_pct if level_pct is None else max(0, min(100, int(level_pct)))
        if pct >= 100:
            return 0
        try:
            count: int = await self._run_on_sta_async(self._duck_impl, pct)
            return count
        except Exception as exc:
            logger.warning("Error durante audio ducking (restaurando preventivamente): %s", exc)
            await self.restore()
            return 0

    async def restore(self) -> int:
        """Restaura el volumen original de todas las sesiones atenuadas."""
        try:
            count: int = await self._run_on_sta_async(self._restore_impl)
            return count
        except Exception as exc:
            logger.warning("Error al restaurar sesiones de audio async: %s", exc)
            return 0

    def restore_sync(self) -> int:
        """Restauración síncrona rápida para cierre de módulo o apagado <1 s."""
        with self._lock:
            if not self._saved_volumes and not self._is_ducked:
                return 0
        try:
            count: int = self._run_on_sta_sync(self._restore_impl, timeout_sec=0.8)
            return count
        except Exception as exc:
            logger.warning("Error en restore_sync de SelectiveAudioDucker: %s", exc)
            with self._lock:
                self._saved_volumes.clear()
                self._is_ducked = False
            return 0

    def stop(self) -> None:
        self.restore_sync()
