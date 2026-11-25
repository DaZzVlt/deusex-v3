from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
import logging
import re
import time
from typing import Any
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

logger = logging.getLogger(__name__)

# ÚNICAMENTE tier gratuito de Open-Meteo (0 API keys, atribución CC BY 4.0)
_OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Divisas gratuitas sin clave (con soporte CLP y principales divisas ISO 4217)
_OPEN_ER_API_URL = "https://open.er-api.com/v6/latest/{base}"
_FRANKFURTER_API_URL = "https://api.frankfurter.app/latest"

_WMO_CODES_ES: dict[int, str] = {
    0: "Despejado",
    1: "Mayormente despejado",
    2: "Parcialmente nublado",
    3: "Nublado",
    45: "Niebla",
    48: "Niebla con escarcha",
    51: "Llovizna ligera",
    53: "Llovizna moderada",
    55: "Llovizna intensa",
    56: "Llovizna helada ligera",
    57: "Llovizna helada intensa",
    61: "Lluvia ligera",
    63: "Lluvia moderada",
    65: "Lluvia fuerte",
    66: "Lluvia helada ligera",
    67: "Lluvia helada fuerte",
    71: "Nieve ligera",
    73: "Nieve moderada",
    75: "Nieve intensa",
    77: "Granizo fino",
    80: "Chubascos ligeros",
    81: "Chubascos moderados",
    82: "Chubascos intensos",
    85: "Chubascos de nieve ligeros",
    86: "Chubascos de nieve intensos",
    95: "Tormenta eléctrica",
    96: "Tormenta con granizo ligero",
    99: "Tormenta con granizo fuerte",
}

# (IANA, offset estándar en minutos como respaldo si Windows no tiene tzdata)
_WORLD_TIMEZONES: dict[str, tuple[str, int, str]] = {
    "santiago": ("America/Santiago", -180, "Santiago, Chile"),
    "chile": ("America/Santiago", -180, "Santiago, Chile"),
    "concepcion": ("America/Santiago", -180, "Concepción, Chile"),
    "valparaiso": ("America/Santiago", -180, "Valparaíso, Chile"),
    "buenos aires": ("America/Argentina/Buenos_Aires", -180, "Buenos Aires, Argentina"),
    "argentina": ("America/Argentina/Buenos_Aires", -180, "Buenos Aires, Argentina"),
    "lima": ("America/Lima", -300, "Lima, Perú"),
    "peru": ("America/Lima", -300, "Lima, Perú"),
    "bogota": ("America/Bogota", -300, "Bogotá, Colombia"),
    "colombia": ("America/Bogota", -300, "Bogotá, Colombia"),
    "ciudad de mexico": ("America/Mexico_City", -360, "Ciudad de México, México"),
    "cdmx": ("America/Mexico_City", -360, "Ciudad de México, México"),
    "mexico": ("America/Mexico_City", -360, "Ciudad de México, México"),
    "sao paulo": ("America/Sao_Paulo", -180, "São Paulo, Brasil"),
    "brasil": ("America/Sao_Paulo", -180, "São Paulo, Brasil"),
    "montevideo": ("America/Montevideo", -180, "Montevideo, Uruguay"),
    "quito": ("America/Guayaquil", -300, "Quito, Ecuador"),
    "caracas": ("America/Caracas", -240, "Caracas, Venezuela"),
    "nueva york": ("America/New_York", -300, "Nueva York, EE. UU."),
    "new york": ("America/New_York", -300, "Nueva York, EE. UU."),
    "miami": ("America/New_York", -300, "Miami, EE. UU."),
    "los angeles": ("America/Los_Angeles", -480, "Los Ángeles, EE. UU."),
    "san francisco": ("America/Los_Angeles", -480, "San Francisco, EE. UU."),
    "chicago": ("America/Chicago", -360, "Chicago, EE. UU."),
    "madrid": ("Europe/Madrid", 60, "Madrid, España"),
    "barcelona": ("Europe/Madrid", 60, "Barcelona, España"),
    "espana": ("Europe/Madrid", 60, "Madrid, España"),
    "londres": ("Europe/London", 0, "Londres, Reino Unido"),
    "london": ("Europe/London", 0, "Londres, Reino Unido"),
    "paris": ("Europe/Paris", 60, "París, Francia"),
    "berlin": ("Europe/Berlin", 60, "Berlín, Alemania"),
    "roma": ("Europe/Rome", 60, "Roma, Italia"),
    "lisboa": ("Europe/Lisbon", 0, "Lisboa, Portugal"),
    "tokio": ("Asia/Tokyo", 540, "Tokio, Japón"),
    "tokyo": ("Asia/Tokyo", 540, "Tokio, Japón"),
    "japon": ("Asia/Tokyo", 540, "Tokio, Japón"),
    "seul": ("Asia/Seoul", 540, "Seúl, Corea del Sur"),
    "seoul": ("Asia/Seoul", 540, "Seúl, Corea del Sur"),
    "pekin": ("Asia/Shanghai", 480, "Pekín, China"),
    "shanghai": ("Asia/Shanghai", 480, "Shanghái, China"),
    "dubai": ("Asia/Dubai", 240, "Dubái, EAU"),
    "sidney": ("Australia/Sydney", 660, "Sídney, Australia"),
    "sydney": ("Australia/Sydney", 660, "Sídney, Australia"),
    "utc": ("UTC", 0, "UTC"),
    "gmt": ("UTC", 0, "GMT / UTC"),
}

_CURRENCY_ALIASES: dict[str, str] = {
    "usd": "USD",
    "dolar": "USD",
    "dolares": "USD",
    "us": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "clp": "CLP",
    "peso chileno": "CLP",
    "pesos chilenos": "CLP",
    "ars": "ARS",
    "peso argentino": "ARS",
    "pesos argentinos": "ARS",
    "mxn": "MXN",
    "peso mexicano": "MXN",
    "pesos mexicanos": "MXN",
    "pen": "PEN",
    "sol": "PEN",
    "soles": "PEN",
    "cop": "COP",
    "peso colombiano": "COP",
    "pesos colombianos": "COP",
    "brl": "BRL",
    "real": "BRL",
    "reales": "BRL",
    "gbp": "GBP",
    "libra": "GBP",
    "libras": "GBP",
    "jpy": "JPY",
    "yen": "JPY",
    "yenes": "JPY",
    "cad": "CAD",
    "aud": "AUD",
    "chf": "CHF",
    "cny": "CNY",
    "krw": "KRW",
}

# Unidades lineales: (categoría, factor hacia unidad base SI, etiqueta corta)
_LINEAR_UNITS: dict[str, tuple[str, float, str]] = {
    # Longitud -> metros
    "mm": ("length", 0.001, "mm"),
    "milimetro": ("length", 0.001, "mm"),
    "milimetros": ("length", 0.001, "mm"),
    "cm": ("length", 0.01, "cm"),
    "centimetro": ("length", 0.01, "cm"),
    "centimetros": ("length", 0.01, "cm"),
    "m": ("length", 1.0, "m"),
    "metro": ("length", 1.0, "m"),
    "metros": ("length", 1.0, "m"),
    "km": ("length", 1000.0, "km"),
    "kilometro": ("length", 1000.0, "km"),
    "kilometros": ("length", 1000.0, "km"),
    "in": ("length", 0.0254, "in"),
    "pulgada": ("length", 0.0254, "in"),
    "pulgadas": ("length", 0.0254, "in"),
    "ft": ("length", 0.3048, "ft"),
    "pie": ("length", 0.3048, "ft"),
    "pies": ("length", 0.3048, "ft"),
    "yd": ("length", 0.9144, "yd"),
    "yarda": ("length", 0.9144, "yd"),
    "yardas": ("length", 0.9144, "yd"),
    "mi": ("length", 1609.344, "mi"),
    "milla": ("length", 1609.344, "mi"),
    "millas": ("length", 1609.344, "mi"),
    # Masa -> gramos
    "mg": ("mass", 0.001, "mg"),
    "g": ("mass", 1.0, "g"),
    "gr": ("mass", 1.0, "g"),
    "gramo": ("mass", 1.0, "g"),
    "gramos": ("mass", 1.0, "g"),
    "kg": ("mass", 1000.0, "kg"),
    "kilo": ("mass", 1000.0, "kg"),
    "kilos": ("mass", 1000.0, "kg"),
    "kilogramo": ("mass", 1000.0, "kg"),
    "kilogramos": ("mass", 1000.0, "kg"),
    "t": ("mass", 1_000_000.0, "t"),
    "tonelada": ("mass", 1_000_000.0, "t"),
    "toneladas": ("mass", 1_000_000.0, "t"),
    "oz": ("mass", 28.349523125, "oz"),
    "onza": ("mass", 28.349523125, "oz"),
    "onzas": ("mass", 28.349523125, "oz"),
    "lb": ("mass", 453.59237, "lb"),
    "lbs": ("mass", 453.59237, "lb"),
    "libra": ("mass", 453.59237, "lb"),
    "libras": ("mass", 453.59237, "lb"),
    # Volumen -> litros
    "ml": ("volume", 0.001, "mL"),
    "mililitro": ("volume", 0.001, "mL"),
    "mililitros": ("volume", 0.001, "mL"),
    "l": ("volume", 1.0, "L"),
    "lt": ("volume", 1.0, "L"),
    "litro": ("volume", 1.0, "L"),
    "litros": ("volume", 1.0, "L"),
    "gal": ("volume", 3.785411784, "gal"),
    "galon": ("volume", 3.785411784, "gal"),
    "galones": ("volume", 3.785411784, "gal"),
    "taza": ("volume", 0.2365882365, "tazas"),
    "tazas": ("volume", 0.2365882365, "tazas"),
    # Velocidad -> m/s
    "m/s": ("speed", 1.0, "m/s"),
    "km/h": ("speed", 1000.0 / 3600.0, "km/h"),
    "kmh": ("speed", 1000.0 / 3600.0, "km/h"),
    "mph": ("speed", 1609.344 / 3600.0, "mph"),
    "nudo": ("speed", 1852.0 / 3600.0, "kn"),
    "nudos": ("speed", 1852.0 / 3600.0, "kn"),
    "kn": ("speed", 1852.0 / 3600.0, "kn"),
    # Datos digitales (binario IEC común en SO) -> bytes
    "b": ("data", 1.0, "B"),
    "byte": ("data", 1.0, "B"),
    "bytes": ("data", 1.0, "B"),
    "kb": ("data", 1024.0, "KB"),
    "mb": ("data", 1024.0**2, "MB"),
    "gb": ("data", 1024.0**3, "GB"),
    "tb": ("data", 1024.0**4, "TB"),
}

_TEMP_ALIASES: dict[str, str] = {
    "c": "C",
    "celsius": "C",
    "centigrados": "C",
    "grados c": "C",
    "f": "F",
    "fahrenheit": "F",
    "grados f": "F",
    "k": "K",
    "kelvin": "K",
}


def _normalize_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents)


@dataclass(frozen=True, slots=True)
class DailyForecast:
    date: str
    temp_min_c: float
    temp_max_c: float
    weather_code: int
    condition_es: str
    precip_prob_pct: int | None = None


@dataclass(frozen=True, slots=True)
class WeatherReport:
    location_name: str
    country: str
    latitude: float
    longitude: float
    timezone_name: str
    temperature_c: float
    apparent_temperature_c: float
    humidity_pct: int
    wind_kmh: float
    weather_code: int
    condition_es: str
    daily: tuple[DailyForecast, ...] = ()
    cached: bool = False

    def to_sources(self) -> tuple[dict[str, str], ...]:
        return ({"title": "Open-Meteo (CC BY 4.0)", "url": "https://open-meteo.com/"},)

    def format_summary(self, *, include_forecast: bool = False) -> str:
        loc = f"{self.location_name}, {self.country}" if self.country else self.location_name
        base = (
            f"En {loc}: {self.temperature_c:.1f} °C (sensación {self.apparent_temperature_c:.1f} °C), "
            f"{self.condition_es.lower()}, humedad {self.humidity_pct}% y viento {self.wind_kmh:.0f} km/h."
        )
        if not include_forecast or not self.daily:
            return base
        lines = [base, "Pronóstico:"]
        for day in self.daily[:4]:
            prob = f" · lluvia {day.precip_prob_pct}%" if day.precip_prob_pct is not None else ""
            lines.append(
                f"- {day.date}: {day.temp_min_c:.0f} °C a {day.temp_max_c:.0f} °C, {day.condition_es}{prob}"
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class WorldTimeInfo:
    location: str
    timezone_name: str
    local_time_str: str
    local_date_str: str
    utc_offset_str: str


@dataclass(frozen=True, slots=True)
class UnitConversion:
    value: float
    from_unit: str
    to_unit: str
    result: float
    category: str

    def format_text(self) -> str:
        return f"{self.value:g} {self.from_unit} = {self.result:.4g} {self.to_unit}"


@dataclass(frozen=True, slots=True)
class CurrencyConversion:
    amount: float
    from_code: str
    to_code: str
    rate: float
    converted: float
    provider: str
    cached: bool = False

    def format_text(self) -> str:
        return (
            f"{self.amount:,.2f} {self.from_code} = {self.converted:,.2f} {self.to_code} "
            f"(1 {self.from_code} = {self.rate:.4f} {self.to_code})"
        )


def normalize_currency_code(raw: str) -> str:
    key = _normalize_key(raw)
    if key in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[key]
    upper = key.upper()
    if len(upper) == 3 and upper.isalpha():
        return upper
    raise ValueError(f"Divisa no reconocida: {raw!r}.")


def convert_units(value: float, from_unit: str, to_unit: str) -> UnitConversion:
    """Conversor 100% offline de temperatura, longitud, masa, volumen, velocidad y datos."""
    src_key = _normalize_key(from_unit).replace("°", "")
    dst_key = _normalize_key(to_unit).replace("°", "")

    if src_key in _TEMP_ALIASES and dst_key in _TEMP_ALIASES:
        src_t = _TEMP_ALIASES[src_key]
        dst_t = _TEMP_ALIASES[dst_key]
        if src_t == "C":
            celsius = value
        elif src_t == "F":
            celsius = (value - 32.0) * 5.0 / 9.0
        else:
            celsius = value - 273.15

        if dst_t == "C":
            out = celsius
        elif dst_t == "F":
            out = celsius * 9.0 / 5.0 + 32.0
        else:
            out = celsius + 273.15
        return UnitConversion(
            value=value,
            from_unit=f"°{src_t}" if src_t != "K" else "K",
            to_unit=f"°{dst_t}" if dst_t != "K" else "K",
            result=round(out, 4),
            category="temperature",
        )

    if src_key not in _LINEAR_UNITS or dst_key not in _LINEAR_UNITS:
        raise ValueError(f"No puedo convertir de '{from_unit}' a '{to_unit}'.")

    src_cat, src_factor, src_label = _LINEAR_UNITS[src_key]
    dst_cat, dst_factor, dst_label = _LINEAR_UNITS[dst_key]
    if src_cat != dst_cat:
        raise ValueError(
            f"Unidades incompatibles: '{from_unit}' ({src_cat}) y '{to_unit}' ({dst_cat})."
        )
    result = (value * src_factor) / dst_factor
    return UnitConversion(
        value=value,
        from_unit=src_label,
        to_unit=dst_label,
        result=result,
        category=src_cat,
    )


def get_world_time(location: str, *, now_utc: datetime | None = None) -> WorldTimeInfo:
    """Calcula la hora mundial offline usando ZoneInfo con respaldo de offset para Windows sin tzdata."""
    raw = location.strip()
    if not raw:
        raise ValueError("Debes indicar una ciudad, país o zona horaria.")

    utc_ref = now_utc.astimezone(UTC) if now_utc is not None else datetime.now(UTC)
    key = _normalize_key(raw)
    iana_name: str
    fallback_minutes: int | None = None
    display_loc = raw

    if key in _WORLD_TIMEZONES:
        iana_name, fallback_minutes, display_loc = _WORLD_TIMEZONES[key]
    elif "/" in raw or raw.upper() in ("UTC", "GMT"):
        iana_name = "UTC" if raw.upper() in ("UTC", "GMT") else raw
        fallback_minutes = 0 if iana_name == "UTC" else None
    else:
        matched = next(
            (entry for alias, entry in _WORLD_TIMEZONES.items() if alias in key or key in alias),
            None,
        )
        if matched is None:
            raise ValueError(
                f"No reconocí la zona horaria de '{raw}'. Prueba con una ciudad principal (ej. Madrid, Tokio, Nueva York)."
            )
        iana_name, fallback_minutes, display_loc = matched

    tz_obj: timezone | ZoneInfo
    try:
        tz_obj = ZoneInfo(iana_name)
    except (ZoneInfoNotFoundError, KeyError):
        if fallback_minutes is None:
            raise ValueError(f"Zona horaria '{iana_name}' no disponible localmente.") from None
        tz_obj = timezone(timedelta(minutes=fallback_minutes))

    local_dt = utc_ref.astimezone(tz_obj)
    offset = local_dt.utcoffset() or timedelta(0)
    total_min = int(offset.total_seconds() // 60)
    sign = "+" if total_min >= 0 else "-"
    abs_min = abs(total_min)
    offset_str = f"UTC{sign}{abs_min // 60:02d}:{abs_min % 60:02d}"

    return WorldTimeInfo(
        location=display_loc,
        timezone_name=iana_name,
        local_time_str=local_dt.strftime("%H:%M"),
        local_date_str=local_dt.strftime("%d/%m/%Y"),
        utc_offset_str=offset_str,
    )


class OpenMeteoService:
    """
    Cliente Open-Meteo (estrictamente tier gratuito) con caché TTL de 15 min
    y conversor de divisas gratuito sin API keys.
    """

    def __init__(
        self,
        *,
        weather_ttl_sec: float = 900.0,
        currency_ttl_sec: float = 3600.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._weather_ttl_sec = max(1.0, float(weather_ttl_sec))
        self._currency_ttl_sec = max(1.0, float(currency_ttl_sec))
        self._transport = transport
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._weather_cache: dict[tuple[str, str, int], tuple[float, WeatherReport]] = {}
        self._geo_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._rates_cache: dict[str, tuple[float, str, dict[str, float]]] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    def clear_cache(self) -> None:
        self._weather_cache.clear()
        self._geo_cache.clear()
        self._rates_cache.clear()

    async def _geocode(self, city: str, country: str = "") -> dict[str, Any]:
        geo_key = (_normalize_key(city), _normalize_key(country))
        now = self._clock()
        cached_geo = self._geo_cache.get(geo_key)
        if cached_geo is not None and (now - cached_geo[0]) < 86400.0:
            return cached_geo[1]

        client = self._get_client()
        resp = await client.get(
            _OPEN_METEO_GEOCODE_URL,
            params={
                "name": city.strip(),
                "count": 5,
                "language": "es",
                "format": "json",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not results:
            raise ValueError(f"No encontré la ciudad '{city}' en Open-Meteo.")

        chosen: dict[str, Any] = results[0]
        if country.strip():
            norm_country = _normalize_key(country)
            for item in results:
                if not isinstance(item, dict):
                    continue
                item_country = _normalize_key(str(item.get("country") or ""))
                item_code = _normalize_key(str(item.get("country_code") or ""))
                if norm_country in (item_country, item_code) or norm_country in item_country:
                    chosen = item
                    break

        self._geo_cache[geo_key] = (now, chosen)
        return chosen

    async def get_weather(
        self,
        city: str,
        *,
        country: str = "",
        forecast_days: int = 3,
    ) -> WeatherReport:
        clean_city = city.strip()
        if not clean_city:
            raise ValueError("Debes indicar una ciudad o configurarla en Ajustes -> General.")

        days = max(1, min(7, int(forecast_days)))
        cache_key = (_normalize_key(clean_city), _normalize_key(country), days)
        now = self._clock()

        cached_entry = self._weather_cache.get(cache_key)
        if cached_entry is not None and (now - cached_entry[0]) < self._weather_ttl_sec:
            stored = cached_entry[1]
            return WeatherReport(
                location_name=stored.location_name,
                country=stored.country,
                latitude=stored.latitude,
                longitude=stored.longitude,
                timezone_name=stored.timezone_name,
                temperature_c=stored.temperature_c,
                apparent_temperature_c=stored.apparent_temperature_c,
                humidity_pct=stored.humidity_pct,
                wind_kmh=stored.wind_kmh,
                weather_code=stored.weather_code,
                condition_es=stored.condition_es,
                daily=stored.daily,
                cached=True,
            )

        geo = await self._geocode(clean_city, country)
        lat = float(geo["latitude"])
        lon = float(geo["longitude"])
        loc_name = str(geo.get("name") or clean_city)
        loc_country = str(geo.get("country") or country)

        client = self._get_client()
        resp = await client.get(
            _OPEN_METEO_FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "weather_code,wind_speed_10m"
                ),
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": days,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current") if isinstance(data, dict) else None
        if not isinstance(current, dict):
            raise RuntimeError("Respuesta inesperada de Open-Meteo.")

        code = int(current.get("weather_code", 0) or 0)
        condition = _WMO_CODES_ES.get(code, f"Condición {code}")

        daily_items: list[DailyForecast] = []
        daily_raw = data.get("daily")
        if isinstance(daily_raw, dict):
            times = daily_raw.get("time") or []
            t_mins = daily_raw.get("temperature_2m_min") or []
            t_maxs = daily_raw.get("temperature_2m_max") or []
            codes = daily_raw.get("weather_code") or []
            probs = daily_raw.get("precipitation_probability_max") or []
            for idx, day_str in enumerate(times):
                d_code = int(codes[idx]) if idx < len(codes) and codes[idx] is not None else 0
                d_min = float(t_mins[idx]) if idx < len(t_mins) and t_mins[idx] is not None else 0.0
                d_max = float(t_maxs[idx]) if idx < len(t_maxs) and t_maxs[idx] is not None else 0.0
                d_prob = (
                    int(probs[idx])
                    if idx < len(probs) and probs[idx] is not None
                    else None
                )
                daily_items.append(
                    DailyForecast(
                        date=str(day_str),
                        temp_min_c=d_min,
                        temp_max_c=d_max,
                        weather_code=d_code,
                        condition_es=_WMO_CODES_ES.get(d_code, f"Condición {d_code}"),
                        precip_prob_pct=d_prob,
                    )
                )

        report = WeatherReport(
            location_name=loc_name,
            country=loc_country,
            latitude=lat,
            longitude=lon,
            timezone_name=str(data.get("timezone") or "auto"),
            temperature_c=float(current.get("temperature_2m", 0.0) or 0.0),
            apparent_temperature_c=float(
                current.get("apparent_temperature", current.get("temperature_2m", 0.0)) or 0.0
            ),
            humidity_pct=int(current.get("relative_humidity_2m", 0) or 0),
            wind_kmh=float(current.get("wind_speed_10m", 0.0) or 0.0),
            weather_code=code,
            condition_es=condition,
            daily=tuple(daily_items),
            cached=False,
        )
        self._weather_cache[cache_key] = (now, report)
        return report

    async def convert_currency(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
    ) -> CurrencyConversion:
        """Convierte divisas sin API key usando open.er-api.com con respaldo Frankfurter."""
        base = normalize_currency_code(from_currency)
        target = normalize_currency_code(to_currency)
        if base == target:
            return CurrencyConversion(
                amount=amount,
                from_code=base,
                to_code=target,
                rate=1.0,
                converted=amount,
                provider="local",
                cached=True,
            )

        now = self._clock()
        cached_rates = self._rates_cache.get(base)
        if (
            cached_rates is not None
            and (now - cached_rates[0]) < self._currency_ttl_sec
            and target in cached_rates[2]
        ):
            rate = cached_rates[2][target]
            return CurrencyConversion(
                amount=amount,
                from_code=base,
                to_code=target,
                rate=rate,
                converted=amount * rate,
                provider=cached_rates[1],
                cached=True,
            )

        client = self._get_client()
        # 1) Principal: ExchangeRate-API Open Access (0 claves, incluye CLP y LATAM)
        try:
            resp = await client.get(_OPEN_ER_API_URL.format(base=base))
            if resp.status_code == 200:
                payload = resp.json()
                rates_raw = payload.get("rates") if isinstance(payload, dict) else None
                if isinstance(rates_raw, dict) and target in rates_raw:
                    rates = {
                        str(k).upper(): float(v)
                        for k, v in rates_raw.items()
                        if isinstance(v, (int, float))
                    }
                    self._rates_cache[base] = (now, "open.er-api.com", rates)
                    rate = rates[target]
                    return CurrencyConversion(
                        amount=amount,
                        from_code=base,
                        to_code=target,
                        rate=rate,
                        converted=amount * rate,
                        provider="open.er-api.com",
                        cached=False,
                    )
        except httpx.HTTPError as exc:
            logger.debug("Fallo open.er-api.com (%s); intentando Frankfurter.", exc)

        # 2) Respaldo: Frankfurter (BCE, 0 claves)
        resp2 = await client.get(_FRANKFURTER_API_URL, params={"from": base, "to": target})
        resp2.raise_for_status()
        payload2 = resp2.json()
        rates2 = payload2.get("rates") if isinstance(payload2, dict) else None
        if not isinstance(rates2, dict) or target not in rates2:
            raise ValueError(f"No hay tasa de cambio disponible de {base} a {target}.")

        rate2 = float(rates2[target])
        self._rates_cache[base] = (now, "frankfurter.app", {target: rate2})
        return CurrencyConversion(
            amount=amount,
            from_code=base,
            to_code=target,
            rate=rate2,
            converted=amount * rate2,
            provider="frankfurter.app",
            cached=False,
        )
