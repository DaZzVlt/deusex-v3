from __future__ import annotations

from pathlib import Path

import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.modules.vision.live_guide import LiveGuideState


@pytest.mark.live
def test_slice5_live_reconciliation_and_fast_shutdown(tmp_path: Path) -> None:
    """Activa `intel` y `vision`, reinicia y valida reconciliación automática (Antídoto #2) + cierre <3 s."""
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app1.start()
    try:
        assert app1.set_module_enabled("intel", True).result(timeout=5.0) is True
        assert app1.set_module_enabled("vision", True).result(timeout=5.0) is True
        assert app1.intel_module.state == ModuleState.ACTIVE
        assert app1.vision_module.state == ModuleState.ACTIVE
    finally:
        elapsed1 = app1.shutdown()
        assert elapsed1 < 3.0

    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled = app2.start()
    try:
        assert "intel" in reconciled
        assert "vision" in reconciled
        assert app2.intel_module.state == ModuleState.ACTIVE
        assert app2.vision_module.state == ModuleState.ACTIVE
    finally:
        elapsed2 = app2.shutdown()
        assert elapsed2 < 3.0


@pytest.mark.live
def test_slice5_live_open_meteo_cache_news_rss_and_open_url(tmp_path: Path) -> None:
    """
    Valida Inteligencia Web real con 0 claves adicionales:
    - Clima real Open-Meteo (tier gratuito) + caché TTL 15 min.
    - Noticias reales Google News RSS con fuentes clickeables.
    - Apertura real de URL segura en navegador predeterminado.
    """
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.settings.general.city = "Concepción"
    app.settings.general.country = "Chile"
    app.save_settings(section="general")
    app.start()

    try:
        assert app.set_module_enabled("intel", True).result(timeout=5.0) is True

        # 1) Open-Meteo real (1ª llamada desde red, 2ª desde caché 15 min)
        res1 = app.runtime.async_core.run_sync(
            app.engine.tools.execute(
                "intel_weather",
                {"city": "Concepción", "country": "Chile", "include_forecast": True},
            ),
            timeout=15.0,
        )
        assert res1.ok is True
        assert res1.data["cached"] is False
        assert -30.0 <= float(res1.data["temperature_c"]) <= 55.0
        assert len(res1.sources) >= 1

        res2 = app.runtime.async_core.run_sync(
            app.engine.tools.execute(
                "intel_weather",
                {"city": "Concepción", "country": "Chile", "include_forecast": False},
            ),
            timeout=5.0,
        )
        assert res2.ok is True
        assert res2.data["cached"] is True

        # 2) Google News RSS real (0 claves) con fuentes clickeables
        res_news = app.runtime.async_core.run_sync(
            app.engine.tools.execute("intel_news", {"limit": 5}),
            timeout=15.0,
        )
        assert res_news.ok is True
        assert len(res_news.data["items"]) >= 1
        assert len(res_news.sources) >= 1
        assert all(src["url"].startswith(("http://", "https://")) for src in res_news.sources)

        # 3) Apertura real de URL segura en navegador predeterminado de Windows 11
        res_open = app.runtime.async_core.run_sync(
            app.engine.tools.execute("intel_open_url", {"url": "https://open-meteo.com/"}),
            timeout=10.0,
        )
        assert res_open.ok is True
        assert res_open.data["opened"] is True
    finally:
        elapsed = app.shutdown()
        assert elapsed < 3.0


@pytest.mark.live
def test_slice5_live_multimonitor_capture_and_live_guide_turns(tmp_path: Path) -> None:
    """
    Valida captura multi-monitor real (mss + Pillow) en Windows 11 y
    Guía Live por turnos con 0 capturas en pausa y cierre <3 s.
    """
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()

    try:
        assert app.set_module_enabled("vision", True).result(timeout=5.0) is True

        # 1) Listar monitores reales
        res_mon = app.runtime.async_core.run_sync(
            app.engine.tools.execute("vision_list_monitors", {}),
            timeout=10.0,
        )
        assert res_mon.ok is True
        assert int(res_mon.data["count"]) >= 1

        # 2) Captura puntual real
        frame = app.runtime.async_core.run_sync(
            app.vision_module.capture_quick(monitor="active", source="manual"),
            timeout=10.0,
        )
        assert frame.width > 0
        assert frame.height > 0
        assert frame.mime_type == "image/jpeg"
        assert frame.size_bytes > 1000

        # 3) Guía Live por turnos real: OFF (0) -> ACTIVE (1 por turno) -> PAUSED (0) -> OFF
        assert app.vision_module.live_guide_state == LiveGuideState.OFF
        assert (
            app.runtime.async_core.run_sync(
                app.vision_module.capture_for_live_turn(),
                timeout=5.0,
            )
            is None
        )

        snap_started = app.start_live_guide(monitor="active")
        assert snap_started.state == LiveGuideState.ACTIVE
        assert snap_started.captures_count == 0

        att = app.runtime.async_core.run_sync(
            app.vision_module.capture_for_live_turn(),
            timeout=10.0,
        )
        assert att is not None
        assert att.mime_type == "image/jpeg"
        assert len(att.data) > 1000
        assert app.vision_module.live_guide_snapshot().captures_count == 1

        snap_paused = app.pause_live_guide()
        assert snap_paused.state == LiveGuideState.PAUSED
        assert (
            app.runtime.async_core.run_sync(
                app.vision_module.capture_for_live_turn(),
                timeout=5.0,
            )
            is None
        )
        assert (
            app.runtime.async_core.run_sync(
                app.vision_module.capture_for_live_turn(),
                timeout=5.0,
            )
            is None
        )
        assert app.vision_module.live_guide_snapshot().captures_count == 1

        snap_stopped = app.stop_live_guide()
        assert snap_stopped.state == LiveGuideState.OFF
    finally:
        elapsed = app.shutdown()
        assert elapsed < 3.0
