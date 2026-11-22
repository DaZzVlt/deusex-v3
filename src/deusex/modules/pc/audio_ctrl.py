from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import importlib
import logging
import subprocess
import sys
from typing import Any

from deusex.core.runtime import ComStaThread
from deusex.modules.pc.apps import normalize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioSessionInfo:
    process_name: str
    pid: int
    volume_pct: int
    muted: bool


@dataclass(frozen=True, slots=True)
class BrightnessResult:
    ok: bool
    supported: bool
    summary: str
    level_pct: int | None = None
    backend: str | None = None
    dry_run: bool = False


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", ctypes.c_wchar * 128),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _clamp_pct(value: int) -> int:
    return max(0, min(100, int(value)))


def _get_pycaw_endpoint_volume() -> Any:
    """Obtiene la interfaz `IAudioEndpointVolume` dentro de `ComStaThread`."""
    pycaw_mod = importlib.import_module("pycaw.pycaw")
    audio_utils = pycaw_mod.AudioUtilities
    endpoint_cls = pycaw_mod.IAudioEndpointVolume

    speakers = audio_utils.GetSpeakers()
    if speakers is None:
        raise RuntimeError("No hay dispositivo de salida de audio predeterminado.")

    # Compatibilidad con pycaw moderno (AudioDevice.EndpointVolume) y clásico (IMMDevice.Activate)
    direct_endpoint = getattr(speakers, "EndpointVolume", None)
    if direct_endpoint is not None:
        return direct_endpoint

    comtypes_mod = importlib.import_module("comtypes")
    interface = speakers.Activate(endpoint_cls._iid_, comtypes_mod.CLSCTX_ALL, None)
    return interface.QueryInterface(endpoint_cls)


def _get_pycaw_sessions() -> list[Any]:
    pycaw_mod = importlib.import_module("pycaw.pycaw")
    sessions = pycaw_mod.AudioUtilities.GetAllSessions()
    return list(sessions) if sessions is not None else []


def _match_session_process(session_proc_name: str, query: str) -> bool:
    clean_q = normalize_text(query)
    if not clean_q:
        return False
    low = session_proc_name.lower()
    stem = normalize_text(low.removesuffix(".exe"))
    return (
        clean_q in (low, stem)
        or (len(clean_q) >= 3 and clean_q in stem)
    )


def _enum_hmonitors() -> list[int]:
    if sys.platform != "win32":
        return []
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return []

    handles: list[int] = []
    cb_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        wintypes.LPARAM,
    )

    def _cb(hmon: int, _hdc: int, _rect: Any, _lparam: int) -> bool:
        handles.append(int(hmon))
        return True

    callback = cb_type(_cb)
    windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    return handles


def _ddcci_get_brightness() -> int | None:
    """Lee brillo actual vía DDC/CI (`dxva2.dll`) en monitores externos compatibles."""
    if sys.platform != "win32":
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        dxva2 = windll.dxva2
    except OSError:
        return None

    for hmon in _enum_hmonitors():
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
            continue
        if count.value <= 0:
            continue
        arr = (_PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            continue
        try:
            for idx in range(count.value):
                min_b = wintypes.DWORD(0)
                cur_b = wintypes.DWORD(0)
                max_b = wintypes.DWORD(0)
                ok = bool(
                    dxva2.GetMonitorBrightness(
                        arr[idx].hPhysicalMonitor,
                        ctypes.byref(min_b),
                        ctypes.byref(cur_b),
                        ctypes.byref(max_b),
                    )
                )
                if ok and max_b.value > min_b.value:
                    span = max_b.value - min_b.value
                    pct = round(((cur_b.value - min_b.value) / span) * 100)
                    return _clamp_pct(pct)
        finally:
            with suppress(Exception):
                dxva2.DestroyPhysicalMonitors(count.value, arr)
    return None


def _ddcci_set_brightness(level_pct: int) -> bool:
    """Aplica brillo vía DDC/CI (`dxva2.dll`) en todos los monitores externos compatibles."""
    if sys.platform != "win32":
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    try:
        dxva2 = windll.dxva2
    except OSError:
        return False

    clamped = _clamp_pct(level_pct)
    applied = False
    for hmon in _enum_hmonitors():
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
            continue
        if count.value <= 0:
            continue
        arr = (_PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            continue
        try:
            for idx in range(count.value):
                min_b = wintypes.DWORD(0)
                cur_b = wintypes.DWORD(0)
                max_b = wintypes.DWORD(0)
                ok = bool(
                    dxva2.GetMonitorBrightness(
                        arr[idx].hPhysicalMonitor,
                        ctypes.byref(min_b),
                        ctypes.byref(cur_b),
                        ctypes.byref(max_b),
                    )
                )
                if not ok or max_b.value <= min_b.value:
                    continue
                target_val = min_b.value + round(((max_b.value - min_b.value) * clamped) / 100)
                if dxva2.SetMonitorBrightness(arr[idx].hPhysicalMonitor, int(target_val)):
                    applied = True
        finally:
            with suppress(Exception):
                dxva2.DestroyPhysicalMonitors(count.value, arr)
    return applied


def _run_powershell_quiet(command: str, *, timeout: float = 4.0) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, ""
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creation_flags,
            check=False,
        )
        return proc.returncode == 0, proc.stdout.strip()
    except Exception as exc:
        logger.debug("Comando PowerShell WMI falló: %s", exc)
        return False, ""


def _wmi_get_brightness() -> int | None:
    ok, out = _run_powershell_quiet(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness"
    )
    if not ok or not out:
        return None
    with suppress(ValueError):
        return _clamp_pct(int(out.splitlines()[0].strip()))
    return None


def _wmi_set_brightness(level_pct: int) -> bool:
    clamped = _clamp_pct(level_pct)
    cmd = (
        "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        "-ErrorAction Stop; "
        f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
        f"-Arguments @{{ Timeout = 1; Brightness = [byte]{clamped} }} | Out-Null"
    )
    ok, _ = _run_powershell_quiet(cmd)
    return ok


class AudioBrightnessController:
    """
    Control de volumen maestro/por aplicación (`pycaw` estrictamente en `ComStaThread`)
    y control de brillo de pantalla (WMI nativo en laptops + DDC/CI con degradación amable).
    """

    def __init__(
        self,
        com_sta: ComStaThread,
        *,
        wmi_get: Callable[[], int | None] = _wmi_get_brightness,
        wmi_set: Callable[[int], bool] = _wmi_set_brightness,
        ddcci_get: Callable[[], int | None] = _ddcci_get_brightness,
        ddcci_set: Callable[[int], bool] = _ddcci_set_brightness,
    ) -> None:
        self._com_sta = com_sta
        self._wmi_get = wmi_get
        self._wmi_set = wmi_set
        self._ddcci_get = ddcci_get
        self._ddcci_set = ddcci_set

    # ------------------------------------------------------------------
    # Volumen Maestro (pycaw en ComStaThread)
    # ------------------------------------------------------------------
    async def get_master_volume(self) -> tuple[int, bool]:
        def _read() -> tuple[int, bool]:
            ep = _get_pycaw_endpoint_volume()
            scalar = float(ep.GetMasterVolumeLevelScalar())
            muted = bool(ep.GetMute())
            return _clamp_pct(round(scalar * 100)), muted

        return await self._com_sta.run_async(_read)

    async def set_master_volume(self, level_pct: int, *, dry_run: bool = False) -> tuple[bool, str, int]:
        target = _clamp_pct(level_pct)
        if dry_run:
            return True, f"[Dry-Run] Ajustaría el volumen maestro al {target}%.", target

        def _write() -> int:
            ep = _get_pycaw_endpoint_volume()
            ep.SetMasterVolumeLevelScalar(target / 100.0, None)
            if target > 0 and bool(ep.GetMute()):
                ep.SetMute(0, None)
            return _clamp_pct(round(float(ep.GetMasterVolumeLevelScalar()) * 100))

        actual = await self._com_sta.run_async(_write)
        return True, f"Volumen ajustado al {actual}%.", actual

    async def adjust_master_volume(self, delta_pct: int, *, dry_run: bool = False) -> tuple[bool, str, int]:
        current, _muted = await self.get_master_volume()
        target = _clamp_pct(current + int(delta_pct))
        return await self.set_master_volume(target, dry_run=dry_run)

    async def set_master_mute(self, muted: bool, *, dry_run: bool = False) -> tuple[bool, str]:
        label = "silenciado" if muted else "activado"
        if dry_run:
            return True, f"[Dry-Run] Dejaría el audio del sistema {label}."

        def _apply() -> None:
            ep = _get_pycaw_endpoint_volume()
            ep.SetMute(1 if muted else 0, None)

        await self._com_sta.run_async(_apply)
        return True, f"Audio del sistema {label}."

    # ------------------------------------------------------------------
    # Volumen por Aplicación (pycaw en ComStaThread)
    # ------------------------------------------------------------------
    async def list_audio_sessions(self) -> list[AudioSessionInfo]:
        def _read_sessions() -> list[AudioSessionInfo]:
            out: list[AudioSessionInfo] = []
            for session in _get_pycaw_sessions():
                proc = getattr(session, "Process", None)
                if proc is None:
                    continue
                with suppress(Exception):
                    vol = session.SimpleAudioVolume
                    out.append(
                        AudioSessionInfo(
                            process_name=str(proc.name()),
                            pid=int(proc.pid),
                            volume_pct=_clamp_pct(round(float(vol.GetMasterVolume()) * 100)),
                            muted=bool(vol.GetMute()),
                        )
                    )
            return out

        return await self._com_sta.run_async(_read_sessions)

    async def set_app_volume(
        self,
        app_query: str,
        level_pct: int,
        *,
        dry_run: bool = False,
    ) -> tuple[bool, str, int]:
        target = _clamp_pct(level_pct)
        if dry_run:
            return True, f"[Dry-Run] Ajustaría el volumen de '{app_query}' al {target}%.", target

        def _write_app() -> list[str]:
            matched: list[str] = []
            for session in _get_pycaw_sessions():
                proc = getattr(session, "Process", None)
                if proc is None:
                    continue
                with suppress(Exception):
                    pname = str(proc.name())
                    if _match_session_process(pname, app_query):
                        vol = session.SimpleAudioVolume
                        vol.SetMasterVolume(target / 100.0, None)
                        if target > 0 and bool(vol.GetMute()):
                            vol.SetMute(0, None)
                        matched.append(pname)
            return matched

        matched_names = await self._com_sta.run_async(_write_app)
        if not matched_names:
            return (
                False,
                f"No encontré ninguna sesión de audio activa para '{app_query}'.",
                target,
            )
        unique = ", ".join(sorted(set(matched_names)))
        return True, f"Volumen de {unique} ajustado al {target}%.", target

    async def set_app_mute(
        self,
        app_query: str,
        muted: bool,
        *,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        label = "silenciado" if muted else "reactivado"
        if dry_run:
            return True, f"[Dry-Run] Dejaría el audio de '{app_query}' {label}."

        def _apply_app() -> list[str]:
            matched: list[str] = []
            for session in _get_pycaw_sessions():
                proc = getattr(session, "Process", None)
                if proc is None:
                    continue
                with suppress(Exception):
                    pname = str(proc.name())
                    if _match_session_process(pname, app_query):
                        session.SimpleAudioVolume.SetMute(1 if muted else 0, None)
                        matched.append(pname)
            return matched

        matched_names = await self._com_sta.run_async(_apply_app)
        if not matched_names:
            return False, f"No encontré audio activo de '{app_query}'."
        unique = ", ".join(sorted(set(matched_names)))
        return True, f"Audio de {unique} {label}."

    async def capture_audio_snapshot(self) -> dict[str, Any]:
        """Captura volumen maestro y sesiones activas para guardar en una Rutina."""
        master_pct, master_muted = await self.get_master_volume()
        sessions = await self.list_audio_sessions()
        per_app: dict[str, int] = {}
        for sess in sessions:
            per_app[sess.process_name.lower()] = sess.volume_pct
        return {
            "master_volume_pct": master_pct,
            "master_muted": master_muted,
            "app_volumes": per_app,
        }

    # ------------------------------------------------------------------
    # Brillo de Pantalla (WMI nativo + DDC/CI con degradación amable)
    # ------------------------------------------------------------------
    def get_brightness(self) -> BrightnessResult:
        wmi_level = self._wmi_get()
        if wmi_level is not None:
            return BrightnessResult(
                ok=True,
                supported=True,
                summary=f"El brillo actual es {wmi_level}%.",
                level_pct=wmi_level,
                backend="wmi",
            )

        ddc_level = self._ddcci_get()
        if ddc_level is not None:
            return BrightnessResult(
                ok=True,
                supported=True,
                summary=f"El brillo actual del monitor externo es {ddc_level}%.",
                level_pct=ddc_level,
                backend="ddc_ci",
            )

        return BrightnessResult(
            ok=False,
            supported=False,
            summary=(
                "Tu pantalla actual no expone control de brillo por software "
                "(monitor externo sin DDC/CI activo o pantalla interna desactivada)."
            ),
            level_pct=None,
            backend=None,
        )

    def set_brightness(self, level_pct: int, *, dry_run: bool = False) -> BrightnessResult:
        target = _clamp_pct(level_pct)
        if dry_run:
            return BrightnessResult(
                ok=True,
                supported=True,
                summary=f"[Dry-Run] Ajustaría el brillo al {target}%.",
                level_pct=target,
                backend="dry_run",
                dry_run=True,
            )

        applied_backends: list[str] = []
        if self._wmi_set(target):
            applied_backends.append("wmi")
        if self._ddcci_set(target):
            applied_backends.append("ddc_ci")

        if applied_backends:
            backend_str = "+".join(applied_backends)
            return BrightnessResult(
                ok=True,
                supported=True,
                summary=f"Brillo ajustado al {target}%.",
                level_pct=target,
                backend=backend_str,
            )

        return BrightnessResult(
            ok=False,
            supported=False,
            summary=(
                "No pude cambiar el brillo: el monitor conectado no soporta DDC/CI por software "
                "y no hay pantalla interna activa vía WMI."
            ),
            level_pct=None,
            backend=None,
        )

    def adjust_brightness(self, delta_pct: int, *, dry_run: bool = False) -> BrightnessResult:
        current = self.get_brightness()
        if not current.ok or current.level_pct is None:
            return current
        return self.set_brightness(current.level_pct + int(delta_pct), dry_run=dry_run)
