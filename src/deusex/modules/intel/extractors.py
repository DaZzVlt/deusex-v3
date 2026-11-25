from __future__ import annotations

import asyncio
from collections.abc import Callable
import ctypes
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
import sys
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from deusex.modules.intel.web_search import WebSearchService

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 8_000
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 40

_ALLOWED_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml"}
)
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CF_UNICODETEXT = 13

type YouTubeTranscriptFetcher = Callable[[str, tuple[str, ...]], str]


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    source_kind: Literal["url", "pdf", "text_file", "youtube", "clipboard"]
    source_ref: str
    title: str
    text: str
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_sources(self) -> tuple[dict[str, str], ...]:
        if self.source_kind in ("url", "youtube") and self.source_ref.startswith(
            ("http://", "https://")
        ):
            return ({"title": self.title or self.source_ref, "url": self.source_ref},)
        return ()


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    limit = max(200, int(max_chars))
    clean = text.strip()
    if len(clean) <= limit:
        return clean, False
    return clean[:limit].rstrip() + "...", True


def extract_youtube_video_id(url_or_id: str) -> str:
    """Extrae el ID de 11 caracteres desde una URL o ID directo de YouTube."""
    raw = url_or_id.strip()
    if not raw:
        raise ValueError("Debes proporcionar una URL o ID de video de YouTube.")
    if _YT_ID_RE.match(raw):
        return raw

    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")

    video_id = ""
    if host == "youtu.be":
        segments = [seg for seg in parsed.path.split("/") if seg]
        if segments:
            video_id = segments[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            vals = qs.get("v")
            if vals:
                video_id = vals[0]
        else:
            segments = [seg for seg in parsed.path.split("/") if seg]
            if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live", "v"):
                video_id = segments[1]

    if not _YT_ID_RE.match(video_id):
        raise ValueError(f"No se pudo identificar un video de YouTube válido en '{url_or_id}'.")
    return video_id


def read_windows_clipboard_text() -> str:
    """Lee texto Unicode del portapapeles de Windows 64-bit sin importar otros módulos."""
    if sys.platform != "win32":
        return ""

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return ""

    user32 = windll.user32
    kernel32 = windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool

    if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
        return ""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return str(ctypes.wstring_at(ptr))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _read_pdf_sync(path: Path, *, max_pages: int) -> tuple[str, int, int]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        raise ValueError("El PDF está protegido con contraseña y no se puede leer.")

    total_pages = len(reader.pages)
    pages_to_read = min(total_pages, max(1, max_pages))
    chunks: list[str] = []
    for idx in range(pages_to_read):
        page_text = str(reader.pages[idx].extract_text() or "").strip()
        if page_text:
            chunks.append(page_text)
    return "\n\n".join(chunks), pages_to_read, total_pages


def _inspect_pdf_sync(
    file_path: str | Path, *, max_pages: int
) -> tuple[Path, str, int, int, int]:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No encontré el archivo PDF: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"El archivo '{path.name}' no tiene extensión .pdf.")
    size_bytes = path.stat().st_size
    if size_bytes > MAX_PDF_BYTES:
        raise ValueError(
            f"El PDF supera el límite seguro de {MAX_PDF_BYTES // (1024 * 1024)} MB."
        )
    raw_text, pages_read, total_pages = _read_pdf_sync(path, max_pages=max_pages)
    return path, raw_text, pages_read, total_pages, size_bytes


def _read_text_file_sync(path: Path) -> str:
    raw_bytes = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _inspect_text_file_sync(file_path: str | Path) -> tuple[Path, str, int]:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"No encontré el archivo de texto: {path}")
    if path.suffix.lower() not in _ALLOWED_TEXT_SUFFIXES:
        raise ValueError(
            f"Extensión '{path.suffix}' no soportada para lectura directa de texto."
        )
    size_bytes = path.stat().st_size
    if size_bytes > MAX_TEXT_FILE_BYTES:
        raise ValueError(
            f"El archivo supera el límite seguro de {MAX_TEXT_FILE_BYTES // (1024 * 1024)} MB."
        )
    raw_text = _read_text_file_sync(path)
    return path, raw_text, size_bytes


def _extract_snippet_text(items: Any) -> str:
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            txt = str(item.get("text") or "").strip()
        else:
            txt = str(getattr(item, "text", "") or "").strip()
        if txt:
            parts.append(txt)
    return " ".join(parts)


def _fetch_youtube_transcript_sync(video_id: str, languages: tuple[str, ...]) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    lang_list = list(languages)
    if hasattr(YouTubeTranscriptApi, "fetch"):
        api = YouTubeTranscriptApi()
        try:
            fetched = api.fetch(video_id, languages=lang_list)
            text = _extract_snippet_text(fetched)
            if text:
                return text
        except Exception:
            transcript_list = api.list(video_id)
            for transcript in transcript_list:
                fetched_any = transcript.fetch()
                text = _extract_snippet_text(fetched_any)
                if text:
                    return text
            raise

    legacy_get = getattr(YouTubeTranscriptApi, "get_transcript", None)
    if callable(legacy_get):
        fetched_legacy = legacy_get(video_id, languages=lang_list)
        return _extract_snippet_text(fetched_legacy)

    raise RuntimeError("No se encontró un método compatible en youtube-transcript-api.")


class ContentExtractorService:
    """
    Extractores con límites seguros de tamaño y protección estricta de Privacidad:
    URL (trafilatura), PDF (pypdf), TXT/MD, YouTube transcript y portapapeles.
    """

    def __init__(
        self,
        *,
        web_search: WebSearchService,
        clipboard_reader: Callable[[], str] | None = None,
        youtube_fetcher: YouTubeTranscriptFetcher | None = None,
    ) -> None:
        self._web_search = web_search
        self._clipboard_reader: Callable[[], str] = clipboard_reader or read_windows_clipboard_text
        self._youtube_fetcher: YouTubeTranscriptFetcher = (
            youtube_fetcher or _fetch_youtube_transcript_sync
        )

    async def extract_url(
        self,
        url: str,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ExtractedDocument:
        page = await self._web_search.fetch_and_extract(url, max_chars=max(200, int(max_chars)))
        return ExtractedDocument(
            source_kind="url",
            source_ref=page.url,
            title=page.title,
            text=page.text,
            truncated=page.truncated,
            metadata={"char_count": len(page.text)},
        )

    async def extract_pdf(
        self,
        file_path: str | Path,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_pages: int = MAX_PDF_PAGES,
    ) -> ExtractedDocument:
        path, raw_text, pages_read, total_pages, size_bytes = await asyncio.to_thread(
            _inspect_pdf_sync, file_path, max_pages=max_pages
        )
        text, text_truncated = _truncate_text(raw_text, max_chars)
        truncated = text_truncated or (pages_read < total_pages)
        return ExtractedDocument(
            source_kind="pdf",
            source_ref=str(path),
            title=path.name,
            text=text,
            truncated=truncated,
            metadata={
                "pages_read": pages_read,
                "total_pages": total_pages,
                "size_bytes": size_bytes,
            },
        )

    async def extract_text_file(
        self,
        file_path: str | Path,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ExtractedDocument:
        path, raw_text, size_bytes = await asyncio.to_thread(_inspect_text_file_sync, file_path)
        text, truncated = _truncate_text(raw_text, max_chars)
        return ExtractedDocument(
            source_kind="text_file",
            source_ref=str(path),
            title=path.name,
            text=text,
            truncated=truncated,
            metadata={"size_bytes": size_bytes, "char_count": len(text)},
        )

    async def extract_file(
        self,
        file_path: str | Path,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ExtractedDocument:
        path = Path(file_path)
        if path.suffix.lower() == ".pdf":
            return await self.extract_pdf(path, max_chars=max_chars)
        return await self.extract_text_file(path, max_chars=max_chars)

    async def extract_youtube_transcript(
        self,
        url_or_id: str,
        *,
        languages: tuple[str, ...] = ("es", "es-419", "es-ES", "en"),
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ExtractedDocument:
        video_id = extract_youtube_video_id(url_or_id)
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        raw_text = await asyncio.to_thread(self._youtube_fetcher, video_id, languages)
        text, truncated = _truncate_text(raw_text, max_chars)
        return ExtractedDocument(
            source_kind="youtube",
            source_ref=canonical_url,
            title=f"YouTube ({video_id})",
            text=text,
            truncated=truncated,
            metadata={"video_id": video_id, "char_count": len(text)},
        )

    async def extract_clipboard(
        self,
        *,
        privacy_mode: bool,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> ExtractedDocument:
        if privacy_mode:
            raise PermissionError(
                "La lectura del portapapeles está bloqueada mientras el Modo Privacidad está activo."
            )
        raw_text = await asyncio.to_thread(self._clipboard_reader)
        text, truncated = _truncate_text(raw_text, max_chars)
        return ExtractedDocument(
            source_kind="clipboard",
            source_ref="clipboard",
            title="Portapapeles",
            text=text,
            truncated=truncated,
            metadata={"char_count": len(text)},
        )
