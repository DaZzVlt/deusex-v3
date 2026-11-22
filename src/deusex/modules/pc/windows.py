from __future__ import annotations

from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import logging
import sys
from typing import Any

import psutil

from deusex.modules.pc.apps import normalize_text

logger = logging.getLogger(__name__)

_MONITORINFOF_PRIMARY = 0x00000001
_DWMWA_CLOAKED = 14
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080

_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_SW_RESTORE = 9

_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    index: int
    name: str
    is_primary: bool
    left: int
    top: int
    right: int
    bottom: int
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)

    @property
    def work_width(self) -> int:
        return max(1, self.work_right - self.work_left)

    @property
    def work_height(self) -> int:
        return max(1, self.work_bottom - self.work_top)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    process_name: str
    monitor_index: int
    left: int
    top: int
    right: int
    bottom: int
    is_minimized: bool
    is_maximized: bool

    @property
    def width(self) -> int:
        return max(1, self.right - self.left)

    @property
    def height(self) -> int:
        return max(1, self.bottom - self.top)


def compute_target_rect(
    win_left: int,
    win_top: int,
    win_width: int,
    win_height: int,
    src_monitor: MonitorInfo,
    dst_monitor: MonitorInfo,
) -> tuple[int, int, int, int]:
    """Proyecta y ajusta coordenadas de una ventana al área útil de otro monitor (DPI-aware)."""
    rel_x = (win_left - src_monitor.work_left) / src_monitor.work_width
    rel_y = (win_top - src_monitor.work_top) / src_monitor.work_height
    rel_x = max(0.0, min(0.85, rel_x))
    rel_y = max(0.0, min(0.85, rel_y))

    new_w = min(max(320, win_width), dst_monitor.work_width)
    new_h = min(max(240, win_height), dst_monitor.work_height)

    new_x = dst_monitor.work_left + int(rel_x * dst_monitor.work_width)
    new_y = dst_monitor.work_top + int(rel_y * dst_monitor.work_height)

    max_x = dst_monitor.work_right - new_w
    max_y = dst_monitor.work_bottom - new_h
    new_x = max(dst_monitor.work_left, min(new_x, max_x))
    new_y = max(dst_monitor.work_top, min(new_y, max_y))
    return new_x, new_y, new_w, new_h


class WindowManager:
    """Gestión multi-monitor DPI-aware de ventanas sobre Win32 nativo."""

    def list_monitors(self) -> list[MonitorInfo]:
        if sys.platform != "win32":
            return []
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return []

        user32 = windll.user32
        raw_monitors: list[MonitorInfo] = []
        cb_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(_RECT),
            wintypes.LPARAM,
        )

        def _enum_cb(hmon: int, _hdc: int, _lprect: Any, _lparam: int) -> bool:
            info = _MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                raw_monitors.append(
                    MonitorInfo(
                        index=0,
                        name=str(info.szDevice).strip(),
                        is_primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                        left=int(info.rcMonitor.left),
                        top=int(info.rcMonitor.top),
                        right=int(info.rcMonitor.right),
                        bottom=int(info.rcMonitor.bottom),
                        work_left=int(info.rcWork.left),
                        work_top=int(info.rcWork.top),
                        work_right=int(info.rcWork.right),
                        work_bottom=int(info.rcWork.bottom),
                    )
                )
            return True

        callback = cb_type(_enum_cb)
        user32.EnumDisplayMonitors(None, None, callback, 0)

        raw_monitors.sort(key=lambda m: (not m.is_primary, m.left, m.top))
        return [
            MonitorInfo(
                index=idx,
                name=m.name,
                is_primary=m.is_primary,
                left=m.left,
                top=m.top,
                right=m.right,
                bottom=m.bottom,
                work_left=m.work_left,
                work_top=m.work_top,
                work_right=m.work_right,
                work_bottom=m.work_bottom,
            )
            for idx, m in enumerate(raw_monitors)
        ]

    @staticmethod
    def _monitor_index_for_rect(rect: _RECT, monitors: list[MonitorInfo]) -> int:
        if not monitors:
            return 0
        cx = (int(rect.left) + int(rect.right)) // 2
        cy = (int(rect.top) + int(rect.bottom)) // 2
        for mon in monitors:
            if mon.left <= cx < mon.right and mon.top <= cy < mon.bottom:
                return mon.index
        return 0

    @staticmethod
    def _is_cloaked(hwnd: int) -> bool:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        dwmapi = getattr(windll, "dwmapi", None)
        if dwmapi is None:
            return False
        cloaked = ctypes.c_int(0)
        hr = int(
            dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                _DWMWA_CLOAKED,
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
        )
        return hr == 0 and cloaked.value != 0

    def _build_window_info(self, hwnd: int, monitors: list[MonitorInfo]) -> WindowInfo | None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        user32 = windll.user32
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return None

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title == "Program Manager":
            return None

        rect = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        pid_val = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        pid = int(pid_val.value)
        proc_name = ""
        if pid > 0:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                proc_name = psutil.Process(pid).name()

        return WindowInfo(
            hwnd=hwnd,
            title=title,
            pid=pid,
            process_name=proc_name,
            monitor_index=self._monitor_index_for_rect(rect, monitors),
            left=int(rect.left),
            top=int(rect.top),
            right=int(rect.right),
            bottom=int(rect.bottom),
            is_minimized=bool(user32.IsIconic(hwnd)),
            is_maximized=bool(user32.IsZoomed(hwnd)),
        )

    def list_windows(self, *, include_minimized: bool = True) -> list[WindowInfo]:
        if sys.platform != "win32":
            return []
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return []

        user32 = windll.user32
        monitors = self.list_monitors()
        windows: list[WindowInfo] = []
        cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            ex_style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE))
            if ex_style & _WS_EX_TOOLWINDOW:
                return True
            if self._is_cloaked(hwnd):
                return True
            info = self._build_window_info(hwnd, monitors)
            if info is None:
                return True
            if not include_minimized and info.is_minimized:
                return True
            windows.append(info)
            return True

        callback = cb_type(_enum_cb)
        user32.EnumWindows(callback, 0)
        return windows

    def get_foreground_window(self) -> WindowInfo | None:
        if sys.platform != "win32":
            return None
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return None
        hwnd = int(windll.user32.GetForegroundWindow())
        if hwnd <= 0:
            return None
        monitors = self.list_monitors()
        return self._build_window_info(hwnd, monitors)

    def find_window(self, query: str) -> WindowInfo | None:
        clean = normalize_text(query)
        if not clean:
            return None
        windows = self.list_windows(include_minimized=True)
        for win in windows:
            proc_stem = normalize_text(win.process_name.removesuffix(".exe"))
            title_norm = normalize_text(win.title)
            if clean in (proc_stem, title_norm):
                return win
        for win in windows:
            proc_stem = normalize_text(win.process_name.removesuffix(".exe"))
            title_norm = normalize_text(win.title)
            if clean in proc_stem or clean in title_norm:
                return win
        return None

    def _resolve_target_window(self, query: str | None) -> WindowInfo | None:
        if query and query.strip():
            return self.find_window(query)
        return self.get_foreground_window()

    def focus_window(self, query: str | None = None, *, dry_run: bool = False) -> tuple[bool, str]:
        win = self._resolve_target_window(query)
        if win is None:
            return False, f"No encontré ninguna ventana abierta para '{query or 'activa'}'."
        if dry_run:
            return True, f"[Dry-Run] Enfocaría la ventana '{win.title}'."
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible en esta plataforma."
        user32 = windll.user32
        if win.is_minimized:
            user32.ShowWindow(win.hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(win.hwnd)
        return True, f"Ventana '{win.title}' enfocada."

    def minimize_window(
        self, query: str | None = None, *, dry_run: bool = False
    ) -> tuple[bool, str]:
        win = self._resolve_target_window(query)
        if win is None:
            return False, f"No encontré la ventana '{query or 'activa'}'."
        if dry_run:
            return True, f"[Dry-Run] Minimizaría '{win.title}'."
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible."
        windll.user32.ShowWindow(win.hwnd, _SW_MINIMIZE)
        return True, f"Minimicé '{win.title}'."

    def maximize_window(
        self, query: str | None = None, *, dry_run: bool = False
    ) -> tuple[bool, str]:
        win = self._resolve_target_window(query)
        if win is None:
            return False, f"No encontré la ventana '{query or 'activa'}'."
        if dry_run:
            return True, f"[Dry-Run] Maximizaría '{win.title}'."
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible."
        windll.user32.ShowWindow(win.hwnd, _SW_MAXIMIZE)
        return True, f"Maximicé '{win.title}'."

    def move_window_to_monitor(
        self,
        *,
        query: str | None = None,
        target_monitor_index: int | None = None,
        dry_run: bool = False,
    ) -> tuple[bool, str]:
        monitors = self.list_monitors()
        if len(monitors) < 2:
            return False, "Solo hay un monitor activo conectado en este momento."

        win = self._resolve_target_window(query)
        if win is None:
            return False, f"No encontré la ventana '{query or 'activa'}' para moverla."

        src_mon = monitors[min(win.monitor_index, len(monitors) - 1)]
        if target_monitor_index is None:
            dst_idx = (src_mon.index + 1) % len(monitors)
        else:
            if target_monitor_index < 0 or target_monitor_index >= len(monitors):
                return (
                    False,
                    f"El monitor {target_monitor_index + 1} no existe (hay {len(monitors)}).",
                )
            dst_idx = target_monitor_index

        dst_mon = monitors[dst_idx]
        if dry_run:
            return (
                True,
                f"[Dry-Run] Movería '{win.title}' al monitor {dst_mon.index + 1}.",
            )

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible."
        user32 = windll.user32

        was_maximized = win.is_maximized
        if was_maximized or win.is_minimized:
            user32.ShowWindow(win.hwnd, _SW_RESTORE)

        rect = _RECT()
        user32.GetWindowRect(win.hwnd, ctypes.byref(rect))
        cur_w = max(320, int(rect.right - rect.left))
        cur_h = max(240, int(rect.bottom - rect.top))
        new_x, new_y, new_w, new_h = compute_target_rect(
            int(rect.left),
            int(rect.top),
            cur_w,
            cur_h,
            src_mon,
            dst_mon,
        )

        user32.SetWindowPos(
            win.hwnd,
            0,
            new_x,
            new_y,
            new_w,
            new_h,
            _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )
        if was_maximized:
            user32.ShowWindow(win.hwnd, _SW_MAXIMIZE)

        return True, f"Moví '{win.title}' al monitor {dst_mon.index + 1}."

    def capture_layout_snapshot(self, *, max_windows: int = 12) -> list[dict[str, Any]]:
        """Captura el estado de ventanas visibles para las Rutinas ('Capturar estado actual')."""
        snapshot: list[dict[str, Any]] = []
        for win in self.list_windows(include_minimized=False)[:max_windows]:
            if not win.process_name:
                continue
            snapshot.append(
                {
                    "process_name": win.process_name,
                    "title": win.title,
                    "monitor_index": win.monitor_index,
                    "maximized": win.is_maximized,
                }
            )
        return snapshot
