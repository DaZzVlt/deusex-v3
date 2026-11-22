from __future__ import annotations

import asyncio
import contextlib
import ctypes
from dataclasses import dataclass
import json
import logging
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

import httpx
import psutil

from deusex.core.runtime import ComStaThread
from deusex.modules.network.adapters import (
    AdapterActionResult,
    DeviceCapability,
    DeviceType,
    NetworkDeviceAdapter,
    NetworkDeviceRecord,
)
from deusex.modules.network.store import DeviceAliasStore

logger = logging.getLogger(__name__)

_LOUNGE_BASE = "https://www.youtube.com/api/lounge"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BROWSER_NAME_HINTS: tuple[str, ...] = (
    "chrome",
    "edge",
    "brave",
    "firefox",
    "opera",
    "vivaldi",
    "chromium",
)
_ADDRESS_BAR_HINTS: tuple[str, ...] = (
    "address",
    "barra de direcciones",
    "direcciones",
    "search or enter",
    "url",
    "location",
)


@dataclass(frozen=True, slots=True)
class BrowserTabInfo:
    """URL activa detectada desde un navegador mediante UIA en `ComStaThread`."""

    url: str
    window_title: str = ""
    video_id: str | None = None


def extract_youtube_video_id(raw: str) -> str | None:
    """Extrae el ID de 11 caracteres de cualquier URL estándar de YouTube o ID directo."""
    text = raw.strip()
    if not text:
        return None
    if _VIDEO_ID_RE.fullmatch(text):
        return text

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if not any(
        domain in host
        for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com")
    ):
        return None

    if host == "youtu.be" or host.endswith(".youtu.be"):
        seg = parsed.path.strip("/").split("/", 1)[0]
        return seg if _VIDEO_ID_RE.fullmatch(seg) else None

    query_v = parse_qs(parsed.query).get("v", [""])[0]
    if _VIDEO_ID_RE.fullmatch(query_v):
        return query_v

    parts = [p for p in parsed.path.split("/") if p]
    if (
        len(parts) >= 2
        and parts[0] in ("shorts", "embed", "live", "v")
        and _VIDEO_ID_RE.fullmatch(parts[1])
    ):
        return parts[1]

    return None


_KNOWN_BROWSER_EXES: frozenset[str] = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "opera.exe",
        "opera_gx.exe",
        "brave.exe",
        "firefox.exe",
        "vivaldi.exe",
        "arc.exe",
        "chromium.exe",
        "waterfox.exe",
    }
)

_SKIP_CONTROL_TYPES: frozenset[str] = frozenset(
    {
        "DocumentControl",
        "DataGridControl",
        "TableControl",
        "TreeControl",
        "ListControl",
        "MenuControl",
        "ScrollBarControl",
    }
)


def _is_browser_process(handle: int) -> bool:
    if not handle or sys.platform != "win32":
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    pid = ctypes.c_ulong()
    windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if not pid.value:
        return False
    try:
        proc_name = psutil.Process(pid.value).name().lower()
        return proc_name in _KNOWN_BROWSER_EXES
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def _find_address_bar_url(
    window_ctrl: Any,
    *,
    max_depth: int = 5,
    max_nodes: int = 80,
    deadline: float = 0.0,
) -> str:
    queue: list[tuple[Any, int]] = [(window_ctrl, 0)]
    nodes_inspected = 0
    while queue:
        if deadline > 0 and time.monotonic() > deadline:
            break
        ctrl, depth = queue.pop(0)
        nodes_inspected += 1
        if nodes_inspected > max_nodes:
            break
        if depth > max_depth:
            continue

        ctrl_type = str(getattr(ctrl, "ControlTypeName", "") or "")
        if ctrl_type in _SKIP_CONTROL_TYPES:
            continue

        if ctrl_type == "EditControl":
            name = str(getattr(ctrl, "Name", "") or "").lower()
            auto_id = str(getattr(ctrl, "AutomationId", "") or "").lower()
            if any(h in name or h in auto_id for h in _ADDRESS_BAR_HINTS):
                with contextlib.suppress(Exception):
                    val = str(ctrl.GetValuePattern().Value or "").strip()
                    if val and ("." in val or "://" in val):
                        return val

        if depth < max_depth:
            children: list[Any] = []
            with contextlib.suppress(Exception):
                children = list(ctrl.GetChildren())
            for child in children:
                queue.append((child, depth + 1))
    return ""


class ActiveBrowserUrlReader:
    """
    Lector UIA del navegador activo ejecutado exclusivamente en `ComStaThread`.
    Inspecciona primero la ventana en foco y luego navegadores visibles (útil cuando
    el usuario invoca el comando desde el Mini-Chat o la ventana de DeusEx).
    """

    def __init__(self, com_sta: ComStaThread) -> None:
        self._com_sta = com_sta

    def _read_active_url_sta(
        self,
        *,
        prefer_youtube: bool,
        deadline: float = 0.0,
    ) -> BrowserTabInfo | None:
        if sys.platform != "win32":
            return None
        try:
            import uiautomation as auto
        except ImportError:
            return None

        candidates: list[Any] = []
        with contextlib.suppress(Exception):
            fg = auto.GetForegroundControl()
            if fg is not None:
                candidates.append(fg)

        with contextlib.suppress(Exception):
            root = auto.GetRootControl()
            for child in root.GetChildren():
                handle = int(getattr(child, "NativeWindowHandle", 0) or 0)
                if handle and _is_browser_process(handle):
                    candidates.append(child)

        first_valid: BrowserTabInfo | None = None
        seen_handles: set[int] = set()

        for win in candidates:
            if deadline > 0 and time.monotonic() > deadline:
                break
            handle = int(getattr(win, "NativeWindowHandle", 0) or 0)
            if not handle or handle in seen_handles:
                continue
            seen_handles.add(handle)

            if not _is_browser_process(handle):
                continue

            win_title = str(getattr(win, "Name", "") or "")
            raw_url = _find_address_bar_url(win, max_depth=5, max_nodes=80, deadline=deadline)
            if not raw_url:
                continue

            full_url = raw_url if "://" in raw_url else f"https://{raw_url}"
            vid = extract_youtube_video_id(full_url)
            tab = BrowserTabInfo(url=full_url, window_title=win_title, video_id=vid)
            if vid is not None:
                return tab
            if first_valid is None:
                first_valid = tab
            if not prefer_youtube:
                return tab

        return first_valid

    async def read_active_tab(
        self,
        *,
        prefer_youtube: bool = True,
        timeout_sec: float = 2.0,
    ) -> BrowserTabInfo | None:
        deadline = time.monotonic() + min(1.0, max(0.3, timeout_sec - 0.3))
        try:
            return await asyncio.wait_for(
                self._com_sta.run_async(
                    self._read_active_url_sta,
                    prefer_youtube=prefer_youtube,
                    deadline=deadline,
                ),
                timeout=timeout_sec,
            )
        except (TimeoutError, RuntimeError) as exc:
            logger.debug("ActiveBrowserUrlReader no pudo leer el navegador activo: %s", exc)
            return None


def _parse_dial_screen_id(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1].lower()
        if tag == "screenid" and elem.text and elem.text.strip():
            return elem.text.strip()
    return ""


class YouTubeTVAdapter(NetworkDeviceAdapter):
    """
    Adaptador YouTube TV Lounge API (`httpx`) + `ActiveBrowserUrlReader`:
    - Prioritario cuando hay un video web de YouTube activo en el navegador del PC.
    - Guarda `lounge_token` exclusivamente en `.env` (vía `DeviceAliasStore`).
    - Fallback automático a DIAL (`POST http://{ip}:8008/apps/YouTube`) si el dispositivo
      no tiene código Lounge emparejado todavía.
    """

    adapter_id = "youtube_tv"
    supported_types = (
        DeviceType.YOUTUBE_TV,
        DeviceType.SAMSUNG_TV,
        DeviceType.CHROMECAST,
        DeviceType.ANDROID_TV,
        DeviceType.LG_WEBOS,
    )
    capabilities = frozenset(
        {
            DeviceCapability.YOUTUBE_LOUNGE,
            DeviceCapability.MEDIA_PLAYBACK,
            DeviceCapability.VOLUME,
        }
    )

    def __init__(
        self,
        store: DeviceAliasStore,
        browser_reader: ActiveBrowserUrlReader | None = None,
        *,
        timeout_sec: float = 3.5,
    ) -> None:
        self._store = store
        self._browser_reader = browser_reader
        self._timeout_sec = max(1.0, timeout_sec)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_sec, connect=min(1.5, self._timeout_sec)),
                headers={"User-Agent": "DeusEx-v3/3.0"},
            )
        return self._client

    async def pair_with_code(
        self,
        device: NetworkDeviceRecord,
        pairing_code: str,
    ) -> AdapterActionResult:
        digits = re.sub(r"\D+", "", pairing_code)
        if len(digits) < 9:
            return AdapterActionResult(
                ok=False,
                message="El código de vinculación de YouTube TV debe tener al menos 9 dígitos.",
                error_code="invalid_pairing_code",
            )

        client = self._get_client()
        try:
            resp = await client.get(
                f"{_LOUNGE_BASE}/pairing/get_screen",
                params={"pairing_code": digits},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return AdapterActionResult(
                ok=False,
                message=f"No se pudo vincular YouTube TV en {device.alias}: {exc}",
                error_code="lounge_pairing_failed",
            )

        screen = payload.get("screen") if isinstance(payload, dict) else None
        if not isinstance(screen, dict):
            return AdapterActionResult(
                ok=False,
                message="YouTube TV no devolvió datos de pantalla para ese código.",
                error_code="lounge_pairing_invalid",
            )

        screen_id = str(screen.get("screenId") or "").strip()
        lounge_token = str(screen.get("loungeToken") or "").strip()
        if not screen_id or not lounge_token:
            return AdapterActionResult(
                ok=False,
                message="Respuesta incompleta de YouTube Lounge al emparejar.",
                error_code="lounge_pairing_incomplete",
            )

        self._store.set_token(device.device_uid, lounge_token, kind="lounge")
        updated_meta = {**device.metadata, "youtube_screen_id": screen_id}
        self._store.upsert_device(device.with_updates(metadata=updated_meta), preserve_alias=True)

        return AdapterActionResult(
            ok=True,
            message=f"YouTube TV vinculado correctamente con {device.alias}.",
            data={"screen_id": screen_id},
        )

    async def _discover_screen_id_via_dial(self, device: NetworkDeviceRecord) -> str:
        if not device.last_ip:
            return ""
        client = self._get_client()
        for port in (8008, 8080):
            with contextlib.suppress(httpx.HTTPError, OSError):
                resp = await client.get(f"http://{device.last_ip}:{port}/apps/YouTube")
                if resp.status_code == 200 and "<" in resp.text:
                    screen_id = _parse_dial_screen_id(resp.text)
                    if screen_id:
                        return screen_id
        return ""

    async def _ensure_lounge_token(self, device: NetworkDeviceRecord) -> str | None:
        existing_token = self._store.get_token(device.device_uid, kind="lounge")
        if existing_token:
            return existing_token

        screen_id = str(device.metadata.get("youtube_screen_id") or "").strip()
        if not screen_id:
            screen_id = await self._discover_screen_id_via_dial(device)
            if screen_id:
                updated_meta = {**device.metadata, "youtube_screen_id": screen_id}
                self._store.upsert_device(device.with_updates(metadata=updated_meta), preserve_alias=True)

        if not screen_id:
            return None

        client = self._get_client()
        with contextlib.suppress(httpx.HTTPError, ValueError):
            resp = await client.post(
                f"{_LOUNGE_BASE}/pairing/get_lounge_token_batch",
                data={"screen_ids": screen_id},
            )
            if resp.status_code == 200:
                payload = resp.json()
                screens = payload.get("screens") if isinstance(payload, dict) else None
                if isinstance(screens, list) and screens:
                    tok = str(screens[0].get("loungeToken") or "").strip()
                    if tok:
                        self._store.set_token(device.device_uid, tok, kind="lounge")
                        return tok
        return None

    async def _send_lounge_command(
        self,
        device: NetworkDeviceRecord,
        command_name: str,
        params: dict[str, Any] | None = None,
    ) -> AdapterActionResult:
        token = await self._ensure_lounge_token(device)
        if not token:
            return AdapterActionResult(
                ok=False,
                message=(
                    f"{device.alias} no está vinculado a YouTube TV Lounge todavía. "
                    "Obtén el código en YouTube TV (Ajustes -> Vincular con código de TV)."
                ),
                error_code="lounge_not_paired",
            )

        client = self._get_client()
        form_data: dict[str, str] = {
            "count": "1",
            "req0__sc": command_name,
        }
        for key, value in (params or {}).items():
            form_data[f"req0_{key}"] = str(value)

        query_params = {
            "device": "REMOTE_CONTROL",
            "name": "DeusEx",
            "app": "youtube-desktop",
            "mdx-version": "3",
            "loungeIdToken": token,
            "VER": "8",
            "v": "2",
            "RID": "1",
            "CVER": "1",
        }
        try:
            resp = await client.post(
                f"{_LOUNGE_BASE}/bc/bind",
                params=query_params,
                data=form_data,
            )
            if resp.status_code in (401, 403):
                self._store.delete_token(device.device_uid, kind="lounge")
                return AdapterActionResult(
                    ok=False,
                    message=f"El token de YouTube TV para {device.alias} expiró; vuelve a vincularlo.",
                    error_code="lounge_token_expired",
                )
            resp.raise_for_status()
            return AdapterActionResult(
                ok=True,
                message=f"Comando YouTube TV ({command_name}) enviado a {device.alias}.",
                data={"command": command_name, "transport": "lounge"},
            )
        except httpx.HTTPError as exc:
            return AdapterActionResult(
                ok=False,
                message=f"Error enviando comando YouTube Lounge a {device.alias}: {exc}",
                error_code="lounge_http_error",
            )

    async def _launch_via_dial_fallback(
        self,
        device: NetworkDeviceRecord,
        video_id: str,
    ) -> AdapterActionResult | None:
        if not device.last_ip:
            return None
        client = self._get_client()
        for port in (8008, 8080):
            with contextlib.suppress(httpx.HTTPError, OSError):
                resp = await client.post(
                    f"http://{device.last_ip}:{port}/apps/YouTube",
                    content=f"v={video_id}",
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
                if resp.status_code in (200, 201, 204):
                    return AdapterActionResult(
                        ok=True,
                        message=f"Video de YouTube enviado a {device.alias} vía DIAL.",
                        data={"video_id": video_id, "transport": "dial"},
                    )
        return None

    async def send_video(
        self,
        device: NetworkDeviceRecord,
        *,
        url_or_id: str = "",
    ) -> AdapterActionResult:
        source_url = url_or_id.strip()
        video_id = extract_youtube_video_id(source_url) if source_url else None

        if video_id is None and self._browser_reader is not None:
            tab = await self._browser_reader.read_active_tab(prefer_youtube=True)
            if tab is not None and tab.video_id:
                video_id = tab.video_id
                source_url = tab.url

        if not video_id:
            return AdapterActionResult(
                ok=False,
                message="No se detectó un video válido de YouTube en la orden ni en el navegador activo.",
                error_code="no_youtube_video",
            )

        lounge_res = await self._send_lounge_command(
            device,
            "setPlaylist",
            {"videoId": video_id, "currentIndex": "0", "videoIds": json.dumps([video_id])},
        )
        if lounge_res.ok:
            return AdapterActionResult(
                ok=True,
                message=f"Video de YouTube enviado a {device.alias}.",
                data={"video_id": video_id, "url": source_url, "transport": "lounge"},
            )

        if lounge_res.error_code == "lounge_not_paired":
            dial_res = await self._launch_via_dial_fallback(device, video_id)
            if dial_res is not None:
                return dial_res

        return lounge_res

    async def set_volume(
        self,
        device: NetworkDeviceRecord,
        *,
        level: int | None = None,
        delta: int | None = None,
        mute: bool | None = None,
    ) -> AdapterActionResult:
        _ = (delta, mute)
        if level is None:
            return AdapterActionResult(
                ok=False,
                message="YouTube TV Lounge requiere un nivel de volumen entre 0 y 100.",
                error_code="invalid_args",
            )
        clamped = max(0, min(100, int(level)))
        return await self._send_lounge_command(device, "setVolume", {"volume": str(clamped)})

    async def media_control(
        self,
        device: NetworkDeviceRecord,
        action: str,
        *,
        media_url: str = "",
        title: str = "",
    ) -> AdapterActionResult:
        _ = title
        clean = action.strip().lower()
        if clean in ("cast", "play_media", "send_video"):
            return await self.send_video(device, url_or_id=media_url)

        cmd_map = {
            "play": "play",
            "resume": "play",
            "pause": "pause",
            "stop": "stopVideo",
            "next": "next",
            "previous": "previous",
        }
        lounge_cmd = cmd_map.get(clean)
        if lounge_cmd is None:
            return AdapterActionResult(
                ok=False,
                message=f"Acción no soportada en YouTube TV: {action}",
                error_code="unsupported_action",
            )
        return await self._send_lounge_command(device, lounge_cmd)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.aclose(), timeout=0.6)
        self._client = None
