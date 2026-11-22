# FILE: tests/ui/test_modules_drawer.py
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
import pytest

from deusex.app import DeusExApplication
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.modules_view import ModulesView


@pytest.mark.ui
def test_modules_view_drawer_toggle_close_button_and_escape(
    qapp: Any,
    qtbot: Any,
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
    view = ModulesView(app, bridge)
    view.show()
    qtbot.waitExposed(view)

    try:
        # 1. El drawer inicia oculto
        drawer = view._drawer
        assert drawer.isVisible() is False
        assert view._selected_module_id is None

        # 2. Localizar botones "Configurar"
        cfg_buttons = [
            btn
            for btn in view.findChildren(QPushButton)
            if btn.text().strip().lower() == "configurar"
        ]
        assert len(cfg_buttons) > 0
        first_cfg_btn = cfg_buttons[0]

        # 3. Primer click en "Configurar" abre el drawer
        first_cfg_btn.click()
        qapp.processEvents()
        assert drawer.isVisible() is True
        assert view._selected_module_id is not None

        # 4. Segundo click en el mismo "Configurar" cierra el drawer (toggle)
        first_cfg_btn.click()
        qapp.processEvents()
        assert drawer.isVisible() is False
        assert view._selected_module_id is None

        # 5. Abrir nuevamente y cerrar con el botón "✕" (ModuleDrawerCloseButton)
        first_cfg_btn.click()
        qapp.processEvents()
        assert drawer.isVisible() is True

        close_btn = view.findChild(QPushButton, "ModuleDrawerCloseButton")
        assert close_btn is not None
        close_btn.click()
        qapp.processEvents()
        assert drawer.isVisible() is False
        assert view._selected_module_id is None

        # 6. Abrir nuevamente y cerrar presionando Escape
        first_cfg_btn.click()
        qapp.processEvents()
        assert drawer.isVisible() is True

        qtbot.keyClick(view, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert drawer.isVisible() is False
        assert view._selected_module_id is None
    finally:
        bridge.detach()
        view.deleteLater()
        qapp.processEvents()
        assert app.shutdown() < 3.0
