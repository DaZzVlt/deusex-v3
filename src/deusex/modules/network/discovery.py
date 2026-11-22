from __future__ import annotations

import asyncio
import contextlib
import ctypes
from dataclasses import dataclass, field
import ipaddress
import logging
import re
import socket
import struct
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx
import psutil

from deusex.modules.network.adapters import DeviceType, NetworkDeviceRecord, normalize_mac
from deusex.modules.network.store import DeviceAliasStore

logger = logging.getLogger(__name__)

# Constantes estándar de protocolo (RFC SSDP / Xbox SmartGlass) — cero IPs privadas hardcodeadas
_SSDP_MULTICAST_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_XBOX_SMARTGLASS_PORT = 5050

_MDNS_SERVICE_TYPES: tuple[str, ...] = (
    "_googlecast._tcp.local.",
    "_smartview._tcp.local.",
    "_samsungmsf._tcp.local.",
    "_androidtvremote2._tcp.local.",
    "_airplay._tcp.local.",
    "_home-assistant._tcp.local.",
)

_ARP_LINE_RE = re.compile(
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<mac>(?:[0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2})"
)

_VIRTUAL_IFACE_HINTS: tuple[str, ...] = (
    "vethernet",
    "wsl",
    "docker",
    "vmware",
    "virtualbox",
    "loopback",
    "tailscale",
    "zerotier",
    "hamachi",
    "wireguard",
    "npcap",
)

_DEVICE_TYPE_PRIORITY: dict[DeviceType, int] = {
    DeviceType.SAMSUNG_TV: 100,
    DeviceType.CHROMECAST: 95,
    DeviceType.XBOX: 90,
    DeviceType.LG_WEBOS: 85,
    DeviceType.ANDROID_TV: 80,
    DeviceType.YOUTUBE_TV: 70,
    DeviceType.HOME_ASSISTANT: 65,
    DeviceType.SMART_LIGHT: 60,
    DeviceType.MIRACAST: 55,
    DeviceType.GENERIC: 10,
}


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """Candidato detectado durante un ciclo de descubrimiento híbrido LAN."""

    device_uid: str
    friendly_name: str
    device_type: DeviceType
    ip: str
    port: int = 0
    mac_address: str = ""
    manufacturer: str = ""
    model: str = ""
    discovery_sources: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_arp_output(raw_output: str) -> dict[str, str]:
    """Extrae pares `ip -> MAC normalizada` desde la salida de `arp -a`."""
    mapping: dict[str, str] = {}
    for line in raw_output.splitlines():
        match = _ARP_LINE_RE.search(line)
        if not match:
            continue
        ip_str = match.group("ip")
        mac = normalize_mac(match.group("mac"))
        if not mac:
            continue
        try:
            ip_obj = ipaddress.IPv4Address(ip_str)
        except ValueError:
            continue
        if ip_obj.is_multicast or ip_obj.is_loopback or ip_obj.is_link_local:
            continue
        mapping[ip_str] = mac
    return mapping


def read_system_arp_table(*, timeout_sec: float = 1.5) -> dict[str, str]:
    """Lee la tabla ARP local del sistema sin abrir consola visible en Windows."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            timeout=timeout_sec,
            check=False,
            creationflags=creationflags,
        )
        raw_text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("No se pudo leer tabla ARP del sistema: %s", exc)
        return {}
    return parse_arp_output(raw_text)


def resolve_mac_via_sendarp(ip_str: str) -> str:
    """Resuelve la MAC de un host LAN en Windows usando `iphlpapi.SendARP`."""
    if sys.platform != "win32":
        return ""
    try:
        ip_bytes = socket.inet_aton(ip_str)
    except OSError:
        return ""

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return ""

    dest_ip = struct.unpack("<I", ip_bytes)[0]
    mac_buf = (ctypes.c_ubyte * 6)()
    mac_len = ctypes.c_ulong(6)
    try:
        ret = int(windll.iphlpapi.SendARP(dest_ip, 0, ctypes.byref(mac_buf), ctypes.byref(mac_len)))
    except OSError:
        return ""
    if ret != 0 or mac_len.value != 6:
        return ""
    raw_mac = ":".join(f"{mac_buf[i]:02X}" for i in range(6))
    return normalize_mac(raw_mac)


def get_local_ipv4_networks() -> list[tuple[str, ipaddress.IPv4Network]]:
    """
    Detecta dinámicamente las interfaces IPv4 privadas activas del equipo (cero hardcoding).
    Limita cada red a `/24` para mantener el sondeo activo rápido y determinista.
    """
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    primary: list[tuple[str, ipaddress.IPv4Network]] = []
    fallback: list[tuple[str, ipaddress.IPv4Network]] = []
    seen_nets: set[str] = set()

    for iface_name, addr_list in addrs.items():
        iface_stat = stats.get(iface_name)
        if iface_stat is not None and not iface_stat.isup:
            continue

        is_virtual = any(hint in iface_name.lower() for hint in _VIRTUAL_IFACE_HINTS)
        for snic in addr_list:
            if snic.family != socket.AF_INET:
                continue
            ip_str = str(snic.address or "")
            netmask = str(snic.netmask or "255.255.255.0")
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
                iface = ipaddress.IPv4Interface(f"{ip_str}/{netmask}")
            except ValueError:
                continue

            if ip_obj.is_loopback or ip_obj.is_link_local or not ip_obj.is_private:
                continue

            network = iface.network
            if network.prefixlen < 24:
                network = ipaddress.IPv4Network(f"{ip_str}/24", strict=False)

            net_key = str(network)
            if net_key in seen_nets:
                continue
            seen_nets.add(net_key)
            entry = (ip_str, network)
            if is_virtual:
                fallback.append(entry)
            else:
                primary.append(entry)

    return primary or fallback


def _classify_device_type(
    *,
    friendly_name: str = "",
    manufacturer: str = "",
    model: str = "",
    server: str = "",
    st: str = "",
    service_type: str = "",
) -> DeviceType:
    blob = " ".join([friendly_name, manufacturer, model, server, st, service_type]).lower()
    if "_googlecast" in blob or "chromecast" in blob or "eureka" in blob or "google tv" in blob:
        return DeviceType.CHROMECAST
    if "samsung" in blob or "tizen" in blob or "_smartview" in blob or "_samsungmsf" in blob:
        return DeviceType.SAMSUNG_TV
    if "xbox" in blob or "smartglass" in blob:
        return DeviceType.XBOX
    if "webos" in blob or "lg electronics" in blob:
        return DeviceType.LG_WEBOS
    if "androidtv" in blob or "android tv" in blob or "bravia" in blob:
        return DeviceType.ANDROID_TV
    if "home-assistant" in blob or "home assistant" in blob:
        return DeviceType.HOME_ASSISTANT
    if "dial" in blob or "youtube" in blob:
        return DeviceType.YOUTUBE_TV
    return DeviceType.GENERIC


def _build_device_uid(
    *,
    device_type: DeviceType,
    mac_address: str = "",
    native_id: str = "",
    ip: str = "",
) -> str:
    clean_mac = normalize_mac(mac_address)
    if clean_mac:
        return f"mac:{clean_mac.replace(':', '').lower()}"
    clean_native = re.sub(r"[^A-Za-z0-9_.-]+", "", native_id.strip()).lower()
    if clean_native:
        return f"{device_type.value}:{clean_native}"
    return f"{device_type.value}:{ip.strip()}"


def _parse_ssdp_headers(raw_packet: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    text = raw_packet.decode("utf-8", errors="ignore")
    for line in text.splitlines()[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return headers


def _parse_upnp_device_xml(xml_text: str) -> dict[str, str]:
    """Extrae `friendlyName`, `manufacturer`, `modelName` y `UDN` de un XML UPnP/DIAL."""
    info: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return info

    wanted = {
        "friendlyname": "friendly_name",
        "manufacturer": "manufacturer",
        "modelname": "model",
        "udn": "udn",
    }
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1].lower()
        if tag in wanted and elem.text and elem.text.strip():
            key = wanted[tag]
            info.setdefault(key, elem.text.strip())
    return info


def _build_xbox_discovery_packet() -> bytes:
    """Construye el paquete estándar UDP Discovery Request de Xbox SmartGlass (`:5050`)."""
    # Cabecera SmartGlass Discovery Request (type=0xDD00, payload_len=10, version=0x0002)
    payload = struct.pack("!IIH", 0x00000000, 0x00000000, 0x0002)
    return struct.pack("!HHH", 0xDD00, len(payload), 0x0002) + payload


def _parse_xbox_discovery_response(packet: bytes, ip: str) -> DiscoveredDevice | None:
    """Parsea una respuesta UDP SmartGlass (cabecera `0xDD01`) para extraer nombre y Live ID."""
    if len(packet) < 16:
        return None
    pkt_type = struct.unpack("!H", packet[:2])[0]
    if pkt_type != 0xDD01:
        return None

    # Extraer cadenas ASCII imprimibles (el certificado/LiveID suele aparecer como FD00... o XBOX)
    ascii_tokens = re.findall(rb"[A-Za-z0-9 _.-]{4,48}", packet[6:])
    decoded = [tok.decode("ascii", errors="ignore").strip() for tok in ascii_tokens]
    live_id = ""
    friendly_name = f"Xbox ({ip})"
    for tok in decoded:
        if re.fullmatch(r"FD[0-9A-Fa-f]{14}", tok):
            live_id = tok.upper()
        elif "xbox" in tok.lower() and len(tok) <= 32:
            friendly_name = tok

    meta: dict[str, Any] = {}
    if live_id:
        meta["live_id"] = live_id

    return DiscoveredDevice(
        device_uid=_build_device_uid(device_type=DeviceType.XBOX, native_id=live_id, ip=ip),
        friendly_name=friendly_name,
        device_type=DeviceType.XBOX,
        ip=ip,
        port=_XBOX_SMARTGLASS_PORT,
        manufacturer="Microsoft",
        model="Xbox",
        discovery_sources=("xbox_udp",),
        metadata=meta,
    )


class _MdnsCollector:
    """Colector ligero por duck-typing para `zeroconf.ServiceBrowser` (compatible con mypy strict)."""

    def __init__(self, out: list[DiscoveredDevice]) -> None:
        self._out = out

    def remove_service(self, zc: Any, service_type: str, name: str) -> None:
        return None

    def update_service(self, zc: Any, service_type: str, name: str) -> None:
        self.add_service(zc, service_type, name)

    def add_service(self, zc: Any, service_type: str, name: str) -> None:
        try:
            info = zc.get_service_info(service_type, name, timeout=500)
        except Exception:
            return
        if info is None:
            return

        addresses: list[str] = []
        with contextlib.suppress(Exception):
            addresses = list(info.parsed_scoped_addresses())
        ipv4_list = [addr for addr in addresses if ":" not in addr and not addr.startswith("127.")]
        if not ipv4_list:
            return
        ip = ipv4_list[0]

        props: dict[str, str] = {}
        raw_props = getattr(info, "properties", None) or {}
        if isinstance(raw_props, dict):
            for k, v in raw_props.items():
                key = k.decode("utf-8", errors="ignore") if isinstance(k, bytes) else str(k)
                val = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v or "")
                props[key.lower()] = val.strip()

        friendly_name = (
            props.get("fn")
            or props.get("name")
            or name.split(".", 1)[0]
            or f"Dispositivo ({ip})"
        )
        model = props.get("md") or props.get("model") or ""
        native_id = props.get("id") or props.get("uuid") or ""
        mac = normalize_mac(props.get("mac") or props.get("deviceid") or "")
        dev_type = _classify_device_type(
            friendly_name=friendly_name,
            model=model,
            service_type=service_type,
        )

        self._out.append(
            DiscoveredDevice(
                device_uid=_build_device_uid(
                    device_type=dev_type,
                    mac_address=mac,
                    native_id=native_id,
                    ip=ip,
                ),
                friendly_name=friendly_name,
                device_type=dev_type,
                ip=ip,
                port=int(getattr(info, "port", 0) or 0),
                mac_address=mac,
                model=model,
                discovery_sources=("mdns",),
                metadata={
                    "mdns_service": service_type,
                    "mdns_name": name,
                    "friendly_name": friendly_name,
                    "model": model,
                    "native_id": native_id,
                },
            )
        )


def _run_mdns_browse_sync(timeout_sec: float) -> list[DiscoveredDevice]:
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []

    found: list[DiscoveredDevice] = []
    zc: Any = None
    try:
        zc = Zeroconf()
        collector: Any = _MdnsCollector(found)
        _browser = ServiceBrowser(zc, list(_MDNS_SERVICE_TYPES), collector)
        deadline = time.monotonic() + max(0.3, timeout_sec)
        while time.monotonic() < deadline:
            time.sleep(0.05)
    except Exception as exc:
        logger.debug("mDNS discovery degradado: %s", exc)
    finally:
        if zc is not None:
            with contextlib.suppress(Exception):
                zc.close()
    return found


def _run_ssdp_msearch_sync(timeout_sec: float) -> list[tuple[str, dict[str, str]]]:
    targets = (
        "urn:dial-multiscreen-org:service:dial:1",
        "urn:samsung.com:device:RemoteControlReceiver:1",
        "ssdp:all",
    )
    results: list[tuple[str, dict[str, str]]] = []
    seen_locations: set[tuple[str, str]] = set()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.2)

        for st in targets:
            msg = (
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {_SSDP_MULTICAST_ADDR}:{_SSDP_PORT}\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 1\r\n"
                f"ST: {st}\r\n\r\n"
            ).encode("ascii")
            with contextlib.suppress(OSError):
                sock.sendto(msg, (_SSDP_MULTICAST_ADDR, _SSDP_PORT))

        deadline = time.monotonic() + max(0.4, timeout_sec)
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            ip = str(addr[0])
            headers = _parse_ssdp_headers(data)
            location = headers.get("location", "")
            dedup_key = (ip, location or headers.get("usn", ""))
            if dedup_key in seen_locations:
                continue
            seen_locations.add(dedup_key)
            results.append((ip, headers))
    return results


def _run_xbox_udp_discovery_sync(
    broadcast_targets: list[str],
    *,
    timeout_sec: float = 0.8,
) -> list[DiscoveredDevice]:
    packet = _build_xbox_discovery_packet()
    found: list[DiscoveredDevice] = []
    seen_ips: set[str] = set()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.15)
        for target in broadcast_targets:
            with contextlib.suppress(OSError):
                sock.sendto(packet, (target, _XBOX_SMARTGLASS_PORT))

        deadline = time.monotonic() + max(0.3, timeout_sec)
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            ip = str(addr[0])
            if ip in seen_ips:
                continue
            parsed = _parse_xbox_discovery_response(data, ip)
            if parsed is not None:
                seen_ips.add(ip)
                found.append(parsed)
    return found


class NetworkDiscoveryService:
    """
    Servicio híbrido de descubrimiento LAN sin IPs ni MACs hardcodeadas:
    1. Pasivo: mDNS (`zeroconf`) + SSDP (`M-SEARCH` + XML descriptivo).
    2. Activo: lectura de tabla ARP + sondeo concurrente TCP/HTTP sobre subred local + Xbox UDP.
    3. Resolución MAC en cascada (ARP -> Win32 `SendARP` -> metadata del dispositivo) para WoL.
    4. Deduplicación inteligente y persistencia en `DeviceAliasStore`.
    """

    def __init__(
        self,
        store: DeviceAliasStore,
        *,
        max_concurrency: int = 48,
    ) -> None:
        self._store = store
        self._max_concurrency = max(8, max_concurrency)

    async def scan(
        self,
        *,
        timeout_sec: float = 2.5,
        active_subnet_scan: bool = True,
        persist: bool = True,
    ) -> list[NetworkDeviceRecord]:
        """Ejecuta un escaneo híbrido completo con cancelación limpia y timeouts acotados."""
        local_nets = await asyncio.to_thread(get_local_ipv4_networks)
        local_ips = {ip for ip, _ in local_nets}
        arp_table = await asyncio.to_thread(read_system_arp_table, timeout_sec=1.2)

        mdns_timeout = min(1.8, max(0.5, timeout_sec * 0.65))
        ssdp_timeout = min(1.5, max(0.4, timeout_sec * 0.55))

        bcast_targets = ["255.255.255.255"]
        for _, net in local_nets:
            bcast = str(net.broadcast_address)
            if bcast not in bcast_targets:
                bcast_targets.append(bcast)

        # Fase 1: descubrimiento pasivo + Xbox UDP en paralelo
        mdns_task = asyncio.create_task(self._discover_mdns(mdns_timeout))
        ssdp_task = asyncio.create_task(self._discover_ssdp(ssdp_timeout))
        xbox_task = asyncio.create_task(self._discover_xbox(bcast_targets, min(0.8, ssdp_timeout)))

        candidates: list[DiscoveredDevice] = []
        try:
            passive_results = await asyncio.gather(
                mdns_task,
                ssdp_task,
                xbox_task,
                return_exceptions=True,
            )
            for item in passive_results:
                if isinstance(item, list):
                    candidates.extend(item)

            # Fase 2: sondeo activo TCP/HTTP sobre hosts ARP + subred local
            probe_ips = self._collect_probe_targets(
                local_nets=local_nets,
                local_ips=local_ips,
                arp_table=arp_table,
                existing_candidates=candidates,
                include_full_subnet=active_subnet_scan,
            )
            if probe_ips:
                active_found = await self._active_probe_hosts(
                    probe_ips,
                    timeout_sec=min(0.85, max(0.35, timeout_sec * 0.35)),
                )
                candidates.extend(active_found)
        finally:
            for task in (mdns_task, ssdp_task, xbox_task):
                if not task.done():
                    task.cancel()

        # Releer ARP tras el sondeo activo para capturar MACs recién resueltas por el OS
        refreshed_arp = await asyncio.to_thread(read_system_arp_table, timeout_sec=0.9)
        merged_arp = {**arp_table, **refreshed_arp}

        # Fase 3: resolución MAC en cascada + deduplicación
        enriched = await self._resolve_macs_cascade(candidates, merged_arp)
        deduped = self.deduplicate_devices(enriched)

        # Fase 4: persistir en SQLite preservando alias elegidos por el usuario
        records: list[NetworkDeviceRecord] = []
        for dev in deduped:
            meta = dict(dev.metadata)
            meta.setdefault("friendly_name", dev.friendly_name)
            if dev.manufacturer:
                meta.setdefault("manufacturer", dev.manufacturer)
            if dev.model:
                meta.setdefault("model", dev.model)
            if dev.discovery_sources:
                meta["discovery_sources"] = list(dev.discovery_sources)

            rec = NetworkDeviceRecord(
                device_uid=dev.device_uid,
                alias=dev.friendly_name or f"{dev.device_type.value} ({dev.ip})",
                device_type=dev.device_type,
                last_ip=dev.ip,
                mac_address=dev.mac_address,
                port=dev.port,
                metadata=meta,
                online=True,
            )
            if persist:
                rec = self._store.upsert_device(rec, preserve_alias=True).with_updates(online=True)
            records.append(rec)

        return records

    async def _discover_mdns(self, timeout_sec: float) -> list[DiscoveredDevice]:
        return await asyncio.to_thread(_run_mdns_browse_sync, timeout_sec)

    async def _discover_xbox(
        self,
        broadcast_targets: list[str],
        timeout_sec: float,
    ) -> list[DiscoveredDevice]:
        return await asyncio.to_thread(
            _run_xbox_udp_discovery_sync,
            broadcast_targets,
            timeout_sec=timeout_sec,
        )

    async def _discover_ssdp(self, timeout_sec: float) -> list[DiscoveredDevice]:
        raw_hits = await asyncio.to_thread(_run_ssdp_msearch_sync, timeout_sec)
        if not raw_hits:
            return []

        found: list[DiscoveredDevice] = []
        sem = asyncio.Semaphore(16)
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.9, connect=0.45)) as client:
            async def _enrich_hit(ip: str, headers: dict[str, str]) -> DiscoveredDevice | None:
                location = headers.get("location", "")
                st = headers.get("st", "")
                server = headers.get("server", "")
                usn = headers.get("usn", "")

                xml_info: dict[str, str] = {}
                port = 0
                if location:
                    parsed_url = urlparse(location)
                    if parsed_url.port:
                        port = parsed_url.port
                    if parsed_url.scheme in ("http", "https"):
                        async with sem:
                            with contextlib.suppress(httpx.HTTPError, OSError):
                                resp = await client.get(location)
                                if resp.status_code == 200 and "<" in resp.text:
                                    xml_info = _parse_upnp_device_xml(resp.text)

                friendly = xml_info.get("friendly_name", "")
                manufacturer = xml_info.get("manufacturer", "")
                model = xml_info.get("model", "")
                dev_type = _classify_device_type(
                    friendly_name=friendly,
                    manufacturer=manufacturer,
                    model=model,
                    server=server,
                    st=st,
                )
                # Filtrar ruido SSDP irrelevante (routers/IGD genéricos sin perfil media/TV)
                if dev_type == DeviceType.GENERIC and not any(
                    hint in f"{st} {server} {friendly} {model}".lower()
                    for hint in ("tv", "cast", "media", "render", "dial", "xbox", "speaker")
                ):
                    return None

                native_id = xml_info.get("udn") or usn.split("::", 1)[0]
                return DiscoveredDevice(
                    device_uid=_build_device_uid(
                        device_type=dev_type,
                        native_id=native_id,
                        ip=ip,
                    ),
                    friendly_name=friendly or f"{dev_type.value} ({ip})",
                    device_type=dev_type,
                    ip=ip,
                    port=port,
                    manufacturer=manufacturer,
                    model=model,
                    discovery_sources=("ssdp",),
                    metadata={
                        "ssdp_location": location,
                        "ssdp_st": st,
                        "ssdp_server": server,
                        "native_id": native_id,
                    },
                )

            enriched = await asyncio.gather(
                *(_enrich_hit(ip, hdrs) for ip, hdrs in raw_hits),
                return_exceptions=True,
            )
            for res in enriched:
                if isinstance(res, DiscoveredDevice):
                    found.append(res)
        return found

    def _collect_probe_targets(
        self,
        *,
        local_nets: list[tuple[str, ipaddress.IPv4Network]],
        local_ips: set[str],
        arp_table: dict[str, str],
        existing_candidates: list[DiscoveredDevice],
        include_full_subnet: bool,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set(local_ips)

        def _add(ip: str) -> None:
            if ip and ip not in seen:
                seen.add(ip)
                ordered.append(ip)

        for cand in existing_candidates:
            _add(cand.ip)
        for ip in arp_table:
            _add(ip)

        if include_full_subnet:
            for _, net in local_nets:
                for host in net.hosts():
                    _add(str(host))

        return ordered

    async def _probe_tcp_port(self, ip: str, port: int, timeout_sec: float) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=timeout_sec,
            )
            _ = reader
            writer.close()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.15)
            return True
        except (OSError, TimeoutError):
            return False

    async def _active_probe_hosts(
        self,
        ips: list[str],
        *,
        timeout_sec: float = 0.65,
    ) -> list[DiscoveredDevice]:
        sem = asyncio.Semaphore(self._max_concurrency)
        tcp_timeout = min(0.35, max(0.15, timeout_sec * 0.55))
        http_timeout = httpx.Timeout(timeout_sec, connect=tcp_timeout)
        found: list[DiscoveredDevice] = []

        async with httpx.AsyncClient(timeout=http_timeout) as client:
            async def _probe_single_ip(ip: str) -> list[DiscoveredDevice]:
                async with sem:
                    hits: list[DiscoveredDevice] = []
                    # Sondeo rápido paralelo de puertos conocidos de Smart TV / Cast
                    has_8001, has_8008 = await asyncio.gather(
                        self._probe_tcp_port(ip, 8001, tcp_timeout),
                        self._probe_tcp_port(ip, 8008, tcp_timeout),
                    )
                    if has_8001:
                        sam = await self._probe_samsung_api(client, ip)
                        if sam is not None:
                            hits.append(sam)
                    if has_8008:
                        cast = await self._probe_cast_http(client, ip)
                        if cast is not None:
                            hits.append(cast)
                    return hits

            results = await asyncio.gather(
                *(_probe_single_ip(ip) for ip in ips),
                return_exceptions=True,
            )
            for item in results:
                if isinstance(item, list):
                    found.extend(item)
        return found

    async def _probe_samsung_api(
        self,
        client: httpx.AsyncClient,
        ip: str,
    ) -> DiscoveredDevice | None:
        with contextlib.suppress(httpx.HTTPError, ValueError, OSError):
            resp = await client.get(f"http://{ip}:8001/api/v2/")
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if not isinstance(payload, dict):
                return None
            dev = payload.get("device")
            dev_info = dev if isinstance(dev, dict) else {}
            name = str(payload.get("name") or dev_info.get("name") or f"Samsung TV ({ip})")
            native_id = str(payload.get("id") or dev_info.get("id") or "")
            mac = normalize_mac(str(dev_info.get("wifiMac") or ""))
            model = str(dev_info.get("modelName") or dev_info.get("model") or "")
            token_auth = str(dev_info.get("TokenAuthSupport") or "").lower() == "true"
            port = 8002 if token_auth else 8001
            meta: dict[str, Any] = {
                "friendly_name": name,
                "model": model,
                "os": str(dev_info.get("OS") or "Tizen"),
                "token_auth_support": token_auth,
                "power_state": str(dev_info.get("PowerState") or ""),
                "native_id": native_id,
            }
            if mac:
                meta["wifi_mac"] = mac

            return DiscoveredDevice(
                device_uid=_build_device_uid(
                    device_type=DeviceType.SAMSUNG_TV,
                    mac_address=mac,
                    native_id=native_id,
                    ip=ip,
                ),
                friendly_name=name,
                device_type=DeviceType.SAMSUNG_TV,
                ip=ip,
                port=port,
                mac_address=mac,
                manufacturer="Samsung",
                model=model,
                discovery_sources=("active_http",),
                metadata=meta,
            )
        return None

    async def _probe_cast_http(
        self,
        client: httpx.AsyncClient,
        ip: str,
    ) -> DiscoveredDevice | None:
        with contextlib.suppress(httpx.HTTPError, ValueError, OSError):
            resp = await client.get(f"http://{ip}:8008/setup/eureka_info")
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and (payload.get("name") or payload.get("ssdp_udn")):
                    name = str(payload.get("name") or f"Chromecast ({ip})")
                    mac = normalize_mac(str(payload.get("mac_address") or ""))
                    native_id = str(payload.get("ssdp_udn") or "")
                    return DiscoveredDevice(
                        device_uid=_build_device_uid(
                            device_type=DeviceType.CHROMECAST,
                            mac_address=mac,
                            native_id=native_id,
                            ip=ip,
                        ),
                        friendly_name=name,
                        device_type=DeviceType.CHROMECAST,
                        ip=ip,
                        port=8009,
                        mac_address=mac,
                        manufacturer="Google",
                        model=str(payload.get("build_version") or "Chromecast"),
                        discovery_sources=("active_http",),
                        metadata={
                            "friendly_name": name,
                            "native_id": native_id,
                        },
                    )

        with contextlib.suppress(httpx.HTTPError, OSError):
            resp_xml = await client.get(f"http://{ip}:8008/ssdp/device-desc.xml")
            if resp_xml.status_code == 200 and "<" in resp_xml.text:
                xml_info = _parse_upnp_device_xml(resp_xml.text)
                friendly = xml_info.get("friendly_name", "")
                if friendly:
                    dev_type = _classify_device_type(
                        friendly_name=friendly,
                        manufacturer=xml_info.get("manufacturer", ""),
                        model=xml_info.get("model", ""),
                    )
                    if dev_type == DeviceType.GENERIC:
                        dev_type = DeviceType.CHROMECAST
                    return DiscoveredDevice(
                        device_uid=_build_device_uid(
                            device_type=dev_type,
                            native_id=xml_info.get("udn", ""),
                            ip=ip,
                        ),
                        friendly_name=friendly,
                        device_type=dev_type,
                        ip=ip,
                        port=8009 if dev_type == DeviceType.CHROMECAST else 8008,
                        manufacturer=xml_info.get("manufacturer", ""),
                        model=xml_info.get("model", ""),
                        discovery_sources=("active_http",),
                        metadata={"friendly_name": friendly, "native_id": xml_info.get("udn", "")},
                    )
        return None

    async def _resolve_macs_cascade(
        self,
        candidates: list[DiscoveredDevice],
        arp_table: dict[str, str],
    ) -> list[DiscoveredDevice]:
        """Resolución MAC en cascada: candidato -> tabla ARP -> Win32 `SendARP`."""
        resolved_cache: dict[str, str] = dict(arp_table)
        missing_ips = sorted(
            {
                cand.ip
                for cand in candidates
                if cand.ip and not normalize_mac(cand.mac_address) and cand.ip not in resolved_cache
            }
        )
        if missing_ips and sys.platform == "win32":
            mac_results = await asyncio.gather(
                *(asyncio.to_thread(resolve_mac_via_sendarp, ip) for ip in missing_ips),
                return_exceptions=True,
            )
            for ip, mac_res in zip(missing_ips, mac_results, strict=True):
                if isinstance(mac_res, str) and mac_res:
                    resolved_cache[ip] = mac_res

        enriched: list[DiscoveredDevice] = []
        for cand in candidates:
            arp_mac = resolved_cache.get(cand.ip, "")
            cand_mac = normalize_mac(cand.mac_address)
            primary_mac = arp_mac or cand_mac

            meta = dict(cand.metadata)
            existing_alts: list[str] = list(meta.get("alt_macs", []))
            for m in (cand_mac, meta.get("wifi_mac", "")):
                clean_m = normalize_mac(m)
                if clean_m and clean_m != primary_mac and clean_m not in existing_alts:
                    existing_alts.append(clean_m)
            if existing_alts:
                meta["alt_macs"] = sorted(existing_alts)

            uid = cand.device_uid
            if primary_mac and not uid.startswith("mac:"):
                uid = _build_device_uid(device_type=cand.device_type, mac_address=primary_mac, ip=cand.ip)
            enriched.append(
                DiscoveredDevice(
                    device_uid=uid,
                    friendly_name=cand.friendly_name,
                    device_type=cand.device_type,
                    ip=cand.ip,
                    port=cand.port,
                    mac_address=primary_mac,
                    manufacturer=cand.manufacturer,
                    model=cand.model,
                    discovery_sources=cand.discovery_sources,
                    metadata=meta,
                )
            )
        return enriched

    @staticmethod
    def deduplicate_devices(candidates: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
        """Deduplica dispositivos detectados por múltiples protocolos (mDNS + SSDP + HTTP/ARP)."""
        merged: dict[str, DiscoveredDevice] = {}

        for cand in candidates:
            existing_key_by_ip: str | None = None
            if cand.ip:
                for existing_key, existing_dev in merged.items():
                    if existing_dev.ip == cand.ip:
                        existing_key_by_ip = existing_key
                        break

            if existing_key_by_ip is not None:
                key = existing_key_by_ip
            else:
                key = (
                    f"mac:{cand.mac_address}"
                    if cand.mac_address
                    else f"ip:{cand.ip}:{cand.device_type.value}"
                    if cand.ip
                    else cand.device_uid
                )

            existing = merged.get(key)
            if existing is None:
                merged[key] = cand
                continue

            # Elegir el DeviceType más específico según prioridad
            existing_prio = _DEVICE_TYPE_PRIORITY.get(existing.device_type, 0)
            cand_prio = _DEVICE_TYPE_PRIORITY.get(cand.device_type, 0)
            best_type = cand.device_type if cand_prio > existing_prio else existing.device_type

            # Elegir el nombre más descriptivo (evitando fallback tipo `generic (ip)`)
            best_name = existing.friendly_name
            should_take_cand_name = bool(
                cand.friendly_name
                and (
                    cand_prio > existing_prio
                    or (
                        (
                            not best_name
                            or best_name.startswith(f"{existing.device_type.value} (")
                            or f"({existing.ip})" in best_name
                        )
                        and f"({cand.ip})" not in cand.friendly_name
                    )
                )
            )
            if should_take_cand_name:
                best_name = cand.friendly_name

            best_mac = existing.mac_address or cand.mac_address
            combined_meta = {**existing.metadata, **cand.metadata}
            alt_macs_set = set(combined_meta.get("alt_macs", []))
            for m in (existing.mac_address, cand.mac_address, combined_meta.get("wifi_mac", "")):
                clean_m = normalize_mac(m)
                if clean_m and clean_m != best_mac:
                    alt_macs_set.add(clean_m)
            if alt_macs_set:
                combined_meta["alt_macs"] = sorted(alt_macs_set)

            best_ip = existing.ip or cand.ip
            best_port = cand.port if (cand_prio > existing_prio and cand.port) else (existing.port or cand.port)
            best_uid = (
                f"mac:{best_mac.replace(':', '').lower()}"
                if best_mac
                else (cand.device_uid if cand_prio > existing_prio else existing.device_uid)
            )
            combined_sources = tuple(sorted(set(existing.discovery_sources) | set(cand.discovery_sources)))

            merged[key] = DiscoveredDevice(
                device_uid=best_uid,
                friendly_name=best_name,
                device_type=best_type,
                ip=best_ip,
                port=best_port,
                mac_address=best_mac,
                manufacturer=existing.manufacturer or cand.manufacturer,
                model=existing.model or cand.model,
                discovery_sources=combined_sources,
                metadata=combined_meta,
            )

        return sorted(merged.values(), key=lambda d: (d.friendly_name.lower(), d.ip))
