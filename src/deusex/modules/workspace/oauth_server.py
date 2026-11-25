from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hmac
import html
import json
import logging
import secrets
import time
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

LOOPBACK_HOST: Final[str] = "127.0.0.1"
DEFAULT_OAUTH_PORT: Final[int] = 8888
MAX_REQUEST_HEADER_BYTES: Final[int] = 16 * 1024
MAX_REQUEST_BODY_BYTES: Final[int] = 16 * 1024

ALLOWED_SETUP_SYNC_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
        "DISCORD_BOT_TOKEN",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "SPOTIFY_CLIENT_ID",
        "TIDAL_CLIENT_ID",
        "DEUSEX_PORTAL_URL",
    }
)

ALLOWED_SETUP_SYNC_SETTINGS: Final[frozenset[str]] = frozenset(
    {
        "general.user_name",
        "general.city",
        "general.country",
        "general.language",
    }
)

_HTTP_STATUS_TEXT: Final[dict[int, str]] = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    """Resultado validado recibido en `http://127.0.0.1:<port>/callback`."""

    provider: str
    state: str
    code: str | None = None
    error: str | None = None
    error_description: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return bool(self.code) and not self.error


@dataclass(frozen=True, slots=True)
class SetupSyncPayload:
    """Carga útil saneada recibida en `POST http://127.0.0.1:<port>/setup-sync`."""

    secrets: dict[str, str] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)
    source_origin: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


type SetupSyncHandler = Callable[
    [SetupSyncPayload],
    tuple[list[str], list[str]] | Awaitable[tuple[list[str], list[str]]],
]
type OAuthCallbackHook = Callable[[OAuthCallbackResult], Awaitable[None] | None]


@dataclass(slots=True)
class _PendingState:
    provider: str
    future: asyncio.Future[OAuthCallbackResult]
    expires_at: float


@dataclass(slots=True)
class _SyncSession:
    token: str
    expires_at: float


def _is_allowed_cors_origin(origin: str, extra_origins: frozenset[str]) -> bool:
    clean = origin.strip().rstrip("/")
    if not clean:
        return False
    if clean in extra_origins:
        return True
    parsed = urlsplit(clean)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme == "https" and (host == "github.io" or host.endswith(".github.io")):
        return True
    return scheme == "http" and host in ("127.0.0.1", "localhost")


def _render_callback_html(*, title: str, message: str, ok: bool) -> bytes:
    badge = "Vinculación completada" if ok else "No se pudo completar"
    accent = "#10b981" if ok else "#f43f5e"
    safe_title = html.escape(title, quote=True)
    safe_msg = html.escape(message, quote=True)
    doc = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title} — DeusEx v3</title>
  <style>
    body {{
      margin: 0; min-height: 100vh; display: grid; place-items: center;
      background: #0b0f17; color: #f1f5f9;
      font-family: "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
    }}
    .card {{
      max-width: 520px; margin: 1.5rem; padding: 1.8rem 2rem;
      background: #121826; border: 1px solid rgba(148,163,184,.2);
      border-radius: 16px; box-shadow: 0 14px 34px rgba(0,0,0,.45);
    }}
    .pill {{
      display: inline-block; padding: .25rem .75rem; border-radius: 999px;
      font-size: .8rem; font-weight: 600; color: {accent};
      border: 1px solid {accent}; margin-bottom: .8rem;
    }}
    h1 {{ margin: 0 0 .5rem; font-size: 1.35rem; }}
    p {{ margin: 0; color: #94a3b8; line-height: 1.55; }}
  </style>
</head>
<body>
  <main class="card">
    <span class="pill">{badge}</span>
    <h1>{safe_title}</h1>
    <p>{safe_msg}</p>
  </main>
</body>
</html>
"""
    return doc.encode("utf-8")


class OAuthCallbackServer:
    """
    Servidor HTTP local asíncrono estrictamente ligado a `127.0.0.1` en `AsyncCoreThread`.
    Soporta:
      - `GET /health`
      - `GET /callback` (OAuth 2.0 PKCE con validación anti-CSRF `state`)
      - `POST /setup-sync` (CORS controlado + token efímero de sesión)
    Garantiza parada determinista en `< 0.5 s`.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_OAUTH_PORT,
        allowed_origins: tuple[str, ...] = (),
        on_setup_sync: SetupSyncHandler | None = None,
        on_oauth_callback: OAuthCallbackHook | None = None,
    ) -> None:
        if not isinstance(port, int) or port < 0 or port > 65535:
            raise ValueError(f"Puerto OAuth inválido: {port!r}")
        if port != 0 and port < 1024:
            raise ValueError(f"El puerto local debe ser >= 1024 (o 0 en tests): {port!r}")

        self._requested_port = port
        self._effective_port = port
        self._allowed_origins = frozenset(o.strip().rstrip("/") for o in allowed_origins if o.strip())
        self._on_setup_sync = on_setup_sync
        self._on_oauth_callback = on_oauth_callback

        self._server: asyncio.AbstractServer | None = None
        self._pending_states: dict[str, _PendingState] = {}
        self._sync_session: _SyncSession | None = None
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def host(self) -> str:
        return LOOPBACK_HOST

    @property
    def port(self) -> int:
        return self._effective_port

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self._effective_port}"

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    def set_port(self, port: int) -> None:
        if self.is_running:
            raise RuntimeError("No se puede cambiar el puerto mientras el servidor está activo.")
        if port != 0 and (port < 1024 or port > 65535):
            raise ValueError(f"Puerto OAuth inválido: {port!r}")
        self._requested_port = port
        self._effective_port = port

    def set_setup_sync_handler(self, handler: SetupSyncHandler | None) -> None:
        self._on_setup_sync = handler

    def set_oauth_callback_hook(self, hook: OAuthCallbackHook | None) -> None:
        self._on_oauth_callback = hook

    async def start(self) -> int:
        """Inicia escucha estrictamente en `127.0.0.1` y devuelve el puerto efectivo."""
        if self.is_running:
            return self._effective_port

        self._server = await asyncio.start_server(
            self._accept_client,
            host=LOOPBACK_HOST,
            port=self._requested_port,
            backlog=32,
        )
        sockets = self._server.sockets
        if sockets:
            sock_addr = sockets[0].getsockname()
            if isinstance(sock_addr, tuple) and len(sock_addr) >= 2:
                self._effective_port = int(sock_addr[1])

        logger.info("OAuthCallbackServer activo en %s", self.base_url)
        return self._effective_port

    async def stop(self, *, stop_timeout_sec: float = 0.4) -> None:
        """Detiene el servidor y cancela estados/conexiones pendientes en < 0.5 s."""
        server = self._server
        self._server = None
        self._sync_session = None

        for pending in list(self._pending_states.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending_states.clear()

        for task in list(self._client_tasks):
            if not task.done():
                task.cancel()

        if server is not None:
            server.close()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, OSError):
                await asyncio.wait_for(server.wait_closed(), timeout=max(0.05, stop_timeout_sec))

        if self._client_tasks:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(
                    asyncio.gather(*self._client_tasks, return_exceptions=True),
                    timeout=max(0.05, stop_timeout_sec * 0.5),
                )
            self._client_tasks.clear()

        logger.info("OAuthCallbackServer detenido limpiamente.")

    # ------------------------------------------------------------------
    # Gestión de estados anti-CSRF y tokens efímeros de Setup Sync
    # ------------------------------------------------------------------
    def _prune_expired_states(self) -> None:
        now = time.monotonic()
        expired = [st for st, entry in self._pending_states.items() if entry.expires_at <= now]
        for st in expired:
            entry = self._pending_states.pop(st, None)
            if entry is not None and not entry.future.done():
                entry.future.set_exception(TimeoutError("Sesión OAuth expirada."))

    def register_pending_state(
        self,
        state: str,
        *,
        provider: str,
        ttl_sec: float = 300.0,
    ) -> asyncio.Future[OAuthCallbackResult]:
        clean_state = state.strip()
        if len(clean_state) < 16:
            raise ValueError("El parámetro 'state' anti-CSRF debe tener al menos 16 caracteres.")
        self._prune_expired_states()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[OAuthCallbackResult] = loop.create_future()
        self._pending_states[clean_state] = _PendingState(
            provider=provider.strip().lower() or "unknown",
            future=fut,
            expires_at=time.monotonic() + max(10.0, float(ttl_sec)),
        )
        return fut

    def cancel_pending_state(self, state: str) -> None:
        entry = self._pending_states.pop(state, None)
        if entry is not None and not entry.future.done():
            entry.future.cancel()

    def issue_sync_token(self, *, ttl_sec: float = 600.0) -> str:
        token = secrets.token_urlsafe(32)
        self._sync_session = _SyncSession(
            token=token,
            expires_at=time.monotonic() + max(15.0, float(ttl_sec)),
        )
        return token

    def revoke_sync_token(self) -> None:
        self._sync_session = None

    def has_active_sync_token(self) -> bool:
        sess = self._sync_session
        return sess is not None and sess.expires_at > time.monotonic()

    def _validate_sync_token(self, candidate: str) -> bool:
        sess = self._sync_session
        if sess is None or sess.expires_at <= time.monotonic():
            self._sync_session = None
            return False
        if not candidate:
            return False
        return hmac.compare_digest(sess.token, candidate.strip())

    # ------------------------------------------------------------------
    # Manejo de conexiones HTTP/1.1 liviano (stdlib asyncio)
    # ------------------------------------------------------------------
    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=3.0,
            )
            if len(raw_head) > MAX_REQUEST_HEADER_BYTES:
                await self._send_json(writer, 413, {"ok": False, "error": "Encabezados demasiado grandes."})
                return
        except (asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError, OSError):
            await self._close_writer(writer)
            return

        try:
            head_text = raw_head.decode("iso-8859-1")
            lines = head_text.split("\r\n")
            if not lines:
                await self._send_json(writer, 400, {"ok": False, "error": "Solicitud HTTP vacía."})
                return

            parts = lines[0].split(" ")
            if len(parts) < 2:
                await self._send_json(writer, 400, {"ok": False, "error": "Línea HTTP malformada."})
                return

            method = parts[0].upper()
            raw_target = parts[1]
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

            content_length = 0
            if "content-length" in headers:
                try:
                    content_length = int(headers["content-length"])
                except ValueError:
                    await self._send_json(writer, 400, {"ok": False, "error": "Content-Length inválido."})
                    return
                if content_length < 0 or content_length > MAX_REQUEST_BODY_BYTES:
                    await self._send_json(
                        writer,
                        413,
                        {"ok": False, "error": "El cuerpo excede el límite de 16 KB."},
                    )
                    return

            body = b""
            if content_length > 0:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=3.0)

            await self._dispatch_request(writer, method, raw_target, headers, body)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error procesando petición HTTP en OAuthCallbackServer")
            with contextlib.suppress(OSError):
                await self._send_json(
                    writer,
                    500,
                    {"ok": False, "error": "Error interno en el servidor OAuth local."},
                )
        finally:
            await self._close_writer(writer)

    async def _dispatch_request(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        raw_target: str,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        parsed = urlsplit(raw_target)
        path = parsed.path or "/"
        origin = headers.get("origin", "")
        cors_headers = self._build_cors_headers(origin)

        if method == "OPTIONS":
            if origin and not cors_headers:
                await self._send_json(writer, 403, {"ok": False, "error": "Origen CORS no permitido."})
                return
            await self._send_raw(writer, 204, b"", "text/plain; charset=utf-8", extra_headers=cors_headers)
            return

        if path == "/health":
            if method != "GET":
                await self._send_json(
                    writer,
                    405,
                    {"ok": False, "error": "Método no permitido."},
                    extra_headers=cors_headers,
                )
                return
            await self._send_json(
                writer,
                200,
                {
                    "ok": True,
                    "service": "deusex-oauth-local",
                    "host": LOOPBACK_HOST,
                    "port": self._effective_port,
                    "setup_sync_ready": self.has_active_sync_token(),
                },
                extra_headers=cors_headers,
            )
            return

        if path == "/callback":
            if method != "GET":
                await self._send_json(writer, 405, {"ok": False, "error": "Método no permitido."})
                return
            await self._handle_oauth_callback(writer, parsed.query)
            return

        if path == "/setup-sync":
            if method != "POST":
                await self._send_json(
                    writer,
                    405,
                    {"ok": False, "error": "Usa POST para Setup Sync."},
                    extra_headers=cors_headers,
                )
                return
            if origin and not cors_headers:
                await self._send_json(
                    writer,
                    403,
                    {"ok": False, "error": "Origen CORS no autorizado para Setup Sync."},
                )
                return
            await self._handle_setup_sync(writer, headers, body, origin, cors_headers)
            return

        await self._send_json(
            writer,
            404,
            {"ok": False, "error": "Endpoint no encontrado."},
            extra_headers=cors_headers,
        )

    async def _handle_oauth_callback(self, writer: asyncio.StreamWriter, query_string: str) -> None:
        self._prune_expired_states()
        query = parse_qs(query_string, keep_blank_values=False)

        state = (query.get("state", [""])[0] or "").strip()
        code = (query.get("code", [""])[0] or "").strip() or None
        error = (query.get("error", [""])[0] or "").strip() or None
        error_description = (query.get("error_description", [""])[0] or "").strip()
        provider_hint = (query.get("provider", [""])[0] or "").strip().lower()

        if not state or state not in self._pending_states:
            page = _render_callback_html(
                title="Sesión OAuth inválida o expirada",
                message=(
                    "El parámetro de seguridad anti-CSRF ('state') no coincide con ninguna solicitud "
                    "activa en DeusEx v3. Inicia nuevamente la vinculación desde la aplicación."
                ),
                ok=False,
            )
            await self._send_raw(writer, 400, page, "text/html; charset=utf-8")
            return

        pending = self._pending_states.pop(state)
        provider = pending.provider or provider_hint or "unknown"

        if not code and not error:
            error = "missing_code"
            error_description = "El proveedor no devolvió un código de autorización válido."

        result = OAuthCallbackResult(
            provider=provider,
            state=state,
            code=code,
            error=error,
            error_description=error_description,
        )

        if not pending.future.done():
            pending.future.set_result(result)

        if self._on_oauth_callback is not None:
            try:
                hook_res = self._on_oauth_callback(result)
                if isinstance(hook_res, Awaitable):
                    await hook_res
            except Exception:
                logger.exception("Error ejecutando hook on_oauth_callback")

        if result.ok:
            page = _render_callback_html(
                title="Autorización recibida",
                message=(
                    f"DeusEx v3 recibió tu inicio de sesión de {provider.capitalize()} correctamente. "
                    "Ya puedes cerrar esta pestaña y volver a la aplicación."
                ),
                ok=True,
            )
            await self._send_raw(writer, 200, page, "text/html; charset=utf-8")
        else:
            detail = error_description or error or "Acceso denegado por el usuario."
            page = _render_callback_html(
                title="Autorización cancelada",
                message=f"No se pudo vincular {provider.capitalize()}: {detail}",
                ok=False,
            )
            await self._send_raw(writer, 400, page, "text/html; charset=utf-8")

    async def _handle_setup_sync(
        self,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        body: bytes,
        origin: str,
        cors_headers: dict[str, str],
    ) -> None:
        token = headers.get("x-deusex-sync-token", "")
        if not self._validate_sync_token(token):
            await self._send_json(
                writer,
                401,
                {
                    "ok": False,
                    "error": "Token efímero de Setup Sync inválido o expirado. Abre Setup Sync desde DeusEx.",
                },
                extra_headers=cors_headers,
            )
            return

        try:
            raw_data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._send_json(
                writer,
                400,
                {"ok": False, "error": "JSON inválido en Setup Sync."},
                extra_headers=cors_headers,
            )
            return

        if not isinstance(raw_data, dict):
            await self._send_json(
                writer,
                400,
                {"ok": False, "error": "El cuerpo de Setup Sync debe ser un objeto JSON."},
                extra_headers=cors_headers,
            )
            return

        clean_secrets: dict[str, str] = {}
        rejected_keys: list[str] = []
        raw_secrets = raw_data.get("secrets", {})
        if isinstance(raw_secrets, dict):
            for key, val in raw_secrets.items():
                key_str = str(key).strip()
                if key_str not in ALLOWED_SETUP_SYNC_SECRETS:
                    rejected_keys.append(key_str)
                    continue
                if isinstance(val, str) and val.strip():
                    clean_secrets[key_str] = val.strip()

        clean_settings: dict[str, str] = {}
        raw_settings = raw_data.get("settings", {})
        if isinstance(raw_settings, dict):
            for key, val in raw_settings.items():
                key_str = str(key).strip()
                if key_str not in ALLOWED_SETUP_SYNC_SETTINGS:
                    rejected_keys.append(key_str)
                    continue
                if isinstance(val, str) and val.strip():
                    clean_settings[key_str] = val.strip()

        if not clean_secrets and not clean_settings:
            await self._send_json(
                writer,
                400,
                {
                    "ok": False,
                    "error": "No se recibieron claves ni ajustes válidos en Setup Sync.",
                    "rejected_keys": rejected_keys,
                },
                extra_headers=cors_headers,
            )
            return

        payload = SetupSyncPayload(
            secrets=clean_secrets,
            settings=clean_settings,
            source_origin=origin,
        )

        saved_secrets: list[str] = sorted(clean_secrets)
        updated_settings: list[str] = sorted(clean_settings)
        if self._on_setup_sync is not None:
            handler_res = self._on_setup_sync(payload)
            if isinstance(handler_res, Awaitable):
                saved_secrets, updated_settings = await handler_res
            else:
                saved_secrets, updated_settings = handler_res

        await self._send_json(
            writer,
            200,
            {
                "ok": True,
                "saved_secrets": saved_secrets,
                "updated_settings": updated_settings,
                "rejected_keys": rejected_keys,
                "message": (
                    f"Sincronizado localmente: {len(saved_secrets)} clave(s) y "
                    f"{len(updated_settings)} ajuste(s) guardados en DeusEx."
                ),
            },
            extra_headers=cors_headers,
        )

    def _build_cors_headers(self, origin: str) -> dict[str, str]:
        if not origin or not _is_allowed_cors_origin(origin, self._allowed_origins):
            return {}
        return {
            "Access-Control-Allow-Origin": origin.strip().rstrip("/"),
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-DeusEx-Sync-Token",
            "Access-Control-Max-Age": "300",
            "Vary": "Origin",
        }

    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._send_raw(
            writer,
            status,
            raw,
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    async def _send_raw(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        reason = _HTTP_STATUS_TEXT.get(status, "OK")
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Connection": "close",
        }
        if extra_headers:
            headers.update(extra_headers)

        head_lines = [f"HTTP/1.1 {status} {reason}"]
        for key, val in headers.items():
            head_lines.append(f"{key}: {val}")
        raw_response = ("\r\n".join(head_lines) + "\r\n\r\n").encode("ascii") + body
        writer.write(raw_response)
        await writer.drain()

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
