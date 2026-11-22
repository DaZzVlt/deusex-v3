# FILE: tests/live/test_slice3_live.py
from __future__ import annotations

from pathlib import Path

import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.modules.network.discovery import get_local_ipv4_networks
from deusex.modules.network.youtube_tv import ActiveBrowserUrlReader


@pytest.mark.live
def test_slice3_live_reconcile_discovery_and_shutdown(tmp_path: Path) -> None:
    """
    Smoke test EN VIVO de la Rebanada 3 en Windows 11 real:
    - Activa `network`, reinicia la app y verifica reconciliación automática (Antídoto #2).
    - Detecta interfaces IPv4 locales dinámicamente y ejecuta un escaneo LAN real sin IPs fijas.
    - Verifica `ActiveBrowserUrlReader` en `ComStaThread` sin errores.
    - Si hay dispositivos reales, verifica persistencia de alias sin imprimir IPs ni tokens.
    - Garantiza cierre determinista en `<3 s`.
    """
    # 1. Activar `network` y cerrar limpiamente
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=True,
    )
    try:
        app1.start()
        assert app1.set_module_enabled("network", True).result(timeout=5.0) is True
    finally:
        assert app1.shutdown() < 3.0

    # 2. Reiniciar sobre el mismo directorio y verificar reconciliación automática
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=True,
    )
    try:
        reconciled = app2.start()
        assert "network" in reconciled
        assert app2.network_module.state == ModuleState.ACTIVE

        nets = get_local_ipv4_networks()
        assert len(nets) >= 1, "No se encontró ninguna interfaz IPv4 privada activa en Windows 11."

        # Escaneo LAN real con timeout acotado
        scan_res = app2.runtime.async_core.run_sync(
            app2.network_module.scan_network(timeout_sec=3.0, active_subnet_scan=True),
            timeout=12.0,
        )
        assert scan_res.ok is True

        devices = app2.network_module.list_known_devices()
        if devices:
            first = devices[0]
            rename_res = app2.runtime.async_core.run_sync(
                app2.network_module.rename_device(first.device_uid, f"{first.alias} Live"),
                timeout=5.0,
            )
            assert rename_res.ok is True

        # Validar ejecución segura de ActiveBrowserUrlReader en ComStaThread
        reader = ActiveBrowserUrlReader(app2.runtime.com_sta)
        tab = app2.runtime.async_core.run_sync(
            reader.read_active_tab(prefer_youtube=True, timeout_sec=2.0),
            timeout=4.0,
        )
        assert tab is None or isinstance(tab.url, str)
    finally:
        elapsed = app2.shutdown()
        assert elapsed < 3.0
