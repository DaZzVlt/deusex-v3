from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field, replace
from enum import StrEnum
import ipaddress
import re
import socket
from typing import Any

_MAC_HEX_RE = re.compile(r"[^0-9A-Fa-f]")


class DeviceType(StrEnum):
    """Tipos de dispositivos LAN soportados y contratos preparados para expansión futura."""

    SAMSUNG_TV = "samsung_tv"
    CHROMECAST = "chromecast"
    MIRACAST = "miracast"
    YOUTUBE_TV = "youtube_tv"
    XBOX = "xbox"
    # Contratos listos para adaptadores futuros sin tocar el núcleo:
    LG_WEBOS = "lg_webos"
    ANDROID_TV = "android_tv"
    SMART_LIGHT = "smart_light"
    HOME_ASSISTANT = "home_assistant"
    GENERIC = "generic"


class DeviceCapability(StrEnum):
    """Capacidades declarativas expuestas por cada adaptador de red."""

    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    VOLUME = "volume"
    MUTE = "mute"
    MEDIA_PLAYBACK = "media_playback"
    NAVIGATION = "navigation"
    LAUNCH_APP = "launch_app"
    CAST_MEDIA = "cast_media"
    MIRACAST = "miracast"
    YOUTUBE_LOUNGE = "youtube_lounge"


@dataclass(frozen=True, slots=True)
class NetworkDeviceRecord:
    """Registro canónico de un dispositivo LAN persistido en `device_aliases` (cero secretos)."""

    device_uid: str
    alias: str
    device_type: DeviceType
    last_ip: str = ""
    mac_address: str = ""
    port: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""
    online: bool = False

    @property
    def is_favorite(self) -> bool:
        return bool(self.metadata.get("favorite", False))

    def with_updates(
        self,
        *,
        alias: str | None = None,
        device_type: DeviceType | None = None,
        last_ip: str | None = None,
        mac_address: str | None = None,
        port: int | None = None,
        metadata: dict[str, Any] | None = None,
        updated_at: str | None = None,
        online: bool | None = None,
    ) -> NetworkDeviceRecord:
        return replace(
            self,
            alias=self.alias if alias is None else alias,
            device_type=self.device_type if device_type is None else device_type,
            last_ip=self.last_ip if last_ip is None else last_ip,
            mac_address=self.mac_address if mac_address is None else mac_address,
            port=self.port if port is None else port,
            metadata=dict(self.metadata) if metadata is None else dict(metadata),
            updated_at=self.updated_at if updated_at is None else updated_at,
            online=self.online if online is None else online,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "device_uid": self.device_uid,
            "alias": self.alias,
            "device_type": self.device_type.value,
            "last_ip": self.last_ip,
            "mac_address": self.mac_address,
            "port": self.port,
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
            "online": self.online,
            "favorite": self.is_favorite,
        }


@dataclass(frozen=True, slots=True)
class AdapterActionResult:
    """Resultado estandarizado de una operación ejecutada por un adaptador LAN."""

    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


def normalize_mac(raw: str | None) -> str:
    """Normaliza una dirección MAC al formato `AA:BB:CC:DD:EE:FF` o devuelve `""` si es inválida."""
    if not raw:
        return ""
    hex_only = _MAC_HEX_RE.sub("", raw).upper()
    if len(hex_only) != 12:
        return ""
    if hex_only in ("000000000000", "FFFFFFFFFFFF"):
        return ""
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))


def send_wake_on_lan(
    mac_address: str,
    *,
    target_ip: str = "",
    broadcast_ip: str = "255.255.255.255",
    ports: tuple[int, ...] = (9, 7),
) -> bool:
    """Envía un Magic Packet Wake-on-LAN UDP sin depender de librerías externas."""
    clean_mac = normalize_mac(mac_address)
    if not clean_mac:
        return False

    mac_bytes = bytes.fromhex(clean_mac.replace(":", ""))
    magic_packet = (b"\xff" * 6) + (mac_bytes * 16)

    destinations: list[str] = [broadcast_ip]
    if target_ip:
        try:
            ip_obj = ipaddress.IPv4Address(target_ip)
            subnet_bcast = str(ipaddress.IPv4Network(f"{ip_obj}/24", strict=False).broadcast_address)
            if subnet_bcast not in destinations:
                destinations.append(subnet_bcast)
        except ValueError:
            pass

    sent_any = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)
        for dest in destinations:
            for port in ports:
                try:
                    sock.sendto(magic_packet, (dest, port))
                    sent_any = True
                except OSError:
                    continue
    return sent_any


class NetworkDeviceAdapter(ABC):  # noqa: B024
    """
    Interfaz extensible para dispositivos LAN.
    Cada adaptador concreto sobrescribe únicamente las capacidades que soporta.
    """

    adapter_id: str = "generic"
    supported_types: tuple[DeviceType, ...] = ()
    capabilities: frozenset[DeviceCapability] = frozenset()

    def supports(self, device: NetworkDeviceRecord) -> bool:
        return device.device_type in self.supported_types

    def has_capability(self, capability: DeviceCapability) -> bool:
        return capability in self.capabilities

    async def power_on(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta encendido remoto con el adaptador {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def power_off(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta apagado remoto con el adaptador {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def set_volume(
        self,
        device: NetworkDeviceRecord,
        *,
        level: int | None = None,
        delta: int | None = None,
        mute: bool | None = None,
    ) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta control de volumen con el adaptador {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def media_control(
        self,
        device: NetworkDeviceRecord,
        action: str,
        *,
        media_url: str = "",
        title: str = "",
    ) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta control multimedia ({action}) con {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def send_key(self, device: NetworkDeviceRecord, key: str) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta envío de tecla ({key}) con {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def launch_app(self, device: NetworkDeviceRecord, app_id: str) -> AdapterActionResult:
        return AdapterActionResult(
            ok=False,
            message=f"{device.alias} no soporta apertura de apps ({app_id}) con {self.adapter_id}.",
            error_code="unsupported_capability",
        )

    async def close(self) -> None:
        """Libera recursos abiertos de forma idempotente (para cierre determinista <3 s)."""
        return None
