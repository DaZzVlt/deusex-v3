from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import Image
import pytest

from deusex.brain.providers import MediaAttachment
from deusex.core.config import DeusExSettings, SecretsManager
from deusex.core.contracts import DiagnosticSeverity, ModuleContext, ModuleState
from deusex.core.db import Database
from deusex.core.engine import CoreEngine
from deusex.core.events import (
    EventBus,
    LiveGuideStateChangedEvent,
    PrivacyModeChangedEvent,
    ScreenCapturedEvent,
    VisionShareRequestedEvent,
)
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import RuntimeServices
from deusex.modules.vision.capture import (
    MonitorInfo,
    MonitorSelector,
    RawGrabResult,
    ScreenCaptureService,
    ScreenGrabber,
    resolve_active_monitor_index,
)
from deusex.modules.vision.live_guide import LiveGuideController, LiveGuideState
from deusex.modules.vision.module import VisionModule


def _fake_monitors() -> list[MonitorInfo]:
    return [
        MonitorInfo(index=0, left=0, top=0, width=3840, height=1080, is_combined=True),
        MonitorInfo(index=1, left=0, top=0, width=1920, height=1080, is_primary=True),
        MonitorInfo(index=2, left=1920, top=0, width=1920, height=1080, is_primary=False),
    ]


def _make_fake_grabber(call_counter: list[int]) -> tuple[list[MonitorSelector], ScreenGrabber]:
    requested: list[MonitorSelector] = []

    def _grab(selector: MonitorSelector) -> RawGrabResult:
        call_counter[0] += 1
        requested.append(selector)
        if selector == "all" or selector == 0:
            w, h, idx = 2560, 720, 0
            color = (30, 80, 140)
        elif selector == 2:
            w, h, idx = 1920, 1080, 2
            color = (40, 160, 90)
        else:
            w, h, idx = 1920, 1080, 1
            color = (120, 50, 180)
        img = Image.new("RGB", (w, h), color=color)
        return img.tobytes(), w, h, idx

    return requested, _grab


def test_resolve_active_monitor_index_multi_monitor() -> None:
    monitors = _fake_monitors()
    assert resolve_active_monitor_index(monitors, (500, 300)) == 1
    assert resolve_active_monitor_index(monitors, (2400, 400)) == 2
    assert resolve_active_monitor_index(monitors, (-50, -50)) == 1


@pytest.mark.asyncio
async def test_capture_compression_multi_monitor_and_privacy() -> None:
    calls = [0]
    requested, grabber = _make_fake_grabber(calls)
    assert callable(grabber)

    service = ScreenCaptureService(
        monitor_lister=_fake_monitors,
        screen_grabber=grabber,
        default_max_edge=1280,
        default_jpeg_quality=80,
    )

    frame = await service.capture(monitor="active", privacy_mode=False)
    assert calls[0] == 1
    assert frame.width == 1280
    assert frame.height == 720
    assert frame.mime_type == "image/jpeg"
    assert frame.size_bytes > 0

    att = frame.to_media_attachment()
    assert isinstance(att, MediaAttachment)
    assert att.mime_type == "image/jpeg"
    assert att.data == frame.image_bytes

    frame_all = await service.capture(monitor="all", privacy_mode=False)
    assert frame_all.monitor_index == 0
    assert frame_all.width == 1280
    assert requested == ["active", "all"]

    # Bloqueo estricto en Modo Privacidad (0 capturas nuevas)
    with pytest.raises(PermissionError):
        await service.capture(monitor=1, privacy_mode=True)
    assert calls[0] == 2

    await service.close()
    assert service.last_capture is None


@pytest.mark.asyncio
async def test_live_guide_zero_captures_in_off_and_paused() -> None:
    calls = [0]
    _, grabber = _make_fake_grabber(calls)
    assert callable(grabber)

    events = EventBus()
    state_events: list[LiveGuideStateChangedEvent] = []
    events.subscribe(LiveGuideStateChangedEvent, lambda ev: state_events.append(ev))

    service = ScreenCaptureService(monitor_lister=_fake_monitors, screen_grabber=grabber)
    controller = LiveGuideController(service, events=events)

    # En OFF: 0 capturas
    assert controller.snapshot().state == LiveGuideState.OFF
    assert await controller.capture_for_turn(privacy_mode=False) is None
    assert calls[0] == 0

    # Iniciar Guía Live: todavía 0 capturas hasta que llegue un turno real
    snap_active = controller.start(monitor=2, privacy_mode=False)
    assert snap_active.state == LiveGuideState.ACTIVE
    assert calls[0] == 0

    # Turno 1 en ACTIVE -> exactamente 1 captura
    f1 = await controller.capture_for_turn(privacy_mode=False)
    assert f1 is not None
    assert f1.monitor_index == 2
    assert calls[0] == 1
    assert controller.snapshot().turns_count == 1
    assert controller.snapshot().captures_count == 1

    # Pausar -> 0 capturas adicionales aunque lleguen turnos
    snap_paused = controller.pause()
    assert snap_paused.state == LiveGuideState.PAUSED
    assert await controller.capture_for_turn(privacy_mode=False) is None
    assert await controller.capture_for_turn(privacy_mode=False) is None
    assert calls[0] == 1

    # Reanudar -> vuelve a capturar solo bajo demanda
    assert controller.resume(privacy_mode=False).state == LiveGuideState.ACTIVE
    f2 = await controller.capture_for_turn(privacy_mode=False)
    assert f2 is not None
    assert calls[0] == 2

    # Privacidad -> pausa automática y 0 capturas
    assert controller.handle_privacy_changed(True) is True
    assert controller.snapshot().state == LiveGuideState.PAUSED
    assert await controller.capture_for_turn(privacy_mode=True) is None
    assert calls[0] == 2

    # Detener -> limpia estado y 0 capturas
    snap_off = controller.stop()
    assert snap_off.state == LiveGuideState.OFF
    assert await controller.capture_for_turn(privacy_mode=False) is None
    assert calls[0] == 2
    assert len(state_events) >= 5


@pytest.mark.asyncio
async def test_vision_module_fast_path_privacy_share_and_diagnostics(tmp_path: Path) -> None:
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    settings = DeusExSettings()
    settings.modules.set_enabled("vision", True)
    settings.set_permission(Permission.VISION_CAPTURE, True)
    settings.set_permission(Permission.VISION_SHARE, True)

    secrets = SecretsManager(paths, fallback_to_os_env=False)
    events = EventBus()
    events.bind_loop(asyncio.get_running_loop())
    db = Database(paths.db_file)
    db.open()
    runtime = RuntimeServices(events)

    calls = [0]
    _, grabber = _make_fake_grabber(calls)
    assert callable(grabber)

    analyzed_calls: list[tuple[str, str, int]] = []

    async def _fake_analyzer(prompt: str, att: MediaAttachment, mode: str) -> str:
        analyzed_calls.append((prompt, mode, len(att.data)))
        return "Veo el editor de código con pruebas en verde."

    captured_events: list[ScreenCapturedEvent] = []
    shared_events: list[VisionShareRequestedEvent] = []
    events.subscribe(ScreenCapturedEvent, lambda ev: captured_events.append(ev))
    events.subscribe(VisionShareRequestedEvent, lambda ev: shared_events.append(ev))

    ctx = ModuleContext(
        paths=paths,
        settings=settings,
        secrets=secrets,
        db=db,
        events=events,
        runtime=runtime,
    )
    engine = CoreEngine(ctx)
    module = VisionModule(
        image_analyzer=_fake_analyzer,
        monitor_lister=_fake_monitors,
        screen_grabber=grabber,
    )
    engine.modules.register(module)

    try:
        assert await engine.modules.enable_module("vision") is True
        assert module.state == ModuleState.ACTIVE

        # Fast-Path: "qué ves en mi pantalla"
        res_desc = await engine.fast_path.try_execute("qué ves en mi pantalla")
        assert res_desc is not None and res_desc.ok is True
        assert "editor de código" in res_desc.summary
        assert len(analyzed_calls) == 1
        assert calls[0] == 1

        # Fast-Path: "toma una captura"
        res_cap = await engine.fast_path.try_execute("toma una captura del monitor 2")
        assert res_cap is not None and res_cap.ok is True
        assert res_cap.data["monitor_index"] == 2
        assert calls[0] == 2

        # Contrato VisionShareRequestedEvent (Permission.VISION_SHARE)
        res_share = await engine.tools.execute(
            "vision_share_capture",
            {"target": "discord", "recipient": "#general", "caption": "Mira esto"},
        )
        assert res_share.ok is True
        assert len(shared_events) == 1
        assert shared_events[0].target == "discord"
        assert shared_events[0].caption == "Mira esto"
        assert len(shared_events[0].image_bytes) > 0

        # Fast-Path Guía Live: iniciar -> turno -> pausar (0 capturas) -> privacidad -> detener
        res_lg_start = await engine.fast_path.try_execute("inicia guía live")
        assert res_lg_start is not None and res_lg_start.ok is True
        assert module.live_guide_snapshot().state == LiveGuideState.ACTIVE

        att_turn = await module.capture_for_live_turn()
        assert att_turn is not None
        assert calls[0] == 3

        res_lg_pause = await engine.fast_path.try_execute("pausa guía live")
        assert res_lg_pause is not None and res_lg_pause.ok is True
        assert module.live_guide_snapshot().state == LiveGuideState.PAUSED
        assert await module.capture_for_live_turn() is None
        assert calls[0] == 3

        # Reanudar y activar Modo Privacidad -> pausa automática y bloquea Fast-Path
        res_lg_resume = await engine.fast_path.try_execute("reanuda guía live")
        assert res_lg_resume is not None and res_lg_resume.ok is True
        settings.general.privacy_mode = True
        events.publish(PrivacyModeChangedEvent(enabled=True))
        assert module.live_guide_snapshot().state == LiveGuideState.PAUSED

        res_blocked = await engine.fast_path.try_execute("qué ves en mi pantalla")
        assert res_blocked is not None and res_blocked.ok is False
        assert calls[0] == 3

        res_lg_stop = await engine.fast_path.try_execute("detén guía live")
        assert res_lg_stop is not None and res_lg_stop.ok is True
        assert module.live_guide_snapshot().state == LiveGuideState.OFF

        # Diagnósticos Doctor
        diags = module.diagnose()
        assert any(d.code == "vision.deps.ok" and d.severity == DiagnosticSeverity.OK for d in diags)
        assert any(d.code == "vision.analyzer.ok" for d in diags)
        assert len(captured_events) == 3

        await engine.modules.disable_module("vision")
        assert module.live_guide_snapshot().state == LiveGuideState.OFF
    finally:
        events.unbind_loop()
        db.close()
