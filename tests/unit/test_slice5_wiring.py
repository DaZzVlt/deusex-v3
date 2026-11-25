from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from deusex.app import DeusExApplication
from deusex.brain.providers import (
    PROVIDER_METADATA,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelInfo,
    ModelTier,
    ProviderError,
)
from deusex.core.config import SecretsManager
from deusex.core.contracts import ModuleState
from deusex.core.events import (
    LiveGuideStateChangedEvent,
    ResearchExportRequestedEvent,
    ScreenCapturedEvent,
    VisionShareRequestedEvent,
)
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.modules.vision.capture import (
    MonitorInfo,
    MonitorSelector,
    RawGrabResult,
    ScreenCaptureService,
)
from deusex.modules.vision.live_guide import LiveGuideController, LiveGuideState
from deusex.ui.bridge_qt import QtEventBridge


class _FakeVisionProvider(LLMProvider):
    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(PROVIDER_METADATA["gemini"], secrets)
        self.received_requests: list[LLMRequest] = []

    def is_configured(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="gemini-fake-flash",
                provider_id="gemini",
                display_name="Gemini Fake Flash",
                supports_vision=True,
                supports_audio=True,
                supports_tools=True,
                supports_web_search=True,
            )
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.received_requests.append(request)
        att_count = sum(len(m.attachments) for m in request.messages)
        text = (
            f"Analicé {att_count} imagen(es) adjunta(s) de tu pantalla."
            if att_count > 0
            else "Respuesta de texto sin imagen."
        )
        return LLMResponse(
            text=text,
            provider_id="gemini",
            model=request.model,
            tokens_in=20,
            tokens_out=15,
        )


def _install_fake_capture(app: DeusExApplication, counter: list[int]) -> None:
    def _monitors() -> list[MonitorInfo]:
        return [
            MonitorInfo(index=0, left=0, top=0, width=1920, height=1080, is_combined=True),
            MonitorInfo(index=1, left=0, top=0, width=1920, height=1080, is_primary=True),
        ]

    def _grab(_sel: MonitorSelector) -> RawGrabResult:
        counter[0] += 1
        img = Image.new("RGB", (1280, 720), color=(25, 90, 160))
        return img.tobytes(), 1280, 720, 1

    fake_service = ScreenCaptureService(monitor_lister=_monitors, screen_grabber=_grab)
    app.vision_module.capture_service = fake_service
    app.vision_module.live_guide = LiveGuideController(fake_service, events=app.events)


def test_slice5_reconciliation_live_guide_turns_and_fast_shutdown(tmp_path: Path) -> None:
    # 1) Primer arranque: activar intel y vision y cerrar
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app1.start()
    try:
        assert app1.intel_module.state == ModuleState.DISABLED
        assert app1.vision_module.state == ModuleState.DISABLED
        assert app1.set_module_enabled("intel", True).result(timeout=5.0) is True
        assert app1.set_module_enabled("vision", True).result(timeout=5.0) is True
        assert app1.intel_module.state == ModuleState.ACTIVE  # type: ignore[comparison-overlap]
        assert app1.vision_module.state == ModuleState.ACTIVE
    finally:
        elapsed1 = app1.shutdown()
        assert elapsed1 < 3.0

    # 2) Segundo arranque (Antídoto #2): ambos módulos deben reconciliarse automáticamente
    fake_paths = resolve_app_paths(user_data_override=tmp_path)
    fake_secrets = SecretsManager(fake_paths, fallback_to_os_env=False)
    fake_provider = _FakeVisionProvider(fake_secrets)

    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={"gemini": fake_provider},
    )

    reconciled = app2.start()
    capture_calls = [0]
    _install_fake_capture(app2, capture_calls)

    try:
        assert "intel" in reconciled
        assert "vision" in reconciled
        assert app2.intel_module.state == ModuleState.ACTIVE
        assert app2.vision_module.state == ModuleState.ACTIVE
        assert app2.settings.is_permission_enabled(Permission.INTEL_WEB)
        assert app2.settings.is_permission_enabled(Permission.VISION_CAPTURE)

        # Guía Live en OFF: turno conversacional -> 0 capturas
        turn_off = app2.submit_user_message("Hola, ¿cómo estás?").result(timeout=5.0)
        assert capture_calls[0] == 0
        assert "sin imagen" in turn_off.reply_text

        # Iniciar Guía Live -> todavía 0 capturas
        snap_active = app2.start_live_guide()
        assert snap_active.state == LiveGuideState.ACTIVE
        assert capture_calls[0] == 0

        # Turno conversacional en ACTIVE -> exactamente 1 captura adjunta al LLM
        turn_live = app2.submit_user_message("¿Qué botón debo presionar aquí?").result(timeout=5.0)
        assert capture_calls[0] == 1
        assert "1 imagen" in turn_live.reply_text

        # Orden Fast-Path "pausa guía live" -> 0 capturas nuevas y pasa a PAUSED
        turn_pause = app2.submit_user_message("pausa guía live").result(timeout=5.0)
        assert turn_pause.fast_path is True
        assert capture_calls[0] == 1
        assert app2.get_live_guide_state() == "paused"

        # Turno conversacional en PAUSED -> 0 capturas (0 tokens de imagen)
        turn_paused = app2.submit_user_message("Cuéntame un chiste corto").result(timeout=5.0)
        assert capture_calls[0] == 1
        assert "sin imagen" in turn_paused.reply_text

        # Botón de captura puntual manual desde UI
        turn_snap = app2.capture_screen_and_analyze("Resume esta ventana").result(timeout=5.0)
        assert capture_calls[0] == 2
        assert "1 imagen" in turn_snap.reply_text

        # Fast-Path de Intel propaga sources clickeables
        turn_time = app2.submit_user_message("qué hora es en tokio").result(timeout=5.0)
        assert turn_time.fast_path is True
        assert "Tokio" in turn_time.reply_text
    finally:
        elapsed2 = app2.shutdown()
        assert elapsed2 < 3.0
        assert app2.get_live_guide_state() == "off"


@pytest.mark.ui
def test_qt_bridge_forwards_slice5_events(qapp: Any, tmp_path: Path) -> None:
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    bridge = QtEventBridge(app.events)
    received: dict[str, list[object]] = {
        "export": [],
        "capture": [],
        "live_guide": [],
        "share": [],
    }
    bridge.research_export_requested.connect(received["export"].append)
    bridge.screen_captured.connect(received["capture"].append)
    bridge.live_guide_state_changed.connect(received["live_guide"].append)
    bridge.vision_share_requested.connect(received["share"].append)

    try:
        app.events.publish(ResearchExportRequestedEvent(export_id="e1", title="Doc"))
        app.events.publish(ScreenCapturedEvent(capture_id="c1", monitor_index=1, width=1280, height=720))
        app.events.publish(LiveGuideStateChangedEvent(state="active", session_id="s1"))
        app.events.publish(VisionShareRequestedEvent(share_id="sh1", target="mobile"))
        qapp.processEvents()

        assert len(received["export"]) == 1
        assert len(received["capture"]) == 1
        assert len(received["live_guide"]) == 1
        assert len(received["share"]) == 1
    finally:
        bridge.detach()
        bridge.deleteLater()
        qapp.processEvents()


class _Fail400Provider(LLMProvider):
    def __init__(self, provider_id: str, secrets: SecretsManager) -> None:
        super().__init__(PROVIDER_METADATA[provider_id], secrets)
        self.received_requests: list[LLMRequest] = []

    def is_configured(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="gemini-3.8-flash",
                provider_id=self.provider_id,
                display_name="Gemini 3.8 Flash",
                supports_vision=True,
            )
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.received_requests.append(request)
        raise ProviderError(
            "400 INVALID_ARGUMENT",
            provider_id=self.provider_id,
            status_code=400,
            retryable=False,
        )


class _ConfigurableTextOrVisionProvider(LLMProvider):
    def __init__(
        self,
        provider_id: str,
        secrets: SecretsManager,
        models: list[ModelInfo],
    ) -> None:
        super().__init__(PROVIDER_METADATA[provider_id], secrets)
        self._models = models
        self.received_requests: list[LLMRequest] = []

    def is_configured(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        return list(self._models)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.received_requests.append(request)
        att_count = sum(len(m.attachments) for m in request.messages)
        return LLMResponse(
            text=f"Respondido por {self.provider_id}:{request.model} ({att_count} img)",
            provider_id=self.provider_id,
            model=request.model,
            tokens_in=15,
            tokens_out=10,
        )


def test_live_guide_long_history_and_vision_provider_fallback(tmp_path: Path) -> None:
    bootstrap = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    secrets = bootstrap.secrets
    bootstrap.shutdown()

    fake_gemini = _FakeVisionProvider(secrets)
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={"gemini": fake_gemini},
    )
    app.start()
    calls = [0]
    _install_fake_capture(app, calls)

    try:
        assert app.set_module_enabled("vision", True).result(timeout=5.0) is True
        conv = app.memory.create_conversation(title="Hilo largo")
        # Inserta >16 mensajes previos (18 mensajes alternados user/assistant)
        for idx in range(9):
            app.memory.append_message(
                conversation_id=conv.id,
                role="user",
                content=f"Pregunta previa #{idx}",
            )
            app.memory.append_message(
                conversation_id=conv.id,
                role="assistant",
                content=f"Respuesta previa #{idx}",
            )

        app.start_live_guide()
        turn = app.submit_user_message(
            "¿Qué ventana ves ahora?",
            conversation_id=conv.id,
        ).result(timeout=5.0)

        assert turn.fast_path is False
        assert calls[0] == 1
        assert len(fake_gemini.received_requests) == 1
        req = fake_gemini.received_requests[0]
        assert req.messages[-1].role == "user"
        assert "¿Qué ventana ves ahora?" in req.messages[-1].content
        assert len(req.messages[-1].attachments) == 1
        assert req.messages[-1].attachments[0].mime_type.startswith("image/")
    finally:
        assert app.shutdown() < 3.0

    # Verifica que con imagen adjunta prioriza openrouter sobre modelos de solo texto en groq
    dir_fb = tmp_path / "fallback"
    boot_fb = DeusExApplication(
        user_data_dir=dir_fb,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    sec_fb = boot_fb.secrets
    boot_fb.shutdown()

    failing_gemini = _Fail400Provider("gemini", sec_fb)
    groq_text_only = _ConfigurableTextOrVisionProvider(
        "groq",
        sec_fb,
        [
            ModelInfo(
                id="qwen/qwen3.8-27b",
                provider_id="groq",
                display_name="Qwen 3.8 27B",
                supports_vision=True,
            ),
            ModelInfo(
                id="llama-3.3-70b-versatile",
                provider_id="groq",
                display_name="Llama 3.3 70B",
                supports_vision=False,
            ),
        ],
    )
    openrouter_vision = _ConfigurableTextOrVisionProvider(
        "openrouter",
        sec_fb,
        [
            ModelInfo(
                id="google/gemini-3.8-flash",
                provider_id="openrouter",
                display_name="Gemini 3.8 Flash",
                supports_vision=True,
            ),
        ],
    )
    app_fb = DeusExApplication(
        user_data_dir=dir_fb,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={
            "gemini": failing_gemini,
            "groq": groq_text_only,
            "openrouter": openrouter_vision,
        },
    )
    app_fb.start()
    calls_fb = [0]
    _install_fake_capture(app_fb, calls_fb)
    try:
        app_fb.refresh_models("groq", force=True).result(timeout=5.0)
        app_fb.refresh_models("openrouter", force=True).result(timeout=5.0)
        assert app_fb.set_module_enabled("vision", True).result(timeout=5.0) is True
        app_fb.start_live_guide()

        order = app_fb.orchestrator._pick_provider_order(
            require_vision=True,
            tier=ModelTier.DEEP,
        )
        assert order.index("openrouter") < order.index("groq")

        turn_fb = app_fb.submit_user_message("¿Qué botón debo presionar en esta ventana?").result(timeout=5.0)
        assert turn_fb.fast_path is False
        assert "openrouter:" in turn_fb.model_used
        assert len(groq_text_only.received_requests) == 0
        assert len(openrouter_vision.received_requests) == 1
        fb_req = openrouter_vision.received_requests[0]
        assert fb_req.messages[-1].role == "user"
        assert "¿Qué botón debo presionar en esta ventana?" in fb_req.messages[-1].content
        assert len(fb_req.messages[-1].attachments) == 1
    finally:
        assert app_fb.shutdown() < 3.0
