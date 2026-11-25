from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import ctypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
import logging
import sys
import threading
from typing import Literal
import uuid

from PIL import Image

from deusex.brain.providers import MediaAttachment
from deusex.core.events import VisionShareRequestedEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_EDGE = 1280
DEFAULT_JPEG_QUALITY = 80

type MonitorSelector = int | Literal["active", "all", "primary"]
type ShareTarget = Literal["discord", "whatsapp", "mobile"]
type RawGrabResult = tuple[bytes, int, int, int]  # (rgb_bytes, width, height, monitor_index)


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    index: int  # 0 = todos combinados, 1..N = monitores físicos
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False
    is_combined: bool = False


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    capture_id: str
    monitor_index: int
    width: int
    height: int
    mime_type: str
    image_bytes: bytes
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "manual"

    @property
    def size_bytes(self) -> int:
        return len(self.image_bytes)

    @property
    def filename(self) -> str:
        ext = "png" if self.mime_type == "image/png" else "jpg"
        return f"deusex_screen_m{self.monitor_index}_{self.capture_id[:8]}.{ext}"

    def to_media_attachment(self) -> MediaAttachment:
        return MediaAttachment(
            mime_type=self.mime_type,
            data=self.image_bytes,
            filename=self.filename,
        )


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
    ]


def _get_active_focus_point_win32() -> tuple[int, int] | None:
    """Obtiene el punto central de la ventana activa o del cursor en coordenadas físicas."""
    if sys.platform != "win32":
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    user32 = windll.user32

    with contextlib.suppress(Exception):
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            rect = _RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w > 0 and h > 0:
                    return (rect.left + w // 2, rect.top + h // 2)

    with contextlib.suppress(Exception):
        pt = _POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))

    return None


def resolve_active_monitor_index(
    monitors: list[MonitorInfo],
    focus_point: tuple[int, int] | None = None,
) -> int:
    """Devuelve el índice (1..N) del monitor que contiene la ventana activa o el cursor."""
    physical = [m for m in monitors if not m.is_combined and m.index >= 1]
    if not physical:
        return 1

    point = focus_point if focus_point is not None else _get_active_focus_point_win32()
    if point is not None:
        px, py = point
        for mon in physical:
            if mon.left <= px < (mon.left + mon.width) and mon.top <= py < (mon.top + mon.height):
                return mon.index

    primary = next((m.index for m in physical if m.is_primary), physical[0].index)
    return primary


def compress_rgb_image(
    rgb_bytes: bytes,
    width: int,
    height: int,
    *,
    max_edge: int = DEFAULT_MAX_EDGE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    output_format: Literal["jpeg", "png"] = "jpeg",
) -> tuple[bytes, int, int, str]:
    """Redimensiona proporcionalmente y comprime un frame RGB para envío multimodal ligero."""
    if width <= 0 or height <= 0:
        raise ValueError("Dimensiones de captura inválidas.")

    img = Image.frombytes("RGB", (width, height), rgb_bytes)
    longest = max(width, height)
    bounded_edge = max(320, int(max_edge))
    if longest > bounded_edge:
        scale = bounded_edge / float(longest)
        new_w = max(1, round(width * scale))
        new_h = max(1, round(height * scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    out_w, out_h = img.size
    buf = BytesIO()
    if output_format == "png":
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), out_w, out_h, "image/png"

    quality = max(40, min(95, int(jpeg_quality)))
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), out_w, out_h, "image/jpeg"


def _attach_thread_to_input_desktop() -> None:
    """Asegura que el hilo actual esté vinculado al escritorio interactivo activo (Default) en Windows."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if not hdesk:
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            user32.CloseDesktop(hdesk)
    except Exception:
        pass


def _list_monitors_mss_sync() -> list[MonitorInfo]:
    from mss import MSS

    _attach_thread_to_input_desktop()
    with MSS() as sct:
        result: list[MonitorInfo] = []
        for idx, raw in enumerate(sct.monitors):
            result.append(
                MonitorInfo(
                    index=idx,
                    left=int(raw["left"]),
                    top=int(raw["top"]),
                    width=int(raw["width"]),
                    height=int(raw["height"]),
                    is_primary=(idx == 1),
                    is_combined=(idx == 0),
                )
            )
        return result


def _grab_monitor_mss_sync(
    monitor: MonitorSelector,
    focus_point_provider: Callable[[], tuple[int, int] | None],
) -> RawGrabResult:
    from mss import MSS

    _attach_thread_to_input_desktop()
    with MSS() as sct:
        raw_monitors = sct.monitors
        if not raw_monitors:
            raise RuntimeError("No se detectaron monitores disponibles.")

        infos = [
            MonitorInfo(
                index=idx,
                left=int(raw["left"]),
                top=int(raw["top"]),
                width=int(raw["width"]),
                height=int(raw["height"]),
                is_primary=(idx == 1),
                is_combined=(idx == 0),
            )
            for idx, raw in enumerate(raw_monitors)
        ]

        if monitor == "all":
            target_idx = 0
        elif monitor == "primary":
            target_idx = 1 if len(raw_monitors) > 1 else 0
        elif monitor == "active":
            target_idx = resolve_active_monitor_index(infos, focus_point_provider())
        else:
            target_idx = int(monitor)

        if target_idx < 0 or target_idx >= len(raw_monitors):
            raise ValueError(
                f"Índice de monitor fuera de rango ({target_idx}). "
                f"Monitores físicos disponibles: 1..{max(1, len(raw_monitors) - 1)} (0 = todos)."
            )

        shot = sct.grab(raw_monitors[target_idx])
        return bytes(shot.rgb), int(shot.width), int(shot.height), target_idx


def build_share_event(
    frame: CapturedFrame,
    *,
    target: ShareTarget,
    recipient: str = "",
    caption: str = "",
) -> VisionShareRequestedEvent:
    """Construye el contrato `VisionShareRequestedEvent` para Rebanadas 8-10 sin imports cruzados."""
    clean_target = target.strip().lower()
    if clean_target not in ("discord", "whatsapp", "mobile"):
        raise ValueError(
            f"Destino de envío no soportado: {target!r}. Usa 'discord', 'whatsapp' o 'mobile'."
        )
    return VisionShareRequestedEvent(
        share_id=uuid.uuid4().hex[:12],
        target=clean_target,
        recipient=recipient.strip(),
        caption=caption.strip(),
        mime_type=frame.mime_type,
        image_bytes=frame.image_bytes,
        filename=frame.filename,
    )


type MonitorLister = Callable[[], list[MonitorInfo]]
type ScreenGrabber = Callable[[MonitorSelector], RawGrabResult]


class ScreenCaptureService:
    """
    Servicio de captura multi-monitor DPI-aware sobre `mss` + `Pillow`.
    Cada captura abre y libera su contexto `mss` de inmediato (0 handles GDI retenidos, cierre <1 s).
    """

    def __init__(
        self,
        *,
        monitor_lister: MonitorLister | None = None,
        screen_grabber: ScreenGrabber | None = None,
        focus_point_provider: Callable[[], tuple[int, int] | None] | None = None,
        default_max_edge: int = DEFAULT_MAX_EDGE,
        default_jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> None:
        self._focus_point_provider = focus_point_provider or _get_active_focus_point_win32
        self._monitor_lister: MonitorLister = monitor_lister or _list_monitors_mss_sync
        self._screen_grabber: ScreenGrabber = screen_grabber or (
            lambda sel: _grab_monitor_mss_sync(sel, self._focus_point_provider)
        )
        self._default_max_edge = max(320, int(default_max_edge))
        self._default_jpeg_quality = max(40, min(95, int(default_jpeg_quality)))
        self._lock = threading.RLock()
        self._closed = False
        self._last_capture: CapturedFrame | None = None

    @property
    def last_capture(self) -> CapturedFrame | None:
        with self._lock:
            return self._last_capture

    def reopen(self) -> None:
        with self._lock:
            self._closed = False

    def clear_last_capture(self) -> None:
        with self._lock:
            self._last_capture = None

    async def close(self) -> None:
        with self._lock:
            self._closed = True
            self._last_capture = None

    async def list_monitors(self) -> list[MonitorInfo]:
        return await asyncio.to_thread(self._monitor_lister)

    async def capture(
        self,
        *,
        monitor: MonitorSelector = "active",
        privacy_mode: bool = False,
        max_edge: int | None = None,
        jpeg_quality: int | None = None,
        output_format: Literal["jpeg", "png"] = "jpeg",
        source: str = "manual",
    ) -> CapturedFrame:
        if privacy_mode:
            raise PermissionError(
                "Las capturas de pantalla están bloqueadas mientras el Modo Privacidad está activo."
            )
        with self._lock:
            if self._closed:
                raise RuntimeError("ScreenCaptureService está detenido.")

        target_edge = self._default_max_edge if max_edge is None else max_edge
        target_quality = self._default_jpeg_quality if jpeg_quality is None else jpeg_quality

        def _capture_and_encode() -> CapturedFrame:
            rgb_bytes, raw_w, raw_h, resolved_idx = self._screen_grabber(monitor)
            encoded, out_w, out_h, mime_type = compress_rgb_image(
                rgb_bytes,
                raw_w,
                raw_h,
                max_edge=target_edge,
                jpeg_quality=target_quality,
                output_format=output_format,
            )
            return CapturedFrame(
                capture_id=uuid.uuid4().hex[:12],
                monitor_index=resolved_idx,
                width=out_w,
                height=out_h,
                mime_type=mime_type,
                image_bytes=encoded,
                source=source,
            )

        frame = await asyncio.to_thread(_capture_and_encode)
        with self._lock:
            if self._closed:
                raise RuntimeError("ScreenCaptureService fue detenido durante la captura.")
            self._last_capture = frame
        return frame
