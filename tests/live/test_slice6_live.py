from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from PySide6.QtWidgets import QApplication, QLabel
import pytest
from pytestqt.qtbot import QtBot

from deusex.app import DeusExApplication
from deusex.core.paths import resolve_app_paths
from deusex.modules.workspace import GOOGLE_SECRET_NAME, GoogleAuthService, load_oauth_defaults
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.integrations_view import IntegrationsView


def _is_loopback_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.mark.live
def test_slice6_live_oauth_loopback_8888_setup_sync_ui_and_shutdown(
    qapp: QApplication,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    """
    Smoke test EN VIVO de Rebanada 6 en Windows 11:
      1. Inicia `OAuthCallbackServer` en el puerto oficial `127.0.0.1:8888`.
      2. Verifica `/health`, Setup Sync real con CORS de GitHub Pages y persistencia aislada en `.env`.
      3. Ejecuta callback OAuth 2.0 PKCE (`S256` + `state` anti-CSRF) real sobre `127.0.0.1:8888`,
         persiste tokens en `USER_DATA_DIR/secrets/google_oauth.json` (cero secretos en `settings.json`)
         y valida actualización en `IntegrationsView`.
      4. Cierra toda la aplicación en `< 3 s` y libera el puerto `8888`.
    """
    official_port = 8888
    if not _is_loopback_port_free(official_port):
        pytest.skip(
            "El puerto local 127.0.0.1:8888 ya está en uso (cierra cualquier instancia previa de DeusEx)."
        )

    app = DeusExApplication(
        user_data_dir=tmp_path / "live_slice6_user_data",
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.settings.developer.oauth_callback_port = official_port
    app.start()

    # Transporte controlado solo para el intercambio /token sin requerir consentimiento manual interactivo
    def _google_token_mock(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.live-slice6-access",
                    "refresh_token": "1//live-slice6-refresh",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "openid email profile",
                },
            )
        if request.url.path == "/v1/userinfo":
            return httpx.Response(
                200,
                json={"email": "live-test@example.com", "name": "Live Tester"},
            )
        return httpx.Response(404, json={"error": "not_found"})

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(_google_token_mock))
    app.google_auth = GoogleAuthService(app.paths, app.secrets, http_client=mock_http)

    bridge = QtEventBridge(app.events)
    view = IntegrationsView(app, bridge)
    qtbot.addWidget(view)
    view.show()
    qapp.processEvents()

    try:
        # 1. Preparar sesión Setup Sync sobre 127.0.0.1:8888 real
        sync_url = app.prepare_setup_sync_session(page="guides.html").result(timeout=5.0)
        assert app.oauth_server.is_running is True
        assert app.oauth_server.port == official_port

        frag = parse_qs(urlsplit(sync_url).fragment)
        sync_token = frag["sync_token"][0]
        assert frag["port"][0] == str(official_port)

        # 2. Verificar /health real sobre TCP 127.0.0.1:8888
        health_resp = httpx.get(
            f"http://127.0.0.1:{official_port}/health",
            headers={"Origin": "https://dazzvlt.github.io"},
            timeout=3.0,
        )
        assert health_resp.status_code == 200
        assert health_resp.json()["port"] == official_port
        assert health_resp.json()["setup_sync_ready"] is True

        # 3. Enviar Setup Sync real hacia 127.0.0.1:8888/setup-sync
        sync_resp = httpx.post(
            f"http://127.0.0.1:{official_port}/setup-sync",
            headers={
                "Origin": "https://dazzvlt.github.io",
                "X-DeusEx-Sync-Token": sync_token,
            },
            json={
                "secrets": {
                    "GEMINI_API_KEY": "AIzaSyLiveSmokeTestKey0001",
                    "GOOGLE_OAUTH_CLIENT_ID": "live-slice6-client.apps.googleusercontent.com",
                },
                "settings": {"general.user_name": "Live Slice6"},
            },
            timeout=3.0,
        )
        assert sync_resp.status_code == 200
        assert app.secrets.get("GEMINI_API_KEY") == "AIzaSyLiveSmokeTestKey0001"
        assert "AIzaSyLiveSmokeTestKey0001" not in app.paths.settings_file.read_text(encoding="utf-8")

        sync_feedback = view.findChild(QLabel, "SetupSyncFeedbackLabel")
        assert sync_feedback is not None
        qtbot.waitUntil(lambda: "Setup Sync aplicado" in sync_feedback.text(), timeout=4000)

        # 4. Flujo OAuth PKCE real golpeando el socket TCP http://127.0.0.1:8888/callback
        def _simulate_browser_redirect(auth_url: str) -> bool:
            query = parse_qs(urlsplit(auth_url).query)
            redirect_uri = query["redirect_uri"][0]
            state = query["state"][0]
            assert redirect_uri == f"http://127.0.0.1:{official_port}/callback"
            assert query["code_challenge_method"][0] == "S256"

            async def _deliver_cb() -> None:
                async with httpx.AsyncClient() as cb_client:
                    r = await cb_client.get(
                        redirect_uri,
                        params={"code": "live_pkce_auth_code", "state": state},
                        timeout=3.0,
                    )
                    assert r.status_code == 200

            asyncio.get_running_loop().create_task(_deliver_cb())
            return True

        session = app.start_google_oauth_flow(
            open_browser=_simulate_browser_redirect,
            timeout_sec=10.0,
        ).result(timeout=10.0)

        assert session.connected is True
        assert session.email == "live-test@example.com"
        secret_path = app.paths.secrets_dir / f"{GOOGLE_SECRET_NAME}.json"
        assert secret_path.is_file()
        assert "1//live-slice6-refresh" not in app.paths.settings_file.read_text(encoding="utf-8")

        google_feedback = view.findChild(QLabel, "GoogleFeedbackLabel")
        assert google_feedback is not None
        qtbot.waitUntil(lambda: "live-test@example.com" in google_feedback.text(), timeout=4000)
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        qapp.processEvents()

        t0 = time.perf_counter()
        elapsed = app.shutdown()
        total_wall = time.perf_counter() - t0
        asyncio.run(mock_http.aclose())

        assert elapsed < 3.0
        assert total_wall < 3.0
        assert app.oauth_server.is_running is False
        assert _is_loopback_port_free(official_port) is True


@pytest.mark.live
def test_slice6_live_github_pages_portal_deployed() -> None:
    """Verifica el portal estático publicado en GitHub Pages y sus cláusulas obligatorias."""
    repo_root = Path(__file__).resolve().parents[2]
    paths = resolve_app_paths(app_dir_override=repo_root)
    defaults = load_oauth_defaults(paths)
    base_url = defaults.portal_base_url.rstrip("/")

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        try:
            index_resp = client.get(f"{base_url}/index.html")
        except httpx.HTTPError as exc:
            pytest.skip(f"Sin conectividad hacia GitHub Pages ({base_url}): {exc}")

        if index_resp.status_code == 404:
            pytest.skip(
                f"GitHub Pages aún no está publicado en {base_url} (HTTP 404). "
                "Haz push a 'main' y habilita Settings -> Pages -> GitHub Actions, luego re-ejecuta."
            )

        assert index_resp.status_code == 200
        assert "DeusEx" in index_resp.text

        privacy_resp = client.get(f"{base_url}/privacy.html")
        assert privacy_resp.status_code == 200
        assert "Google API Services User Data Policy" in privacy_resp.text
        assert "Limited Use" in privacy_resp.text

        terms_resp = client.get(f"{base_url}/terms.html")
        assert terms_resp.status_code == 200
        assert "Open-Meteo" in terms_resp.text
        assert "CC BY 4.0" in terms_resp.text

        callback_resp = client.get(f"{base_url}/callback.html")
        assert callback_resp.status_code == 200
        assert "FORBIDDEN_REDIRECT_KEYS" in callback_resp.text
