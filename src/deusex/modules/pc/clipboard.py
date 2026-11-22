from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
import re
import sys
import time
from typing import Any

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True, slots=True)
class ClipboardInspection:
    has_text: bool
    kind: str
    char_count: int
    word_count: int
    line_count: int
    preview: str
    full_text: str


def _open_clipboard_retry(user32: Any, *, attempts: int = 5) -> bool:
    open_fn = user32.OpenClipboard
    for _ in range(attempts):
        if open_fn(None):
            return True
        time.sleep(0.015)
    return False


def _win32_read_clipboard() -> str:
    if sys.platform != "win32":
        return ""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return ""

    user32 = windll.user32
    kernel32 = windll.kernel32

    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not _open_clipboard_retry(user32):
        raise OSError("El portapapeles de Windows está bloqueado por otra aplicación.")
    try:
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win32_write_clipboard(text: str) -> None:
    if sys.platform != "win32":
        return
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return

    user32 = windll.user32
    kernel32 = windll.kernel32

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard_retry(user32):
        raise OSError("No se pudo abrir el portapapeles de Windows.")
    try:
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            raise MemoryError("GlobalAlloc falló al escribir en el portapapeles.")
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            kernel32.GlobalFree(h_mem)
            raise OSError("GlobalLock falló al escribir en el portapapeles.")
        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(h_mem)
        if not user32.SetClipboardData(_CF_UNICODETEXT, h_mem):
            kernel32.GlobalFree(h_mem)
            raise OSError("SetClipboardData falló.")
    finally:
        user32.CloseClipboard()


def classify_clipboard_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "empty"
    if _URL_RE.match(stripped):
        return "url"
    if _EMAIL_RE.match(stripped):
        return "email"
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        with suppress(json.JSONDecodeError):
            json.loads(stripped)
            return "json"
    if "\n" in stripped and any(
        token in stripped for token in ("def ", "class ", "import ", "function ", "SELECT ", "{")
    ):
        return "code"
    return "text"


class ClipboardService:
    """Portapapeles inteligente Win32 nativo con defensa adicional ante Modo Privacidad."""

    def __init__(
        self,
        *,
        is_privacy_active: Callable[[], bool] | None = None,
        reader: Callable[[], str] = _win32_read_clipboard,
        writer: Callable[[str], None] = _win32_write_clipboard,
    ) -> None:
        self._is_privacy_active = is_privacy_active
        self._reader = reader
        self._writer = writer

    def _assert_not_private(self) -> None:
        if self._is_privacy_active is not None and self._is_privacy_active():
            raise PermissionError("Acceso al portapapeles bloqueado por el Modo Privacidad.")

    def read_text(self) -> str:
        self._assert_not_private()
        return self._reader()

    def write_text(self, text: str, *, dry_run: bool = False) -> tuple[bool, str]:
        self._assert_not_private()
        if dry_run:
            return True, f"[Dry-Run] Copiaría {len(text)} caracteres al portapapeles."
        self._writer(text)
        return True, f"Copié {len(text)} caracteres al portapapeles."

    def inspect(self, *, preview_limit: int = 180) -> ClipboardInspection:
        text = self.read_text()
        if not text:
            return ClipboardInspection(
                has_text=False,
                kind="empty",
                char_count=0,
                word_count=0,
                line_count=0,
                preview="",
                full_text="",
            )
        clean_preview = " ".join(text.split())
        if len(clean_preview) > preview_limit:
            clean_preview = f"{clean_preview[:preview_limit - 1]}…"
        return ClipboardInspection(
            has_text=True,
            kind=classify_clipboard_text(text),
            char_count=len(text),
            word_count=len(text.split()),
            line_count=len(text.splitlines()),
            preview=clean_preview,
            full_text=text,
        )
