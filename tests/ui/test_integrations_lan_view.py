# FILE: tests/ui/test_integrations_lan_view.py
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLineEdit, QPushButton, QTableWidget
import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ToolResult
from deusex.modules.network.adapters import DeviceType, NetworkDeviceRecord
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.integrations_view import IntegrationsView


def _find_button_by_text(parent: IntegrationsView, text: str) -> QPushButton:
    for btn in parent.findChildren(QPushButton):
        if btn.text().strip().lower() == text.strip().lower():
            return btn
    raise AssertionError(f"No se encontró el botón {text!r} en IntegrationsView.")


@pytest.mark.ui
def test_integrations_view_lan_scan_alias_and_controls(
    qapp: Any,
    qtbot: Any,
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
    view = IntegrationsView(app, bridge)
    view.show()
    qtbot.waitExposed(view)

    try:
        enable_btn = view.findChild(QPushButton, "LanEnableButton")
        scan_btn = view.findChild(QPushButton, "LanScanButton")
        table = view.findChild(QTableWidget, "LanDevicesTable")
        alias_input = view.findChild(QLineEdit, "LanAliasInput")
        save_alias_btn = view.findChild(QPushButton, "LanSaveAliasButton")

        assert enable_btn is not None
        assert scan_btn is not None
        assert table is not None
        assert alias_input is not None
        assert save_alias_btn is not None

        # Estado inicial Zero-Base: módulo `network` desactivado
        assert scan_btn.isEnabled() is False

        # Activar módulo `network`
        assert app.set_module_enabled("network", True).result(timeout=5.0) is True
        qtbot.waitUntil(lambda: scan_btn.isEnabled(), timeout=3000)

        # Mockear escaneo LAN para devolver 2 dispositivos sin tráfico de red
        async def _fake_scan(
            *,
            timeout_sec: float = 2.5,
            active_subnet_scan: bool = True,
        ) -> ToolResult:
            _ = (timeout_sec, active_subnet_scan)
            assert app.network_module.store is not None
            d1 = app.network_module.store.upsert_device(
                NetworkDeviceRecord(
                    device_uid="mac:00005e005310",
                    alias="Samsung QLED",
                    device_type=DeviceType.SAMSUNG_TV,
                    last_ip="192.0.2.10",
                    mac_address="00:00:5E:00:53:10",
                    port=8002,
                    online=True,
                ),
                preserve_alias=True,
            )
            d2 = app.network_module.store.upsert_device(
                NetworkDeviceRecord(
                    device_uid="mac:00005e005320",
                    alias="Chromecast Pieza",
                    device_type=DeviceType.CHROMECAST,
                    last_ip="192.0.2.20",
                    mac_address="00:00:5E:00:53:20",
                    port=8009,
                    online=True,
                ),
                preserve_alias=True,
            )
            return ToolResult(
                ok=True,
                summary="Se detectaron 2 dispositivos en la red local.",
                data={"devices": [d1.to_public_dict(), d2.to_public_dict()], "detected_count": 2},
            )

        monkeypatch.setattr(app.network_module, "scan_network", _fake_scan)

        qtbot.mouseClick(scan_btn, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: table.rowCount() == 2, timeout=3000)

        # Seleccionar primer dispositivo y guardar nuevo alias
        table.selectRow(0)
        qapp.processEvents()
        alias_input.setText("TV Principal")
        qtbot.mouseClick(save_alias_btn, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(
            lambda: any(
                (item := table.item(row, 1)) is not None and item.text() == "TV Principal"
                for row in range(table.rowCount())
            ),
            timeout=3000,
        )

        # Verificar persistencia real del alias en SQLite
        assert app.network_module.store is not None
        assert app.network_module.store.find_by_query("TV Principal") is not None

        # Marcar casilla Fav (columna 0) y verificar persistencia exclusiva en SQLite
        target_row = next(
            row
            for row in range(table.rowCount())
            if (item := table.item(row, 1)) is not None and item.text() == "TV Principal"
        )
        fav_item = table.item(target_row, 0)
        assert fav_item is not None
        fav_item.setCheckState(Qt.CheckState.Checked)
        qapp.processEvents()
        qtbot.waitUntil(
            lambda: (
                app.network_module.store is not None
                and (fav_dev := app.network_module.store.find_by_query("TV Principal")) is not None
                and fav_dev.is_favorite is True
            ),
            timeout=3000,
        )

        # Probar mini control rápido en modo dry_run sin tocar red real
        app.settings.developer.dry_run = True
        app.save_settings(section="developer")
        table.selectRow(0)
        qapp.processEvents()
        power_on_btn = _find_button_by_text(view, "Encender")
        assert power_on_btn.isEnabled() is True
        qtbot.mouseClick(power_on_btn, Qt.MouseButton.LeftButton)
        qtbot.wait(150)
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        bridge.deleteLater()
        qapp.processEvents()
        app.shutdown()
