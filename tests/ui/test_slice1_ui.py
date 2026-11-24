from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication
import pytest

from deusex.app import DeusExApplication
from deusex.core.events import SafetyConfirmationRequestedEvent
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import SafetyCard
from deusex.ui.main_window import MainWindow
from deusex.ui.minichat import MiniChatWindow
from deusex.ui.onboarding import OnboardingDialog


@pytest.mark.ui
def test_main_window_navigation_dev_mode_and_support_report(
    qapp: QApplication,
    qtbot: Any,
    qt_widget_cleanup: Callable[[Any], Any],
    tmp_path: Path,
) -> None:
    core_app = DeusExApplication(
        user_data_dir=tmp_path / "ui_main",
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    core_app.start()
    bridge = QtEventBridge(core_app.events)
    try:
        win = qt_widget_cleanup(MainWindow(core_app, bridge))
        qtbot.addWidget(win)

        # Modo Desarrollador oculto por defecto
        assert win.settings_view.dev_panel.isVisible() is False

        # Navegación por todas las secciones + Doctor
        for section in ("chat", "modules", "routines", "memory", "integrations", "settings", "doctor"):
            win.navigate_to(section)
            qapp.processEvents()

        # Colapso de Sidebar
        initial_width = win.sidebar.width()
        win.toggle_sidebar()
        assert win.sidebar.width() != initial_width

        # Reporte de soporte anonimizado sin fugas
        core_app.save_api_key("GEMINI_API_KEY", "AIza" + "9" * 35, provider_id="gemini")
        report = win.doctor_logs_view.build_anonymized_support_report()
        assert "Reporte de Soporte Anonimizado" in report
        assert "AIza999" not in report

        win.prepare_for_exit()
    finally:
        bridge.detach()
        assert core_app.shutdown() < 3.0


@pytest.mark.ui
def test_safety_card_minichat_and_onboarding_clipboard(
    qapp: QApplication,
    qtbot: Any,
    qt_widget_cleanup: Callable[[Any], Any],
    tmp_path: Path,
) -> None:
    core_app = DeusExApplication(
        user_data_dir=tmp_path / "ui_sub",
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    core_app.start()
    bridge = QtEventBridge(core_app.events)
    try:
        # Tarjeta SafetyGate
        decisions: list[tuple[str, bool]] = []
        card = qt_widget_cleanup(
            SafetyCard(
                SafetyConfirmationRequestedEvent(
                    confirmation_id="cid_demo",
                    tool_name="pc.trash",
                    summary_es="Vaciar papelera de reciclaje",
                    timeout_sec=15,
                )
            )
        )
        qtbot.addWidget(card)
        card.decision_made.connect(lambda cid, ok: decisions.append((cid, ok)))
        card.approve_btn.click()
        assert card.is_resolved is True
        assert decisions == [("cid_demo", True)]

        # MiniChat
        minichat = qt_widget_cleanup(MiniChatWindow(core_app, bridge))
        qtbot.addWidget(minichat)
        assert minichat.capture_btn.isVisible() is False

        # Onboarding + Smart Clipboard Catcher
        clipboard = qapp.clipboard()
        if clipboard is not None:
            clipboard.setText("AIza" + "K" * 35)
        onboarding = qt_widget_cleanup(OnboardingDialog(core_app))
        qtbot.addWidget(onboarding)
        assert onboarding.check_clipboard_for_key() is True
        onboarding.finish_onboarding()
        assert core_app.settings.general.onboarding_completed is True
        assert core_app.secrets.has("GEMINI_API_KEY") is True
    finally:
        bridge.detach()
        assert core_app.shutdown() < 3.0
