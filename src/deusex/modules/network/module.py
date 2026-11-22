# FILE: src/deusex/modules/network/module.py
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import re
import time
from typing import Any

from deusex.core.contracts import (
    BaseModule,
    DiagnosticItem,
    DiagnosticSeverity,
    FastPathRule,
    ModuleContext,
    ModuleState,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from deusex.core.permissions import Permission
from deusex.modules.network.adapters import (
    AdapterActionResult,
    DeviceCapability,
    DeviceType,
    NetworkDeviceAdapter,
    NetworkDeviceRecord,
    normalize_mac,
    send_wake_on_lan,
)
from deusex.modules.network.cast import ChromecastAdapter
from deusex.modules.network.discovery import (
    NetworkDiscoveryService,
    get_local_ipv4_networks,
)
from deusex.modules.network.miracast import MiracastAdapter
from deusex.modules.network.samsung import SamsungTizenAdapter
from deusex.modules.network.store import DeviceAliasStore
from deusex.modules.network.xbox import XboxSmartGlassAdapter
from deusex.modules.network.youtube_tv import (
    ActiveBrowserUrlReader,
    YouTubeTVAdapter,
    extract_youtube_video_id,
)

logger = logging.getLogger(__name__)

_SCAN_RE = re.compile(
    r"^(?:escanea(?:r)?|busca(?:r)?|detecta(?:r)?)\s+(?:la\s+)?"
    r"(?:red(?:\s+local)?|dispositivos(?:\s+(?:de\s+red|lan|en\s+la\s+red))?)\s*$",
    re.IGNORECASE,
)

_SEND_YOUTUBE_RE = re.compile(
    r"^(?:manda(?:r)?|env[ií]a(?:r)?|pon(?:er)?|reproduc(?:e|ir))\s+"
    r"(?:este\s+|el\s+)?v[ií]deo(?:\s+de\s+youtube)?\s+"
    r"(?:a|en)\s+(?:la\s+|el\s+)?(?P<target>.+?)\s*$",
    re.IGNORECASE,
)

_PROJECT_CONNECT_RE = re.compile(
    r"^(?P<mode>proyecta(?:r)?|transmite|transmitir|duplica(?:r)?|extiende|extender)\s+"
    r"(?:mi\s+|la\s+)?pantalla(?:\s+(?:a|en|hacia)\s+(?:la\s+|el\s+)?(?P<target>.+?))?\s*$",
    re.IGNORECASE,
)

_PROJECT_DISCONNECT_RE = re.compile(
    r"^(?:deja(?:r)?\s+de\s+proyectar|desconecta(?:r)?\s+(?:la\s+)?(?:pantalla|proyecci[oó]n|miracast))\s*$",
    re.IGNORECASE,
)

_POWER_RE = re.compile(
    r"^(?P<verb>enciende|encender|prende|prender|apaga|apagar)\s+"
    r"(?:la\s+|el\s+)?(?P<target>(?:tv|tele|televisor|samsung|xbox|consola)(?:\s+[\w-]+){0,3})\s*$",
    re.IGNORECASE,
)

_VOLUME_DELTA_RE = re.compile(
    r"^(?P<verb>sube|subir|baja|bajar|silencia|silenciar|mutea|mutear)\s+"
    r"(?:(?:el\s+)?volumen\s+(?:de\s+(?:la\s+|el\s+)?|en\s+(?:la\s+|el\s+)?)?)?"
    r"(?P<target>(?:tv|tele|televisor|samsung|chromecast)(?:\s+[\w-]+){0,3})"
    r"(?:\s+(?:en\s+)?(?P<amount>\d+))?\s*$",
    re.IGNORECASE,
)

_VOLUME_LEVEL_RE = re.compile(
    r"^(?:pon(?:er)?\s+)?(?:el\s+)?volumen\s+(?:de\s+|en\s+)(?:la\s+|el\s+)?"
    r"(?P<target>(?:tv|tele|televisor|samsung|chromecast)(?:\s+[\w-]+){0,3})\s+"
    r"(?:al?\s+)?(?P<level>\d{1,3})\s*%?\s*$",
    re.IGNORECASE,
)

_MEDIA_CAST_RE = re.compile(
    r"^(?P<verb>pausa|pausar|reanuda|reanudar|contin[uú]a|continuar|det[eé]n|detener|para|parar)\s+"
    r"(?:(?:la\s+)?(?:reproducci[oó]n|pel[ií]cula|serie|m[uú]sica|v[ií]deo)\s+(?:en\s+)?)?"
    r"(?:el\s+|la\s+)?(?P<target>(?:cast|chromecast|tv|tele|televisor|youtube\s+tv)(?:\s+[\w-]+){0,3})\s*$",
    re.IGNORECASE,
)

_GENERIC_TARGET_TOKENS: frozenset[str] = frozenset(
    {
        "tv",
        "tele",
        "televisor",
        "pantalla",
        "cast",
        "chromecast",
        "xbox",
        "consola",
        "youtube tv",
    }
)


def _clean_target_query(raw: str | None) -> tuple[str, tuple[DeviceType, ...] | None]:
    """Limpia un destino hablado y deduce filtros de tipo cuando el usuario usa nombres genéricos."""
    text = (raw or "").strip()
    lower = text.lower()
    if not lower:
        return "", None
    if lower in ("tv", "tele", "televisor", "pantalla"):
        return "", (
            DeviceType.SAMSUNG_TV,
            DeviceType.CHROMECAST,
            DeviceType.ANDROID_TV,
            DeviceType.LG_WEBOS,
            DeviceType.YOUTUBE_TV,
            DeviceType.MIRACAST,
        )
    if lower in ("cast", "chromecast"):
        return "", (DeviceType.CHROMECAST, DeviceType.ANDROID_TV)
    if lower in ("xbox", "consola"):
        return "", (DeviceType.XBOX,)
    if lower == "youtube tv":
        return "", (
            DeviceType.YOUTUBE_TV,
            DeviceType.SAMSUNG_TV,
            DeviceType.CHROMECAST,
            DeviceType.ANDROID_TV,
            DeviceType.LG_WEBOS,
        )
    return text, None


def _adapter_result_to_tool(res: AdapterActionResult, *, dry_run: bool = False) -> ToolResult:
    return ToolResult(
        ok=res.ok,
        summary=res.message,
        data=dict(res.data),
        error_code=res.error_code,
        spoken_text=res.message,
        dry_run=dry_run,
    )


class NetworkModule(BaseModule):
    """
    Módulo enchufable de Red Local (Rebanada 3):
    - Descubrimiento híbrido sin IPs fijas ni MACs hardcodeadas (`NetworkDiscoveryService`).
    - Adaptadores extensibles: Samsung Tizen, Chromecast, Miracast (UIA STA), YouTube TV y Xbox.
    - Persistencia de alias en SQLite WAL y tokens aislados en `.env`.
    """

    module_id = "network"
    display_name = "Red Local y Smart TV"
    description = (
        "Descubrimiento híbrido LAN (mDNS/SSDP/ARP) y control de Samsung TV, "
        "Chromecast, Miracast, YouTube TV y Xbox sin IPs fijas."
    )
    required_permissions = (
        Permission.NETWORK_DISCOVERY,
        Permission.NETWORK_CONTROL,
    )

    def __init__(self) -> None:
        super().__init__()
        self._ctx: ModuleContext | None = None
        self._store: DeviceAliasStore | None = None
        self._discovery: NetworkDiscoveryService | None = None
        self._browser_reader: ActiveBrowserUrlReader | None = None
        self._samsung: SamsungTizenAdapter | None = None
        self._cast: ChromecastAdapter | None = None
        self._miracast: MiracastAdapter | None = None
        self._youtube_tv: YouTubeTVAdapter | None = None
        self._xbox: XboxSmartGlassAdapter | None = None
        self._adapters: list[NetworkDeviceAdapter] = []
        self._online_uids: set[str] = set()
        self._active_scan_task: asyncio.Task[list[NetworkDeviceRecord]] | None = None

    @property
    def store(self) -> DeviceAliasStore | None:
        return self._store

    def register_adapter(self, adapter: NetworkDeviceAdapter) -> None:
        """Permite registrar adaptadores futuros (LG WebOS, luces, Home Assistant) sin tocar el núcleo."""
        if adapter not in self._adapters:
            self._adapters.append(adapter)

    async def start(self, ctx: ModuleContext) -> None:
        self.set_state(ModuleState.STARTING)
        self._ctx = ctx
        self._store = DeviceAliasStore(ctx.db, ctx.secrets)
        self._discovery = NetworkDiscoveryService(self._store)
        self._browser_reader = ActiveBrowserUrlReader(ctx.runtime.com_sta)

        self._samsung = SamsungTizenAdapter(self._store)
        self._cast = ChromecastAdapter()
        self._miracast = MiracastAdapter(ctx.runtime.com_sta)
        self._youtube_tv = YouTubeTVAdapter(self._store, self._browser_reader)
        self._xbox = XboxSmartGlassAdapter(self._store)

        self._adapters = [
            self._samsung,
            self._cast,
            self._xbox,
            self._youtube_tv,
            self._miracast,
        ]
        self._online_uids.clear()
        self.set_state(ModuleState.ACTIVE)
        logger.info("NetworkModule iniciado con %d adaptadores LAN.", len(self._adapters))

    async def stop(self) -> None:
        self.set_state(ModuleState.STOPPING)

        scan_task = self._active_scan_task
        self._active_scan_task = None
        if scan_task is not None and not scan_task.done():
            scan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(scan_task, timeout=0.5)

        adapters_to_close = list(self._adapters)
        self._adapters.clear()
        if adapters_to_close:
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    asyncio.gather(
                        *(adapter.close() for adapter in adapters_to_close),
                        return_exceptions=True,
                    ),
                    timeout=1.0,
                )

        self._samsung = None
        self._cast = None
        self._miracast = None
        self._youtube_tv = None
        self._xbox = None
        self._discovery = None
        self._browser_reader = None
        self._store = None
        self._ctx = None
        self._online_uids.clear()
        self.set_state(ModuleState.DISABLED)

    # ------------------------------------------------------------------
    # Utilidades internas y resolución de dispositivos / adaptadores
    # ------------------------------------------------------------------
    def _is_dry_run(self) -> bool:
        return bool(self._ctx is not None and self._ctx.settings.developer.dry_run)

    def _check_ready(self, permission: Permission) -> ToolResult | None:
        if self.state != ModuleState.ACTIVE or self._ctx is None or self._store is None:
            return ToolResult(
                ok=False,
                summary="El módulo de Red Local está desactivado.",
                error_code="module_inactive",
            )
        if not self._ctx.settings.is_permission_enabled(permission):
            return ToolResult(
                ok=False,
                summary=f"Permiso '{permission.value}' desactivado en la configuración.",
                error_code="permission_denied",
            )
        return None

    def list_known_devices(self) -> list[NetworkDeviceRecord]:
        """Devuelve los dispositivos guardados en `device_aliases` enriquecidos con estado online de la sesión."""
        if self._store is None:
            return []
        devices = self._store.list_devices()
        return [
            dev.with_updates(online=(dev.device_uid in self._online_uids))
            for dev in devices
        ]

    def _format_device_not_found(self, query: str = "") -> str:
        """Devuelve un mensaje amigable indicando dispositivos LAN guardados en SQLite si existen."""
        if self._store is None:
            return "Módulo de Red Local no disponible."
        known = self._store.list_devices()
        if not known:
            return "No encontré ningún dispositivo LAN guardado. Escanea la red primero."
        names = ", ".join(f"'{d.alias}'" for d in known[:5])
        suffix = "…" if len(known) > 5 else ""
        q_desc = f"'{query}'" if query.strip() else "el dispositivo solicitado"
        return f"No encontré {q_desc}. Dispositivos guardados: {names}{suffix}."

    def _resolve_device(
        self,
        query: str = "",
        *,
        preferred_types: tuple[DeviceType, ...] | None = None,
        required_capability: DeviceCapability | None = None,
    ) -> NetworkDeviceRecord | None:
        if self._store is None:
            return None
        clean_query, inferred_types = _clean_target_query(query)
        types_filter = preferred_types or inferred_types

        all_devices = self._store.list_devices()
        if not all_devices:
            logger.warning("No se pudo resolver dispositivo: base SQLite vacía (query='%s')", query)
            return None

        def _matches_cap(cand: NetworkDeviceRecord) -> bool:
            if required_capability is None:
                return True
            if self._select_adapter(cand, required_capability) is not None:
                return True
            return bool(
                required_capability == DeviceCapability.POWER_ON
                and normalize_mac(cand.mac_address)
            )

        dev: NetworkDeviceRecord | None = None
        is_generic = not clean_query

        if is_generic:
            # 1. Si existe favorito que cumpla tipo y capacidad, SIEMPRE el favorito
            for cand in all_devices:
                if (
                    cand.is_favorite
                    and (types_filter is None or cand.device_type in types_filter)
                    and _matches_cap(cand)
                ):
                    dev = cand
                    break

            # 2. Si no hay favorito y es orden de TV ("tv", "tele", etc.), priorizar Smart TVs reales antes de Chromecast
            if dev is None and types_filter is not None and DeviceType.SAMSUNG_TV in types_filter:
                smart_tv_prio = (
                    DeviceType.SAMSUNG_TV,
                    DeviceType.LG_WEBOS,
                    DeviceType.ANDROID_TV,
                    DeviceType.YOUTUBE_TV,
                    DeviceType.MIRACAST,
                )
                for st_type in smart_tv_prio:
                    for cand in all_devices:
                        if cand.device_type == st_type and _matches_cap(cand):
                            dev = cand
                            break
                    if dev is not None:
                        break

            # 3. Candidato genérico con filtro de tipo y capacidad
            if dev is None:
                for cand in all_devices:
                    if (types_filter is None or cand.device_type in types_filter) and _matches_cap(cand):
                        dev = cand
                        break

            # 4. Fallback final sin filtro de tipo respetando capacidad
            if dev is None:
                for cand in all_devices:
                    if _matches_cap(cand):
                        dev = cand
                        break
        else:
            # Búsqueda específica por término (ej. "tv samsung", "dormitorio", "smart tv diego")
            q_cand = self._store.find_by_query(clean_query, device_types=types_filter)
            if q_cand is not None and (_matches_cap(q_cand) or required_capability is None):
                dev = q_cand
            else:
                q_cand_any = self._store.find_by_query(clean_query)
                if q_cand_any is not None and (_matches_cap(q_cand_any) or required_capability is None):
                    dev = q_cand_any

        if dev is None:
            logger.warning(
                "No se pudo resolver dispositivo para query '%s' (limpio='%s', tipos=%s, cap=%s)",
                query,
                clean_query,
                types_filter,
                required_capability,
            )
            return None

        enriched = dev.with_updates(online=(dev.device_uid in self._online_uids))
        logger.info(
            "Dispositivo resuelto para query '%s': '%s' (tipo=%s, fav=%s, online=%s)",
            query,
            enriched.alias,
            enriched.device_type.value,
            enriched.is_favorite,
            enriched.online,
        )
        return enriched

    def _select_adapter(
        self,
        device: NetworkDeviceRecord,
        capability: DeviceCapability,
    ) -> NetworkDeviceAdapter | None:
        for adapter in self._adapters:
            if adapter.supports(device) and adapter.has_capability(capability):
                return adapter
        return None

    # ------------------------------------------------------------------
    # Operaciones públicas (usadas por Tools, Fast-Path y UI LAN)
    # ------------------------------------------------------------------
    async def scan_network(
        self,
        *,
        timeout_sec: float = 2.5,
        active_subnet_scan: bool = True,
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_DISCOVERY)
        if err is not None:
            return err
        assert self._discovery is not None

        if self._is_dry_run():
            known = self.list_known_devices()
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Escaneo LAN simulado ({len(known)} dispositivos en caché).",
                data={"devices": [d.to_public_dict() for d in known], "count": len(known)},
                spoken_text="Escaneo de red simulado en modo pruebas.",
                dry_run=True,
            )

        clamped_timeout = max(0.6, min(float(timeout_sec), 8.0))
        t0 = time.monotonic()
        logger.info(
            "Iniciando escaneo LAN (timeout=%.1fs, subred=%s)...",
            clamped_timeout,
            active_subnet_scan,
        )
        task = asyncio.create_task(
            self._discovery.scan(
                timeout_sec=clamped_timeout,
                active_subnet_scan=bool(active_subnet_scan),
                persist=True,
            )
        )
        self._active_scan_task = task
        try:
            detected = await task
        except asyncio.CancelledError:
            logger.warning("Escaneo LAN cancelado.")
            return ToolResult(
                ok=False,
                summary="Escaneo de red cancelado.",
                error_code="cancelled",
            )
        finally:
            if self._active_scan_task is task:
                self._active_scan_task = None

        self._online_uids = {dev.device_uid for dev in detected}
        all_devices = self.list_known_devices()
        count = len(detected)
        duration_ms = (time.monotonic() - t0) * 1000.0
        types_found = sorted({dev.device_type.value for dev in detected})
        logger.info(
            "Escaneo LAN finalizado en %.1f ms: %d dispositivos detectados %s (total guardados: %d)",
            duration_ms,
            count,
            types_found,
            len(all_devices),
        )
        if count == 0:
            summary = "No se detectaron dispositivos compatibles activos en la red local."
        elif count == 1:
            summary = f"Se detectó 1 dispositivo en la red local: {detected[0].alias}."
        else:
            names = ", ".join(dev.alias for dev in detected[:4])
            suffix = "…" if count > 4 else ""
            summary = f"Se detectaron {count} dispositivos en la red local: {names}{suffix}."

        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=summary,
            data={
                "detected_count": count,
                "total_saved": len(all_devices),
                "devices": [dev.to_public_dict() for dev in all_devices],
            },
        )

    async def rename_device(self, device_query: str, new_alias: str) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_DISCOVERY)
        if err is not None:
            return err
        assert self._store is not None

        target = self._resolve_device(device_query)
        if target is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en rename_device: %s", summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )
        clean_alias = new_alias.strip()
        if not clean_alias:
            return ToolResult(
                ok=False,
                summary="El nuevo alias no puede estar vacío.",
                error_code="invalid_alias",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Alias de '{target.alias}' cambiaría a '{clean_alias}'.",
                data={"device_uid": target.device_uid, "alias": clean_alias},
                dry_run=True,
            )

        updated = self._store.rename_alias(target.device_uid, clean_alias)
        if updated is None:
            logger.warning("Fallo en rename_device: error persistiendo en SQLite para '%s'", target.alias)
            return ToolResult(
                ok=False,
                summary="No se pudo actualizar el alias del dispositivo.",
                error_code="rename_failed",
            )
        summary = f"Dispositivo renombrado a '{updated.alias}'."
        logger.info("rename_device: '%s' -> '{%s}' (UID=%s, ok=True)", target.alias, updated.alias, target.device_uid)
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=summary,
            data={"device": updated.with_updates(online=target.online).to_public_dict()},
        )

    async def set_favorite_device(self, device_query: str, *, favorite: bool = True) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_DISCOVERY)
        if err is not None:
            return err
        assert self._store is not None

        target = self._resolve_device(device_query)
        if target is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en set_favorite_device: %s", summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            action_desc = "marcaría como favorito" if favorite else "desmarcaría de favoritos"
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Se {action_desc} a '{target.alias}'.",
                data={"device_uid": target.device_uid, "favorite": favorite},
                dry_run=True,
            )

        updated = self._store.set_favorite(target.device_uid, favorite=favorite)
        if updated is None:
            logger.warning("Fallo en set_favorite_device: error al persistir en SQLite para '%s'", target.alias)
            return ToolResult(
                ok=False,
                summary="No se pudo actualizar el dispositivo favorito.",
                error_code="favorite_update_failed",
            )

        action_lbl = "marcado como dispositivo favorito" if favorite else "desmarcado de favoritos"
        summary = f"'{updated.alias}' {action_lbl}."
        logger.info("set_favorite_device: '%s' %s (ok=True)", updated.alias, action_lbl)
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=summary,
            data={"device": updated.with_updates(online=target.online).to_public_dict(), "favorite": favorite},
        )

    async def control_power(self, device_query: str, action: str) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err

        clean_action = action.strip().lower()
        if clean_action not in ("on", "off"):
            return ToolResult(
                ok=False,
                summary="Acción de energía inválida; usa 'on' u 'off'.",
                error_code="invalid_action",
            )

        cap = DeviceCapability.POWER_ON if clean_action == "on" else DeviceCapability.POWER_OFF
        device = self._resolve_device(device_query, required_capability=cap)
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en control_power (%s): %s", clean_action, summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            verb = "encendería" if clean_action == "on" else "apagaría"
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Se {verb} '{device.alias}'.",
                data={"device_uid": device.device_uid, "action": clean_action},
                dry_run=True,
            )

        adapter = self._select_adapter(device, cap)

        if clean_action == "on":
            if adapter is not None:
                res = _adapter_result_to_tool(await adapter.power_on(device))
                logger.info("control_power (on) en '%s' (tipo=%s): ok=%s", device.alias, device.device_type.value, res.ok)
                if not res.ok:
                    logger.warning("control_power (on) falló en '%s': %s (code=%s)", device.alias, res.summary, res.error_code)
                return res

            # Fallback universal Wake-on-LAN si el dispositivo tiene MAC registrada
            mac = normalize_mac(device.mac_address)
            if mac:
                sent = await asyncio.to_thread(send_wake_on_lan, mac, target_ip=device.last_ip)
                msg = (
                    f"Señal Wake-on-LAN enviada a {device.alias}."
                    if sent
                    else f"No se pudo enviar Wake-on-LAN a {device.alias}."
                )
                logger.info("control_power (on/wol) en '%s' (MAC=%s): sent=%s", device.alias, mac, sent)
                return ToolResult(ok=sent, summary=msg, spoken_text=msg, error_code=None if sent else "wol_failed")

            logger.warning("control_power (on) falló en '%s': sin soporte remoto ni MAC para WoL", device.alias)
            return ToolResult(
                ok=False,
                summary=f"{device.alias} no soporta encendido remoto ni tiene MAC registrada.",
                error_code="unsupported_capability",
            )

        if adapter is None:
            logger.warning("control_power (off) falló en '%s': adaptador no soporta apagado remoto", device.alias)
            return ToolResult(
                ok=False,
                summary=f"{device.alias} no soporta apagado remoto con los adaptadores activos.",
                error_code="unsupported_capability",
            )
        res = _adapter_result_to_tool(await adapter.power_off(device))
        logger.info("control_power (off) en '%s' (tipo=%s): ok=%s", device.alias, device.device_type.value, res.ok)
        if not res.ok:
            logger.warning("control_power (off) falló en '%s': %s (code=%s)", device.alias, res.summary, res.error_code)
        return res

    async def control_volume(
        self,
        device_query: str = "",
        *,
        level: int | None = None,
        delta: int | None = None,
        mute: bool | None = None,
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err

        cap = DeviceCapability.MUTE if mute is not None else DeviceCapability.VOLUME
        device = self._resolve_device(
            device_query,
            preferred_types=(
                DeviceType.SAMSUNG_TV,
                DeviceType.CHROMECAST,
                DeviceType.ANDROID_TV,
                DeviceType.YOUTUBE_TV,
            ),
            required_capability=cap,
        )
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en control_volume: %s", summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Ajuste de volumen simulado en '{device.alias}'.",
                data={"device_uid": device.device_uid, "level": level, "delta": delta, "mute": mute},
                dry_run=True,
            )

        adapter = self._select_adapter(device, cap)
        if adapter is None:
            logger.warning("control_volume falló en '%s': sin soporte para %s", device.alias, cap.value)
            return ToolResult(
                ok=False,
                summary=f"{device.alias} no soporta control de volumen remoto.",
                error_code="unsupported_capability",
            )

        res = await adapter.set_volume(device, level=level, delta=delta, mute=mute)
        tool_res = _adapter_result_to_tool(res)
        logger.info("control_volume en '%s' (level=%s, delta=%s, mute=%s): ok=%s", device.alias, level, delta, mute, tool_res.ok)
        if not tool_res.ok:
            logger.warning("control_volume falló en '%s': %s (code=%s)", device.alias, tool_res.summary, tool_res.error_code)
        return tool_res

    async def control_media(
        self,
        device_query: str = "",
        *,
        action: str = "pause",
        media_url: str = "",
        title: str = "",
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err

        # Priorizar YouTube TV Lounge si la URL corresponde a YouTube
        if media_url and extract_youtube_video_id(media_url) is not None:
            return await self.send_youtube_to_tv(device_query, url_or_id=media_url)

        device = self._resolve_device(
            device_query,
            preferred_types=(
                DeviceType.CHROMECAST,
                DeviceType.SAMSUNG_TV,
                DeviceType.ANDROID_TV,
                DeviceType.YOUTUBE_TV,
            ),
            required_capability=DeviceCapability.MEDIA_PLAYBACK,
        )
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en control_media (%s): %s", action, summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Control multimedia '{action}' simulado en '{device.alias}'.",
                data={"device_uid": device.device_uid, "action": action, "media_url": media_url},
                dry_run=True,
            )

        adapter = self._select_adapter(device, DeviceCapability.MEDIA_PLAYBACK)
        if adapter is None:
            logger.warning("control_media (%s) falló en '%s': sin soporte multimedia", action, device.alias)
            return ToolResult(
                ok=False,
                summary=f"{device.alias} no soporta control de reproducción multimedia.",
                error_code="unsupported_capability",
            )

        res = await adapter.media_control(device, action, media_url=media_url, title=title)
        tool_res = _adapter_result_to_tool(res)
        logger.info("control_media (%s) en '%s': ok=%s", action, device.alias, tool_res.ok)
        if not tool_res.ok:
            logger.warning("control_media falló en '%s': %s (code=%s)", device.alias, tool_res.summary, tool_res.error_code)
        return tool_res

    async def send_youtube_to_tv(
        self,
        device_query: str = "",
        *,
        url_or_id: str = "",
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err
        assert self._youtube_tv is not None

        device = self._resolve_device(
            device_query,
            preferred_types=(
                DeviceType.YOUTUBE_TV,
                DeviceType.SAMSUNG_TV,
                DeviceType.CHROMECAST,
                DeviceType.ANDROID_TV,
                DeviceType.LG_WEBOS,
            ),
        )
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en send_youtube_to_tv: %s", summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Se enviaría el video de YouTube a '{device.alias}'.",
                data={"device_uid": device.device_uid, "url": url_or_id},
                dry_run=True,
            )

        res = await self._youtube_tv.send_video(device, url_or_id=url_or_id)
        tool_res = _adapter_result_to_tool(res)
        logger.info("send_youtube_to_tv en '%s': ok=%s", device.alias, tool_res.ok)
        if not tool_res.ok:
            logger.warning("send_youtube_to_tv falló en '%s': %s (code=%s)", device.alias, tool_res.summary, tool_res.error_code)
        return tool_res

    async def pair_youtube_tv(self, device_query: str, pairing_code: str) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err
        assert self._youtube_tv is not None

        device = self._resolve_device(
            device_query,
            preferred_types=(
                DeviceType.YOUTUBE_TV,
                DeviceType.SAMSUNG_TV,
                DeviceType.CHROMECAST,
                DeviceType.ANDROID_TV,
                DeviceType.LG_WEBOS,
            ),
        )
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en pair_youtube_tv: %s", summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Vinculación de YouTube TV simulada para '{device.alias}'.",
                dry_run=True,
            )

        res = await self._youtube_tv.pair_with_code(device, pairing_code)
        tool_res = _adapter_result_to_tool(res)
        logger.info("pair_youtube_tv en '%s': ok=%s", device.alias, tool_res.ok)
        if not tool_res.ok:
            logger.warning("pair_youtube_tv falló en '%s': %s (code=%s)", device.alias, tool_res.summary, tool_res.error_code)
        return tool_res

    async def project_screen(
        self,
        device_query: str = "",
        *,
        action: str = "connect",
        display_mode: str = "extend",
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err
        assert self._miracast is not None

        clean_action = action.strip().lower()
        if clean_action in ("disconnect", "stop", "off"):
            if self._is_dry_run():
                return ToolResult(
                    ok=True,
                    summary="[Dry-Run] Desconexión de proyección Miracast simulada.",
                    dry_run=True,
                )
            disc_res = _adapter_result_to_tool(await self._miracast.disconnect_projection())
            logger.info("project_screen (disconnect): ok=%s", disc_res.ok)
            return disc_res

        device = self._resolve_device(
            device_query,
            preferred_types=(
                DeviceType.MIRACAST,
                DeviceType.SAMSUNG_TV,
                DeviceType.LG_WEBOS,
                DeviceType.ANDROID_TV,
            ),
        )
        if device is None:
            target_label = device_query.strip()
            if not target_label or target_label.lower() in _GENERIC_TARGET_TOKENS:
                summary = self._format_device_not_found(device_query)
                logger.warning("Fallo en project_screen: %s", summary)
                return ToolResult(
                    ok=False,
                    summary=summary,
                    error_code="device_not_found",
                )
            device = NetworkDeviceRecord(
                device_uid=f"miracast:{target_label.lower()}",
                alias=target_label,
                device_type=DeviceType.MIRACAST,
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Se proyectaría la pantalla hacia '{device.alias}' ({display_mode}).",
                data={"device_uid": device.device_uid, "display_mode": display_mode},
                dry_run=True,
            )

        res = await self._miracast.project_to_device(device, display_mode=display_mode)
        tool_res = _adapter_result_to_tool(res)
        logger.info("project_screen (connect) hacia '%s' (%s): ok=%s", device.alias, display_mode, tool_res.ok)
        if not tool_res.ok:
            logger.warning("project_screen falló hacia '%s': %s (code=%s)", device.alias, tool_res.summary, tool_res.error_code)
        return tool_res

    async def launch_tv_app(
        self,
        device_query: str = "",
        *,
        app: str = "",
        key: str = "",
    ) -> ToolResult:
        err = self._check_ready(Permission.NETWORK_CONTROL)
        if err is not None:
            return err

        device = self._resolve_device(
            device_query,
            preferred_types=(DeviceType.SAMSUNG_TV, DeviceType.LG_WEBOS, DeviceType.ANDROID_TV),
        )
        if device is None:
            summary = self._format_device_not_found(device_query)
            logger.warning("Fallo en launch_tv_app (%s/%s): %s", app, key, summary)
            return ToolResult(
                ok=False,
                summary=summary,
                error_code="device_not_found",
            )

        if self._is_dry_run():
            return ToolResult(
                ok=True,
                summary=f"[Dry-Run] Acción remota ({app or key}) simulada en '{device.alias}'.",
                dry_run=True,
            )

        if app.strip():
            adapter = self._select_adapter(device, DeviceCapability.LAUNCH_APP)
            if adapter is None:
                logger.warning("launch_tv_app falló en '%s': sin soporte para LAUNCH_APP", device.alias)
                return ToolResult(
                    ok=False,
                    summary=f"{device.alias} no soporta apertura remota de apps.",
                    error_code="unsupported_capability",
                )
            app_res = _adapter_result_to_tool(await adapter.launch_app(device, app.strip()))
            logger.info("launch_tv_app '%s' en '%s': ok=%s", app.strip(), device.alias, app_res.ok)
            if not app_res.ok:
                logger.warning("launch_tv_app '%s' falló en '%s': %s", app.strip(), device.alias, app_res.summary)
            return app_res

        if key.strip():
            adapter = self._select_adapter(device, DeviceCapability.NAVIGATION)
            if adapter is None:
                logger.warning("launch_tv_app (key) falló en '%s': sin soporte para NAVIGATION", device.alias)
                return ToolResult(
                    ok=False,
                    summary=f"{device.alias} no soporta envío de teclas remotas.",
                    error_code="unsupported_capability",
                )
            key_res = _adapter_result_to_tool(await adapter.send_key(device, key.strip()))
            logger.info("launch_tv_app (key '%s') en '%s': ok=%s", key.strip(), device.alias, key_res.ok)
            if not key_res.ok:
                logger.warning("launch_tv_app (key '%s') falló en '%s': %s", key.strip(), device.alias, key_res.summary)
            return key_res

        return ToolResult(
            ok=False,
            summary="Indica una aplicación (`app`) o tecla remota (`key`) para enviar al televisor.",
            error_code="invalid_args",
        )

    # ------------------------------------------------------------------
    # Registro de Herramientas (`ToolSpec`)
    # ------------------------------------------------------------------
    def get_tools(self) -> list[ToolSpec]:
        async def _tool_scan(args: dict[str, Any]) -> ToolResult:
            timeout = float(args.get("timeout_sec", 2.5))
            active_subnet = bool(args.get("active_subnet_scan", True))
            return await self.scan_network(timeout_sec=timeout, active_subnet_scan=active_subnet)

        async def _tool_list(_args: dict[str, Any]) -> ToolResult:
            err = self._check_ready(Permission.NETWORK_DISCOVERY)
            if err is not None:
                return err
            devices = self.list_known_devices()
            summary = (
                f"Hay {len(devices)} dispositivos LAN guardados."
                if devices
                else "No hay dispositivos LAN guardados todavía."
            )
            return ToolResult(
                ok=True,
                summary=summary,
                spoken_text=summary,
                data={"devices": [d.to_public_dict() for d in devices], "count": len(devices)},
            )

        async def _tool_set_alias(args: dict[str, Any]) -> ToolResult:
            return await self.rename_device(
                str(args.get("device") or ""),
                str(args.get("alias") or ""),
            )

        async def _tool_set_favorite(args: dict[str, Any]) -> ToolResult:
            return await self.set_favorite_device(
                str(args.get("device") or ""),
                favorite=bool(args.get("favorite", True)),
            )

        async def _tool_power(args: dict[str, Any]) -> ToolResult:
            return await self.control_power(
                str(args.get("device") or ""),
                str(args.get("action") or "on"),
            )

        async def _tool_volume(args: dict[str, Any]) -> ToolResult:
            level_raw = args.get("level")
            delta_raw = args.get("delta")
            mute_raw = args.get("mute")
            return await self.control_volume(
                str(args.get("device") or ""),
                level=int(level_raw) if level_raw is not None else None,
                delta=int(delta_raw) if delta_raw is not None else None,
                mute=bool(mute_raw) if mute_raw is not None else None,
            )

        async def _tool_media(args: dict[str, Any]) -> ToolResult:
            return await self.control_media(
                str(args.get("device") or ""),
                action=str(args.get("action") or "pause"),
                media_url=str(args.get("media_url") or ""),
                title=str(args.get("title") or ""),
            )

        async def _tool_send_youtube(args: dict[str, Any]) -> ToolResult:
            return await self.send_youtube_to_tv(
                str(args.get("device") or ""),
                url_or_id=str(args.get("url") or ""),
            )

        async def _tool_pair_youtube(args: dict[str, Any]) -> ToolResult:
            return await self.pair_youtube_tv(
                str(args.get("device") or ""),
                str(args.get("pairing_code") or ""),
            )

        async def _tool_project(args: dict[str, Any]) -> ToolResult:
            return await self.project_screen(
                str(args.get("device") or ""),
                action=str(args.get("action") or "connect"),
                display_mode=str(args.get("display_mode") or "extend"),
            )

        async def _tool_tv_app(args: dict[str, Any]) -> ToolResult:
            return await self.launch_tv_app(
                str(args.get("device") or ""),
                app=str(args.get("app") or ""),
                key=str(args.get("key") or ""),
            )

        return [
            ToolSpec(
                name="network_scan",
                module_id=self.module_id,
                description="Escanea la red local (mDNS, SSDP, ARP y sondeo activo) para detectar TVs, Chromecast y Xbox.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "timeout_sec": {"type": "number", "minimum": 0.6, "maximum": 8.0},
                        "active_subnet_scan": {"type": "boolean"},
                    },
                },
                permission=Permission.NETWORK_DISCOVERY,
                risk_level=RiskLevel.SAFE,
                handler=_tool_scan,
            ),
            ToolSpec(
                name="network_list_devices",
                module_id=self.module_id,
                description="Lista los dispositivos LAN guardados con sus alias, IPs recientes y MACs.",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.NETWORK_DISCOVERY,
                risk_level=RiskLevel.SAFE,
                handler=_tool_list,
            ),
            ToolSpec(
                name="network_set_alias",
                module_id=self.module_id,
                description="Asigna un alias amigable a un dispositivo LAN guardado.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "alias": {"type": "string"},
                    },
                    "required": ["device", "alias"],
                },
                permission=Permission.NETWORK_DISCOVERY,
                risk_level=RiskLevel.SAFE,
                handler=_tool_set_alias,
            ),
            ToolSpec(
                name="network_set_favorite",
                module_id=self.module_id,
                description="Marca o desmarca un dispositivo LAN como favorito predeterminado.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "favorite": {"type": "boolean"},
                    },
                    "required": ["device"],
                },
                permission=Permission.NETWORK_DISCOVERY,
                risk_level=RiskLevel.SAFE,
                handler=_tool_set_favorite,
            ),
            ToolSpec(
                name="network_power",
                module_id=self.module_id,
                description="Enciende (Wake-on-LAN / Xbox SmartGlass) o apaga un dispositivo LAN.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "action": {"type": "string", "enum": ["on", "off"]},
                    },
                    "required": ["action"],
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_tool_power,
            ),
            ToolSpec(
                name="network_volume",
                module_id=self.module_id,
                description="Ajusta el volumen (nivel absoluto, pasos relativos o silencio) en un televisor o Chromecast.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "level": {"type": "integer", "minimum": 0, "maximum": 100},
                        "delta": {"type": "integer"},
                        "mute": {"type": "boolean"},
                    },
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_tool_volume,
            ),
            ToolSpec(
                name="network_media",
                module_id=self.module_id,
                description="Controla reproducción multimedia (play, pause, resume, stop, play_media) en Cast o Smart TV.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["play", "pause", "resume", "stop", "next", "previous", "play_media"],
                        },
                        "media_url": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["action"],
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_tool_media,
            ),
            ToolSpec(
                name="network_send_youtube",
                module_id=self.module_id,
                description="Envía un video de YouTube (desde URL dada o leyendo el navegador activo) a la TV vía YouTube Lounge / DIAL.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_tool_send_youtube,
            ),
            ToolSpec(
                name="network_pair_youtube",
                module_id=self.module_id,
                description="Vincula la app YouTube TV usando el código numérico de TV (guarda el token en .env).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "pairing_code": {"type": "string"},
                    },
                    "required": ["pairing_code"],
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_tool_pair_youtube,
            ),
            ToolSpec(
                name="network_project_screen",
                module_id=self.module_id,
                description="Proyecta o desconecta la pantalla del PC por Miracast (Win+K + DisplaySwitch).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "action": {"type": "string", "enum": ["connect", "disconnect"]},
                        "display_mode": {
                            "type": "string",
                            "enum": ["extend", "clone", "internal", "external"],
                        },
                    },
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_tool_project,
            ),
            ToolSpec(
                name="network_tv_app",
                module_id=self.module_id,
                description="Abre una app (YouTube, Netflix, Spotify...) o envía una tecla de navegación a un Smart TV.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "device": {"type": "string"},
                        "app": {"type": "string"},
                        "key": {"type": "string"},
                    },
                },
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_tool_tv_app,
            ),
        ]

    # ------------------------------------------------------------------
    # Reglas Fast-Path locales (<50 ms en detección, 0 tokens)
    # ------------------------------------------------------------------
    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp_scan(_m: re.Match[str], _raw: str) -> ToolResult:
            return await self.scan_network()

        async def _fp_send_youtube(m: re.Match[str], _raw: str) -> ToolResult:
            target = (m.group("target") or "").strip()
            return await self.send_youtube_to_tv(target)

        async def _fp_project_connect(m: re.Match[str], _raw: str) -> ToolResult:
            verb = (m.group("mode") or "").lower()
            target = (m.group("target") or "").strip()
            mode = "clone" if verb.startswith("duplica") else "extend"
            return await self.project_screen(target, action="connect", display_mode=mode)

        async def _fp_project_disconnect(_m: re.Match[str], _raw: str) -> ToolResult:
            return await self.project_screen(action="disconnect")

        async def _fp_power(m: re.Match[str], _raw: str) -> ToolResult:
            verb = (m.group("verb") or "").lower()
            target = (m.group("target") or "").strip()
            action = "off" if verb.startswith("apag") else "on"
            return await self.control_power(target, action)

        async def _fp_volume_delta(m: re.Match[str], _raw: str) -> ToolResult:
            verb = (m.group("verb") or "").lower()
            target = (m.group("target") or "").strip()
            amount_raw = m.group("amount")
            if verb.startswith(("silenc", "mute")):
                return await self.control_volume(target, mute=True)

            resolved = self._resolve_device(target)
            default_step = (
                3
                if (resolved is not None and resolved.device_type == DeviceType.SAMSUNG_TV)
                else 10
            )
            step = int(amount_raw) if amount_raw else default_step
            delta = step if verb.startswith("sub") else -step
            return await self.control_volume(target, delta=delta)

        async def _fp_volume_level(m: re.Match[str], _raw: str) -> ToolResult:
            target = (m.group("target") or "").strip()
            level = max(0, min(100, int(m.group("level"))))
            return await self.control_volume(target, level=level)

        async def _fp_media_cast(m: re.Match[str], _raw: str) -> ToolResult:
            verb = (m.group("verb") or "").lower()
            target = (m.group("target") or "").strip()
            if verb.startswith("paus"):
                action = "pause"
            elif verb.startswith(("reanud", "contin")):
                action = "resume"
            else:
                action = "stop"
            return await self.control_media(target, action=action)

        return [
            FastPathRule(
                rule_id="network.scan",
                module_id=self.module_id,
                priority=62,
                pattern=_SCAN_RE,
                permission=Permission.NETWORK_DISCOVERY,
                risk_level=RiskLevel.SAFE,
                handler=_fp_scan,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.send_youtube",
                module_id=self.module_id,
                priority=61,
                pattern=_SEND_YOUTUBE_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_fp_send_youtube,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.project_disconnect",
                module_id=self.module_id,
                priority=61,
                pattern=_PROJECT_DISCONNECT_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_fp_project_disconnect,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.project_connect",
                module_id=self.module_id,
                priority=60,
                pattern=_PROJECT_CONNECT_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_fp_project_connect,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.power",
                module_id=self.module_id,
                priority=59,
                pattern=_POWER_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.MODERATE,
                handler=_fp_power,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.volume_level",
                module_id=self.module_id,
                priority=58,
                pattern=_VOLUME_LEVEL_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_fp_volume_level,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.volume_delta",
                module_id=self.module_id,
                priority=57,
                pattern=_VOLUME_DELTA_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_fp_volume_delta,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="network.media_cast",
                module_id=self.module_id,
                priority=56,
                pattern=_MEDIA_CAST_RE,
                permission=Permission.NETWORK_CONTROL,
                risk_level=RiskLevel.SAFE,
                handler=_fp_media_cast,
                earcon="confirm",
            ),
        ]

    # ------------------------------------------------------------------
    # Diagnósticos del Doctor
    # ------------------------------------------------------------------
    def diagnose(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = []

        nets = get_local_ipv4_networks()
        if nets:
            items.append(
                DiagnosticItem(
                    code="network.interfaces",
                    area="Red Local",
                    title="Interfaces LAN IPv4",
                    severity=DiagnosticSeverity.OK,
                    message=f"{len(nets)} subred(es) privada(s) activa(s) lista(s) para descubrimiento dinámico.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="network.interfaces",
                    area="Red Local",
                    title="Interfaces LAN IPv4",
                    severity=DiagnosticSeverity.WARNING,
                    message="No se detectó una interfaz IPv4 privada activa.",
                    remediation="Verifica tu conexión Wi-Fi o Ethernet local.",
                )
            )

        required_pkgs = ("zeroconf", "pychromecast", "websockets", "httpx")
        missing = [pkg for pkg in required_pkgs if importlib.util.find_spec(pkg) is None]
        if not missing:
            items.append(
                DiagnosticItem(
                    code="network.dependencies",
                    area="Red Local",
                    title="Librerías de protocolos LAN",
                    severity=DiagnosticSeverity.OK,
                    message="zeroconf, pychromecast, websockets y httpx están disponibles.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="network.dependencies",
                    area="Red Local",
                    title="Librerías de protocolos LAN",
                    severity=DiagnosticSeverity.WARNING,
                    message=f"Faltan paquetes LAN opcionales: {', '.join(missing)}.",
                    remediation="Ejecuta `uv sync` para restaurar las dependencias de red.",
                )
            )

        if self._store is not None:
            devices = self._store.list_devices()
            wol_ready = sum(1 for d in devices if bool(d.mac_address))
            if devices:
                items.append(
                    DiagnosticItem(
                        code="network.devices",
                        area="Red Local",
                        title="Dispositivos registrados",
                        severity=DiagnosticSeverity.OK,
                        message=(
                            f"{len(devices)} dispositivo(s) guardado(s) en SQLite "
                            f"({wol_ready} con MAC lista para Wake-on-LAN)."
                        ),
                    )
                )
            else:
                items.append(
                    DiagnosticItem(
                        code="network.devices",
                        area="Red Local",
                        title="Dispositivos registrados",
                        severity=DiagnosticSeverity.WARNING,
                        message="Aún no hay dispositivos LAN guardados.",
                        remediation="Abre Integraciones y pulsa 'Escanear red' para detectarlos.",
                    )
                )

        return items
