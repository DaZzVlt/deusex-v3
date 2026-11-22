from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import ssl
from typing import Any
from urllib.parse import quote

import httpx
import websockets

from deusex.modules.network.adapters import (
    AdapterActionResult,
    DeviceCapability,
    DeviceType,
    NetworkDeviceAdapter,
    NetworkDeviceRecord,
    normalize_mac,
    send_wake_on_lan,
)
from deusex.modules.network.store import DeviceAliasStore

logger = logging.getLogger(__name__)

_KEY_ALIASES: dict[str, str] = {
    "power": "KEY_POWER",
    "power_off": "KEY_POWER",
    "vol_up": "KEY_VOLUP",
    "volume_up": "KEY_VOLUP",
    "vol_down": "KEY_VOLDOWN",
    "volume_down": "KEY_VOLDOWN",
    "mute": "KEY_MUTE",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "enter": "KEY_ENTER",
    "ok": "KEY_ENTER",
    "back": "KEY_RETURN",
    "return": "KEY_RETURN",
    "home": "KEY_HOME",
    "menu": "KEY_MENU",
    "source": "KEY_SOURCE",
    "hdmi": "KEY_HDMI",
    "play": "KEY_PLAY",
    "pause": "KEY_PAUSE",
    "play_pause": "KEY_PLAY_BACK",
    "stop": "KEY_STOP",
    "rewind": "KEY_REWIND",
    "ff": "KEY_FF",
    "next": "KEY_FF",
    "previous": "KEY_REWIND",
    "ch_up": "KEY_CHUP",
    "ch_down": "KEY_CHDOWN",
}

_APP_ALIASES: dict[str, str] = {
    "youtube": "111299001912",
    "netflix": "3201907018807",
    "spotify": "3201606009684",
    "prime": "3201910019365",
    "prime_video": "3201910019365",
    "disney": "3201901017640",
    "disney+": "3201901017640",
    "twitch": "3202203026841",
    "hbo": "3201601007230",
    "max": "3201601007230",
    "browser": "org.tizen.browser",
}


def build_tizen_ws_url(
    ip: str,
    *,
    port: int = 8002,
    app_name: str = "DeusEx v3",
    token: str | None = None,
) -> tuple[str, bool]:
    """Construye la URL WebSocket de Samsung Tizen (`wss` en 8002 o `ws` en 8001)."""
    use_tls = port == 8002
    scheme = "wss" if use_tls else "ws"
    encoded_name = base64.b64encode(app_name.encode("utf-8")).decode("ascii")
    url = (
        f"{scheme}://{ip}:{port}/api/v2/channels/samsung.remote.control"
        f"?name={quote(encoded_name, safe='')}"
    )
    if use_tls and token:
        url = f"{url}&token={quote(token, safe='')}"
    return url, use_tls


def build_tizen_key_payload(key_code: str) -> dict[str, Any]:
    """Construye el payload JSON estándar `ms.remote.control` de Samsung Tizen."""
    return {
        "method": "ms.remote.control",
        "params": {
            "Cmd": "Click",
            "DataOfCmd": key_code,
            "Option": "false",
            "TypeOfRemote": "SendRemoteKey",
        },
    }


def resolve_tizen_key(raw_key: str) -> str:
    clean = raw_key.strip()
    if clean.upper().startswith("KEY_"):
        return clean.upper()
    return _KEY_ALIASES.get(clean.lower(), f"KEY_{clean.upper()}")


def resolve_tizen_app_id(raw_app: str) -> str:
    clean = raw_app.strip()
    return _APP_ALIASES.get(clean.lower(), clean)


def _create_unverified_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SamsungTizenAdapter(NetworkDeviceAdapter):
    """
    Adaptador Samsung Tizen Smart TV:
    - WebSocket WSS (`:8002`) / WS (`:8001`) con persistencia automática de token en `.env`.
    - Encendido por Wake-on-LAN (MAC aprendida dinámicamente) y apagado vía `KEY_POWER`.
    - Control de volumen en pasos, silencio, navegación, reproducción y apertura de apps.
    """

    adapter_id = "samsung_tizen"
    supported_types = (DeviceType.SAMSUNG_TV,)
    capabilities = frozenset(
        {
            DeviceCapability.POWER_ON,
            DeviceCapability.POWER_OFF,
            DeviceCapability.VOLUME,
            DeviceCapability.MUTE,
            DeviceCapability.MEDIA_PLAYBACK,
            DeviceCapability.NAVIGATION,
            DeviceCapability.LAUNCH_APP,
        }
    )

    def __init__(
        self,
        store: DeviceAliasStore,
        *,
        app_name: str = "DeusEx v3",
        connect_timeout_sec: float = 2.5,
    ) -> None:
        self._store = store
        self._app_name = app_name
        self._connect_timeout_sec = max(0.5, connect_timeout_sec)

    def _preferred_port(self, device: NetworkDeviceRecord) -> int:
        if device.port in (8001, 8002):
            return device.port
        if bool(device.metadata.get("token_auth_support", True)):
            return 8002
        return 8001

    async def _send_ws_payloads(
        self,
        device: NetworkDeviceRecord,
        payloads: list[dict[str, Any]],
        *,
        step_delay_sec: float = 0.14,
    ) -> AdapterActionResult:
        if not device.last_ip:
            return AdapterActionResult(
                ok=False,
                message=f"No se conoce la IP de {device.alias}. Escanea la red primero.",
                error_code="missing_ip",
            )

        token = self._store.get_token(device.device_uid, kind="tizen")
        primary_port = self._preferred_port(device)
        requires_tls = primary_port == 8002 or bool(device.metadata.get("token_auth_support", True))
        candidate_ports = [8002] if requires_tls else [8001, 8002]
        last_error = ""

        idx_port = 0
        while idx_port < len(candidate_ports):
            port = candidate_ports[idx_port]
            idx_port += 1
            url, use_tls = build_tizen_ws_url(
                device.last_ip,
                port=port,
                app_name=self._app_name,
                token=token,
            )
            ssl_ctx = _create_unverified_ssl_context() if use_tls else None
            handshake_timeout = 15.0 if (use_tls and not token) else self._connect_timeout_sec
            try:
                async with websockets.connect(
                    url,
                    ssl=ssl_ctx,
                    open_timeout=self._connect_timeout_sec,
                    close_timeout=0.6,
                ) as ws:
                    handshake_raw = await asyncio.wait_for(ws.recv(), timeout=handshake_timeout)
                    handshake = json.loads(str(handshake_raw)) if handshake_raw else {}
                    event_name = str(handshake.get("event") or "")

                    if event_name == "ms.channel.unauthorized":
                        logger.warning(
                            "Samsung TV '%s': handshake rechazado (ms.channel.unauthorized - dispositivo bloqueado)",
                            device.alias,
                        )
                        return AdapterActionResult(
                            ok=False,
                            message=(
                                f"{device.alias} rechazó el emparejamiento sin mostrar aviso. "
                                "En el televisor revisa: Ajustes → General → Administrador de dispositivos externos "
                                "→ Administrador de conexión de dispositivos → Lista de dispositivos "
                                "(elimina 'DeusEx' si figura denegado y activa 'Notificación de acceso')."
                            ),
                            error_code="pairing_unauthorized",
                        )

                    if event_name == "ms.channel.timeOut":
                        logger.warning("Samsung TV '%s': expiró espera de confirmación en pantalla", device.alias)
                        return AdapterActionResult(
                            ok=False,
                            message=f"Expiró la espera de confirmación en la pantalla de {device.alias}.",
                            error_code="pairing_timeout",
                        )

                    data_obj = handshake.get("data")
                    if isinstance(data_obj, dict):
                        new_token = str(data_obj.get("token") or "").strip()
                        if new_token and new_token != token:
                            self._store.set_token(device.device_uid, new_token, kind="tizen")
                            token = new_token

                    for idx, payload in enumerate(payloads):
                        await asyncio.wait_for(
                            ws.send(json.dumps(payload)),
                            timeout=self._connect_timeout_sec,
                        )
                        if idx < len(payloads) - 1:
                            await asyncio.sleep(step_delay_sec)

                    logger.info("Samsung TV '%s': comando enviado exitosamente (puerto %d, payloads=%d)", device.alias, port, len(payloads))
                    return AdapterActionResult(
                        ok=True,
                        message=f"Comando enviado a {device.alias}.",
                        data={"port": port, "token_saved": bool(token)},
                    )
            except TimeoutError:
                logger.warning("Samsung TV '%s': timeout al conectar (puerto %d)", device.alias, port)
                if use_tls and not token:
                    return AdapterActionResult(
                        ok=False,
                        message=(
                            f"Expiró la espera de confirmación en la pantalla de {device.alias}. "
                            "Asegúrate de pulsar 'Permitir' con el control remoto del televisor."
                        ),
                        error_code="pairing_timeout",
                    )
                last_error = f"Tiempo de espera agotado al conectar con {device.alias} (:{port})."
            except ConnectionRefusedError as exc:
                last_error = str(exc)
                logger.warning("Samsung TV '%s': conexión rechazada en puerto %d", device.alias, port)
                if port == 8002 and 8001 not in candidate_ports:
                    candidate_ports.append(8001)
            except (OSError, ValueError, websockets.WebSocketException) as exc:
                last_error = str(exc)

        return AdapterActionResult(
            ok=False,
            message=f"No se pudo comunicar con {device.alias}: {last_error or 'TV no accesible'}",
            error_code="connection_failed",
        )

    async def power_on(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        macs_to_try: list[str] = []
        raw_candidates: list[Any] = [
            device.mac_address,
            device.metadata.get("wifi_mac"),
        ]
        alts = device.metadata.get("alt_macs")
        if isinstance(alts, list):
            raw_candidates.extend(alts)

        for raw in raw_candidates:
            clean = normalize_mac(str(raw or ""))
            if clean and clean not in macs_to_try:
                macs_to_try.append(clean)

        if not macs_to_try:
            return AdapterActionResult(
                ok=False,
                message=(
                    f"{device.alias} no tiene dirección MAC guardada para Wake-on-LAN. "
                    "Escanea la red mientras el televisor esté encendido para registrarla."
                ),
                error_code="missing_mac",
            )

        sent_any = False
        for burst in range(2):
            for mac in macs_to_try:
                sent = await asyncio.to_thread(send_wake_on_lan, mac, target_ip=device.last_ip)
                if sent:
                    sent_any = True
            if burst == 0:
                await asyncio.sleep(0.25)

        if not sent_any:
            return AdapterActionResult(
                ok=False,
                message=f"No se pudo enviar el paquete Wake-on-LAN a {device.alias}.",
                error_code="wol_failed",
            )
        return AdapterActionResult(
            ok=True,
            message=f"Señal de encendido (Wake-on-LAN) enviada a {device.alias}.",
            data={"mac_addresses": macs_to_try},
        )

    async def power_off(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        res = await self._send_ws_payloads(device, [build_tizen_key_payload("KEY_POWER")])
        if res.ok:
            return AdapterActionResult(
                ok=True,
                message=f"Orden de apagado enviada a {device.alias}.",
                data=res.data,
            )
        return res

    async def set_volume(
        self,
        device: NetworkDeviceRecord,
        *,
        level: int | None = None,
        delta: int | None = None,
        mute: bool | None = None,
    ) -> AdapterActionResult:
        if mute is not None:
            res = await self._send_ws_payloads(device, [build_tizen_key_payload("KEY_MUTE")])
            if res.ok:
                return AdapterActionResult(
                    ok=True,
                    message=f"Silencio alternado en {device.alias}.",
                    data=res.data,
                )
            return res

        if delta is not None and delta != 0:
            steps = min(max(abs(int(delta)), 1), 10)
            key = "KEY_VOLUP" if delta > 0 else "KEY_VOLDOWN"
            payloads = [build_tizen_key_payload(key) for _ in range(steps)]
            res = await self._send_ws_payloads(device, payloads)
            if res.ok:
                direction = "subido" if delta > 0 else "bajado"
                return AdapterActionResult(
                    ok=True,
                    message=f"Volumen {direction} ({steps} pasos) en {device.alias}.",
                    data={"steps": steps, "key": key, **res.data},
                )
            return res

        if level is not None:
            return AdapterActionResult(
                ok=False,
                message=(
                    f"El protocolo remoto de {device.alias} ajusta volumen en pasos "
                    "(usa subir/bajar volumen o silenciar)."
                ),
                error_code="absolute_volume_unsupported",
            )

        return AdapterActionResult(
            ok=False,
            message="Debes indicar delta de volumen o silencio para el televisor Samsung.",
            error_code="invalid_args",
        )

    async def media_control(
        self,
        device: NetworkDeviceRecord,
        action: str,
        *,
        media_url: str = "",
        title: str = "",
    ) -> AdapterActionResult:
        _ = (media_url, title)
        clean_action = action.strip().lower()
        key_map = {
            "play": "KEY_PLAY",
            "resume": "KEY_PLAY",
            "pause": "KEY_PAUSE",
            "play_pause": "KEY_PLAY_BACK",
            "toggle": "KEY_PLAY_BACK",
            "stop": "KEY_STOP",
            "next": "KEY_FF",
            "previous": "KEY_REWIND",
        }
        key = key_map.get(clean_action)
        if key is None:
            return AdapterActionResult(
                ok=False,
                message=f"Acción multimedia no soportada en {device.alias}: {action}",
                error_code="unsupported_action",
            )
        res = await self._send_ws_payloads(device, [build_tizen_key_payload(key)])
        if res.ok:
            return AdapterActionResult(
                ok=True,
                message=f"Reproducción ({clean_action}) enviada a {device.alias}.",
                data={"key": key, **res.data},
            )
        return res

    async def send_key(self, device: NetworkDeviceRecord, key: str) -> AdapterActionResult:
        resolved = resolve_tizen_key(key)
        res = await self._send_ws_payloads(device, [build_tizen_key_payload(resolved)])
        if res.ok:
            return AdapterActionResult(
                ok=True,
                message=f"Tecla {resolved} enviada a {device.alias}.",
                data={"key": resolved, **res.data},
            )
        return res

    async def launch_app(self, device: NetworkDeviceRecord, app_id: str) -> AdapterActionResult:
        if not device.last_ip:
            return AdapterActionResult(
                ok=False,
                message=f"No se conoce la IP de {device.alias}. Escanea la red primero.",
                error_code="missing_ip",
            )

        resolved_id = resolve_tizen_app_id(app_id)
        rest_url = f"http://{device.last_ip}:8001/api/v2/applications/{quote(resolved_id, safe='')}"
        with contextlib.suppress(httpx.HTTPError, OSError):
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
                resp = await client.post(rest_url)
                if resp.status_code in (200, 201, 204):
                    return AdapterActionResult(
                        ok=True,
                        message=f"Aplicación '{app_id}' abierta en {device.alias}.",
                        data={"app_id": resolved_id, "transport": "rest"},
                    )

        ws_payload = {
            "method": "ms.channel.emit",
            "params": {
                "event": "ed.apps.launch",
                "to": "host",
                "data": {
                    "appId": resolved_id,
                    "action_type": "DEEP_LINK" if resolved_id.isdigit() else "NATIVE_LAUNCH",
                },
            },
        }
        res = await self._send_ws_payloads(device, [ws_payload])
        if res.ok:
            return AdapterActionResult(
                ok=True,
                message=f"Aplicación '{app_id}' lanzada en {device.alias}.",
                data={"app_id": resolved_id, "transport": "ws", **res.data},
            )
        return res
