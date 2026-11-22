from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import struct

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

XBOX_SMARTGLASS_PORT = 5050
_LIVE_ID_RE = re.compile(r"^[A-Za-z0-9]{12,20}$")


def normalize_live_id(raw: str | None) -> str:
    """Normaliza y valida un Xbox Live ID (descubierto por red o ingresado por el usuario)."""
    if not raw:
        return ""
    clean = re.sub(r"[^A-Za-z0-9]+", "", raw.strip()).upper()
    if not _LIVE_ID_RE.fullmatch(clean):
        return ""
    return clean


def build_xbox_discovery_packet() -> bytes:
    """Paquete UDP Discovery Request estándar de Xbox SmartGlass (`0xDD00`)."""
    payload = struct.pack("!IIH", 0x00000000, 0x00000000, 0x0002)
    return struct.pack("!HHH", 0xDD00, len(payload), 0x0002) + payload


def build_xbox_power_on_packet(live_id: str) -> bytes:
    """
    Construye el paquete UDP SmartGlass Power On (`0xDD02`) para Xbox One / Series.
    Estructura: header `0xDD02` + payload_len + version `0x0000` + live_id_len + live_id + NUL.
    """
    clean_id = normalize_live_id(live_id)
    if not clean_id:
        raise ValueError("Xbox Live ID inválido.")
    id_bytes = clean_id.encode("ascii")
    payload = struct.pack("!B", len(id_bytes)) + id_bytes + b"\x00"
    header = struct.pack("!HHH", 0xDD02, len(payload), 0x0000)
    return header + payload


def parse_xbox_live_id_from_response(packet: bytes) -> str:
    """Extrae el Live ID de una respuesta UDP Discovery (`0xDD01`) de Xbox."""
    if len(packet) < 16:
        return ""
    pkt_type = struct.unpack("!H", packet[:2])[0]
    if pkt_type != 0xDD01:
        return ""
    for match in re.findall(rb"FD[0-9A-Fa-f]{14}", packet[6:]):
        return str(match.decode("ascii", errors="ignore").upper())
    return ""


def _ping_xbox_sync(ip: str, *, timeout_sec: float = 0.7) -> tuple[bool, str]:
    packet = build_xbox_discovery_packet()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_sec)
        try:
            sock.sendto(packet, (ip, XBOX_SMARTGLASS_PORT))
            data, _ = sock.recvfrom(4096)
        except OSError:
            return False, ""
    live_id = parse_xbox_live_id_from_response(data)
    is_xbox = len(data) >= 2 and struct.unpack("!H", data[:2])[0] == 0xDD01
    return is_xbox, live_id


def _send_xbox_power_on_sync(
    live_id: str,
    *,
    target_ip: str = "",
    bursts: int = 4,
) -> bool:
    packet = build_xbox_power_on_packet(live_id)
    targets: list[str] = []
    if target_ip:
        targets.append(target_ip)
        with contextlib.suppress(ValueError):
            ip_obj = ipaddress.IPv4Address(target_ip)
            bcast = str(ipaddress.IPv4Network(f"{ip_obj}/24", strict=False).broadcast_address)
            if bcast not in targets:
                targets.append(bcast)
    if "255.255.255.255" not in targets:
        targets.append("255.255.255.255")

    sent_any = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.4)
        for _ in range(max(1, bursts)):
            for dest in targets:
                with contextlib.suppress(OSError):
                    sock.sendto(packet, (dest, XBOX_SMARTGLASS_PORT))
                    sent_any = True
    return sent_any


class XboxSmartGlassAdapter(NetworkDeviceAdapter):
    """
    Adaptador Xbox SmartGlass UDP `:5050` sin hardcodeo de IP ni Live ID:
    - Aprende el Live ID dinámicamente desde el escaneo LAN o al hacer ping a la consola.
    - Enciende la consola con paquetes UDP SmartGlass `0xDD02` (y refuerzo WoL si hay MAC).
    """

    adapter_id = "xbox_smartglass"
    supported_types = (DeviceType.XBOX,)
    capabilities = frozenset({DeviceCapability.POWER_ON})

    def __init__(self, store: DeviceAliasStore, *, ping_timeout_sec: float = 0.8) -> None:
        self._store = store
        self._ping_timeout_sec = max(0.3, ping_timeout_sec)

    def _resolve_live_id(self, device: NetworkDeviceRecord) -> str:
        from_meta = normalize_live_id(str(device.metadata.get("live_id") or ""))
        if from_meta:
            return from_meta
        from_env = normalize_live_id(self._store.get_token(device.device_uid, kind="xbox"))
        return from_env

    async def probe_console(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        if not device.last_ip:
            return AdapterActionResult(
                ok=False,
                message=f"No se conoce la IP de {device.alias}. Escanea la red primero.",
                error_code="missing_ip",
            )
        awake, discovered_live_id = await asyncio.to_thread(
            _ping_xbox_sync,
            device.last_ip,
            timeout_sec=self._ping_timeout_sec,
        )
        if discovered_live_id:
            updated_meta = {**device.metadata, "live_id": discovered_live_id}
            self._store.upsert_device(device.with_updates(metadata=updated_meta), preserve_alias=True)

        if awake:
            return AdapterActionResult(
                ok=True,
                message=f"{device.alias} está encendida y respondió por SmartGlass (:5050).",
                data={"online": True, "live_id_detected": bool(discovered_live_id)},
            )
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no respondió al ping SmartGlass (puede estar apagada o en reposo).",
            data={"online": False},
            error_code="xbox_unreachable",
        )

    async def power_on(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        live_id = self._resolve_live_id(device)

        if not live_id and device.last_ip:
            awake, discovered = await asyncio.to_thread(
                _ping_xbox_sync,
                device.last_ip,
                timeout_sec=self._ping_timeout_sec,
            )
            if discovered:
                live_id = discovered
                updated_meta = {**device.metadata, "live_id": live_id}
                self._store.upsert_device(device.with_updates(metadata=updated_meta), preserve_alias=True)
            if awake:
                return AdapterActionResult(
                    ok=True,
                    message=f"{device.alias} ya se encuentra encendida.",
                    data={"already_on": True, "live_id_detected": bool(live_id)},
                )

        sent_smartglass = False
        if live_id:
            sent_smartglass = await asyncio.to_thread(
                _send_xbox_power_on_sync,
                live_id,
                target_ip=device.last_ip,
            )

        sent_wol = False
        mac = normalize_mac(device.mac_address)
        if mac:
            sent_wol = await asyncio.to_thread(send_wake_on_lan, mac, target_ip=device.last_ip)

        if sent_smartglass or sent_wol:
            transport = "smartglass+wol" if (sent_smartglass and sent_wol) else ("smartglass" if sent_smartglass else "wol")
            return AdapterActionResult(
                ok=True,
                message=f"Señal de encendido enviada a {device.alias}.",
                data={"transport": transport, "live_id_used": bool(live_id)},
            )

        return AdapterActionResult(
            ok=False,
            message=(
                f"{device.alias} aún no tiene Live ID ni MAC registrados. "
                "Escanea la red mientras la Xbox esté encendida (con encendido remoto activado) para aprenderlos."
            ),
            error_code="missing_xbox_live_id",
        )
