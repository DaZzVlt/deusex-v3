from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import html
import logging
import re
import time
import unicodedata
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

_GOOGLE_NEWS_BASE = "https://news.google.com/rss"
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_TOPIC_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_TOPIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "para",
        "como",
        "sobre",
        "tras",
        "desde",
        "hasta",
        "esta",
        "este",
        "estos",
        "estas",
        "pero",
        "porque",
        "cuando",
        "donde",
        "quien",
        "cual",
        "entre",
        "hacia",
        "segun",
        "todo",
        "toda",
        "todos",
        "todas",
        "algo",
        "otro",
        "otra",
        "otros",
        "otras",
        "mismo",
        "misma",
        "tiene",
        "tienen",
        "sera",
        "fueron",
        "hace",
        "puede",
        "pueden",
        "contra",
        "ante",
        "bajo",
        "durante",
        "mediante",
        "luego",
        "despues",
        "antes",
        "aqui",
        "ahora",
        "solo",
        "chile",
        "concepcion",
        "biobio",
        "nuevo",
        "nueva",
        "nuevos",
        "nuevas",
        "ultimo",
        "ultima",
        "ultimos",
        "ultimas",
        "confirma",
        "anuncia",
        "vuelve",
        "regresa",
        "region",
        "pais",
        "mundo",
        "noticias",
        "noticia",
        "video",
        "fotos",
        "vivo",
        "directo",
    }
)

# País normalizado -> (gl, hl, ceid)
_COUNTRY_LOCALE_MAP: dict[str, tuple[str, str, str]] = {
    "chile": ("CL", "es-419", "CL:es-419"),
    "cl": ("CL", "es-419", "CL:es-419"),
    "argentina": ("AR", "es-419", "AR:es-419"),
    "ar": ("AR", "es-419", "AR:es-419"),
    "peru": ("PE", "es-419", "PE:es-419"),
    "pe": ("PE", "es-419", "PE:es-419"),
    "colombia": ("CO", "es-419", "CO:es-419"),
    "co": ("CO", "es-419", "CO:es-419"),
    "mexico": ("MX", "es-419", "MX:es-419"),
    "mx": ("MX", "es-419", "MX:es-419"),
    "espana": ("ES", "es", "ES:es"),
    "spain": ("ES", "es", "ES:es"),
    "es": ("ES", "es", "ES:es"),
    "estados unidos": ("US", "en-US", "US:en"),
    "usa": ("US", "en-US", "US:en"),
    "us": ("US", "en-US", "US:en"),
}

_TOPIC_MAP: dict[str, str] = {
    "tecnologia": "TECHNOLOGY",
    "tech": "TECHNOLOGY",
    "ciencia": "SCIENCE",
    "negocios": "BUSINESS",
    "economia": "BUSINESS",
    "deportes": "SPORTS",
    "entretenimiento": "ENTERTAINMENT",
    "salud": "HEALTH",
    "mundo": "WORLD",
    "internacional": "WORLD",
    "nacional": "NATION",
}


def _normalize_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _WHITESPACE_RE.sub(" ", without_accents)


def _extract_topic_tokens(title: str) -> frozenset[str]:
    """Extrae tokens temáticos significativos (>=4 chars sin tildes ni stopwords)."""
    norm = _normalize_key(title)
    return frozenset(
        tok for tok in _TOPIC_TOKEN_RE.findall(norm) if tok not in _TOPIC_STOPWORDS
    )


def _is_duplicate_topic(
    tokens: frozenset[str],
    accepted_token_sets: list[frozenset[str]],
) -> bool:
    if not tokens:
        return False
    for prev in accepted_token_sets:
        if not prev:
            continue
        overlap = len(tokens & prev)
        if overlap >= 2:
            return True
        union = len(tokens | prev)
        if union > 0 and (overlap / union) >= 0.40:
            return True
    return False


def resolve_news_locale(country: str = "", language: str = "es-CL") -> tuple[str, str, str]:
    """Devuelve (gl, hl, ceid) de Google News RSS a partir de país/idioma configurados."""
    if country.strip():
        key = _normalize_key(country)
        if key in _COUNTRY_LOCALE_MAP:
            return _COUNTRY_LOCALE_MAP[key]

    lang_clean = language.strip().lower()
    if "-" in lang_clean:
        _, region = lang_clean.split("-", 1)
        if region in _COUNTRY_LOCALE_MAP:
            return _COUNTRY_LOCALE_MAP[region]
    if lang_clean.startswith("en"):
        return ("US", "en-US", "US:en")
    return ("CL", "es-419", "CL:es-419")


@dataclass(frozen=True, slots=True)
class NewsItem:
    title: str
    url: str
    source: str = ""
    published: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class NewsReport:
    query_label: str
    locale: str
    items: tuple[NewsItem, ...]
    used_interests: tuple[str, ...] = ()
    cached: bool = False

    def to_sources(self) -> tuple[dict[str, str], ...]:
        seen: set[str] = set()
        sources: list[dict[str, str]] = []
        for item in self.items:
            if item.url and item.url not in seen:
                seen.add(item.url)
                label = f"{item.title} ({item.source})" if item.source else item.title
                sources.append({"title": label, "url": item.url})
        return tuple(sources)

    def format_text(self) -> str:
        if not self.items:
            return f"No encontré noticias recientes para '{self.query_label}'."
        header = f"Noticias ({self.query_label}):"
        if self.used_interests:
            header += f" [intereses: {', '.join(self.used_interests)}]"
        lines = [header]
        for idx, item in enumerate(self.items, start=1):
            src = f" — {item.source}" if item.source else ""
            lines.append(f"{idx}. {item.title}{src}")
        return "\n".join(lines)


def parse_rss_xml(xml_text: str, *, limit: int = 8) -> list[NewsItem]:
    """Parsea XML de Google News RSS en items normalizados sin dependencias externas."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("Error parseando feed RSS XML: %s", exc)
        return []

    items: list[NewsItem] = []
    seen_titles: set[str] = set()
    accepted_topic_tokens: list[frozenset[str]] = []
    for elem in root.findall(".//item"):
        raw_title = (elem.findtext("title") or "").strip()
        raw_link = (elem.findtext("link") or "").strip()
        raw_source = (elem.findtext("source") or "").strip()
        raw_pub = (elem.findtext("pubDate") or "").strip()
        raw_desc = (elem.findtext("description") or "").strip()

        if not raw_title or not raw_link:
            continue
        if not (raw_link.startswith("http://") or raw_link.startswith("https://")):
            continue

        clean_title = html.unescape(raw_title)
        source = html.unescape(raw_source)
        # Google News suele anexar " - NombreMedio" al título
        if source and clean_title.endswith(f" - {source}"):
            clean_title = clean_title[: -(len(source) + 3)].strip()
        elif not source and " - " in clean_title:
            head, tail = clean_title.rsplit(" - ", 1)
            if head and len(tail) <= 45:
                clean_title, source = head.strip(), tail.strip()

        norm_title = _normalize_key(clean_title)
        if norm_title in seen_titles:
            continue
        topic_tokens = _extract_topic_tokens(clean_title)
        if _is_duplicate_topic(topic_tokens, accepted_topic_tokens):
            continue
        seen_titles.add(norm_title)
        accepted_topic_tokens.append(topic_tokens)

        published = raw_pub
        if raw_pub:
            try:
                published = parsedate_to_datetime(raw_pub).strftime("%d/%m %H:%M")
            except (TypeError, ValueError):
                published = raw_pub[:25]

        clean_desc = _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", html.unescape(raw_desc))).strip()

        items.append(
            NewsItem(
                title=clean_title,
                url=raw_link,
                source=source,
                published=published,
                summary=clean_desc[:220],
            )
        )
        if len(items) >= limit:
            break
    return items


class NewsRssService:
    """
    Google News RSS hiper-contextual (0 claves):
    filtra por país/ciudad e intereses de `MemoryManager` inyectados desde `app.py`.
    """

    def __init__(
        self,
        *,
        interests_provider: Callable[[], list[str]] | None = None,
        cache_ttl_sec: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interests_provider = interests_provider
        self._cache_ttl_sec = max(1.0, float(cache_ttl_sec))
        self._transport = transport
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[tuple[str, str, int], tuple[float, NewsReport]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=True,
                headers={"User-Agent": "DeusEx-v3/3.0 (RSS Reader)"},
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def clear_cache(self) -> None:
        self._cache.clear()

    def _collect_memory_interests(self, max_items: int = 3) -> tuple[str, ...]:
        if self._interests_provider is None:
            return ()
        try:
            raw = self._interests_provider()
        except Exception as exc:
            logger.debug("No se pudieron leer intereses de memoria: %s", exc)
            return ()
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw:
            val = item.strip()
            key = _normalize_key(val)
            if val and key not in seen:
                seen.add(key)
                cleaned.append(val)
            if len(cleaned) >= max_items:
                break
        return tuple(cleaned)

    def _build_feed_url(
        self,
        *,
        query: str,
        city: str,
        country: str,
        language: str,
        interests: tuple[str, ...],
    ) -> tuple[str, str, str]:
        gl, hl, ceid = resolve_news_locale(country=country, language=language)
        locale_qs = f"hl={hl}&gl={gl}&ceid={ceid}"

        clean_query = query.strip()
        if clean_query:
            norm_q = _normalize_key(clean_query)
            if norm_q in _TOPIC_MAP:
                topic_code = _TOPIC_MAP[norm_q]
                url = f"{_GOOGLE_NEWS_BASE}/headlines/section/topic/{topic_code}?{locale_qs}"
                return url, clean_query.capitalize(), ceid
            encoded = quote_plus(clean_query)
            url = f"{_GOOGLE_NEWS_BASE}/search?q={encoded}&{locale_qs}"
            return url, clean_query, ceid

        # Sin tema explícito: combina intereses de memoria y/o ciudad local
        if interests:
            terms = " OR ".join(f'"{t}"' if " " in t else t for t in interests)
            if city.strip():
                search_expr = f"({terms}) OR {city.strip()}"
                label = f"{city.strip()} e intereses"
            else:
                search_expr = terms
                label = "personalizadas"
            encoded = quote_plus(search_expr)
            url = f"{_GOOGLE_NEWS_BASE}/search?q={encoded}&{locale_qs}"
            return url, label, ceid

        if city.strip():
            encoded = quote_plus(city.strip())
            url = f"{_GOOGLE_NEWS_BASE}/search?q={encoded}&{locale_qs}"
            return url, city.strip(), ceid

        return f"{_GOOGLE_NEWS_BASE}?{locale_qs}", country.strip() or "Portada", ceid

    async def get_news(
        self,
        *,
        query: str = "",
        city: str = "",
        country: str = "",
        language: str = "es-CL",
        use_memory_interests: bool = True,
        limit: int = 5,
    ) -> NewsReport:
        bounded_limit = max(1, min(15, int(limit)))
        interests = (
            self._collect_memory_interests()
            if (use_memory_interests and not query.strip())
            else ()
        )
        url, label, ceid = self._build_feed_url(
            query=query,
            city=city,
            country=country,
            language=language,
            interests=interests,
        )

        cache_key = (url, ceid, bounded_limit)
        now = self._clock()
        cached = self._cache.get(cache_key)
        if cached is not None and (now - cached[0]) < self._cache_ttl_sec:
            stored = cached[1]
            return NewsReport(
                query_label=stored.query_label,
                locale=stored.locale,
                items=stored.items,
                used_interests=stored.used_interests,
                cached=True,
            )

        client = self._get_client()
        resp = await client.get(url)
        resp.raise_for_status()
        parsed_items = parse_rss_xml(resp.text, limit=bounded_limit)

        report = NewsReport(
            query_label=label,
            locale=ceid,
            items=tuple(parsed_items),
            used_interests=interests,
            cached=False,
        )
        self._cache[cache_key] = (now, report)
        return report
