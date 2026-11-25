from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import contextlib
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Final
from urllib.parse import urlencode
import webbrowser

import httpx

from deusex.core.config import SecretsManager
from deusex.core.paths import AppPaths
from deusex.modules.workspace.oauth_server import (
    DEFAULT_OAUTH_PORT,
    OAuthCallbackResult,
    OAuthCallbackServer,
)

logger = logging.getLogger(__name__)

GOOGLE_SECRET_NAME: Final[str] = "google_oauth"
DEFAULT_PORTAL_BASE_URL: Final[str] = "https://dazzvlt.github.io/deusex-v3"
DEFAULT_GOOGLE_AUTH_URI: Final[str] = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_GOOGLE_TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"
DEFAULT_GOOGLE_USERINFO_URI: Final[str] = "https://openidconnect.googleapis.com/v1/userinfo"
DEFAULT_GOOGLE_REVOKE_URI: Final[str] = "https://oauth2.googleapis.com/revoke"

DEFAULT_GOOGLE_SCOPES: Final[tuple[str, ...]] = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/youtube.readonly",
)


class GoogleAuthError(RuntimeError):
    """Error controlado durante el flujo OAuth 2.0 PKCE de Google."""


@dataclass(frozen=True, slots=True)
class OAuthProviderDefaults:
    client_id: str
    auth_uri: str
    token_uri: str
    redirect_uri_template: str
    scopes: tuple[str, ...] = ()
    userinfo_uri: str = ""
    revoke_uri: str = ""

    def format_redirect_uri(self, port: int) -> str:
        return self.redirect_uri_template.format(port=port)


@dataclass(frozen=True, slots=True)
class OAuthDefaults:
    portal_base_url: str
    oauth_callback_port: int
    allowed_cors_origins: tuple[str, ...]
    google: OAuthProviderDefaults
    spotify: OAuthProviderDefaults
    tidal: OAuthProviderDefaults


def _parse_provider_defaults(
    raw: dict[str, Any],
    *,
    fallback_auth_uri: str,
    fallback_token_uri: str,
    fallback_redirect: str,
    fallback_scopes: tuple[str, ...] = (),
    fallback_userinfo: str = "",
    fallback_revoke: str = "",
) -> OAuthProviderDefaults:
    raw_scopes = raw.get("scopes", [])
    scopes = (
        tuple(str(s).strip() for s in raw_scopes if str(s).strip())
        if isinstance(raw_scopes, list)
        else fallback_scopes
    )
    redirect_tpl = str(
        raw.get("redirect_uri_template") or raw.get("redirect_uri") or fallback_redirect
    ).strip()
    return OAuthProviderDefaults(
        client_id=str(raw.get("client_id") or "").strip(),
        auth_uri=str(raw.get("auth_uri") or fallback_auth_uri).strip(),
        token_uri=str(raw.get("token_uri") or fallback_token_uri).strip(),
        redirect_uri_template=redirect_tpl,
        scopes=scopes or fallback_scopes,
        userinfo_uri=str(raw.get("userinfo_uri") or fallback_userinfo).strip(),
        revoke_uri=str(raw.get("revoke_uri") or fallback_revoke).strip(),
    )


def load_oauth_defaults(
    paths: AppPaths,
    secrets_manager: SecretsManager | None = None,
) -> OAuthDefaults:
    """Carga `assets/oauth_app_defaults.json` y aplica overrides seguros desde `.env`."""
    raw: dict[str, Any] = {}
    defaults_file = paths.oauth_defaults_file
    if defaults_file.is_file():
        try:
            loaded = json.loads(defaults_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("No se pudo leer oauth_app_defaults.json (%s); usando valores base.", exc)

    portal_base_url = str(raw.get("portal_base_url") or DEFAULT_PORTAL_BASE_URL).strip().rstrip("/")
    if secrets_manager is not None:
        env_portal = secrets_manager.get("DEUSEX_PORTAL_URL")
        if env_portal and env_portal.strip():
            portal_base_url = env_portal.strip().rstrip("/")

    raw_port = raw.get("oauth_callback_port", DEFAULT_OAUTH_PORT)
    try:
        oauth_port = int(raw_port)
        if oauth_port < 1024 or oauth_port > 65535:
            oauth_port = DEFAULT_OAUTH_PORT
    except (TypeError, ValueError):
        oauth_port = DEFAULT_OAUTH_PORT

    raw_origins = raw.get("allowed_cors_origins", [])
    origins: list[str] = []
    if isinstance(raw_origins, list):
        origins.extend(str(item).strip().rstrip("/") for item in raw_origins if str(item).strip())
    if portal_base_url:
        # Asegura el origen del portal configurado (scheme://host)
        parts = portal_base_url.split("/")
        if len(parts) >= 3:
            origin = f"{parts[0]}//{parts[2]}"
            if origin not in origins:
                origins.append(origin)

    google_raw = raw.get("google", {}) if isinstance(raw.get("google"), dict) else {}
    spotify_raw = raw.get("spotify", {}) if isinstance(raw.get("spotify"), dict) else {}
    tidal_raw = raw.get("tidal", {}) if isinstance(raw.get("tidal"), dict) else {}

    google_defaults = _parse_provider_defaults(
        google_raw,
        fallback_auth_uri=DEFAULT_GOOGLE_AUTH_URI,
        fallback_token_uri=DEFAULT_GOOGLE_TOKEN_URI,
        fallback_redirect="http://127.0.0.1:{port}/callback",
        fallback_scopes=DEFAULT_GOOGLE_SCOPES,
        fallback_userinfo=DEFAULT_GOOGLE_USERINFO_URI,
        fallback_revoke=DEFAULT_GOOGLE_REVOKE_URI,
    )
    spotify_defaults = _parse_provider_defaults(
        spotify_raw,
        fallback_auth_uri="https://accounts.spotify.com/authorize",
        fallback_token_uri="https://accounts.spotify.com/api/token",
        fallback_redirect="http://127.0.0.1:8888/callback",
    )
    tidal_defaults = _parse_provider_defaults(
        tidal_raw,
        fallback_auth_uri="https://login.tidal.com/authorize",
        fallback_token_uri="https://auth.tidal.com/v1/oauth2/token",
        fallback_redirect="http://127.0.0.1:{port}/callback",
    )

    if secrets_manager is not None:
        env_google_cid = secrets_manager.get("GOOGLE_OAUTH_CLIENT_ID")
        if env_google_cid:
            google_defaults = OAuthProviderDefaults(
                client_id=env_google_cid.strip(),
                auth_uri=google_defaults.auth_uri,
                token_uri=google_defaults.token_uri,
                redirect_uri_template=google_defaults.redirect_uri_template,
                scopes=google_defaults.scopes,
                userinfo_uri=google_defaults.userinfo_uri,
                revoke_uri=google_defaults.revoke_uri,
            )
        env_spotify_cid = secrets_manager.get("SPOTIFY_CLIENT_ID")
        if env_spotify_cid:
            spotify_defaults = OAuthProviderDefaults(
                client_id=env_spotify_cid.strip(),
                auth_uri=spotify_defaults.auth_uri,
                token_uri=spotify_defaults.token_uri,
                redirect_uri_template=spotify_defaults.redirect_uri_template,
                scopes=spotify_defaults.scopes,
            )
        env_tidal_cid = secrets_manager.get("TIDAL_CLIENT_ID")
        if env_tidal_cid:
            tidal_defaults = OAuthProviderDefaults(
                client_id=env_tidal_cid.strip(),
                auth_uri=tidal_defaults.auth_uri,
                token_uri=tidal_defaults.token_uri,
                redirect_uri_template=tidal_defaults.redirect_uri_template,
                scopes=tidal_defaults.scopes,
            )

    return OAuthDefaults(
        portal_base_url=portal_base_url,
        oauth_callback_port=oauth_port,
        allowed_cors_origins=tuple(origins),
        google=google_defaults,
        spotify=spotify_defaults,
        tidal=tidal_defaults,
    )


def build_pkce_challenge(code_verifier: str) -> str:
    """Calcula `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))` según RFC 7636 (S256)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> tuple[str, str]:
    """Genera `(code_verifier, code_challenge)` criptográficamente seguros para PKCE `S256`."""
    verifier = secrets.token_urlsafe(64)
    challenge = build_pkce_challenge(verifier)
    return verifier, challenge


@dataclass(frozen=True, slots=True)
class PendingGoogleAuth:
    auth_url: str
    state: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoogleOAuthTokens:
    access_token: str
    refresh_token: str = ""
    token_type: str = "Bearer"
    scope: str = ""
    expires_at: float = 0.0
    email: str = ""
    name: str = ""
    obtained_at: float = field(default_factory=time.time)

    def is_expired(self, *, leeway_sec: float = 60.0) -> bool:
        if self.expires_at <= 0:
            return False
        return time.time() >= (self.expires_at - leeway_sec)

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(s for s in self.scope.split() if s)

    def to_secret_dict(self) -> dict[str, Any]:
        return {
            "provider": "google",
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at,
            "email": self.email,
            "name": self.name,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_secret_dict(cls, data: dict[str, Any]) -> GoogleOAuthTokens | None:
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token and not refresh_token:
            return None
        try:
            expires_at = float(data.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        try:
            obtained_at = float(data.get("obtained_at") or 0.0)
        except (TypeError, ValueError):
            obtained_at = 0.0
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=str(data.get("token_type") or "Bearer").strip(),
            scope=str(data.get("scope") or "").strip(),
            expires_at=expires_at,
            email=str(data.get("email") or "").strip(),
            name=str(data.get("name") or "").strip(),
            obtained_at=obtained_at,
        )


@dataclass(frozen=True, slots=True)
class GoogleAuthSessionInfo:
    """Resumen libre de secretos para exponer en UI y diagnósticos."""

    connected: bool
    client_configured: bool
    email: str = ""
    name: str = ""
    scopes: tuple[str, ...] = ()
    expires_at: float = 0.0
    has_refresh_token: bool = False


class GoogleAuthService:
    """
    Flujo OAuth 2.0 PKCE (`S256`) para Google Workspace y YouTube Music.
    Persiste tokens exclusivamente en `USER_DATA_DIR/secrets/google_oauth.json`
    mediante `SecretsManager.save_secret_json(...)`.
    """

    def __init__(
        self,
        paths: AppPaths,
        secrets_manager: SecretsManager,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._paths = paths
        self._secrets = secrets_manager
        self._external_client = http_client is not None
        self._http_client = http_client
        self._refresh_lock = asyncio.Lock()

    def _get_defaults(self) -> OAuthDefaults:
        return load_oauth_defaults(self._paths, self._secrets)

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return self._http_client

    async def close(self) -> None:
        if self._http_client is not None and not self._external_client:
            await self._http_client.aclose()
            self._http_client = None

    def get_client_id(self) -> str:
        return self._get_defaults().google.client_id

    def is_client_configured(self) -> bool:
        return bool(self.get_client_id())

    def get_portal_base_url(self) -> str:
        return self._get_defaults().portal_base_url

    def build_setup_sync_url(
        self,
        *,
        port: int,
        sync_token: str,
        page: str = "guides.html",
    ) -> str:
        """Genera la URL del portal pasando `sync_token` en el fragmento `#` (no viaja al servidor)."""
        base = self.get_portal_base_url().rstrip("/")
        clean_page = page.lstrip("/")
        fragment = urlencode({"port": str(port), "sync_token": sync_token})
        return f"{base}/{clean_page}#{fragment}"

    def load_tokens(self) -> GoogleOAuthTokens | None:
        raw = self._secrets.load_secret_json(GOOGLE_SECRET_NAME)
        if raw is None:
            return None
        return GoogleOAuthTokens.from_secret_dict(raw)

    def is_connected(self) -> bool:
        return self.load_tokens() is not None

    def get_session_info(self) -> GoogleAuthSessionInfo:
        tokens = self.load_tokens()
        client_ok = self.is_client_configured()
        if tokens is None:
            return GoogleAuthSessionInfo(connected=False, client_configured=client_ok)
        return GoogleAuthSessionInfo(
            connected=True,
            client_configured=client_ok,
            email=tokens.email,
            name=tokens.name,
            scopes=tokens.scopes,
            expires_at=tokens.expires_at,
            has_refresh_token=bool(tokens.refresh_token),
        )

    def begin_authorization(
        self,
        *,
        port: int = DEFAULT_OAUTH_PORT,
        scopes: tuple[str, ...] | None = None,
        state: str | None = None,
    ) -> PendingGoogleAuth:
        defaults = self._get_defaults().google
        if not defaults.client_id:
            raise GoogleAuthError(
                "Falta configurar GOOGLE_OAUTH_CLIENT_ID (en assets/oauth_app_defaults.json o .env)."
            )

        effective_scopes = scopes if scopes is not None else defaults.scopes
        code_verifier, code_challenge = generate_pkce_pair()
        effective_state = (state or secrets.token_urlsafe(24)).strip()
        redirect_uri = defaults.format_redirect_uri(port)

        params = {
            "client_id": defaults.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(effective_scopes),
            "state": effective_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{defaults.auth_uri}?{urlencode(params)}"
        return PendingGoogleAuth(
            auth_url=auth_url,
            state=effective_state,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scopes=effective_scopes,
        )

    async def exchange_code(
        self,
        *,
        code: str,
        state: str,
        expected_state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> GoogleOAuthTokens:
        if not state or not expected_state or not hmac.compare_digest(state, expected_state):
            raise GoogleAuthError("Validación anti-CSRF fallida: el parámetro 'state' no coincide.")
        if not code.strip():
            raise GoogleAuthError("El código de autorización OAuth está vacío.")

        defaults = self._get_defaults().google
        if not defaults.client_id:
            raise GoogleAuthError("GOOGLE_OAUTH_CLIENT_ID no está configurado.")

        form_data: dict[str, str] = {
            "client_id": defaults.client_id,
            "code": code.strip(),
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        # Google Desktop OAuth puede requerir client_secret opcional además de PKCE
        client_secret = self._secrets.get("GOOGLE_OAUTH_CLIENT_SECRET")
        if client_secret:
            form_data["client_secret"] = client_secret.strip()

        client = self._get_http_client()
        try:
            resp = await client.post(defaults.token_uri, data=form_data)
        except httpx.HTTPError as exc:
            raise GoogleAuthError(f"Error de red al intercambiar el código con Google: {exc}") from exc

        data = self._parse_token_response(resp)
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise GoogleAuthError("Google no devolvió un access_token válido.")

        expires_in = self._parse_expires_in(data.get("expires_in"))
        email, name = await self._fetch_userinfo_best_effort(access_token, defaults.userinfo_uri)

        tokens = GoogleOAuthTokens(
            access_token=access_token,
            refresh_token=str(data.get("refresh_token") or "").strip(),
            token_type=str(data.get("token_type") or "Bearer").strip(),
            scope=str(data.get("scope") or " ".join(defaults.scopes)).strip(),
            expires_at=time.time() + expires_in if expires_in > 0 else 0.0,
            email=email,
            name=name,
            obtained_at=time.time(),
        )
        self._secrets.save_secret_json(GOOGLE_SECRET_NAME, tokens.to_secret_dict())
        logger.info("Credenciales Google OAuth guardadas en secrets/%s.json", GOOGLE_SECRET_NAME)
        return tokens

    async def complete_oauth_flow(
        self,
        oauth_server: OAuthCallbackServer,
        *,
        open_browser: Callable[[str], bool] | None = None,
        timeout_sec: float = 180.0,
        scopes: tuple[str, ...] | None = None,
    ) -> GoogleAuthSessionInfo:
        """Ejecuta el flujo interactivo completo 1-clic sobre `OAuthCallbackServer`."""
        port = await oauth_server.start()
        pending = self.begin_authorization(port=port, scopes=scopes)
        wait_future = oauth_server.register_pending_state(
            pending.state,
            provider="google",
            ttl_sec=timeout_sec,
        )

        opener = open_browser if open_browser is not None else webbrowser.open
        try:
            opened = opener(pending.auth_url)
            if not opened:
                logger.warning("No se pudo abrir automáticamente el navegador para Google OAuth.")

            cb_result: OAuthCallbackResult = await asyncio.wait_for(
                wait_future,
                timeout=max(5.0, float(timeout_sec)),
            )
        except TimeoutError as exc:
            oauth_server.cancel_pending_state(pending.state)
            raise GoogleAuthError("Se agotó el tiempo de espera del inicio de sesión con Google.") from exc
        except asyncio.CancelledError:
            oauth_server.cancel_pending_state(pending.state)
            raise

        if not cb_result.ok or not cb_result.code:
            detail = cb_result.error_description or cb_result.error or "Autorización cancelada."
            raise GoogleAuthError(f"Google rechazó o canceló la autorización: {detail}")

        await self.exchange_code(
            code=cb_result.code,
            state=cb_result.state,
            expected_state=pending.state,
            code_verifier=pending.code_verifier,
            redirect_uri=pending.redirect_uri,
        )
        return self.get_session_info()

    async def refresh_access_token(self) -> GoogleOAuthTokens:
        async with self._refresh_lock:
            current = self.load_tokens()
            if current is None:
                raise GoogleAuthError("No hay una sesión de Google conectada.")
            if not current.refresh_token:
                raise GoogleAuthError(
                    "La sesión de Google no tiene refresh_token. Vuelve a iniciar sesión."
                )

            defaults = self._get_defaults().google
            if not defaults.client_id:
                raise GoogleAuthError("GOOGLE_OAUTH_CLIENT_ID no está configurado.")

            form_data: dict[str, str] = {
                "client_id": defaults.client_id,
                "refresh_token": current.refresh_token,
                "grant_type": "refresh_token",
            }
            client_secret = self._secrets.get("GOOGLE_OAUTH_CLIENT_SECRET")
            if client_secret:
                form_data["client_secret"] = client_secret.strip()

            client = self._get_http_client()
            try:
                resp = await client.post(defaults.token_uri, data=form_data)
            except httpx.HTTPError as exc:
                raise GoogleAuthError(f"Error de red al refrescar token de Google: {exc}") from exc

            data = self._parse_token_response(resp)
            new_access = str(data.get("access_token") or "").strip()
            if not new_access:
                raise GoogleAuthError("Google no devolvió un nuevo access_token al refrescar.")

            expires_in = self._parse_expires_in(data.get("expires_in"))
            updated = GoogleOAuthTokens(
                access_token=new_access,
                refresh_token=str(data.get("refresh_token") or "").strip() or current.refresh_token,
                token_type=str(data.get("token_type") or current.token_type or "Bearer").strip(),
                scope=str(data.get("scope") or current.scope).strip(),
                expires_at=time.time() + expires_in if expires_in > 0 else 0.0,
                email=current.email,
                name=current.name,
                obtained_at=time.time(),
            )
            self._secrets.save_secret_json(GOOGLE_SECRET_NAME, updated.to_secret_dict())
            return updated

    async def get_valid_access_token(self, *, force_refresh: bool = False) -> str:
        tokens = self.load_tokens()
        if tokens is None:
            raise GoogleAuthError("No hay sesión activa de Google. Conecta tu cuenta en Integraciones.")
        if force_refresh or not tokens.access_token or tokens.is_expired():
            tokens = await self.refresh_access_token()
        return tokens.access_token

    async def disconnect(self, *, revoke_remote: bool = False) -> bool:
        tokens = self.load_tokens()
        if revoke_remote and tokens is not None:
            revoke_token = tokens.refresh_token or tokens.access_token
            revoke_uri = self._get_defaults().google.revoke_uri
            if revoke_token and revoke_uri:
                client = self._get_http_client()
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(revoke_uri, data={"token": revoke_token})
        deleted = self._secrets.delete_secret_json(GOOGLE_SECRET_NAME)
        logger.info("Sesión Google OAuth desconectada (eliminado=%s).", deleted)
        return deleted

    async def _fetch_userinfo_best_effort(
        self,
        access_token: str,
        userinfo_uri: str,
    ) -> tuple[str, str]:
        if not userinfo_uri:
            return "", ""
        client = self._get_http_client()
        try:
            resp = await client.get(
                userinfo_uri,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                return "", ""
            payload = resp.json()
            if not isinstance(payload, dict):
                return "", ""
            email = str(payload.get("email") or "").strip()
            name = str(payload.get("name") or "").strip()
            return email, name
        except Exception as exc:
            logger.debug("No se pudo obtener perfil Google userinfo (best-effort): %s", exc)
            return "", ""

    @staticmethod
    def _parse_token_response(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise GoogleAuthError(
                f"Respuesta inválida de Google OAuth (HTTP {resp.status_code})."
            ) from exc
        if not isinstance(data, dict):
            raise GoogleAuthError(f"Formato inesperado de Google OAuth (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            err = str(data.get("error_description") or data.get("error") or f"HTTP {resp.status_code}")
            raise GoogleAuthError(f"Error de Google OAuth: {err}")
        return data

    @staticmethod
    def _parse_expires_in(raw_value: Any) -> float:
        try:
            val = float(raw_value)
            return max(0.0, val)
        except (TypeError, ValueError):
            return 0.0
