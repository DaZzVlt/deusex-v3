from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from deusex.core.config import SecretsManager, SettingsStore
from deusex.core.paths import AppPaths, resolve_app_paths
from deusex.modules.workspace import (
    GOOGLE_SECRET_NAME,
    GoogleAuthError,
    GoogleAuthService,
    OAuthCallbackResult,
    OAuthCallbackServer,
    SetupSyncPayload,
    build_pkce_challenge,
    generate_pkce_pair,
    load_oauth_defaults,
)


def _make_isolated_paths(tmp_path: Path) -> AppPaths:
    repo_root = Path(__file__).resolve().parents[2]
    return resolve_app_paths(user_data_override=tmp_path / "user_data", app_dir_override=repo_root)


def test_load_oauth_defaults_and_env_override(tmp_path: Path) -> None:
    paths = _make_isolated_paths(tmp_path)
    secrets_mgr = SecretsManager(paths, fallback_to_os_env=False)

    defaults = load_oauth_defaults(paths, secrets_mgr)
    assert defaults.oauth_callback_port == 8888
    assert defaults.spotify.format_redirect_uri(8888) == "http://127.0.0.1:8888/callback"
    assert defaults.google.format_redirect_uri(8888) == "http://127.0.0.1:8888/callback"
    assert "openid" in defaults.google.scopes

    secrets_mgr.set("GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id.apps.googleusercontent.com")
    secrets_mgr.set("DEUSEX_PORTAL_URL", "https://example-user.github.io/deusex-v3/")

    overridden = load_oauth_defaults(paths, secrets_mgr)
    assert overridden.google.client_id == "test-google-client-id.apps.googleusercontent.com"
    assert overridden.portal_base_url == "https://example-user.github.io/deusex-v3"
    assert "https://example-user.github.io" in overridden.allowed_cors_origins


def test_pkce_s256_generation() -> None:
    verifier, challenge = generate_pkce_pair()
    assert len(verifier) >= 43
    assert "=" not in challenge
    assert build_pkce_challenge(verifier) == challenge


@pytest.mark.asyncio
async def test_oauth_server_health_cors_callback_and_fast_shutdown() -> None:
    received_callbacks: list[OAuthCallbackResult] = []
    server = OAuthCallbackServer(
        port=0,
        allowed_origins=("https://custom.github.io",),
        on_oauth_callback=lambda res: received_callbacks.append(res),
    )

    port = await server.start()
    assert port > 0
    assert server.host == "127.0.0.1"

    try:
        async with httpx.AsyncClient() as client:
            # 1. /health con origen GitHub Pages permitido
            resp_ok = await client.get(
                f"{server.base_url}/health",
                headers={"Origin": "https://demo.github.io"},
            )
            assert resp_ok.status_code == 200
            assert resp_ok.headers.get("access-control-allow-origin") == "https://demo.github.io"
            assert resp_ok.json()["ok"] is True

            # 2. CORS bloqueado para origen externo arbitrario
            resp_preflight = await client.options(
                f"{server.base_url}/setup-sync",
                headers={"Origin": "https://evil.example.com"},
            )
            assert resp_preflight.status_code == 403

            # 3. Callback con state inválido -> 400 (anti-CSRF)
            resp_bad_state = await client.get(
                f"{server.base_url}/callback",
                params={"code": "abc", "state": "invalid_state_value_123456"},
            )
            assert resp_bad_state.status_code == 400

            # 4. Callback con state válido -> 200 y resuelve Future
            valid_state = "state_token_valid_1234567890"
            fut = server.register_pending_state(valid_state, provider="google")
            resp_cb = await client.get(
                f"{server.base_url}/callback",
                params={"code": "auth_code_xyz", "state": valid_state},
            )
            assert resp_cb.status_code == 200
            result = await asyncio.wait_for(fut, timeout=2.0)
            assert result.ok is True
            assert result.code == "auth_code_xyz"
            assert result.provider == "google"
            assert len(received_callbacks) == 1

            # 5. Reutilización del mismo state -> 400 (replay rechazado)
            resp_replay = await client.get(
                f"{server.base_url}/callback",
                params={"code": "auth_code_xyz", "state": valid_state},
            )
            assert resp_replay.status_code == 400

            # 6. Escape anti-XSS en descripción de error
            xss_state = "state_token_xss_1234567890"
            fut_err = server.register_pending_state(xss_state, provider="google")
            resp_xss = await client.get(
                f"{server.base_url}/callback",
                params={
                    "error": "access_denied",
                    "error_description": "<script>alert(1)</script>",
                    "state": xss_state,
                },
            )
            assert resp_xss.status_code == 400
            assert "<script>" not in resp_xss.text
            assert "&lt;script&gt;" in resp_xss.text
            err_res = await asyncio.wait_for(fut_err, timeout=2.0)
            assert err_res.ok is False
    finally:
        t0 = time.perf_counter()
        await server.stop()
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5
        assert server.is_running is False


@pytest.mark.asyncio
async def test_oauth_server_setup_sync_security_and_whitelist() -> None:
    captured: list[SetupSyncPayload] = []

    def _on_sync(payload: SetupSyncPayload) -> tuple[list[str], list[str]]:
        captured.append(payload)
        return sorted(payload.secrets), sorted(payload.settings)

    server = OAuthCallbackServer(port=0, on_setup_sync=_on_sync)
    await server.start()
    try:
        async with httpx.AsyncClient() as client:
            # Sin token -> 401
            r1 = await client.post(
                f"{server.base_url}/setup-sync",
                json={"secrets": {"GEMINI_API_KEY": "AIzaFakeKey123"}},
            )
            assert r1.status_code == 401

            # Emite token válido
            token = server.issue_sync_token(ttl_sec=120.0)

            # Origen externo bloqueado -> 403
            r2 = await client.post(
                f"{server.base_url}/setup-sync",
                headers={
                    "Origin": "https://malicious.site",
                    "X-DeusEx-Sync-Token": token,
                },
                json={"secrets": {"GEMINI_API_KEY": "AIzaFakeKey123"}},
            )
            assert r2.status_code == 403

            # Petición válida desde GitHub Pages: filtra claves no autorizadas y nunca refleja valores
            secret_value = "AIzaTestSuperSecretValue999"
            r3 = await client.post(
                f"{server.base_url}/setup-sync",
                headers={
                    "Origin": "https://dazzvlt.github.io",
                    "X-DeusEx-Sync-Token": token,
                },
                json={
                    "secrets": {
                        "GEMINI_API_KEY": secret_value,
                        "UNSAFE_ARBITRARY_KEY": "blocked",
                    },
                    "settings": {
                        "general.user_name": "Tester",
                        "developer.enabled": "true",
                    },
                },
            )
            assert r3.status_code == 200
            body = r3.json()
            assert body["ok"] is True
            assert body["saved_secrets"] == ["GEMINI_API_KEY"]
            assert body["updated_settings"] == ["general.user_name"]
            assert "UNSAFE_ARBITRARY_KEY" in body["rejected_keys"]
            assert secret_value not in r3.text
            assert len(captured) == 1
            assert captured[0].secrets == {"GEMINI_API_KEY": secret_value}
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_google_auth_pkce_full_flow_refresh_and_disconnect(tmp_path: Path) -> None:
    paths = _make_isolated_paths(tmp_path)
    settings_store = SettingsStore(paths)
    settings_store.load()
    secrets_mgr = SecretsManager(paths, fallback_to_os_env=False)
    secrets_mgr.set("GOOGLE_OAUTH_CLIENT_ID", "demo-client.apps.googleusercontent.com")

    token_requests: list[dict[str, str]] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            parsed_body = {
                k: v[0]
                for k, v in parse_qs(request.content.decode("utf-8")).items()
                if v
            }
            token_requests.append(parsed_body)
            if parsed_body.get("grant_type") == "authorization_code":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ya29.initial-token",
                        "refresh_token": "1//refresh-token-secret",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "openid email profile",
                    },
                )
            if parsed_body.get("grant_type") == "refresh_token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ya29.refreshed-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
        if request.url.path == "/v1/userinfo":
            return httpx.Response(
                200,
                json={"email": "usuario@example.com", "name": "Usuario Prueba"},
            )
        if request.url.path == "/revoke":
            return httpx.Response(200, text="")
        return httpx.Response(404, json={"error": "not_found"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler))
    service = GoogleAuthService(paths, secrets_mgr, http_client=mock_client)
    server = OAuthCallbackServer(port=0)

    captured_auth_url: list[str] = []

    def _fake_browser_open(auth_url: str) -> bool:
        captured_auth_url.append(auth_url)
        query = parse_qs(urlsplit(auth_url).query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]
        assert query["code_challenge_method"][0] == "S256"
        assert redirect_uri.startswith("http://127.0.0.1:")

        async def _simulate_callback() -> None:
            async with httpx.AsyncClient() as cb_client:
                resp = await cb_client.get(
                    redirect_uri,
                    params={"code": "google_auth_code_123", "state": state},
                )
                assert resp.status_code == 200

        asyncio.get_running_loop().create_task(_simulate_callback())
        return True

    try:
        session = await service.complete_oauth_flow(
            server,
            open_browser=_fake_browser_open,
            timeout_sec=10.0,
        )
        assert session.connected is True
        assert session.email == "usuario@example.com"
        assert session.name == "Usuario Prueba"
        assert session.has_refresh_token is True

        # Verifica que el token se guardó exclusivamente en secrets/google_oauth.json y NO en settings.json
        secret_file = paths.secrets_dir / f"{GOOGLE_SECRET_NAME}.json"
        assert secret_file.is_file()
        stored = json.loads(secret_file.read_text(encoding="utf-8"))
        assert stored["refresh_token"] == "1//refresh-token-secret"
        assert "1//refresh-token-secret" not in paths.settings_file.read_text(encoding="utf-8")

        # Verifica PKCE verifier enviado al endpoint /token
        assert token_requests[0]["grant_type"] == "authorization_code"
        assert len(token_requests[0]["code_verifier"]) >= 43

        # Fuerza expiración y valida refresh automático preservando refresh_token
        stored["expires_at"] = time.time() - 120.0
        secrets_mgr.save_secret_json(GOOGLE_SECRET_NAME, stored)
        refreshed = await service.get_valid_access_token()
        assert refreshed == "ya29.refreshed-token"
        reloaded = service.load_tokens()
        assert reloaded is not None
        assert reloaded.refresh_token == "1//refresh-token-secret"

        # Rechaza state manipulado
        with pytest.raises(GoogleAuthError, match="anti-CSRF"):
            await service.exchange_code(
                code="code_bad",
                state="wrong_state",
                expected_state="expected_state",
                code_verifier="verifier",
                redirect_uri="http://127.0.0.1:8888/callback",
            )

        # Desconexión limpia elimina secrets/google_oauth.json
        assert await service.disconnect(revoke_remote=True) is True
        assert not secret_file.exists()
        assert service.is_connected() is False
    finally:
        await server.stop()
        await mock_client.aclose()


def test_web_portal_static_files_compliance_and_anti_open_redirect() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    portal_dir = repo_root / "web_portal"
    required = (
        "index.html",
        "guides.html",
        "privacy.html",
        "terms.html",
        "callback.html",
        "404.html",
        "styles.css",
        "app.js",
    )
    for filename in required:
        assert (portal_dir / filename).is_file(), f"Falta archivo del portal: {filename}"

    privacy_text = (portal_dir / "privacy.html").read_text(encoding="utf-8")
    assert "Google API Services User Data Policy" in privacy_text
    assert "Limited Use" in privacy_text

    terms_text = (portal_dir / "terms.html").read_text(encoding="utf-8")
    assert "Open-Meteo" in terms_text
    assert "CC BY 4.0" in terms_text

    callback_text = (portal_dir / "callback.html").read_text(encoding="utf-8")
    app_js_text = (portal_dir / "app.js").read_text(encoding="utf-8")
    assert "FORBIDDEN_REDIRECT_KEYS" in callback_text
    assert "127.0.0.1" in app_js_text
    assert "buildOAuthLoopbackForwardUrl" in app_js_text
    assert "validateOAuthState" in app_js_text
