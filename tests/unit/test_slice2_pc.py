from __future__ import annotations

import asyncio
import io
from pathlib import Path
import struct
import wave

import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState, RiskLevel
from deusex.core.db import Database
from deusex.core.events import FastPathExecutedEvent, SafetyConfirmationRequestedEvent
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import ComStaThread
from deusex.core.system_tools import EarconPlayer
from deusex.modules.pc.apps import (
    AppEntry,
    AppScanner,
    CloseCategory,
    classify_close_target,
    generate_aliases,
    phonetic_key,
)
from deusex.modules.pc.audio_ctrl import AudioBrightnessController
from deusex.modules.pc.clipboard import ClipboardService, classify_clipboard_text
from deusex.modules.pc.files import FileManagerService, is_protected_path
from deusex.modules.pc.module import PCModule
from deusex.modules.pc.routines import RoutineManager, RoutineStep
from deusex.modules.pc.windows import MonitorInfo, WindowManager, compute_target_rect


def test_app_scanner_uwp_phonetic_aliases_and_dry_run() -> None:
    entries = [
        AppEntry(
            name="Spotify",
            launch_target="SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify",
            source="appsfolder",
            aliases=generate_aliases("Spotify"),
            executable_name="spotify.exe",
        ),
        AppEntry(
            name="WhatsApp",
            launch_target="5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App",
            source="appsfolder",
            aliases=generate_aliases("WhatsApp"),
            executable_name="whatsapp.exe",
        ),
        AppEntry(
            name="Calculadora",
            launch_target="calc.exe",
            source="utility",
            aliases=generate_aliases("Calculadora"),
            executable_name="calc.exe",
        ),
    ]
    scanner = AppScanner(entries_override=entries)

    assert phonetic_key("espotifai") == phonetic_key("spotify")
    found_spotify = scanner.find_app("espotifai")
    assert found_spotify is not None
    assert found_spotify.name == "Spotify"
    assert found_spotify.source == "appsfolder"

    found_wa = scanner.find_app("guasap")
    assert found_wa is not None
    assert found_wa.name == "WhatsApp"

    launch = scanner.launch_app("spotify", dry_run=True)
    assert launch.ok is True
    assert launch.dry_run is True
    assert launch.entry is not None
    assert "Spotify" in launch.summary


def test_smart_close_three_levels() -> None:
    running = ["explorer.exe", "svchost.exe", "Code.exe", "notepad.exe", "Spotify.exe", "Overwatch.exe"]

    sys_dec = classify_close_target("explorer", running_process_names=running)
    assert sys_dec.category == CloseCategory.BLOCKED_SYSTEM

    sys_dec2 = classify_close_target("svchost.exe", running_process_names=running)
    assert sys_dec2.category == CloseCategory.BLOCKED_SYSTEM

    prod_dec = classify_close_target("vscode", running_process_names=running)
    assert prod_dec.category == CloseCategory.REQUIRES_SAFETY_GATE
    assert "code.exe" in prod_dec.matched_processes

    prod_dec2 = classify_close_target("bloc de notas", running_process_names=running)
    assert prod_dec2.category == CloseCategory.REQUIRES_SAFETY_GATE

    safe_dec = classify_close_target("spotify", running_process_names=running)
    assert safe_dec.category == CloseCategory.DIRECT_SAFE

    game_dec = classify_close_target("overwatch", running_process_names=running)
    assert game_dec.category == CloseCategory.DIRECT_SAFE

    # Hotfix Universal: typos fonéticos sin alias manual mediante AppScanner
    scanner = AppScanner(
        entries_override=[
            AppEntry(
                name="Bloc de notas",
                launch_target="notepad.exe",
                source="utility",
                aliases=generate_aliases("Bloc de notas"),
                executable_name="notepad.exe",
            ),
            AppEntry(
                name="Spotify",
                launch_target="spotify.exe",
                source="appsfolder",
                aliases=generate_aliases("Spotify"),
                executable_name="spotify.exe",
            ),
        ]
    )
    typo_dec = classify_close_target(
        "cierra el block de notas",
        running_process_names=["notepad.exe"],
        app_scanner=scanner,
    )
    assert typo_dec.category == CloseCategory.REQUIRES_SAFETY_GATE
    assert "notepad.exe" in typo_dec.matched_processes

    typo_dec2 = classify_close_target(
        "bloq de notas",
        running_process_names=["notepad.exe"],
        app_scanner=scanner,
    )
    assert typo_dec2.category == CloseCategory.REQUIRES_SAFETY_GATE
    assert "notepad.exe" in typo_dec2.matched_processes

    # Prioridad de ventanas visibles: nunca arrastra launchers ni helpers de fondo
    spotify_procs = ["spotify.exe", "spotifylauncher.exe", "spotifyxboxgamebarwebview.exe"]
    win_dec = classify_close_target(
        "spotify",
        running_process_names=spotify_procs,
        visible_process_names=["spotify.exe"],
    )
    assert win_dec.category == CloseCategory.DIRECT_SAFE
    assert win_dec.matched_processes == ("spotify.exe",)

    # Descarte de helpers/servicios en fallback sin ventana visible
    xbox_procs = ["xboxgipsvc.exe", "xboxapp.exe"]
    fallback_dec = classify_close_target(
        "xbox",
        running_process_names=xbox_procs,
    )
    assert "xboxapp.exe" in fallback_dec.matched_processes
    assert "xboxgipsvc.exe" not in fallback_dec.matched_processes


def test_files_organize_undo_idempotent_and_protected_paths(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    db.open()
    try:
        inbox = tmp_path / "Descargas"
        inbox.mkdir()
        (inbox / "informe.pdf").write_text("pdf", encoding="utf-8")
        (inbox / "foto.png").write_bytes(b"png")
        (inbox / "script.py").write_text("print(1)\n", encoding="utf-8")

        svc = FileManagerService(db, home_dir=tmp_path)
        assert is_protected_path(tmp_path, home_dir=tmp_path) is True
        assert is_protected_path(inbox, home_dir=tmp_path) is False

        hits = svc.search_files("informe", root_hint=str(inbox))
        assert len(hits) == 1
        assert hits[0].name == "informe.pdf"

        dry = svc.organize_folder(str(inbox), dry_run=True)
        assert dry.ok is True
        assert dry.dry_run is True
        assert (inbox / "informe.pdf").exists()

        res = svc.organize_folder(str(inbox), dry_run=False)
        assert res.ok is True
        assert res.moved_count == 3
        assert res.audit_id is not None
        assert (inbox / "Documentos" / "informe.pdf").exists()
        assert (inbox / "Imágenes" / "foto.png").exists()
        assert (inbox / "Código" / "script.py").exists()

        undo1 = svc.undo_action(res.audit_id)
        assert undo1.ok is True
        assert undo1.restored_count == 3
        assert undo1.already_undone is False
        assert (inbox / "informe.pdf").exists()
        assert (inbox / "foto.png").exists()
        assert (inbox / "script.py").exists()

        # Idempotencia: segunda llamada no falla ni vuelve a mover nada
        undo2 = svc.undo_action(res.audit_id)
        assert undo2.ok is True
        assert undo2.already_undone is True
        assert undo2.restored_count == 0
    finally:
        db.close()


def test_clipboard_respects_privacy_mode_and_classifies_content() -> None:
    storage = {"text": "https://example.com/docs"}
    privacy = {"active": False}

    svc = ClipboardService(
        is_privacy_active=lambda: privacy["active"],
        reader=lambda: storage["text"],
        writer=lambda val: storage.__setitem__("text", val),
    )

    info = svc.inspect()
    assert info.has_text is True
    assert info.kind == "url"
    assert classify_clipboard_text('{"a": 1}') == "json"

    ok, _msg = svc.write_text("hola mundo")
    assert ok is True
    assert storage["text"] == "hola mundo"

    privacy["active"] = True
    with pytest.raises(PermissionError):
        svc.read_text()
    with pytest.raises(PermissionError):
        svc.write_text("bloqueado")


def test_brightness_graceful_degradation_and_ddcci_fallback() -> None:
    sta = ComStaThread(name="TestSta")
    ctrl_none = AudioBrightnessController(
        sta,
        wmi_get=lambda: None,
        wmi_set=lambda _v: False,
        ddcci_get=lambda: None,
        ddcci_set=lambda _v: False,
    )
    res_none = ctrl_none.set_brightness(60)
    assert res_none.ok is False
    assert res_none.supported is False

    ddc_state = {"val": 50}

    def _set_ddc(v: int) -> bool:
        ddc_state["val"] = v
        return True

    ctrl_ddc = AudioBrightnessController(
        sta,
        wmi_get=lambda: None,
        wmi_set=lambda _v: False,
        ddcci_get=lambda: ddc_state["val"],
        ddcci_set=_set_ddc,
    )
    res_ddc = ctrl_ddc.adjust_brightness(20)
    assert res_ddc.ok is True
    assert res_ddc.level_pct == 70
    assert res_ddc.backend == "ddc_ci"


def test_window_compute_target_rect_keeps_window_inside_target_monitor() -> None:
    mon1 = MonitorInfo(0, "M1", True, 0, 0, 1920, 1080, 0, 0, 1920, 1040)
    mon2 = MonitorInfo(1, "M2", False, 1920, 0, 3200, 720, 1920, 0, 3200, 680)
    x, y, w, h = compute_target_rect(1500, 800, 1400, 900, mon1, mon2)
    assert mon2.work_left <= x <= mon2.work_right - w
    assert mon2.work_top <= y <= mon2.work_bottom - h
    assert w <= mon2.work_width
    assert h <= mon2.work_height


def test_routines_crud_and_dry_run_execution(tmp_path: Path) -> None:
    db = Database(tmp_path / "routines.db")
    db.open()
    sta = ComStaThread(name="TestRoutineSta")
    try:
        scanner = AppScanner(
            entries_override=[
                AppEntry(
                    name="Spotify",
                    launch_target="Spotify!App",
                    source="appsfolder",
                    aliases=("spotify",),
                )
            ]
        )
        audio = AudioBrightnessController(
            sta,
            wmi_get=lambda: 70,
            wmi_set=lambda _v: True,
            ddcci_get=lambda: None,
            ddcci_set=lambda _v: False,
        )
        mgr = RoutineManager(db, scanner, audio, WindowManager())

        rec = mgr.save_routine(
            name="Modo Estudio",
            trigger_phrase="estudio",
            steps=[
                RoutineStep(kind="open_app", params={"app": "spotify"}),
                RoutineStep(kind="set_volume", params={"level_pct": 30}),
                RoutineStep(kind="set_brightness", params={"level_pct": 65}),
            ],
        )
        assert len(mgr.list_routines()) == 1
        assert mgr.get_routine("estudio") is not None

        exec_res = asyncio.run(mgr.execute_routine("estudio", dry_run=True))
        assert exec_res.ok is True
        assert exec_res.executed_steps == 3
        assert exec_res.dry_run is True

        assert mgr.set_enabled(rec.id, False) is True
        disabled_res = asyncio.run(mgr.execute_routine(rec.id, dry_run=True))
        assert disabled_res.ok is False

        assert mgr.delete_routine(rec.id) is True
        assert mgr.list_routines() == []
    finally:
        db.close()


def test_pc_module_permissions_catalog_antidote3() -> None:
    mod = PCModule(app_scanner=AppScanner(entries_override=[]))
    allowed = {
        Permission.PC_BASIC,
        Permission.PC_CLIPBOARD,
        Permission.PC_FILES,
        Permission.PC_ADVANCED,
    }
    for spec in mod.get_tools():
        assert isinstance(spec.permission, Permission)
        assert spec.permission in allowed
    for rule in mod.get_fast_path_rules():
        assert isinstance(rule.permission, Permission)
        assert rule.permission in allowed


def test_pc_module_reconciliation_antidote2_and_safety_gate(tmp_path: Path) -> None:
    # Arranque 1: Zero-Base (pc desactivado), luego se habilita y persiste en settings.json
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled1 = app1.start()
    try:
        assert "pc" not in reconciled1
        pc_mod1 = app1.engine.modules.get("pc")
        assert pc_mod1 is not None
        assert pc_mod1.state == ModuleState.DISABLED

        enabled_ok = app1.set_module_enabled("pc", True).result(timeout=10.0)
        assert enabled_ok is True
        pc_mod_active = app1.engine.modules.get("pc")
        assert pc_mod_active is not None
        assert pc_mod_active.state == ModuleState.ACTIVE
    finally:
        elapsed1 = app1.shutdown()
        assert elapsed1 < 3.0

    # Arranque 2: Antídoto #2 -> PCModule y sus FastPathRule deben reconciliarse automáticamente
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled2 = app2.start()
    try:
        assert "pc" in reconciled2
        pc_mod2 = app2.engine.modules.get("pc")
        assert isinstance(pc_mod2, PCModule)
        assert pc_mod2.state == ModuleState.ACTIVE

        # Verificar que una acción CRITICAL en Fast-Path se frena físicamente en SafetyGate
        safety_events: list[SafetyConfirmationRequestedEvent] = []
        fp_events: list[FastPathExecutedEvent] = []

        def _reject_safety(ev: SafetyConfirmationRequestedEvent) -> None:
            safety_events.append(ev)
            app2.resolve_safety_confirmation(ev.confirmation_id, approved=False, reason="test")

        app2.events.subscribe(SafetyConfirmationRequestedEvent, _reject_safety)
        app2.events.subscribe(FastPathExecutedEvent, fp_events.append)

        turn = app2.submit_user_message("vacía la papelera").result(timeout=5.0)
        assert len(safety_events) == 1
        assert turn is not None

        # Verificar regla de productividad también retenida en SafetyGate
        safety_events.clear()
        app2.submit_user_message("cierra notepad").result(timeout=5.0)
        assert len(safety_events) == 1

        # Verificar escalación dinámica con typo fonético ("cierra el block de notas")
        safety_events.clear()
        app2.submit_user_message("cierra el block de notas").result(timeout=5.0)
        assert len(safety_events) == 1
        assert safety_events[0].tool_name in ("pc_close_productivity_app", "pc_fp_close_app")

        # Verificar Fast-Path en modo dry_run sin gastar tokens ni tocar la nube
        app2.settings.developer.dry_run = True
        fp_events.clear()
        app2.submit_user_message("pon el volumen al 35").result(timeout=5.0)
        assert len(fp_events) >= 1

        # Verificar que las herramientas críticas tienen RiskLevel.CRITICAL
        critical_tools = {t.name for t in pc_mod2.get_tools() if t.risk_level == RiskLevel.CRITICAL}
        assert {"pc_empty_recycle_bin", "pc_power_action", "pc_close_productivity_app"} <= critical_tools
    finally:
        elapsed2 = app2.shutdown()
        assert elapsed2 < 3.0


def test_timer_earcon_wav_is_audible_and_long_enough(tmp_path: Path) -> None:
    paths = resolve_app_paths(user_data_override=tmp_path)
    player = EarconPlayer(paths, muted=True)

    timer_wav = player.get_wav_bytes("timer")
    confirm_wav = player.get_wav_bytes("confirm")

    def _stats(raw: bytes) -> tuple[float, int]:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            duration = wf.getnframes() / float(wf.getframerate())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        peak = max((abs(s) for s in samples), default=0)
        return duration, peak

    timer_dur, timer_peak = _stats(timer_wav)
    _confirm_dur, confirm_peak = _stats(confirm_wav)
    assert timer_dur >= 1.0
    assert timer_peak > confirm_peak
    assert timer_peak >= 20_000


def test_smart_close_handles_access_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    import psutil

    from deusex.modules.pc.apps import CloseCategory, CloseDecision, close_application

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.name.return_value = "elevated_service.exe"
    mock_proc.is_running.return_value = True
    mock_proc.terminate.side_effect = psutil.AccessDenied(pid=9999)

    monkeypatch.setattr(
        "deusex.modules.pc.apps.classify_close_target",
        lambda query, **kwargs: CloseDecision(
            category=CloseCategory.DIRECT_SAFE,
            query=query,
            matched_processes=("elevated_service.exe",),
            matched_pids=(9999,),
            reason="Mock safe",
        ),
    )
    monkeypatch.setattr("psutil.Process", lambda pid: mock_proc)
    monkeypatch.setattr("deusex.modules.pc.apps._safe_wait_procs", lambda procs, timeout=1.2: ([], procs))

    ok, msg, _dec = close_application("elevated_service")
    assert ok is False
    assert "denegó el permiso" in msg
