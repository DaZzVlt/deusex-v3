from __future__ import annotations

import contextlib
import ctypes
import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from deusex import __app_name__, __version__
from deusex.app import DeusExApplication
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window import MainWindow
from deusex.ui.minichat import MiniChatWindow
from deusex.ui.onboarding import OnboardingDialog
from deusex.ui.orb import FloatingOrbWindow, SubtitleHolobox
from deusex.ui.theme import ThemeManager
from deusex.ui.tray import SystemTrayController, create_programmatic_app_icon

logger = logging.getLogger(__name__)


def _enable_windows_dpi_awareness() -> None:
    """Habilita DPI PerMonitorV2 en Windows 11 antes de crear QApplication."""
    if sys.platform != "win32":
        return
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return
    with contextlib.suppress(Exception):
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
        windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def main(argv: list[str] | None = None) -> int:
    _enable_windows_dpi_awareness()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    qt_app = QApplication(argv if argv is not None else sys.argv)
    qt_app.setApplicationName(__app_name__)
    qt_app.setApplicationVersion(__version__)
    qt_app.setQuitOnLastWindowClosed(False)

    core_app = DeusExApplication()
    core_app.start()

    bridge = QtEventBridge(core_app.events)
    theme_manager = ThemeManager(core_app.paths)

    main_window = MainWindow(core_app, bridge)
    minichat = MiniChatWindow(core_app, bridge)
    tray = SystemTrayController(core_app, bridge)

    initial_prof = core_app.profiles.get(core_app.settings.ui.active_profile_id)
    orb_window = FloatingOrbWindow(
        style=core_app.settings.ui.orb_style,
        accent_hex=initial_prof.accent_hex,
        visibility_mode=core_app.settings.ui.orb_visibility,
        corner=core_app.settings.ui.orb_corner,
        avatar_path=core_app.settings.ui.orb_avatar_path,
    )
    subtitle_holobox = SubtitleHolobox(accent_hex=initial_prof.accent_hex)
    orb_window.set_privacy_mode(core_app.settings.general.privacy_mode)
    subtitle_holobox.set_privacy_mode(core_app.settings.general.privacy_mode)

    def _apply_theme() -> None:
        prof = core_app.profiles.get(core_app.settings.ui.active_profile_id)
        theme_manager.apply(
            qt_app,
            mode=core_app.settings.ui.theme,
            accent_hex=prof.accent_hex,
            use_mica=core_app.settings.ui.use_mica,
            use_windows_accent=core_app.settings.ui.use_windows_accent,
            windows=(main_window, minichat),
        )
        orb_window.set_accent_color(prof.accent_hex)
        subtitle_holobox.set_accent_color(prof.accent_hex)
        qt_app.setWindowIcon(create_programmatic_app_icon(prof.accent_hex))

    main_window.theme_refresh_requested.connect(_apply_theme)

    def _show_main() -> None:
        main_window.show()
        main_window.raise_()
        main_window.activateWindow()

    def _on_hotkey(hotkey_id: str, _combo: str) -> None:
        if hotkey_id == "minichat":
            minichat.toggle_visibility()
        elif hotkey_id == "privacy":
            core_app.toggle_privacy_mode()

    bridge.hotkey_triggered.connect(_on_hotkey)
    minichat.open_full_history_requested.connect(_show_main)
    tray.open_main_requested.connect(_show_main)
    tray.toggle_minichat_requested.connect(minichat.toggle_visibility)

    def _on_orb_state(event: object) -> None:
        from deusex.core.events import OrbStateChangedEvent

        if isinstance(event, OrbStateChangedEvent):
            orb_window.apply_orb_state(
                event.state,
                visible=event.visible,
                accent_hex=event.accent_hex,
                preview_sec=event.preview_sec,
            )

    def _on_subtitle(event: object) -> None:
        from deusex.core.events import SubtitleUpdatedEvent

        if isinstance(event, SubtitleUpdatedEvent):
            subtitle_holobox.update_subtitle(
                event.text,
                word=event.word,
                is_final=event.is_final,
                clear=event.clear,
            )

    def _on_settings_updated(event: object) -> None:
        from deusex.core.events import SettingsUpdatedEvent

        if isinstance(event, SettingsUpdatedEvent) and event.section in ("all", "ui"):
            orb_window.set_style(
                core_app.settings.ui.orb_style,
                avatar_path=core_app.settings.ui.orb_avatar_path,
            )
            orb_window.set_visibility_mode(core_app.settings.ui.orb_visibility)
            orb_window.set_corner(core_app.settings.ui.orb_corner)

    def _on_profile_changed(event: object) -> None:
        from deusex.core.events import ProfileChangedEvent

        if isinstance(event, ProfileChangedEvent):
            orb_window.set_accent_color(event.accent_hex)
            subtitle_holobox.set_accent_color(event.accent_hex)

    bridge.orb_state_changed.connect(_on_orb_state)
    bridge.audio_level_changed.connect(lambda _src, lvl: orb_window.set_audio_level(lvl))
    bridge.subtitle_updated.connect(_on_subtitle)
    bridge.privacy_mode_changed.connect(orb_window.set_privacy_mode)
    bridge.privacy_mode_changed.connect(subtitle_holobox.set_privacy_mode)
    bridge.settings_updated.connect(_on_settings_updated)
    bridge.profile_changed.connect(_on_profile_changed)

    exiting = False

    def _clean_exit() -> None:
        nonlocal exiting
        if exiting:
            return
        exiting = True
        tray.hide()
        minichat.hide()
        main_window.prepare_for_exit()
        main_window.close()
        orb_window.prepare_for_exit()
        orb_window.close()
        subtitle_holobox.prepare_for_exit()
        subtitle_holobox.close()
        bridge.detach()
        core_app.shutdown()
        qt_app.quit()

    tray.exit_requested.connect(_clean_exit)
    qt_app.aboutToQuit.connect(_clean_exit)

    _apply_theme()
    tray.show()

    if not core_app.settings.general.onboarding_completed:
        onboarding = OnboardingDialog(core_app, parent=main_window)
        onboarding.theme_preview_requested.connect(_apply_theme)
        onboarding.exec()
        onboarding.deleteLater()
        _apply_theme()
        main_window.settings_view.refresh_from_app()
        main_window.chat_view.sync_header_state()

    _show_main()
    return int(qt_app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
