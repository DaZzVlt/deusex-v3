from __future__ import annotations

import ctypes
from dataclasses import dataclass
import logging
import sys
from typing import Literal

from PySide6.QtWidgets import QApplication, QWidget

from deusex.core.paths import AppPaths

logger = logging.getLogger(__name__)

ThemeMode = Literal["auto", "fluent_dark", "fluent_light", "cyber_hud"]
ResolvedTheme = Literal["fluent_dark", "fluent_light", "cyber_hud"]

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
_DWMSBT_MAINWINDOW = 2  # Mica en Windows 11 22H2+


def _parse_hex_rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) != 6:
        return (56, 189, 248)
    try:
        return (
            int(cleaned[0:2], 16),
            int(cleaned[2:4], 16),
            int(cleaned[4:6], 16),
        )
    except ValueError:
        return (56, 189, 248)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    cr = max(0, min(255, r))
    cg = max(0, min(255, g))
    cb = max(0, min(255, b))
    return f"#{cr:02X}{cg:02X}{cb:02X}"


def _derive_accent_tokens(accent_hex: str) -> dict[str, str]:
    r, g, b = _parse_hex_rgb(accent_hex)
    normalized = _rgb_to_hex(r, g, b)
    # Luminancia relativa WCAG para elegir texto negro o blanco legible sobre el botón primario.
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    text_on_accent = "#0B1120" if luminance > 0.56 else "#FFFFFF"
    hover = _rgb_to_hex(min(255, r + 18), min(255, g + 18), min(255, b + 18))
    return {
        "ACCENT": normalized,
        "ACCENT_HOVER": hover,
        "ACCENT_SOFT": f"rgba({r}, {g}, {b}, 0.16)",
        "ACCENT_BORDER": f"rgba({r}, {g}, {b}, 0.48)",
        "ACCENT_TEXT": text_on_accent,
    }


def detect_windows_dark_mode() -> bool:
    """Detecta si Windows 11 está en modo oscuro para apps (True por defecto si no se puede leer)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except OSError:
        return True


def detect_windows_accent_hex() -> str | None:
    """Lee el color de acento configurado en Windows 11 (formato #RRGGBB)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\DWM",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
            abgr = int(value)
            r = abgr & 0xFF
            g = (abgr >> 8) & 0xFF
            b = (abgr >> 16) & 0xFF
            return _rgb_to_hex(r, g, b)
    except OSError:
        return None


def resolve_theme_name(mode: ThemeMode) -> ResolvedTheme:
    if mode == "auto":
        return "fluent_dark" if detect_windows_dark_mode() else "fluent_light"
    return mode


def apply_windows_backdrop(widget: QWidget, *, dark: bool, use_mica: bool) -> bool:
    """Aplica barra de título inmersiva y material Mica opcional en ventanas de Windows 11."""
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(widget.winId())
        if hwnd == 0:
            return False
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False

        dwmapi = windll.dwmapi
        dark_val = ctypes.c_int(1 if dark else 0)
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(dark_val),
            ctypes.sizeof(dark_val),
        )
        backdrop_val = ctypes.c_int(_DWMSBT_MAINWINDOW if use_mica else _DWMSBT_NONE)
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(backdrop_val),
            ctypes.sizeof(backdrop_val),
        )
        return True
    except Exception as exc:
        logger.debug("Backdrop nativo Win11 omitido: %s", exc)
        return False


@dataclass(frozen=True, slots=True)
class ActiveThemeState:
    mode: ThemeMode
    resolved: ResolvedTheme
    accent_hex: str
    is_dark: bool
    use_mica: bool


class ThemeManager:
    """Carga los QSS oficiales, inyecta tokens de acento dinámico y aplica estilos a Qt."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._state = ActiveThemeState(
            mode="auto",
            resolved="fluent_dark",
            accent_hex="#38BDF8",
            is_dark=True,
            use_mica=True,
        )

    @property
    def state(self) -> ActiveThemeState:
        return self._state

    def build_stylesheet(
        self,
        *,
        mode: ThemeMode,
        accent_hex: str,
        use_windows_accent: bool = False,
    ) -> tuple[str, ActiveThemeState]:
        resolved = resolve_theme_name(mode)
        effective_accent = accent_hex
        if use_windows_accent:
            win_accent = detect_windows_accent_hex()
            if win_accent:
                effective_accent = win_accent

        qss_file = self._paths.themes_dir / f"{resolved}.qss"
        raw_qss = qss_file.read_text(encoding="utf-8") if qss_file.is_file() else ""

        tokens = _derive_accent_tokens(effective_accent)
        rendered = raw_qss
        for key, value in tokens.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)

        state = ActiveThemeState(
            mode=mode,
            resolved=resolved,
            accent_hex=tokens["ACCENT"],
            is_dark=resolved != "fluent_light",
            use_mica=self._state.use_mica,
        )
        return rendered, state

    def apply(
        self,
        app: QApplication,
        *,
        mode: ThemeMode,
        accent_hex: str,
        use_mica: bool = True,
        use_windows_accent: bool = False,
        windows: tuple[QWidget, ...] = (),
    ) -> ActiveThemeState:
        stylesheet, base_state = self.build_stylesheet(
            mode=mode,
            accent_hex=accent_hex,
            use_windows_accent=use_windows_accent,
        )
        self._state = ActiveThemeState(
            mode=base_state.mode,
            resolved=base_state.resolved,
            accent_hex=base_state.accent_hex,
            is_dark=base_state.is_dark,
            use_mica=use_mica,
        )
        app.setStyleSheet(stylesheet)
        for win in windows:
            apply_windows_backdrop(win, dark=self._state.is_dark, use_mica=use_mica)
        return self._state
