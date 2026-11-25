from __future__ import annotations

from pathlib import Path
import socket
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import httpx
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
import pytest
from pytestqt.qtbot import QtBot

from deusex.app import DeusExApplication
from deusex.core.events import OAuthCompletedEvent
from deusex.modules.workspace import GOOGLE_SECRET_NAME, GoogleOAuthTokens
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.integrations_view import IntegrationsView
from deusex.ui.onboarding import OnboardingDialog


def _find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.ui
def test_integrations_view_setup_sync_and_google_oauth_ui(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DeusExApplication(
        user_data_dir=tmp_path / "user_data",
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    free_port = _find_free_loopback_port()
    app.settings.developer.oauth_callback_port = free_port
    app.start()

    opened_urls: list[str] = []

    def _fake_open(url: str, *_args: object, **_kwargs: object) -> bool:
        opened_urls.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", _fake_open)

    bridge = QtEventBridge(app.events)
    view = IntegrationsView(app, bridge)
    qtbot.addWidget(view)
    view.show()
    qapp.processEvents()

    try:
        setup_sync_btn = view.findChild(QPushButton, "SetupSyncButton")
        open_portal_btn = view.findChild(QPushButton, "OpenPortalButton")
        sync_pill = view.findChild(QLabel, "SetupSyncStatusPill")
        sync_feedback = view.findChild(QLabel, "SetupSyncFeedbackLabel")
        google_btn = view.findChild(QPushButton, "GoogleOAuthButton")
        google_disconnect_btn = view.findChild(QPushButton, "GoogleDisconnectButton")
        google_feedback = view.findChild(QLabel, "GoogleFeedbackLabel")

        assert setup_sync_btn is not None
        assert open_portal_btn is not None
        assert sync_pill is not None
        assert sync_feedback is not None
        assert google_btn is not None
        assert google_disconnect_btn is not None
        assert google_feedback is not None

        # 1. Abrir Portal Web usa URL base oficial
        open_portal_btn.click()
        assert len(opened_urls) == 1
        assert opened_urls[0].endswith("/index.html")

        # 2. Abrir Setup Sync levanta el servidor local 127.0.0.1 y genera token efímero en fragmento
        setup_sync_btn.click()
        qtbot.waitUntil(lambda: app.oauth_server.is_running and len(opened_urls) == 2, timeout=4000)
        qtbot.waitUntil(lambda: f"127.0.0.1:{free_port}" in sync_pill.text(), timeout=4000)

        sync_url = opened_urls[1]
        frag_params = parse_qs(urlsplit(sync_url).fragment)
        sync_token = frag_params["sync_token"][0]
        assert frag_params["port"][0] == str(free_port)

        # 3. POST real local hacia /setup-sync guarda claves en .env y actualiza UI vía EventBus -> QtEventBridge
        resp = httpx.post(
            f"http://127.0.0.1:{free_port}/setup-sync",
            headers={
                "Origin": "https://dazzvlt.github.io",
                "X-DeusEx-Sync-Token": sync_token,
            },
            json={
                "secrets": {
                    "GEMINI_API_KEY": "AIzaSyUiTestKey1234567890",
                    "GOOGLE_OAUTH_CLIENT_ID": "ui-test-client.apps.googleusercontent.com",
                },
                "settings": {"general.user_name": "Usuario UI"},
            },
            timeout=3.0,
        )
        assert resp.status_code == 200
        assert app.secrets.get("GEMINI_API_KEY") == "AIzaSyUiTestKey1234567890"
        assert app.settings.general.user_name == "Usuario UI"
        qtbot.waitUntil(lambda: "Setup Sync aplicado" in sync_feedback.text(), timeout=4000)

        # 4. Evento de Google conectado actualiza controles de UI
        tokens = GoogleOAuthTokens(
            access_token="ya29.ui-token",
            refresh_token="1//ui-refresh",
            email="ui-user@example.com",
            name="Usuario UI",
            expires_at=time.time() + 3600.0,
        )
        app.secrets.save_secret_json(GOOGLE_SECRET_NAME, tokens.to_secret_dict())
        app.events.publish(
            OAuthCompletedEvent(
                provider="google",
                connected=True,
                account_email="ui-user@example.com",
                account_name="Usuario UI",
                message="Cuenta de Google conectada (ui-user@example.com).",
            )
        )
        qtbot.waitUntil(lambda: google_disconnect_btn.isVisible(), timeout=4000)
        assert google_btn.text() == "Reconectar Google"
        assert "ui-user@example.com" in google_feedback.text()

        # 5. Desconexión en 1 clic elimina el secreto y oculta botón Desconectar
        google_disconnect_btn.click()
        qtbot.waitUntil(lambda: not app.google_auth.is_connected(), timeout=4000)
        qtbot.waitUntil(lambda: not google_disconnect_btn.isVisible(), timeout=4000)
        assert google_btn.text() == "Iniciar sesión con Google"
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        qapp.processEvents()
        elapsed = app.shutdown()
        assert elapsed < 3.0
        assert not app.oauth_server.is_running


@pytest.mark.ui
def test_onboarding_dialog_setup_sync_and_oauth_controls(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = DeusExApplication(
        user_data_dir=tmp_path / "user_data",
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    free_port = _find_free_loopback_port()
    app.settings.developer.oauth_callback_port = free_port
    app.start()

    opened_urls: list[str] = []

    def _fake_open(url: str, *_args: object, **_kwargs: object) -> bool:
        opened_urls.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", _fake_open)

    dialog = OnboardingDialog(app)
    qtbot.addWidget(dialog)
    dialog.show()
    qapp.processEvents()

    try:
        # Paso 1 -> Paso 2 (Setup Sync)
        dialog.next_btn.click()
        qapp.processEvents()
        dialog.setup_sync_btn.click()
        qtbot.waitUntil(lambda: app.oauth_server.is_running and len(opened_urls) == 1, timeout=4000)
        assert "guides.html#" in opened_urls[0]
        assert "sync_token=" in opened_urls[0]

        # Paso 2 -> Paso 3 (Portal Web + Google OAuth)
        dialog.next_btn.click()
        qapp.processEvents()
        dialog.portal_btn.click()
        assert len(opened_urls) == 2
        assert opened_urls[1].endswith("/index.html")

        # Simula cuenta Google conectada y valida indicador
        tokens = GoogleOAuthTokens(
            access_token="ya29.onboarding-token",
            refresh_token="1//onboarding-refresh",
            email="onboard@example.com",
            expires_at=time.time() + 3600.0,
        )
        app.secrets.save_secret_json(GOOGLE_SECRET_NAME, tokens.to_secret_dict())
        dialog._refresh_oauth_status()
        assert "Conectado (onboard@example.com)" in dialog.google_status_lbl.text()
        assert dialog.google_oauth_btn.text() == "Reconectar Google"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()
        elapsed = app.shutdown()
        assert elapsed < 3.0
        assert not app.oauth_server.is_running
