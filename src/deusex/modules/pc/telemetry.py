from __future__ import annotations

from contextlib import suppress
import ctypes
from dataclasses import dataclass
import importlib
import logging
import subprocess
import sys
import threading
from typing import Any, Literal

import psutil

logger = logging.getLogger(__name__)

PowerAction = Literal["shutdown", "restart", "sleep", "hibernate"]


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    name: str
    utilization_pct: float
    memory_used_mb: float
    memory_total_mb: float
    temperature_c: float | None = None


@dataclass(frozen=True, slots=True)
class BatterySnapshot:
    percent: float
    plugged_in: bool
    secs_left: int | None = None


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    mountpoint: str
    used_gb: float
    total_gb: float
    percent: float


@dataclass(frozen=True, slots=True)
class SystemTelemetry:
    cpu_percent: float
    cpu_cores_logical: int
    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float
    disks: tuple[DiskSnapshot, ...] = ()
    battery: BatterySnapshot | None = None
    gpus: tuple[GpuSnapshot, ...] = ()


class TelemetryService:
    """Telemetría local sin UAC (`psutil` + `nvidia-ml-py`) y control de sesión/energía."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nvml: Any | None = None
        self._nvml_initialized = False
        self._nvml_unavailable = False

    def _ensure_nvml(self) -> Any | None:
        with self._lock:
            if self._nvml_initialized:
                return self._nvml
            if self._nvml_unavailable:
                return None
            try:
                nvml_mod = importlib.import_module("pynvml")
                nvml_mod.nvmlInit()
                self._nvml = nvml_mod
                self._nvml_initialized = True
                return self._nvml
            except Exception as exc:
                logger.debug("NVML (GPU NVIDIA) no disponible: %s", exc)
                self._nvml_unavailable = True
                return None

    def close(self) -> None:
        with self._lock:
            if self._nvml_initialized and self._nvml is not None:
                with suppress(Exception):
                    self._nvml.nvmlShutdown()
            self._nvml_initialized = False
            self._nvml = None

    def _read_nvidia_gpus(self) -> tuple[GpuSnapshot, ...]:
        nvml = self._ensure_nvml()
        if nvml is None:
            return ()
        gpus: list[GpuSnapshot] = []
        try:
            count = int(nvml.nvmlDeviceGetCount())
            for idx in range(count):
                handle = nvml.nvmlDeviceGetHandleByIndex(idx)
                raw_name = nvml.nvmlDeviceGetName(handle)
                name = (
                    raw_name.decode("utf-8", errors="replace")
                    if isinstance(raw_name, bytes)
                    else str(raw_name)
                )
                util = nvml.nvmlDeviceGetUtilizationRates(handle)
                mem = nvml.nvmlDeviceGetMemoryInfo(handle)
                temp_c: float | None = None
                with suppress(Exception):
                    temp_type = getattr(nvml, "NVML_TEMPERATURE_GPU", 0)
                    temp_c = float(nvml.nvmlDeviceGetTemperature(handle, temp_type))

                gpus.append(
                    GpuSnapshot(
                        name=name,
                        utilization_pct=float(util.gpu),
                        memory_used_mb=round(float(mem.used) / (1024 * 1024), 1),
                        memory_total_mb=round(float(mem.total) / (1024 * 1024), 1),
                        temperature_c=temp_c,
                    )
                )
        except Exception as exc:
            logger.debug("No se pudo leer telemetría GPU NVIDIA: %s", exc)
        return tuple(gpus)

    def snapshot(self, *, include_disks: bool = True) -> SystemTelemetry:
        cpu_pct = float(psutil.cpu_percent(interval=0.12))
        cores = int(psutil.cpu_count(logical=True) or 1)
        vm = psutil.virtual_memory()
        gb = 1024.0**3

        disks: list[DiskSnapshot] = []
        if include_disks:
            for part in psutil.disk_partitions(all=False):
                if "cdrom" in part.opts.lower() or not part.fstype:
                    continue
                with suppress(PermissionError, OSError):
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append(
                        DiskSnapshot(
                            mountpoint=part.mountpoint,
                            used_gb=round(usage.used / gb, 1),
                            total_gb=round(usage.total / gb, 1),
                            percent=float(usage.percent),
                        )
                    )

        battery: BatterySnapshot | None = None
        with suppress(Exception):
            raw_bat = psutil.sensors_battery()
            if raw_bat is not None:
                secs_left = (
                    int(raw_bat.secsleft)
                    if isinstance(raw_bat.secsleft, (int, float)) and raw_bat.secsleft >= 0
                    else None
                )
                battery = BatterySnapshot(
                    percent=round(float(raw_bat.percent), 1),
                    plugged_in=bool(raw_bat.power_plugged),
                    secs_left=secs_left,
                )

        return SystemTelemetry(
            cpu_percent=round(cpu_pct, 1),
            cpu_cores_logical=cores,
            ram_used_gb=round(vm.used / gb, 1),
            ram_total_gb=round(vm.total / gb, 1),
            ram_percent=round(float(vm.percent), 1),
            disks=tuple(disks),
            battery=battery,
            gpus=self._read_nvidia_gpus(),
        )

    @staticmethod
    def format_summary_es(snap: SystemTelemetry) -> str:
        parts = [
            f"CPU: {snap.cpu_percent:.0f}% ({snap.cpu_cores_logical} hilos)",
            f"RAM: {snap.ram_used_gb:.1f}/{snap.ram_total_gb:.1f} GB ({snap.ram_percent:.0f}%)",
        ]
        if snap.gpus:
            gpu = snap.gpus[0]
            temp_txt = f", {gpu.temperature_c:.0f}°C" if gpu.temperature_c is not None else ""
            parts.append(
                f"GPU ({gpu.name}): {gpu.utilization_pct:.0f}% "
                f"({gpu.memory_used_mb:.0f}/{gpu.memory_total_mb:.0f} MB{temp_txt})"
            )
        if snap.disks:
            main_disk = snap.disks[0]
            parts.append(
                f"Disco {main_disk.mountpoint}: {main_disk.used_gb:.0f}/{main_disk.total_gb:.0f} GB "
                f"({main_disk.percent:.0f}%)"
            )
        if snap.battery is not None:
            estado = "conectado" if snap.battery.plugged_in else "en batería"
            parts.append(f"Batería: {snap.battery.percent:.0f}% ({estado})")
        return " · ".join(parts)

    @staticmethod
    def lock_workstation(*, dry_run: bool = False) -> tuple[bool, str]:
        if dry_run:
            return True, "[Dry-Run] Bloquearía la pantalla de Windows (Win+L)."
        if sys.platform != "win32":
            return False, "Bloqueo de pantalla solo disponible en Windows."
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible."
        ok = bool(windll.user32.LockWorkStation())
        return (ok, "Pantalla bloqueada." if ok else "No pude bloquear la pantalla.")

    @staticmethod
    def execute_power_action(
        action: PowerAction,
        *,
        delay_sec: int = 5,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        labels: dict[PowerAction, str] = {
            "shutdown": "Apagar el equipo",
            "restart": "Reiniciar el equipo",
            "sleep": "Suspender el equipo",
            "hibernate": "Hibernar el equipo",
        }
        if dry_run:
            return True, f"[Dry-Run] Ejecutaría: {labels[action]}."
        if sys.platform != "win32":
            return False, "Acciones de energía solo disponibles en Windows."

        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        safe_delay = max(0, min(300, int(delay_sec)))

        if action == "shutdown":
            subprocess.Popen(
                ["shutdown", "/s", "/t", str(safe_delay)],
                creationflags=creation_flags,
                close_fds=True,
            )
            return True, f"Apagando el equipo en {safe_delay} s."
        if action == "restart":
            subprocess.Popen(
                ["shutdown", "/r", "/t", str(safe_delay)],
                creationflags=creation_flags,
                close_fds=True,
            )
            return True, f"Reiniciando el equipo en {safe_delay} s."

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible para suspender/hibernar."
        hibernate_flag = 1 if action == "hibernate" else 0
        ok = bool(windll.powrprof.SetSuspendState(hibernate_flag, 0, 0))
        if not ok:
            return False, f"No pude completar la acción '{labels[action]}'."
        return True, f"{labels[action]} iniciado."
