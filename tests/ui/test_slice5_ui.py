from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtWidgets import QPushButton
import pytest

from deusex.app import DeusExApplication
from deusex.brain.providers import (
    PROVIDER_METADATA,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelInfo,
)
from deusex.core.config import SecretsManager
from deusex.core.events import ChatMessageEvent
from deusex.modules.vision.capture import (
    MonitorInfo,
    MonitorSelector,
    RawGrabResult,
    ScreenCaptureService,
)
from deusex.modules.vision.live_guide import LiveGuideController
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import ChatBubble
from deusex.ui.main_window.chat_view import ChatView
from deusex.ui.minichat.window import MiniChatWindow


class _DummyVisionProvider(LLMProvider):
    def __init__(self, secrets: SecretsManager) -> None:
        super().__init__(PROVIDER_METADATA["gemini"], secrets)

    def is_configured(self) -> bool:
        return True

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                id="gemini-ui-flash",
                provider_id="gemini",
                display_name="Gemini UI Flash",
                supports_vision=True,
            )
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="Captura analizada correctamente en la UI.",
            provider_id="gemini",
            model=request.model,
            sources=({"title": "Open-Meteo", "url": "https://open-meteo.com/"},),
        )


def _install_mock_capture(app: DeusExApplication, counter: list[int]) -> None:
    def _monitors() -> list[MonitorInfo]:
        return [
            MonitorInfo(index=0, left=0, top=0, width=1920, height=1080, is_combined=True),
            MonitorInfo(index=1, left=0, top=0, width=1920, height=1080, is_primary=True),
        ]

    def _grab(_sel: MonitorSelector) -> RawGrabResult:
        counter[0] += 1
        img = Image.new("RGB", (640, 360), color=(40, 120, 180))
        return img.tobytes(), 640, 360, 1

    svc = ScreenCaptureService(monitor_lister=_monitors, screen_grabber=_grab)
    app.vision_module.capture_service = svc
    app.vision_module.live_guide = LiveGuideController(svc, events=app.events)


@pytest.mark.ui
def test_minichat_vision_controls_and_live_guide_cycle(
    qapp: Any,
    qtbot: Any,
    tmp_path: Path,
) -> None:
    bootstrap = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    fake_provider = _DummyVisionProvider(bootstrap.secrets)
    bootstrap.shutdown()

    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={"gemini": fake_provider},
    )
    app.start()
    bridge = QtEventBridge(app.events)
    mini = MiniChatWindow(app, bridge)
    qtbot.addWidget(mini)

    try:
        # Con vision desactivado: botones ocultos
        assert mini.capture_btn.isHidden() is True
        assert mini.live_guide_btn.isHidden() is True
        assert mini.live_guide_stop_btn.isHidden() is True

        # Activar módulo vision
        assert app.set_module_enabled("vision", True).result(timeout=5.0) is True
        calls = [0]
        _install_mock_capture(app, calls)
        qapp.processEvents()

        assert mini.capture_btn.isHidden() is False
        assert mini.live_guide_btn.isHidden() is False
        assert mini.live_guide_stop_btn.isHidden() is True
        assert mini.live_guide_btn.text() == "Guía"

        # Ciclo Guía Live desde UI: off -> active -> paused -> active -> off
        mini.live_guide_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "active"
        assert mini.live_guide_btn.text() == "Guía: ON"
        assert mini.live_guide_stop_btn.isHidden() is False

        mini.live_guide_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "paused"
        assert mini.live_guide_btn.text() == "Guía: Pausa"
        assert calls[0] == 0  # 0 capturas al iniciar/pausar sin turnos

        mini.live_guide_stop_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "off"
        assert mini.live_guide_stop_btn.isHidden() is True

        # Modo Privacidad deshabilita captura rápida y Guía Live
        app.set_privacy_mode(True)
        qapp.processEvents()
        assert mini.capture_btn.isEnabled() is False
        assert mini.live_guide_btn.isEnabled() is False

        app.set_privacy_mode(False)
        qapp.processEvents()
        assert mini.capture_btn.isEnabled() is True

        assert mini.width() >= 450
        assert mini.minimumWidth() >= 420
        assert mini.mute_btn.width() >= 36
        assert mini.capture_btn.width() >= 36
        assert mini.live_guide_stop_btn.width() >= 36

        # Captura rápida desde MiniChat (sin doble burbuja)
        mini.capture_btn.click()
        qtbot.waitUntil(lambda: not mini._busy, timeout=5000)
        assert calls[0] == 1
        mini_user_bubbles = [
            b for b in mini.findChildren(ChatBubble) if b.property("role") == "user"
        ]
        assert len(mini_user_bubbles) == 1
        assert "📷 Analizar pantalla" in mini_user_bubbles[0].content_label.text()
    finally:
        mini.close()
        mini.deleteLater()
        bridge.detach()
        bridge.deleteLater()
        qapp.processEvents()
        app.shutdown()


@pytest.mark.ui
def test_chat_view_vision_buttons_and_clickable_sources(
    qapp: Any,
    qtbot: Any,
    tmp_path: Path,
) -> None:
    bootstrap = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    fake_provider = _DummyVisionProvider(bootstrap.secrets)
    bootstrap.shutdown()

    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={"gemini": fake_provider},
    )
    app.start()
    bridge = QtEventBridge(app.events)
    view = ChatView(app, bridge)
    qtbot.addWidget(view)

    try:
        assert view.capture_btn.isHidden() is True
        assert view.live_guide_btn.isHidden() is True
        assert view.live_guide_pill.isHidden() is True

        assert app.set_module_enabled("vision", True).result(timeout=5.0) is True
        calls = [0]
        _install_mock_capture(app, calls)
        qapp.processEvents()

        assert view.capture_btn.isHidden() is False
        assert view.live_guide_btn.isHidden() is False
        assert view.capture_btn.width() >= 40
        assert view.capture_btn.height() >= 40
        assert view.live_guide_stop_btn.width() >= 40

        # Activar -> pausar -> detener Guía Live desde ChatView
        view.live_guide_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "active"
        assert view.live_guide_pill.isHidden() is False
        assert "Activa" in view.live_guide_pill.text()

        view.live_guide_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "paused"
        assert "Pausa" in view.live_guide_pill.text()

        view.live_guide_stop_btn.click()
        qapp.processEvents()
        assert app.get_live_guide_state() == "off"
        assert view.live_guide_pill.isHidden() is True

        # Captura rápida en ChatView debe mostrar una sola burbuja de usuario (sin duplicar)
        view.capture_btn.click()
        qtbot.waitUntil(lambda: not view._busy, timeout=5000)
        assert calls[0] == 1
        user_bubbles = [
            b for b in view.findChildren(ChatBubble) if b.property("role") == "user"
        ]
        assert len(user_bubbles) == 1
        assert "📷 Analizar pantalla" in user_bubbles[0].content_label.text()

        # Burbuja con fuentes clickeables (Intel Fast-Path / búsqueda)
        app.events.publish(
            ChatMessageEvent(
                conversation_id=view.conversation_id,
                message_id="msg_sources_test",
                role="assistant",
                content="Clima en Concepción: 18 °C.",
                model_used="fast-path",
                fast_path=True,
                sources=({"title": "Open-Meteo (CC BY 4.0)", "url": "https://open-meteo.com/"},),
            )
        )
        qapp.processEvents()

        buttons = view.findChildren(QPushButton)
        has_link = any(
            "https://open-meteo.com/" in btn.toolTip() or "Open-Meteo" in btn.text()
            for btn in buttons
        )
        assert has_link is True
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        bridge.deleteLater()
        qapp.processEvents()
        app.shutdown()
