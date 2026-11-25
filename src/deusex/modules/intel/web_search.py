from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
from html.parser import HTMLParser
import logging
import re
from typing import Literal
from urllib.parse import parse_qs, unquote, urlparse
import webbrowser

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_MAX_DOWNLOAD_BYTES = 2_000_000
_WHITESPACE_RE = re.compile(r"\s+")
_BARE_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/.*)?$")


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    extracted_text: str = ""


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    url: str
    title: str
    text: str
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SearchReport:
    query: str
    provider: str
    mode: Literal["fast", "deep"]
    hits: tuple[SearchHit, ...]
    answer_summary: str = ""

    def to_sources(self) -> tuple[dict[str, str], ...]:
        seen: set[str] = set()
        sources: list[dict[str, str]] = []
        for hit in self.hits:
            if hit.url and hit.url not in seen:
                seen.add(hit.url)
                sources.append({"title": hit.title or hit.url, "url": hit.url})
        return tuple(sources)


type GeminiSearchCallback = Callable[[str], Awaitable[SearchReport | None]]
type BrowserOpener = Callable[[str], bool]


def validate_http_url(raw_url: str) -> str:
    """Valida y normaliza una URL HTTP/HTTPS segura (rechaza esquemas locales o scripts)."""
    candidate = raw_url.strip().strip("<>\"' ")
    if not candidate:
        raise ValueError("La URL está vacía.")
    if "://" not in candidate and _BARE_DOMAIN_RE.match(candidate):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Esquema de URL no permitido: {parsed.scheme or 'desconocido'}.")
    if not parsed.netloc or "." not in parsed.netloc:
        raise ValueError(f"Host de URL inválido: {candidate!r}.")
    return parsed.geturl()


def _unwrap_ddg_href(raw_href: str) -> str:
    href = raw_href.strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = f"https:{href}"
    elif href.startswith("/"):
        href = f"https://duckduckgo.com{href}"

    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg")
        if uddg and uddg[0]:
            href = unquote(uddg[0])

    try:
        return validate_http_url(href)
    except ValueError:
        return ""


def _clean_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


class _DuckDuckGoHTMLParser(HTMLParser):
    """Parser liviano sin dependencias extra para DuckDuckGo HTML y Lite."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._in_title = False
        self._in_snippet = False
        self._current_url = ""
        self._title_buf: list[str] = []
        self._snippet_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = set(attr_map.get("class", "").split())

        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            resolved = _unwrap_ddg_href(attr_map.get("href", ""))
            if resolved:
                self._in_title = True
                self._current_url = resolved
                self._title_buf.clear()
        elif tag in ("a", "div", "td", "span") and (
            "result__snippet" in classes or "result-snippet" in classes
        ):
            self._in_snippet = True
            self._snippet_buf.clear()

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            title = _clean_text("".join(self._title_buf))
            if self._current_url and title:
                self.hits.append(SearchHit(title=title, url=self._current_url))
            self._in_title = False
            self._current_url = ""
            self._title_buf.clear()
        elif self._in_snippet and tag in ("a", "div", "td", "span"):
            snippet = _clean_text("".join(self._snippet_buf))
            if snippet and self.hits and not self.hits[-1].snippet:
                last = self.hits[-1]
                self.hits[-1] = SearchHit(
                    title=last.title,
                    url=last.url,
                    snippet=snippet,
                    extracted_text=last.extracted_text,
                )
            self._in_snippet = False
            self._snippet_buf.clear()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        elif self._in_snippet:
            self._snippet_buf.append(data)


def parse_duckduckgo_html(html_text: str, *, max_results: int = 6) -> list[SearchHit]:
    parser = _DuckDuckGoHTMLParser()
    parser.feed(html_text)
    parser.close()

    deduped: list[SearchHit] = []
    seen: set[str] = set()
    for hit in parser.hits:
        if hit.url in seen:
            continue
        seen.add(hit.url)
        deduped.append(hit)
        if len(deduped) >= max_results:
            break
    return deduped


def _extract_title_from_html(html_text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _clean_text(re.sub(r"<[^>]+>", "", match.group(1)))


def _extract_with_trafilatura(html_text: str, *, url: str) -> str:
    try:
        import trafilatura
    except ImportError:
        logger.warning("trafilatura no está disponible; usando limpieza HTML básica.")
        plain = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
        plain = re.sub(r"<[^>]+>", " ", plain)
        return _clean_text(plain)

    extracted = trafilatura.extract(
        html_text,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if isinstance(extracted, str) and extracted.strip():
        return extracted.strip()
    return ""


class WebSearchService:
    """
    Búsqueda web con 0 claves obligatorias (DuckDuckGo + trafilatura)
    y soporte opcional para Gemini Search si se inyecta desde `app.py`.
    """

    def __init__(
        self,
        *,
        gemini_search_fn: GeminiSearchCallback | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        browser_opener: BrowserOpener | None = None,
    ) -> None:
        self._gemini_search_fn = gemini_search_fn
        self._transport = transport
        self._browser_opener: BrowserOpener = browser_opener or (
            lambda u: bool(webbrowser.open(u, new=2))
        )
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=True,
                headers={
                    "User-Agent": _DEFAULT_USER_AGENT,
                    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                },
                timeout=httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def fetch_and_extract(
        self,
        url: str,
        *,
        max_chars: int = 3500,
        timeout_sec: float = 10.0,
    ) -> ExtractedPage:
        clean_url = validate_http_url(url)
        client = self._get_client()

        raw_chunks: list[bytes] = []
        total_bytes = 0
        encoding = "utf-8"

        async with client.stream(
            "GET",
            clean_url,
            timeout=httpx.Timeout(connect=5.0, read=timeout_sec, write=5.0, pool=5.0),
        ) as resp:
            resp.raise_for_status()
            if resp.encoding:
                encoding = resp.encoding
            async for chunk in resp.aiter_bytes(chunk_size=16384):
                remaining = _MAX_DOWNLOAD_BYTES - total_bytes
                if remaining <= 0:
                    break
                piece = chunk[:remaining]
                raw_chunks.append(piece)
                total_bytes += len(piece)

        html_text = b"".join(raw_chunks).decode(encoding, errors="replace")
        title = _extract_title_from_html(html_text) or clean_url
        extracted = await asyncio.to_thread(_extract_with_trafilatura, html_text, url=clean_url)
        truncated = len(extracted) > max_chars
        if truncated:
            extracted = extracted[:max_chars].rstrip() + "..."
        return ExtractedPage(url=clean_url, title=title, text=extracted, truncated=truncated)

    async def _search_duckduckgo(
        self,
        query: str,
        *,
        max_results: int = 5,
        timeout_sec: float = 10.0,
    ) -> list[SearchHit]:
        client = self._get_client()
        req_timeout = httpx.Timeout(connect=5.0, read=timeout_sec, write=5.0, pool=5.0)

        for endpoint in (_DDG_HTML_URL, _DDG_LITE_URL):
            try:
                resp = await client.post(
                    endpoint,
                    data={"q": query, "kl": "cl-es"},
                    timeout=req_timeout,
                )
                if resp.status_code == 200 and resp.text:
                    hits = parse_duckduckgo_html(resp.text, max_results=max_results)
                    if hits:
                        return hits
            except httpx.HTTPError as exc:
                logger.debug("Fallo consultando %s para %r: %s", endpoint, query, exc)
                continue
        return []

    async def search(
        self,
        query: str,
        *,
        mode: Literal["fast", "deep"] = "fast",
        max_results: int = 5,
        prefer_gemini: bool = False,
        max_chars_per_page: int = 1800,
    ) -> SearchReport:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("La consulta de búsqueda está vacía.")

        bounded_max = max(1, min(10, int(max_results)))

        if prefer_gemini and self._gemini_search_fn is not None:
            try:
                gemini_report = await self._gemini_search_fn(clean_query)
                if gemini_report is not None and (gemini_report.hits or gemini_report.answer_summary):
                    return gemini_report
            except Exception as exc:
                logger.debug("Gemini Search no disponible (%s); usando DuckDuckGo.", exc)

        hits = await self._search_duckduckgo(clean_query, max_results=bounded_max)
        if mode == "deep" and hits:
            deep_count = min(3, len(hits))
            tasks = [
                self.fetch_and_extract(hit.url, max_chars=max_chars_per_page, timeout_sec=8.0)
                for hit in hits[:deep_count]
            ]
            extracted_results = await asyncio.gather(*tasks, return_exceptions=True)
            enriched: list[SearchHit] = []
            for idx, hit in enumerate(hits):
                if idx < deep_count and isinstance(extracted_results[idx], ExtractedPage):
                    page = extracted_results[idx]
                    assert isinstance(page, ExtractedPage)
                    enriched.append(
                        SearchHit(
                            title=hit.title or page.title,
                            url=hit.url,
                            snippet=hit.snippet,
                            extracted_text=page.text,
                        )
                    )
                else:
                    enriched.append(hit)
            hits = enriched

        return SearchReport(
            query=clean_query,
            provider="duckduckgo",
            mode=mode,
            hits=tuple(hits),
        )

    async def open_url(self, url: str) -> tuple[bool, str]:
        """Abre una URL HTTP/HTTPS validada en el navegador predeterminado del usuario."""
        clean_url = validate_http_url(url)
        opened = await asyncio.to_thread(self._browser_opener, clean_url)
        return opened, clean_url

    def format_report_text(self, report: SearchReport) -> str:
        lines: list[str] = []
        if report.answer_summary:
            lines.append(report.answer_summary)
        if not report.hits:
            return report.answer_summary or f"No encontré resultados web para '{report.query}'."
        for idx, hit in enumerate(report.hits, start=1):
            body = hit.extracted_text or hit.snippet
            preview = body[:360].rstrip() + ("..." if len(body) > 360 else "") if body else ""
            if preview:
                lines.append(f"{idx}. {hit.title}: {preview}")
            else:
                lines.append(f"{idx}. {hit.title} ({hit.url})")
        return "\n".join(lines)


def _ignore_noop() -> None:
    with contextlib.suppress(Exception):
        return
