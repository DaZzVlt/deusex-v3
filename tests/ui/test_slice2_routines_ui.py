from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QApplication
import pytest
from pytestqt.qtbot import QtBot

from deusex.app import DeusExApplication
from deusex.core.events import ScheduledJobFiredEvent
from deusex.modules.pc.module import PCModule
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.routines_view import RoutinesView
from deusex.ui.tray import SystemTrayController


@pytest.mark.ui
def test_routines_view_wires_pc_module_builder_and_capture(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()
    bridge = QtEventBridge(app.events)
    view = RoutinesView(app, bridge)
    qtbot.addWidget(view)

    try:
        # Zero-Base: PCModule desactivado -> botones de rutinas bloqueados
        assert view.capture_state_btn.isEnabled() is False
        assert view.new_routine_btn.isEnabled() is False

        # Activar PCModule -> reactividad vía module_state_changed
        assert app.set_module_enabled("pc", True).result(timeout=10.0) is True
        qtbot.waitUntil(lambda: view.capture_state_btn.isEnabled(), timeout=3000)
        assert view.new_routine_btn.isEnabled() is True

        # Crear rutina desde el constructor visual
        view.new_routine_btn.click()
        view.routine_name_edit.setText("Modo Foco")
        view.routine_trigger_edit.setText("foco")
        view.step_kind_combo.setCurrentIndex(2)  # set_volume
        view.step_value_edit.setText("30")
        view.add_step_btn.click()
        view.save_routine_btn.click()
        qapp.processEvents()

        pc_mod = app.engine.modules.get("pc")
        assert isinstance(pc_mod, PCModule)
        assert pc_mod.routines is not None
        saved = pc_mod.routines.get_routine("Modo Foco")
        assert saved is not None
        assert len(saved.steps) == 1

        # Capturar estado actual de forma asíncrona sin bloquear Qt
        cap_fut = view.capture_state_with_name("Espacio Capturado Test")
        assert cap_fut is not None
        captured = cap_fut.result(timeout=10.0)
        assert captured.name == "Espacio Capturado Test"
        assert len(captured.steps) >= 1
        qtbot.waitUntil(
            lambda: pc_mod.routines is not None
            and pc_mod.routines.get_routine("Espacio Capturado Test") is not None,
            timeout=3000,
        )
    finally:
        bridge.detach()
        view.close()
        view.deleteLater()
        qapp.processEvents()
        elapsed = app.shutdown()
        assert elapsed < 3.0


@pytest.mark.ui
def test_tray_notifies_when_scheduled_job_fires(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()
    bridge = QtEventBridge(app.events)
    tray = SystemTrayController(app, bridge)
    notified: list[tuple[str, str]] = []

    def _capture_notify(title: str, message: str, **_kwargs: Any) -> bool:
        notified.append((title, message))
        return True

    monkeypatch.setattr(tray, "show_notification", _capture_notify)

    try:
        app.events.publish(
            ScheduledJobFiredEvent(
                job_id="job-1",
                kind="timer",
                label="Café",
            )
        )
        qtbot.waitUntil(lambda: len(notified) == 1, timeout=3000)
        title, body = notified[0]
        assert "Temporizador" in title
        assert "Café" in body
    finally:
        bridge.detach()
        tray.hide()
        tray.deleteLater()
        qapp.processEvents()
        elapsed = app.shutdown()
        assert elapsed < 3.0
