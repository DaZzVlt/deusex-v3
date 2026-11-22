from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import re
import subprocess
import sys
import time
from typing import Any
import unicodedata

from deusex.core.runtime import ComStaThread
from deusex.modules.network.adapters import (
    AdapterActionResult,
    DeviceCapability,
    DeviceType,
    NetworkDeviceAdapter,
    NetworkDeviceRecord,
    normalize_mac,
    send_wake_on_lan,
)

logger = logging.getLogger(__name__)

_VK_LWIN = 0x5B
_VK_K = 0x4B
_KEYEVENTF_KEYUP = 0x0002

_DISPLAY_SWITCH_FLAGS: dict[str, str] = {
    "internal": "/internal",
    "solo_pc": "/internal",
    "pc": "/internal",
    "clone": "/clone",
    "duplicate": "/clone",
    "duplicar": "/clone",
    "extend": "/extend",
    "extender": "/extend",
    "external": "/external",
    "second": "/external",
    "segunda_pantalla": "/external",
}


def _normalize_label(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip()


def resolve_display_switch_flag(mode: str) -> str | None:
    """Traduce un modo legible (`duplicar`, `extender`, `internal`, `external`) a flag de DisplaySwitch."""
    return _DISPLAY_SWITCH_FLAGS.get(_normalize_label(mode).replace(" ", "_"))


def _press_win_k_sync() -> bool:
    if sys.platform != "win32":
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    user32 = windll.user32
    user32.keybd_event(_VK_LWIN, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(_VK_K, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(_VK_K, 0, _KEYEVENTF_KEYUP, 0)
    user32.keybd_event(_VK_LWIN, 0, _KEYEVENTF_KEYUP, 0)
    return True


def _find_matching_uia_item(
    root: Any,
    target_queries: list[str],
    *,
    max_depth: int = 12,
    max_nodes: int = 450,
    deadline: float = 0.0,
    discovered_names: set[str] | None = None,
) -> Any | None:
    """Busca en el árbol UIA (BFS acotado) un elemento seleccionable que coincida con el dispositivo."""
    queue: list[tuple[Any, int]] = [(root, 0)]
    nodes_count = 0
    while queue:
        if deadline > 0 and time.monotonic() > deadline:
            break
        ctrl, depth = queue.pop(0)
        nodes_count += 1
        if nodes_count > max_nodes:
            break
        if depth > max_depth:
            continue

        ctrl_type = str(getattr(ctrl, "ControlTypeName", "") or "")
        # Omitir contenedores sin texto seleccionable para no agotar el presupuesto en la barra lateral
        if ctrl_type in ("ScrollBarControl", "ThumbControl", "SeparatorControl"):
            continue

        name = _normalize_label(str(getattr(ctrl, "Name", "") or ""))
        if name and ctrl_type in ("ListItemControl", "ButtonControl", "TextControl", "DataItemControl"):
            if discovered_names is not None:
                discovered_names.add(name)
            for query in target_queries:
                if not query:
                    continue
                if query in name or name in query:
                    return ctrl
                words = [w for w in query.split() if len(w) >= 3 and w not in ("the", "del", "sala", "casa", "living")]
                if words and any(w in name for w in words):
                    return ctrl
        if depth < max_depth:
            children: list[Any] = []
            with contextlib.suppress(Exception):
                children = list(ctrl.GetChildren())
            for child in children:
                queue.append((child, depth + 1))
    return None


def _try_single_invoke(target: Any) -> bool:
    with contextlib.suppress(Exception):
        pattern = target.GetInvokePattern()
        if pattern is not None:
            pattern.Invoke()
            return True
    with contextlib.suppress(Exception):
        sel_pattern = target.GetSelectionItemPattern()
        if sel_pattern is not None:
            sel_pattern.Select()
            return True
    with contextlib.suppress(Exception):
        target.Click(simulateMove=False)
        return True
    return False


def _invoke_uia_control(ctrl: Any) -> bool:
    # 1. Probar en cascada sobre el propio nodo: Invoke -> Select -> Click
    if _try_single_invoke(ctrl):
        return True

    # 2. Si es TextControl no invocable, subir al contenedor inmediato (ListItemControl / ButtonControl)
    ctrl_type = str(getattr(ctrl, "ControlTypeName", "") or "")
    if ctrl_type == "TextControl":
        with contextlib.suppress(Exception):
            parent = ctrl.GetParentControl()
            if parent is not None and _try_single_invoke(parent):
                return True

    return False


class MiracastAdapter(NetworkDeviceAdapter):
    """
    Proyección inalámbrica Miracast en Windows 11:
    - Cadena Wake-on-LAN previa si el televisor está apagado y posee MAC registrada.
    - Apertura y selección guiada de `Win + K` ejecutada estrictamente en `ComStaThread` (UIA).
    - Control de topología de pantallas vía `DisplaySwitch.exe`.
    """

    adapter_id = "miracast"
    supported_types = (
        DeviceType.MIRACAST,
        DeviceType.SAMSUNG_TV,
        DeviceType.LG_WEBOS,
        DeviceType.ANDROID_TV,
    )
    capabilities = frozenset({DeviceCapability.MIRACAST, DeviceCapability.POWER_ON})

    def __init__(
        self,
        com_sta: ComStaThread,
        *,
        wol_wait_sec: float = 2.5,
        ui_wait_sec: float = 5.5,
    ) -> None:
        self._com_sta = com_sta
        self._wol_wait_sec = max(0.0, min(wol_wait_sec, 6.0))
        self._ui_wait_sec = max(0.5, min(ui_wait_sec, 8.0))

    async def set_display_mode(self, mode: str) -> AdapterActionResult:
        flag = resolve_display_switch_flag(mode)
        if flag is None:
            return AdapterActionResult(
                ok=False,
                message=f"Modo de pantalla no reconocido: '{mode}'. Usa duplicar, extender, solo_pc o segunda_pantalla.",
                error_code="invalid_display_mode",
            )
        if sys.platform != "win32":
            return AdapterActionResult(
                ok=False,
                message="DisplaySwitch.exe solo está disponible en Windows.",
                error_code="unsupported_platform",
            )

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = await asyncio.create_subprocess_exec(
                "DisplaySwitch.exe",
                flag,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=creationflags,
            )
            await asyncio.wait_for(proc.wait(), timeout=2.5)
        except TimeoutError:
            return AdapterActionResult(
                ok=True,
                message=f"Cambio de pantalla ({flag}) solicitado.",
                data={"flag": flag},
            )
        except OSError as exc:
            return AdapterActionResult(
                ok=False,
                message=f"No se pudo ejecutar DisplaySwitch.exe: {exc}",
                error_code="display_switch_failed",
            )
        return AdapterActionResult(
            ok=True,
            message=f"Modo de pantalla cambiado ({flag}).",
            data={"flag": flag},
        )

    def _connect_win_k_sta(self, target_queries: list[str], wait_sec: float) -> AdapterActionResult:
        """Corre exclusivamente dentro de `ComStaThread` (COM STA inicializado)."""
        if sys.platform != "win32":
            return AdapterActionResult(
                ok=False,
                message="La proyección Miracast (Win+K) solo está disponible en Windows 11.",
                error_code="unsupported_platform",
            )

        try:
            import uiautomation as auto
        except ImportError:
            return AdapterActionResult(
                ok=False,
                message="uiautomation no está disponible para controlar el panel Miracast.",
                error_code="dependency_missing",
            )

        if not _press_win_k_sync():
            return AdapterActionResult(
                ok=False,
                message="No se pudo enviar el atajo Win+K al sistema.",
                error_code="hotkey_failed",
            )

        normalized_queries = [_normalize_label(q) for q in target_queries if q.strip()]
        deadline = time.monotonic() + wait_sec
        flyout_found = False
        discovered_names: set[str] = set()
        flyout_hints = (
            "cast",
            "transmit",
            "proyect",
            "conect",
            "configuracion rapida",
            "quick settings",
        )

        while time.monotonic() < deadline:
            time.sleep(0.3)
            candidates: list[Any] = []
            with contextlib.suppress(Exception):
                fg = auto.GetForegroundControl()
                if fg is not None:
                    candidates.append(fg)
            with contextlib.suppress(Exception):
                root = auto.GetRootControl()
                for child in root.GetChildren():
                    child_cls = str(getattr(child, "ClassName", "") or "")
                    child_name = _normalize_label(str(getattr(child, "Name", "") or ""))
                    if (
                        "ControlCenterWindow" in child_cls
                        or "Windows.UI.Core.CoreWindow" in child_cls
                        or any(h in child_name for h in flyout_hints)
                    ):
                        candidates.append(child)

            for cand_win in candidates:
                cand_name = _normalize_label(str(getattr(cand_win, "Name", "") or ""))
                cand_cls = str(getattr(cand_win, "ClassName", "") or "")
                if (
                    any(h in cand_name for h in flyout_hints)
                    or "ControlCenterWindow" in cand_cls
                    or "Windows.UI.Core.CoreWindow" in cand_cls
                ):
                    flyout_found = True

                match_ctrl = _find_matching_uia_item(
                    cand_win,
                    normalized_queries,
                    max_depth=12,
                    max_nodes=450,
                    deadline=deadline,
                    discovered_names=discovered_names,
                )
                if match_ctrl is not None and _invoke_uia_control(match_ctrl):
                    matched_name = str(getattr(match_ctrl, "Name", "") or target_queries[0])
                    logger.info("Miracast Win+K: proyección iniciada hacia '%s' (UIA Invoke/Select exitoso)", matched_name)
                    return AdapterActionResult(
                        ok=True,
                        message=f"Proyección inalámbrica Miracast iniciada hacia {matched_name}.",
                        data={"matched_target": matched_name, "via": "win_k_uia"},
                    )

        if discovered_names:
            logger.debug(
                "Win+K finalizó sin coincidencia para %s. Elementos detectados en flyout: %s",
                target_queries,
                sorted(discovered_names),
            )

        if flyout_found:
            logger.warning("Miracast Win+K: flyout abierto pero '%s' no apareció en la lista a tiempo", target_queries[0])
            return AdapterActionResult(
                ok=True,
                message=(
                    f"Panel Miracast (Win+K) abierto; '{target_queries[0]}' aún no aparece en la lista "
                    "o requiere selección manual."
                ),
                data={"flyout_opened": True, "auto_selected": False},
            )
        logger.warning("Miracast Win+K: no se pudo detectar el panel ni el destino '%s'", target_queries[0])
        return AdapterActionResult(
            ok=False,
            message=f"No se pudo detectar el dispositivo '{target_queries[0]}' en el panel Win+K.",
            error_code="miracast_target_not_found",
        )

    def _disconnect_win_k_sta(self, wait_sec: float) -> AdapterActionResult:
        if sys.platform != "win32":
            return AdapterActionResult(
                ok=False,
                message="La desconexión Miracast solo está disponible en Windows 11.",
                error_code="unsupported_platform",
            )
        try:
            import uiautomation as auto
        except ImportError:
            return AdapterActionResult(
                ok=False,
                message="uiautomation no está disponible.",
                error_code="dependency_missing",
            )

        _press_win_k_sync()
        deadline = time.monotonic() + wait_sec
        disconnect_labels = ["desconectar", "disconnect"]
        while time.monotonic() < deadline:
            time.sleep(0.2)
            with contextlib.suppress(Exception):
                fg = auto.GetForegroundControl()
                if fg is None:
                    continue
                btn = _find_matching_uia_item(
                    fg,
                    disconnect_labels,
                    max_depth=6,
                    max_nodes=120,
                    deadline=deadline,
                )
                if btn is not None and _invoke_uia_control(btn):
                    return AdapterActionResult(
                        ok=True,
                        message="Proyección Miracast desconectada.",
                        data={"via": "win_k_uia"},
                    )

        return AdapterActionResult(
            ok=False,
            message="No se encontró un botón activo de desconexión en el panel Win+K.",
            error_code="disconnect_not_found",
        )

    async def project_to_device(
        self,
        device: NetworkDeviceRecord,
        *,
        display_mode: str = "extend",
        wake_if_needed: bool = True,
    ) -> AdapterActionResult:
        wol_sent = False
        mac = normalize_mac(device.mac_address)
        if wake_if_needed and mac and not device.online:
            wol_sent = await asyncio.to_thread(send_wake_on_lan, mac, target_ip=device.last_ip)
            if wol_sent and self._wol_wait_sec > 0:
                await asyncio.sleep(self._wol_wait_sec)

        queries = [
            device.alias,
            str(device.metadata.get("friendly_name") or ""),
            str(device.metadata.get("model") or ""),
        ]
        clean_queries = [q for q in queries if q.strip()] or [device.device_uid]

        try:
            res = await asyncio.wait_for(
                self._com_sta.run_async(self._connect_win_k_sta, clean_queries, self._ui_wait_sec),
                timeout=self._ui_wait_sec + 2.5,
            )
        except TimeoutError:
            return AdapterActionResult(
                ok=False,
                message=f"Tiempo de espera agotado al proyectar hacia {device.alias}.",
                error_code="timeout",
            )

        if res.ok and display_mode:
            mode_res = await self.set_display_mode(display_mode)
            merged_data = {**res.data, "wol_sent": wol_sent, "display_mode": display_mode, "display_switch_ok": mode_res.ok}
            return AdapterActionResult(ok=True, message=res.message, data=merged_data)

        return AdapterActionResult(
            ok=res.ok,
            message=res.message,
            data={**res.data, "wol_sent": wol_sent},
            error_code=res.error_code,
        )

    async def disconnect_projection(self) -> AdapterActionResult:
        try:
            res = await asyncio.wait_for(
                self._com_sta.run_async(self._disconnect_win_k_sta, 1.5),
                timeout=3.0,
            )
        except TimeoutError:
            res = AdapterActionResult(ok=False, message="Timeout al cerrar Miracast.", error_code="timeout")

        if res.ok:
            return res
        fallback = await self.set_display_mode("internal")
        if fallback.ok:
            return AdapterActionResult(
                ok=True,
                message="Pantalla restablecida a solo PC (/internal).",
                data={"via": "display_switch_fallback"},
            )
        return res

    async def power_on(self, device: NetworkDeviceRecord) -> AdapterActionResult:
        mac = normalize_mac(device.mac_address)
        if not mac:
            return AdapterActionResult(
                ok=False,
                message=f"{device.alias} no tiene MAC registrada para Wake-on-LAN.",
                error_code="missing_mac",
            )
        sent = await asyncio.to_thread(send_wake_on_lan, mac, target_ip=device.last_ip)
        return AdapterActionResult(
            ok=sent,
            message=f"Wake-on-LAN enviado a {device.alias}." if sent else f"Error enviando WoL a {device.alias}.",
            error_code=None if sent else "wol_failed",
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
        clean = action.strip().lower()
        if clean in ("project", "cast", "connect", "miracast"):
            return await self.project_to_device(device)
        if clean in ("disconnect", "stop"):
            return await self.disconnect_projection()
        return await super().media_control(device, action)
