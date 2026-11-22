from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.core.events import FastPathExecutedEvent, SafetyConfirmationRequestedEvent
from deusex.modules.pc.module import PCModule


@pytest.mark.live
def test_slice2_pc_live_windows11(tmp_path: Path) -> None:
    """
    Smoke test EN VIVO (Compuerta 4) sobre Windows 11 real:
    - Antídoto #2: activar `pc`, reiniciar app y verificar reconciliación automática.
    - Antídoto #4: `shell:AppsFolder` + `UserChoice` ("navegador") + "calculadora".
    - Fast-Path <50 ms + pycaw en ComStaThread (volumen maestro 35% y restauración).
    - SafetyGate físico bloqueando acción CRITICAL ("vacía la papelera").
    - Telemetría real sin UAC y captura/ejecución dry-run de rutina.
    """
    if sys.platform != "win32":
        pytest.skip("Solo aplicable en Windows 11.")

    # 1. Activar PC y cerrar limpio (<3 s)
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app1.start()
    try:
        assert app1.set_module_enabled("pc", True).result(timeout=10.0) is True
    finally:
        assert app1.shutdown() < 3.0

    # 2. Reiniciar sobre el mismo user_data_dir -> debe reconciliar automáticamente
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled = app2.start()
    original_vol: int | None = None
    original_muted: bool | None = None

    try:
        assert "pc" in reconciled
        pc_mod = app2.engine.modules.get("pc")
        assert isinstance(pc_mod, PCModule)
        assert pc_mod.state == ModuleState.ACTIVE
        assert pc_mod.audio is not None
        assert pc_mod.routines is not None

        # Antídoto #4: shell:AppsFolder real + UserChoice
        assert pc_mod.apps.entry_count >= 10
        assert pc_mod.apps.find_app("calculadora") is not None
        browser = pc_mod.apps.find_app("navegador")
        assert browser is not None

        # Volumen real con pycaw en ComStaThread (restaurado en finally)
        original_vol, original_muted = app2.runtime.async_core.run_sync(
            pc_mod.audio.get_master_volume(),
            timeout=5.0,
        )
        fp_events: list[FastPathExecutedEvent] = []
        app2.events.subscribe(FastPathExecutedEvent, fp_events.append)

        t0 = time.perf_counter()
        app2.submit_user_message("pon el volumen al 35").result(timeout=5.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert len(fp_events) >= 1
        assert elapsed_ms < 500.0  # incluye hilo/event loop; FastPath interno <50 ms

        cur_vol, _cur_muted = app2.runtime.async_core.run_sync(
            pc_mod.audio.get_master_volume(),
            timeout=5.0,
        )
        assert abs(cur_vol - 35) <= 2

        # Telemetría real sin UAC
        snap = pc_mod.telemetry.snapshot()
        assert snap.ram_total_gb > 0
        assert snap.cpu_cores_logical >= 1

        # SafetyGate físico real en acción CRITICAL (se rechaza para no tocar la papelera)
        blocked: list[SafetyConfirmationRequestedEvent] = []

        def _reject(ev: SafetyConfirmationRequestedEvent) -> None:
            blocked.append(ev)
            app2.resolve_safety_confirmation(ev.confirmation_id, approved=False, reason="live_test")

        app2.events.subscribe(SafetyConfirmationRequestedEvent, _reject)
        app2.submit_user_message("vacía la papelera").result(timeout=5.0)
        assert len(blocked) == 1

        # Capturar estado actual real y ejecutar en dry_run
        rec = app2.runtime.async_core.run_sync(
            pc_mod.routines.capture_current_state(name="Live Slice2 Snapshot", save=True),
            timeout=8.0,
        )
        assert len(rec.steps) >= 1
        exec_res = app2.runtime.async_core.run_sync(
            pc_mod.routines.execute_routine(rec.id, dry_run=True),
            timeout=8.0,
        )
        assert exec_res.ok is True
    finally:
        pc_mod_cleanup = app2.engine.modules.get("pc")
        if (
            isinstance(pc_mod_cleanup, PCModule)
            and pc_mod_cleanup.audio is not None
            and original_vol is not None
            and original_muted is not None
        ):
            app2.runtime.async_core.run_sync(
                pc_mod_cleanup.audio.set_master_volume(original_vol),
                timeout=5.0,
            )
            app2.runtime.async_core.run_sync(
                pc_mod_cleanup.audio.set_master_mute(original_muted),
                timeout=5.0,
            )
        assert app2.shutdown() < 3.0
