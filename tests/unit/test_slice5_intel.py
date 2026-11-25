from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from deusex.core.config import DeusExSettings, SecretsManager
from deusex.core.contracts import DiagnosticSeverity, ModuleContext, ModuleState
from deusex.core.db import Database
from deusex.core.engine import CoreEngine
from deusex.core.events import EventBus, ResearchExportRequestedEvent
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import RuntimeServices
from deusex.modules.intel.extractors import (
    ContentExtractorService,
    extract_youtube_video_id,
)
from deusex.modules.intel.module import IntelModule
from deusex.modules.intel.news_rss import NewsRssService, parse_rss_xml
from deusex.modules.intel.weather import OpenMeteoService, convert_units, get_world_time
from deusex.modules.intel.web_search import WebSearchService, validate_http_url

_DDG_SAMPLE_HTML = """
<html><body>
  <div class="result">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpython314">
      Python 3.14 Novedades
    </a>
    <a class="result__snippet">Resumen oficial de mejoras de rendimiento en Python 3.14.</a>
  </div>
  <div class="result">
    <a class="result__a" href="https://open-meteo.com/en/docs">Open-Meteo Free Weather API</a>
    <a class="result__snippet">API meteorologica gratuita de codigo abierto.</a>
  </div>
</body></html>
"""

_ARTICLE_SAMPLE_HTML = """
<html>
  <head><title>Python 3.14 Novedades</title></head>
  <body>
    <article>
      <h1>Python 3.14</h1>
      <p>Python 3.14 mejora la velocidad de interpretacion, incorpora mejoras en anotaciones diferidas
      y optimiza el manejo de memoria para aplicaciones locales de escritorio en Windows 11.</p>
    </article>
  </body>
</html>
"""

_RSS_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>Avances en IA local para PC - TechChile</title>
      <link>https://news.example.cl/ia-local</link>
      <pubDate>Tue, 24 Nov 2026 12:00:00 GMT</pubDate>
      <source url="https://techchile.example">TechChile</source>
      <description>&lt;b&gt;Resumen&lt;/b&gt; de herramientas locales.</description>
    </item>
    <item>
      <title>Avances en IA local para PC - TechChile</title>
      <link>https://news.example.cl/ia-local-dup</link>
      <pubDate>Tue, 24 Nov 2026 12:05:00 GMT</pubDate>
      <source url="https://techchile.example">TechChile</source>
      <description>Duplicado por titulo.</description>
    </item>
    <item>
      <title>Nuevo festival cultural en Concepcion - BioBio</title>
      <link>https://news.example.cl/festival</link>
      <pubDate>Tue, 24 Nov 2026 13:00:00 GMT</pubDate>
      <description>Evento abierto este fin de semana.</description>
    </item>
  </channel>
</rss>
"""


def _build_minimal_pdf_bytes(text: str) -> bytes:
    """Construye un PDF 1.4 válido con tabla xref exacta para probar extracción real con pypdf."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_body = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n".encode("latin-1")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream_body)} >>\nstream\n".encode("ascii")
            + stream_body
            + b"endstream\nendobj\n"
        ),
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj in objects:
        offsets.append(len(buf))
        buf.extend(obj)

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buf.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(buf)


def _make_mock_transport(counters: dict[str, int], last_urls: list[str]) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        last_urls.append(url)

        if "duckduckgo.com" in url:
            counters["ddg"] = counters.get("ddg", 0) + 1
            return httpx.Response(200, text=_DDG_SAMPLE_HTML)

        if url == "https://example.org/python314":
            counters["article"] = counters.get("article", 0) + 1
            return httpx.Response(200, text=_ARTICLE_SAMPLE_HTML)

        if "geocoding-api.open-meteo.com" in url:
            counters["geocode"] = counters.get("geocode", 0) + 1
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "Concepción",
                            "country": "Chile",
                            "country_code": "CL",
                            "latitude": -36.827,
                            "longitude": -73.050,
                        }
                    ]
                },
            )

        if "api.open-meteo.com/v1/forecast" in url:
            counters["forecast"] = counters.get("forecast", 0) + 1
            return httpx.Response(
                200,
                json={
                    "timezone": "America/Santiago",
                    "current": {
                        "temperature_2m": 18.4,
                        "apparent_temperature": 17.8,
                        "relative_humidity_2m": 64,
                        "weather_code": 2,
                        "wind_speed_10m": 14.2,
                    },
                    "daily": {
                        "time": ["2026-11-24", "2026-11-25"],
                        "temperature_2m_min": [11.0, 10.5],
                        "temperature_2m_max": [21.0, 22.0],
                        "weather_code": [2, 0],
                        "precipitation_probability_max": [15, 5],
                    },
                },
            )

        if "open.er-api.com" in url:
            counters["er_api"] = counters.get("er_api", 0) + 1
            if "/latest/EUR" in url:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={"rates": {"CLP": 950.0, "EUR": 0.92}})

        if "api.frankfurter.app" in url:
            counters["frankfurter"] = counters.get("frankfurter", 0) + 1
            return httpx.Response(200, json={"rates": {"USD": 1.08}})

        if "news.google.com/rss" in url:
            counters["rss"] = counters.get("rss", 0) + 1
            return httpx.Response(200, text=_RSS_SAMPLE_XML)

        return httpx.Response(404, text="not found")

    return httpx.MockTransport(_handler)


@pytest.mark.asyncio
async def test_web_search_ddg_fast_deep_and_open_url() -> None:
    counters: dict[str, int] = {}
    last_urls: list[str] = []
    opened_urls: list[str] = []

    def _browser_opener(u: str) -> bool:
        opened_urls.append(u)
        return True

    service = WebSearchService(
        transport=_make_mock_transport(counters, last_urls),
        browser_opener=_browser_opener,
    )
    try:
        fast = await service.search("python 3.14", mode="fast", max_results=5)
        assert fast.provider == "duckduckgo"
        assert len(fast.hits) == 2
        assert fast.hits[0].url == "https://example.org/python314"
        assert counters.get("article", 0) == 0
        assert len(fast.to_sources()) == 2

        deep = await service.search("python 3.14", mode="deep", max_results=2)
        assert counters.get("article", 0) == 1
        assert "Python 3.14" in deep.hits[0].extracted_text

        ok, clean = await service.open_url("open-meteo.com")
        assert ok is True
        assert clean == "https://open-meteo.com"
        assert opened_urls == ["https://open-meteo.com"]

        with pytest.raises(ValueError):
            validate_http_url("javascript:alert(1)")
        with pytest.raises(ValueError):
            validate_http_url("file:///C:/Windows/win.ini")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_open_meteo_cache_ttl_15_min_and_currency() -> None:
    counters: dict[str, int] = {}
    last_urls: list[str] = []
    now = [1000.0]

    service = OpenMeteoService(
        weather_ttl_sec=900.0,
        transport=_make_mock_transport(counters, last_urls),
        clock=lambda: now[0],
    )
    try:
        first = await service.get_weather("Concepción", country="Chile")
        assert first.cached is False
        assert first.temperature_c == pytest.approx(18.4)
        assert first.condition_es == "Parcialmente nublado"
        assert len(first.daily) == 2
        assert counters["forecast"] == 1

        now[0] += 300.0  # 5 minutos después -> caché vigente
        second = await service.get_weather("Concepción", country="Chile")
        assert second.cached is True
        assert counters["forecast"] == 1

        now[0] += 605.0  # >15 minutos totales -> expira caché
        third = await service.get_weather("Concepción", country="Chile")
        assert third.cached is False
        assert counters["forecast"] == 2

        # Divisas: primario + caché + respaldo Frankfurter
        usd_clp = await service.convert_currency(10.0, "dólares", "CLP")
        assert usd_clp.converted == pytest.approx(9500.0)
        assert usd_clp.provider == "open.er-api.com"
        assert usd_clp.cached is False

        usd_clp_cached = await service.convert_currency(20.0, "USD", "CLP")
        assert usd_clp_cached.cached is True
        assert usd_clp_cached.converted == pytest.approx(19000.0)

        eur_usd = await service.convert_currency(10.0, "EUR", "USD")
        assert eur_usd.provider == "frankfurter.app"
        assert eur_usd.converted == pytest.approx(10.8)
    finally:
        await service.close()


def test_world_time_and_unit_conversions() -> None:
    ref_utc = datetime(2026, 11, 24, 15, 30, tzinfo=UTC)
    tokyo = get_world_time("Tokio", now_utc=ref_utc)
    assert tokyo.local_time_str == "00:30"
    assert tokyo.utc_offset_str == "UTC+09:00"

    temp = convert_units(100.0, "celsius", "fahrenheit")
    assert temp.result == pytest.approx(212.0)

    dist = convert_units(10.0, "km", "millas")
    assert dist.result == pytest.approx(6.2137119, rel=1e-4)

    data = convert_units(2.0, "GB", "MB")
    assert data.result == pytest.approx(2048.0)

    with pytest.raises(ValueError):
        convert_units(5.0, "kg", "metros")


@pytest.mark.asyncio
async def test_news_rss_uses_memory_interests_and_deduplicates() -> None:
    counters: dict[str, int] = {}
    last_urls: list[str] = []

    service = NewsRssService(
        interests_provider=lambda: ["inteligencia artificial", "videojuegos"],
        transport=_make_mock_transport(counters, last_urls),
    )
    try:
        report = await service.get_news(city="Concepción", country="Chile", language="es-CL", limit=5)
        assert report.cached is False
        assert report.locale == "CL:es-419"
        assert report.used_interests == ("inteligencia artificial", "videojuegos")
        assert len(report.items) == 2  # duplicado eliminado
        assert report.items[0].title == "Avances en IA local para PC"
        assert report.items[0].source == "TechChile"
        assert len(report.to_sources()) == 2
        assert "inteligencia" in last_urls[-1].lower()

        cached = await service.get_news(city="Concepción", country="Chile", language="es-CL", limit=5)
        assert cached.cached is True
        assert counters["rss"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_extractors_txt_pdf_youtube_and_clipboard_privacy(tmp_path: Path) -> None:
    counters: dict[str, int] = {}
    last_urls: list[str] = []
    web_service = WebSearchService(transport=_make_mock_transport(counters, last_urls))

    extractor = ContentExtractorService(
        web_search=web_service,
        clipboard_reader=lambda: "Texto confidencial del portapapeles",
        youtube_fetcher=lambda vid, _langs: f"Transcripcion real del video {vid}",
    )
    try:
        # TXT / MD con truncamiento seguro
        md_file = tmp_path / "notas.md"
        md_file.write_text("Linea de prueba. " * 40, encoding="utf-8")
        doc_md = await extractor.extract_file(md_file, max_chars=220)
        assert doc_md.source_kind == "text_file"
        assert doc_md.truncated is True
        assert len(doc_md.text) <= 223

        # PDF real con pypdf
        pdf_file = tmp_path / "muestra.pdf"
        pdf_file.write_bytes(_build_minimal_pdf_bytes("Hola DeusEx PDF"))
        doc_pdf = await extractor.extract_file(pdf_file)
        assert doc_pdf.source_kind == "pdf"
        assert "Hola DeusEx PDF" in doc_pdf.text
        assert doc_pdf.metadata["total_pages"] == 1

        # YouTube IDs y transcripción
        assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert (
            extract_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12")
            == "dQw4w9WgXcQ"
        )
        assert (
            extract_youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        )
        doc_yt = await extractor.extract_youtube_transcript("https://youtu.be/dQw4w9WgXcQ")
        assert doc_yt.source_kind == "youtube"
        assert "dQw4w9WgXcQ" in doc_yt.text
        assert len(doc_yt.to_sources()) == 1

        # Portapapeles normal vs Modo Privacidad
        doc_clip = await extractor.extract_clipboard(privacy_mode=False)
        assert "confidencial" in doc_clip.text
        with pytest.raises(PermissionError):
            await extractor.extract_clipboard(privacy_mode=True)
    finally:
        await web_service.close()


@pytest.mark.asyncio
async def test_intel_module_fast_path_privacy_and_export_contract(tmp_path: Path) -> None:
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    settings = DeusExSettings()
    settings.general.city = "Concepción"
    settings.general.country = "Chile"
    settings.modules.set_enabled("intel", True)
    settings.set_permission(Permission.INTEL_WEB, True)

    secrets = SecretsManager(paths, fallback_to_os_env=False)
    events = EventBus()
    events.bind_loop(asyncio.get_running_loop())
    db = Database(paths.db_file)
    db.open()
    runtime = RuntimeServices(events)

    counters: dict[str, int] = {}
    last_urls: list[str] = []
    opened: list[str] = []
    exported_events: list[ResearchExportRequestedEvent] = []
    events.subscribe(ResearchExportRequestedEvent, lambda ev: exported_events.append(ev))

    ctx = ModuleContext(
        paths=paths,
        settings=settings,
        secrets=secrets,
        db=db,
        events=events,
        runtime=runtime,
    )
    engine = CoreEngine(ctx)
    def _engine_browser_opener(u: str) -> bool:
        opened.append(u)
        return True

    module = IntelModule(
        interests_provider=lambda: ["tecnologia"],
        browser_opener=_engine_browser_opener,
        clipboard_reader=lambda: "Dato del portapapeles",
        transport=_make_mock_transport(counters, last_urls),
    )
    engine.modules.register(module)

    try:
        enabled = await engine.modules.enable_module("intel")
        assert enabled is True
        assert module.state == ModuleState.ACTIVE

        # Fast-Path: clima, hora mundial, noticias y abrir enlace
        res_weather = await engine.fast_path.try_execute("clima en concepcion")
        assert res_weather is not None and res_weather.ok is True
        assert "18.4" in res_weather.summary
        assert res_weather.sources

        res_time = await engine.fast_path.try_execute("que hora es en tokio")
        assert res_time is not None and res_time.ok is True
        assert "Tokio" in res_time.summary

        res_news = await engine.fast_path.try_execute("dime las noticias")
        assert res_news is not None and res_news.ok is True
        assert len(res_news.sources) == 2

        res_url = await engine.fast_path.try_execute("abre https://open-meteo.com")
        assert res_url is not None and res_url.ok is True
        assert opened == ["https://open-meteo.com"]

        # Portapapeles permitido fuera de privacidad y bloqueado en Modo Privacidad
        clip_ok = await engine.tools.execute("intel_extract_clipboard", {})
        assert clip_ok.ok is True
        assert "Dato del portapapeles" in clip_ok.summary

        settings.general.privacy_mode = True
        clip_blocked = await engine.tools.execute("intel_extract_clipboard", {})
        assert clip_blocked.ok is False

        # Contrato de exportación hacia Google Docs (Rebanada 7)
        export_args: dict[str, Any] = {
            "title": "Resumen Python 3.14",
            "markdown_body": "# Python 3.14\nMejoras clave.",
            "sources": [{"title": "Docs", "url": "https://example.org/python314"}],
        }
        res_export = await engine.tools.execute("intel_export_research", export_args)
        assert res_export.ok is True
        assert len(exported_events) == 1
        assert exported_events[0].title == "Resumen Python 3.14"
        assert exported_events[0].target == "google_docs"

        # Diagnósticos Doctor
        diags = module.diagnose()
        assert any(d.code == "intel.deps.ok" and d.severity == DiagnosticSeverity.OK for d in diags)
        assert any(d.code == "intel.location.ok" for d in diags)

        await engine.modules.disable_module("intel")
        assert module.state == ModuleState.DISABLED  # type: ignore[comparison-overlap]
    finally:
        events.unbind_loop()
        db.close()


def test_news_rss_deduplicates_same_event_across_outlets() -> None:
    xml_same_event = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    <item>
      <title>Alejandro Sanz confirma concierto en Estadio Ester Roa - BioBioChile</title>
      <link>https://news.example.cl/sanz-1</link>
      <source url="https://biobiochile.example">BioBioChile</source>
    </item>
    <item>
      <title>Alejandro Sanz a región del Biobío: fechas y entradas - Cooperativa</title>
      <link>https://news.example.cl/sanz-2</link>
      <source url="https://cooperativa.example">Cooperativa</source>
    </item>
    <item>
      <title>Alejandro Sanz anuncia concierto en Concepción - La Tercera</title>
      <link>https://news.example.cl/sanz-3</link>
      <source url="https://latercera.example">La Tercera</source>
    </item>
    <item>
      <title>Observatorio ALMA revela nuevos datos de exoplaneta - Emol</title>
      <link>https://news.example.cl/alma</link>
      <source url="https://emol.example">Emol</source>
    </item>
  </channel>
</rss>
"""
    items = parse_rss_xml(xml_same_event, limit=5)
    assert len(items) == 2
    assert items[0].title == "Alejandro Sanz confirma concierto en Estadio Ester Roa"
    assert items[1].title == "Observatorio ALMA revela nuevos datos de exoplaneta"
