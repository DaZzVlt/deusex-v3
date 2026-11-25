from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib.util
import logging
import re
import time
from typing import Any, Literal
import uuid

import httpx

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
from deusex.core.events import ResearchExportRequestedEvent
from deusex.core.permissions import Permission
from deusex.modules.intel.extractors import (
    DEFAULT_MAX_CHARS,
    ContentExtractorService,
    YouTubeTranscriptFetcher,
)
from deusex.modules.intel.news_rss import NewsRssService
from deusex.modules.intel.weather import (
    OpenMeteoService,
    convert_units,
    get_world_time,
)
from deusex.modules.intel.web_search import (
    BrowserOpener,
    GeminiSearchCallback,
    WebSearchService,
)

logger = logging.getLogger(__name__)

_FP_OPEN_URL_RE = re.compile(
    r"^(?:abre|abrir|ve\s+a|ir\s+a)\s+"
    r"(?:el\s+link\s+|la\s+url\s+|la\s+p[aá]gina\s+|el\s+enlace\s+)?"
    r"(https?://\S+|(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|cl|es|io|dev|ai|edu|gov|tv|app|me|co|ar|mx|pe|gg|news|info)(?:/\S*)?)$",
    re.IGNORECASE,
)
_RAW_URL_EXTRACT_RE = re.compile(
    r"(https?://\S+|(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:/\S*)?)",
    re.IGNORECASE,
)
_FP_WEATHER_RE = re.compile(
    r"^(?:(?:c[oó]mo\s+est[aá]|cu[aá]l\s+es|dime|muestra|ver|consulta)\s+)?"
    r"(?:el\s+)?"
    r"(?:clima|pron[oó]stico(?:\s+del\s+(?:clima|tiempo))?|qu[eé]\s+(?:clima|tiempo)\s+hace|c[oó]mo\s+est[aá]\s+el\s+tiempo)"
    r"(?:\s+(?:hoy|ma[nñ]ana|ahora))?"
    r"(?:\s+(?:en|de)\s+(.+?))?$",
    re.IGNORECASE,
)
_FP_WORLD_TIME_RE = re.compile(
    r"^(?:qu[eé]\s+hora\s+es|dime\s+la\s+hora|hora\s+actual|hora)\s+en\s+(.+?)$",
    re.IGNORECASE,
)
_FP_NEWS_RE = re.compile(
    r"^(?:(?:dime|muestra|busca|dame|ver|lee)\s+)?"
    r"(?:las\s+)?noticias"
    r"(?:\s+(?:de\s+hoy|recientes|actuales))?"
    r"(?:\s+(?:sobre|de|en)\s+(.+?))?$",
    re.IGNORECASE,
)


class IntelModule(BaseModule):
    """
    Módulo de Inteligencia Web (Rebanada 5):
    0 claves adicionales obligatorias (DuckDuckGo + trafilatura, Open-Meteo gratis con caché 15 min,
    Google News RSS hiper-contextual, hora mundial, unidades, divisas y extractores seguros).
    """

    module_id = "intel"
    display_name = "Inteligencia Web"
    description = (
        "Búsqueda web con fuentes clickeables, clima Open-Meteo gratuito, noticias RSS, "
        "divisas, hora mundial y extractores de URL/PDF/YouTube."
    )
    required_permissions = (Permission.INTEL_WEB,)

    def __init__(
        self,
        *,
        interests_provider: Callable[[], list[str]] | None = None,
        gemini_search_fn: GeminiSearchCallback | None = None,
        browser_opener: BrowserOpener | None = None,
        clipboard_reader: Callable[[], str] | None = None,
        youtube_fetcher: YouTubeTranscriptFetcher | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        weather_ttl_sec: float = 900.0,
        news_ttl_sec: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._ctx: ModuleContext | None = None
        self.web_search = WebSearchService(
            gemini_search_fn=gemini_search_fn,
            transport=transport,
            browser_opener=browser_opener,
        )
        self.weather = OpenMeteoService(
            weather_ttl_sec=weather_ttl_sec,
            transport=transport,
            clock=clock,
        )
        self.news = NewsRssService(
            interests_provider=interests_provider,
            cache_ttl_sec=news_ttl_sec,
            transport=transport,
            clock=clock,
        )
        self.extractors = ContentExtractorService(
            web_search=self.web_search,
            clipboard_reader=clipboard_reader,
            youtube_fetcher=youtube_fetcher,
        )

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self.set_state(ModuleState.ACTIVE)
        logger.info("IntelModule iniciado (0 claves externas requeridas).")

    async def stop(self) -> None:
        self.set_state(ModuleState.STOPPING)
        await asyncio.gather(
            self.web_search.close(),
            self.weather.close(),
            self.news.close(),
            return_exceptions=True,
        )
        self._ctx = None
        self.set_state(ModuleState.DISABLED)
        logger.info("IntelModule detenido y clientes HTTP cerrados.")

    def _default_location(self) -> tuple[str, str, str]:
        if self._ctx is None:
            return "", "", "es-CL"
        gen = self._ctx.settings.general
        return gen.city.strip(), gen.country.strip(), gen.language.strip() or "es-CL"

    def _is_privacy_mode(self) -> bool:
        return bool(self._ctx is not None and self._ctx.settings.general.privacy_mode)

    # ------------------------------------------------------------------
    # Handlers de Herramientas
    # ------------------------------------------------------------------
    async def _tool_web_search(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False,
                summary="Debes indicar una consulta para buscar en la web.",
                error_code="missing_query",
            )
        raw_mode = str(args.get("mode") or "fast").strip().lower()
        mode: Literal["fast", "deep"] = "deep" if raw_mode == "deep" else "fast"
        max_results = int(args.get("max_results") or 5)
        prefer_gemini = bool(args.get("prefer_gemini", False))

        try:
            report = await self.web_search.search(
                query,
                mode=mode,
                max_results=max_results,
                prefer_gemini=prefer_gemini,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo completar la búsqueda web: {exc}",
                error_code="web_search_failed",
            )

        summary = self.web_search.format_report_text(report)
        sources = report.to_sources()
        spoken = (
            f"Encontré {len(report.hits)} resultados para {query}."
            if report.hits
            else f"No encontré resultados web para {query}."
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=spoken,
            sources=sources,
            data={
                "query": report.query,
                "provider": report.provider,
                "mode": report.mode,
                "hits_count": len(report.hits),
                "hits": [
                    {
                        "title": h.title,
                        "url": h.url,
                        "snippet": h.snippet,
                        "extracted_text": h.extracted_text,
                    }
                    for h in report.hits
                ],
            },
        )

    async def _tool_open_url(self, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return ToolResult(
                ok=False,
                summary="Debes indicar la URL que deseas abrir.",
                error_code="missing_url",
            )
        try:
            opened, clean_url = await self.web_search.open_url(url)
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_url")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo abrir el enlace en el navegador: {exc}",
                error_code="open_url_failed",
            )

        if not opened:
            return ToolResult(
                ok=False,
                summary=f"El sistema no pudo abrir {clean_url} en el navegador predeterminado.",
                error_code="browser_open_failed",
            )
        msg = f"Abriendo {clean_url} en tu navegador."
        return ToolResult(
            ok=True,
            summary=msg,
            spoken_text="Abriendo el enlace en tu navegador.",
            sources=({"title": clean_url, "url": clean_url},),
            data={"url": clean_url, "opened": True},
        )

    async def _tool_weather(self, args: dict[str, Any]) -> ToolResult:
        default_city, default_country, _ = self._default_location()
        city = str(args.get("city") or "").strip() or default_city
        country = str(args.get("country") or "").strip() or (
            default_country if not args.get("city") else ""
        )
        if not city:
            return ToolResult(
                ok=False,
                summary=(
                    "No tengo una ciudad configurada todavía. Indícame la ciudad "
                    "(por ejemplo, 'clima en Concepción') o configúrala en Ajustes → General."
                ),
                error_code="missing_city",
            )
        forecast_days = int(args.get("forecast_days") or 3)
        include_forecast = bool(args.get("include_forecast", False))

        try:
            report = await self.weather.get_weather(
                city,
                country=country,
                forecast_days=forecast_days,
            )
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="city_not_found")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo consultar el clima en Open-Meteo: {exc}",
                error_code="weather_failed",
            )

        summary = report.format_summary(include_forecast=include_forecast)
        spoken = report.format_summary(include_forecast=False)
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=spoken,
            sources=report.to_sources(),
            data={
                "location": report.location_name,
                "country": report.country,
                "temperature_c": report.temperature_c,
                "apparent_temperature_c": report.apparent_temperature_c,
                "humidity_pct": report.humidity_pct,
                "wind_kmh": report.wind_kmh,
                "condition_es": report.condition_es,
                "cached": report.cached,
                "daily": [
                    {
                        "date": d.date,
                        "temp_min_c": d.temp_min_c,
                        "temp_max_c": d.temp_max_c,
                        "condition_es": d.condition_es,
                        "precip_prob_pct": d.precip_prob_pct,
                    }
                    for d in report.daily
                ],
            },
        )

    async def _tool_world_time(self, args: dict[str, Any]) -> ToolResult:
        location = str(args.get("location") or "").strip()
        try:
            info = get_world_time(location)
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_timezone")

        summary = (
            f"En {info.location} son las {info.local_time_str} "
            f"({info.local_date_str}, {info.utc_offset_str})."
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=f"En {info.location} son las {info.local_time_str}.",
            data={
                "location": info.location,
                "timezone": info.timezone_name,
                "time": info.local_time_str,
                "date": info.local_date_str,
                "utc_offset": info.utc_offset_str,
            },
        )

    async def _tool_convert_units(self, args: dict[str, Any]) -> ToolResult:
        try:
            value = float(args["value"])
            from_unit = str(args["from_unit"])
            to_unit = str(args["to_unit"])
            conv = convert_units(value, from_unit, to_unit)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_unit_conversion")

        text = conv.format_text()
        return ToolResult(
            ok=True,
            summary=text,
            spoken_text=text,
            data={
                "value": conv.value,
                "from_unit": conv.from_unit,
                "to_unit": conv.to_unit,
                "result": conv.result,
                "category": conv.category,
            },
        )

    async def _tool_convert_currency(self, args: dict[str, Any]) -> ToolResult:
        try:
            amount = float(args.get("amount", 1.0))
            from_currency = str(args["from_currency"])
            to_currency = str(args["to_currency"])
            conv = await self.weather.convert_currency(amount, from_currency, to_currency)
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_currency")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo consultar la tasa de cambio: {exc}",
                error_code="currency_failed",
            )

        text = conv.format_text()
        return ToolResult(
            ok=True,
            summary=text,
            spoken_text=f"{conv.amount:g} {conv.from_code} equivalen a {conv.converted:,.2f} {conv.to_code}.",
            data={
                "amount": conv.amount,
                "from_code": conv.from_code,
                "to_code": conv.to_code,
                "rate": conv.rate,
                "converted": conv.converted,
                "provider": conv.provider,
                "cached": conv.cached,
            },
        )

    async def _tool_news(self, args: dict[str, Any]) -> ToolResult:
        default_city, default_country, default_lang = self._default_location()
        query = str(args.get("query") or "").strip()
        city = str(args.get("city") or "").strip() or default_city
        country = str(args.get("country") or "").strip() or default_country
        limit = int(args.get("limit") or 5)
        use_interests = bool(args.get("use_memory_interests", True)) and not self._is_privacy_mode()

        try:
            report = await self.news.get_news(
                query=query,
                city=city,
                country=country,
                language=default_lang,
                use_memory_interests=use_interests,
                limit=limit,
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudieron obtener noticias RSS: {exc}",
                error_code="news_failed",
            )

        summary = report.format_text()
        spoken = (
            f"Aquí tienes {len(report.items)} noticias sobre {report.query_label}."
            if report.items
            else summary
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=spoken,
            sources=report.to_sources(),
            data={
                "query_label": report.query_label,
                "locale": report.locale,
                "used_interests": list(report.used_interests),
                "cached": report.cached,
                "items": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "source": item.source,
                        "published": item.published,
                    }
                    for item in report.items
                ],
            },
        )

    async def _tool_extract_content(self, args: dict[str, Any]) -> ToolResult:
        source_type = str(args.get("source_type") or "").strip().lower()
        target = str(args.get("target") or "").strip()
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)

        if source_type == "clipboard":
            return await self._tool_extract_clipboard({"max_chars": max_chars})

        if not target:
            return ToolResult(
                ok=False,
                summary="Debes indicar la URL, ruta de archivo o video de YouTube a extraer.",
                error_code="missing_target",
            )

        try:
            if source_type == "url":
                doc = await self.extractors.extract_url(target, max_chars=max_chars)
            elif source_type in ("file", "pdf", "text", "text_file"):
                doc = await self.extractors.extract_file(target, max_chars=max_chars)
            elif source_type == "youtube":
                doc = await self.extractors.extract_youtube_transcript(target, max_chars=max_chars)
            else:
                return ToolResult(
                    ok=False,
                    summary=f"Tipo de origen no soportado: '{source_type}'. Usa url, file o youtube.",
                    error_code="invalid_source_type",
                )
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="privacy_mode_blocked")
        except (FileNotFoundError, ValueError) as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="extraction_invalid")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"Error extrayendo contenido ({source_type}): {exc}",
                error_code="extraction_failed",
            )

        preview = doc.text[:600].rstrip() + ("..." if len(doc.text) > 600 else "")
        summary = f"Contenido extraído de {doc.title} ({len(doc.text)} caracteres):\n{preview}"
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=f"Extraje el contenido de {doc.title}.",
            sources=doc.to_sources(),
            data={
                "source_kind": doc.source_kind,
                "source_ref": doc.source_ref,
                "title": doc.title,
                "text": doc.text,
                "truncated": doc.truncated,
                "metadata": doc.metadata,
            },
        )

    async def _tool_extract_clipboard(self, args: dict[str, Any]) -> ToolResult:
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
        try:
            doc = await self.extractors.extract_clipboard(
                privacy_mode=self._is_privacy_mode(),
                max_chars=max_chars,
            )
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="privacy_mode_blocked")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo leer el portapapeles: {exc}",
                error_code="clipboard_failed",
            )

        if not doc.text:
            return ToolResult(
                ok=True,
                summary="El portapapeles no contiene texto en este momento.",
                spoken_text="El portapapeles está vacío.",
                data={"text": "", "char_count": 0},
            )
        return ToolResult(
            ok=True,
            summary=f"Texto del portapapeles ({len(doc.text)} caracteres):\n{doc.text}",
            spoken_text="Leí el texto de tu portapapeles.",
            data={"text": doc.text, "truncated": doc.truncated, "char_count": len(doc.text)},
        )

    async def _tool_export_research(self, args: dict[str, Any]) -> ToolResult:
        title = str(args.get("title") or "").strip()
        markdown_body = str(args.get("markdown_body") or "").strip()
        target = str(args.get("target") or "google_docs").strip().lower()
        if not title or not markdown_body:
            return ToolResult(
                ok=False,
                summary="Debes indicar título y contenido para exportar la investigación.",
                error_code="missing_export_fields",
            )

        raw_sources = args.get("sources")
        clean_sources: list[dict[str, str]] = []
        if isinstance(raw_sources, list):
            for item in raw_sources:
                if isinstance(item, dict) and item.get("url"):
                    clean_sources.append(
                        {
                            "title": str(item.get("title") or item["url"]),
                            "url": str(item["url"]),
                        }
                    )

        export_id = uuid.uuid4().hex[:12]
        event = ResearchExportRequestedEvent(
            export_id=export_id,
            title=title,
            markdown_body=markdown_body,
            sources=tuple(clean_sources),
            target=target,
        )
        if self._ctx is not None:
            self._ctx.events.publish(event)

        summary = (
            f"Solicitud de exportación '{title}' emitida hacia {target} "
            f"(ID {export_id}). El módulo de Google Workspace procesará la creación del documento."
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=f"Envié la solicitud para exportar {title} a Google Docs.",
            sources=tuple(clean_sources),
            data={
                "export_id": export_id,
                "title": title,
                "target": target,
                "event_published": self._ctx is not None,
            },
        )

    # ------------------------------------------------------------------
    # Registro de Herramientas y Fast-Path
    # ------------------------------------------------------------------
    def get_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="intel_web_search",
                module_id=self.module_id,
                description=(
                    "Busca información actualizada en la web (DuckDuckGo gratis o Gemini Search opcional) "
                    "con modo rápido ('fast') o extracción profunda de páginas ('deep')."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Consulta de búsqueda."},
                        "mode": {
                            "type": "string",
                            "enum": ["fast", "deep"],
                            "description": "fast=snippets rápidos; deep=extrae contenido con trafilatura.",
                        },
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        "prefer_gemini": {"type": "boolean"},
                    },
                    "required": ["query"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_web_search,
            ),
            ToolSpec(
                name="intel_open_url",
                module_id=self.module_id,
                description="Abre una URL HTTP/HTTPS segura en el navegador predeterminado del usuario.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Dirección web http(s) o dominio."},
                    },
                    "required": ["url"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_open_url,
            ),
            ToolSpec(
                name="intel_weather",
                module_id=self.module_id,
                description=(
                    "Consulta clima actual y pronóstico usando Open-Meteo gratuito (caché 15 min). "
                    "Si no se indica ciudad, usa la configurada en Ajustes."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "country": {"type": "string"},
                        "forecast_days": {"type": "integer", "minimum": 1, "maximum": 7},
                        "include_forecast": {"type": "boolean"},
                    },
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_weather,
            ),
            ToolSpec(
                name="intel_world_time",
                module_id=self.module_id,
                description="Calcula la hora actual en cualquier ciudad principal o zona horaria IANA.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "Ciudad, país o zona IANA."},
                    },
                    "required": ["location"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_world_time,
            ),
            ToolSpec(
                name="intel_convert_units",
                module_id=self.module_id,
                description="Convierte unidades de temperatura, longitud, masa, volumen, velocidad y datos.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "from_unit": {"type": "string"},
                        "to_unit": {"type": "string"},
                    },
                    "required": ["value", "from_unit", "to_unit"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_convert_units,
            ),
            ToolSpec(
                name="intel_convert_currency",
                module_id=self.module_id,
                description="Convierte divisas internacionales (USD, EUR, CLP, etc.) sin requerir API key.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "amount": {"type": "number"},
                        "from_currency": {"type": "string"},
                        "to_currency": {"type": "string"},
                    },
                    "required": ["from_currency", "to_currency"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_convert_currency,
            ),
            ToolSpec(
                name="intel_news",
                module_id=self.module_id,
                description=(
                    "Obtiene titulares de Google News RSS filtrados por país, ciudad e intereses "
                    "de Memoria Adaptativa con fuentes clickeables."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "city": {"type": "string"},
                        "country": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 15},
                        "use_memory_interests": {"type": "boolean"},
                    },
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_news,
            ),
            ToolSpec(
                name="intel_extract_content",
                module_id=self.module_id,
                description="Extrae texto limpio con límite seguro desde una URL, archivo PDF/TXT/MD o transcripción de YouTube.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "source_type": {
                            "type": "string",
                            "enum": ["url", "file", "youtube"],
                        },
                        "target": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
                    },
                    "required": ["source_type", "target"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_extract_content,
            ),
            ToolSpec(
                name="intel_extract_clipboard",
                module_id=self.module_id,
                description="Lee y extrae texto del portapapeles (bloqueado estrictamente en Modo Privacidad).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000},
                    },
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_extract_clipboard,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="intel_export_research",
                module_id=self.module_id,
                description="Emite un evento de exportación de investigación en Markdown hacia Google Docs.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "markdown_body": {"type": "string"},
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "url": {"type": "string"},
                                },
                                "required": ["url"],
                            },
                        },
                        "target": {"type": "string"},
                    },
                    "required": ["title", "markdown_body"],
                },
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_export_research,
            ),
        ]

    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp_open_url(match: re.Match[str], raw: str) -> ToolResult:
            raw_match = _RAW_URL_EXTRACT_RE.search(raw)
            url = raw_match.group(1) if raw_match else match.group(1)
            return await self._tool_open_url({"url": url})

        async def _fp_weather(match: re.Match[str], raw: str) -> ToolResult:
            city = (match.group(1) or "").strip()
            include_forecast = any(
                token in raw.lower() for token in ("pronostico", "pronóstico", "mañana", "manana")
            )
            return await self._tool_weather(
                {"city": city, "include_forecast": include_forecast}
            )

        async def _fp_world_time(match: re.Match[str], _raw: str) -> ToolResult:
            location = (match.group(1) or "").strip()
            return await self._tool_world_time({"location": location})

        async def _fp_news(match: re.Match[str], _raw: str) -> ToolResult:
            query = (match.group(1) or "").strip()
            return await self._tool_news({"query": query, "limit": 5})

        return [
            FastPathRule(
                rule_id="intel.fp.open_url",
                module_id=self.module_id,
                priority=25,
                pattern=_FP_OPEN_URL_RE,
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=_fp_open_url,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="intel.fp.weather",
                module_id=self.module_id,
                priority=26,
                pattern=_FP_WEATHER_RE,
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=_fp_weather,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="intel.fp.world_time",
                module_id=self.module_id,
                priority=27,
                pattern=_FP_WORLD_TIME_RE,
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=_fp_world_time,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="intel.fp.news",
                module_id=self.module_id,
                priority=28,
                pattern=_FP_NEWS_RE,
                permission=Permission.INTEL_WEB,
                risk_level=RiskLevel.SAFE,
                handler=_fp_news,
                earcon="confirm",
            ),
        ]

    def diagnose(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = []

        deps = (
            ("httpx", "Cliente HTTP asíncrono"),
            ("trafilatura", "Extractor de páginas web"),
            ("pypdf", "Extractor de documentos PDF"),
            ("youtube_transcript_api", "Extractor de transcripciones YouTube"),
        )
        missing = [
            f"{pkg} ({label})"
            for pkg, label in deps
            if importlib.util.find_spec(pkg) is None
        ]
        if missing:
            items.append(
                DiagnosticItem(
                    code="intel.deps.missing",
                    area="intel",
                    title="Dependencias de Inteligencia Web",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Faltan paquetes: {', '.join(missing)}.",
                    remediation="Ejecuta `uv sync` para restaurar las dependencias de Rebanada 5.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="intel.deps.ok",
                    area="intel",
                    title="Dependencias de Inteligencia Web",
                    severity=DiagnosticSeverity.OK,
                    message="httpx, trafilatura, pypdf y youtube-transcript-api disponibles (0 API keys extra).",
                )
            )

        city, country, _ = self._default_location()
        if city:
            loc_str = f"{city}, {country}" if country else city
            items.append(
                DiagnosticItem(
                    code="intel.location.ok",
                    area="intel",
                    title="Ubicación por defecto (Open-Meteo / RSS)",
                    severity=DiagnosticSeverity.OK,
                    message=f"Ubicación local configurada: {loc_str}.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="intel.location.unset",
                    area="intel",
                    title="Ubicación por defecto (Open-Meteo / RSS)",
                    severity=DiagnosticSeverity.WARNING,
                    message="No hay ciudad configurada en Ajustes -> General.",
                    remediation="Define tu ciudad y país para consultar clima y noticias locales sin repetir ubicación.",
                )
            )

        return items
