from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import threading
import time

from deusex.brain.providers import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelInfo,
    ModelTier,
    ProviderError,
    create_default_providers,
    is_usable_chat_model,
)
from deusex.core.config import SecretsManager
from deusex.core.db import Database

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_TTL = timedelta(hours=24)
_DEFAULT_FALLBACK_MODELS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini-3.5-flash", "gemini-3.8-flash"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "xai": ("grok-3-mini", "grok-3"),
    "anthropic": ("claude-3-5-haiku-latest", "claude-sonnet-4-0"),
    "groq": ("llama-3.1-8b-instant", "llama-3.3-70b-versatile"),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "openrouter": ("google/gemini-3.5-flash", "google/gemini-3.8-flash"),
}

_STATIC_FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "groq": (
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "llama-3.3-70b-specdec",
        "qwen-2.5-32b",
        "gemma2-9b-it",
        "llama3-70b-8192",
        "llama3-8b-8192",
    ),
    "gemini": ("gemini-3.5-flash", "gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-pro"),
    "openai": ("gpt-4o-mini", "gpt-4o"),
    "xai": ("grok-3-mini", "grok-3"),
    "anthropic": ("claude-3-5-haiku-latest", "claude-sonnet-4-0"),
    "deepseek": ("deepseek-chat", "deepseek-reasoner"),
    "openrouter": ("google/gemini-3.5-flash", "google/gemini-3.8-flash"),
}

_invalid_models: dict[str, set[str]] = {}
_invalid_models_lock = threading.RLock()


def record_invalid_model(provider_id: str, model_id: str) -> None:
    """Registra en memoria que un modelo devolvió HTTP 404 (no existe o no accesible)."""
    clean_id = provider_id.strip().lower()
    with _invalid_models_lock:
        _invalid_models.setdefault(clean_id, set()).add(model_id.strip())


def get_invalid_models(provider_id: str) -> frozenset[str]:
    """Retorna los modelos invalidados para un proveedor."""
    clean_id = provider_id.strip().lower()
    with _invalid_models_lock:
        return frozenset(_invalid_models.get(clean_id, set()))


def clear_invalid_models(provider_id: str | None = None) -> None:
    """Limpia el registro de modelos inválidos (útil para pruebas)."""
    with _invalid_models_lock:
        if provider_id is None:
            _invalid_models.clear()
        else:
            _invalid_models.pop(provider_id.strip().lower(), None)


@dataclass(frozen=True, slots=True)
class ModelSelection:
    provider_id: str
    fast_model: str
    deep_model: str

    def for_tier(self, tier: ModelTier) -> str:
        return self.deep_model if tier == ModelTier.DEEP else self.fast_model


def _score_fast_model(model_id: str) -> int:
    lowered = model_id.lower()
    score = 0
    if "flash-lite" in lowered:
        score += 75
    elif "flash" in lowered:
        score += 100
    if "mini" in lowered or "haiku" in lowered or "nano" in lowered:
        score += 90
    if "chat" in lowered or "versatile" in lowered or "instant" in lowered:
        score += 70
    if "3.5" in lowered:
        score += 55
    elif "3.8" in lowered or "3.6" in lowered:
        score += 45
    elif "3.1" in lowered:
        score += 35
    elif "4o" in lowered or "4.1" in lowered:
        score += 30
    elif "2.5" in lowered:
        score -= 50
    elif "2.0" in lowered:
        score -= 60
    if "pro" in lowered or "opus" in lowered or "reasoner" in lowered:
        score -= 20
    if "preview" in lowered or "exp" in lowered:
        score -= 10
    if "thinking" in lowered:
        score -= 15
    return score


def _score_deep_model(model_id: str) -> int:
    lowered = model_id.lower()
    score = 0
    if "pro" in lowered or "sonnet" in lowered or "opus" in lowered or "reasoner" in lowered:
        score += 100
    elif "grok-3" in lowered or "gpt-4o" in lowered or "gpt-4.1" in lowered:
        score += 85
    elif "flash" in lowered:
        score += 65
    if "3.8" in lowered or "3.6" in lowered:
        score += 55
    elif "3.5" in lowered:
        score += 45
    elif "4o" in lowered or "4.1" in lowered:
        score += 30
    elif "2.5" in lowered:
        score -= 50
    elif "2.0" in lowered:
        score -= 60
    if "lite" in lowered or "mini" in lowered or "haiku" in lowered or "nano" in lowered:
        score -= 35
    if "preview" in lowered or "exp" in lowered:
        score -= 8
    return score


class ModelAutoSelector:
    """Selecciona automáticamente el mejor modelo FAST y DEEP sobre modelos reales listados."""

    @staticmethod
    def select(
        provider_id: str,
        models: list[ModelInfo],
        excluded_models: set[str] | frozenset[str] = frozenset(),
    ) -> ModelSelection:
        clean_id = provider_id.strip().lower()
        usable = [
            m for m in models
            if not m.is_deprecated
            and is_usable_chat_model(m.id)
            and m.id not in excluded_models
        ]
        if not usable:
            candidates = [
                m for m in _STATIC_FALLBACK_ORDER.get(clean_id, ())
                if m not in excluded_models and is_usable_chat_model(m)
            ]
            if candidates:
                best_fast = candidates[0]
                best_deep = candidates[1] if len(candidates) > 1 else candidates[0]
            else:
                fallback_fast, fallback_deep = _DEFAULT_FALLBACK_MODELS.get(
                    clean_id, ("gemini-3.5-flash", "gemini-3.8-flash")
                )
                best_fast = fallback_fast if fallback_fast not in excluded_models else ""
                best_deep = fallback_deep if fallback_deep not in excluded_models else ""

            return ModelSelection(
                provider_id=clean_id,
                fast_model=best_fast,
                deep_model=best_deep,
            )

        best_fast = max(usable, key=lambda m: (_score_fast_model(m.id), m.id)).id
        best_deep = max(usable, key=lambda m: (_score_deep_model(m.id), m.id)).id
        return ModelSelection(
            provider_id=clean_id,
            fast_model=best_fast,
            deep_model=best_deep,
        )


@dataclass(slots=True)
class _ProviderHealth:
    semaphore: asyncio.Semaphore
    tokens: float
    burst_capacity: float
    refill_per_sec: float
    last_refill_mono: float
    ema_latency_ms: float = 0.0
    consecutive_failures: int = 0
    cooldown_until_mono: float = 0.0


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    provider_id: str
    configured: bool
    ema_latency_ms: float
    consecutive_failures: int
    in_cooldown: bool
    cooldown_remaining_sec: float


class ProviderPool:
    """
    Registro de proveedores IA con:
    - Semáforo de concurrencia por proveedor
    - Limitador suave de ráfagas (token bucket)
    - Enfriamiento exponencial (circuit breaker) ante 429/503
    - Seguimiento de latencia EMA
    """

    def __init__(
        self,
        secrets: SecretsManager,
        *,
        providers: dict[str, LLMProvider] | None = None,
        max_concurrency: int = 2,
        burst_capacity: float = 4.0,
        refill_per_sec: float = 1.0,
        cooldown_base_sec: float = 4.0,
        cooldown_max_sec: float = 60.0,
    ) -> None:
        self._secrets = secrets
        self._providers: dict[str, LLMProvider] = (
            dict(providers) if providers is not None else create_default_providers(secrets)
        )
        self._lock = threading.RLock()
        self._max_concurrency = max(1, max_concurrency)
        self._burst_capacity = max(1.0, burst_capacity)
        self._refill_per_sec = max(0.1, refill_per_sec)
        self._cooldown_base_sec = cooldown_base_sec
        self._cooldown_max_sec = cooldown_max_sec
        self._health: dict[str, _ProviderHealth] = {}
        for pid in self._providers:
            self._health[pid] = self._new_health()

    def _new_health(self) -> _ProviderHealth:
        return _ProviderHealth(
            semaphore=asyncio.Semaphore(self._max_concurrency),
            tokens=self._burst_capacity,
            burst_capacity=self._burst_capacity,
            refill_per_sec=self._refill_per_sec,
            last_refill_mono=time.monotonic(),
        )

    def register_provider(self, provider: LLMProvider) -> None:
        with self._lock:
            self._providers[provider.provider_id] = provider
            self._health[provider.provider_id] = self._new_health()

    def get_provider(self, provider_id: str) -> LLMProvider:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id debe ser un string no vacío.")
        clean_id = provider_id.strip().lower()
        with self._lock:
            provider = self._providers.get(clean_id)
        if provider is None:
            raise KeyError(f"Proveedor IA desconocido: {provider_id!r}")
        return provider

    def list_provider_ids(self) -> list[str]:
        with self._lock:
            return list(self._providers.keys())

    def list_configured_provider_ids(self) -> list[str]:
        with self._lock:
            return [pid for pid, prov in self._providers.items() if prov.is_configured()]

    def is_in_cooldown(self, provider_id: str) -> bool:
        with self._lock:
            health = self._health.get(provider_id)
            if health is None:
                return False
            return time.monotonic() < health.cooldown_until_mono

    def health_snapshot(self, provider_id: str) -> ProviderHealthSnapshot:
        provider = self.get_provider(provider_id)
        now = time.monotonic()
        with self._lock:
            health = self._health[provider.provider_id]
            remaining = max(0.0, health.cooldown_until_mono - now)
            return ProviderHealthSnapshot(
                provider_id=provider.provider_id,
                configured=provider.is_configured(),
                ema_latency_ms=round(health.ema_latency_ms, 1),
                consecutive_failures=health.consecutive_failures,
                in_cooldown=remaining > 0.0,
                cooldown_remaining_sec=round(remaining, 1),
            )

    def reset_cooldown(self, provider_id: str) -> None:
        with self._lock:
            health = self._health.get(provider_id)
            if health is not None:
                health.consecutive_failures = 0
                health.cooldown_until_mono = 0.0

    async def _acquire_burst_token(self, health: _ProviderHealth) -> None:
        while True:
            now = time.monotonic()
            elapsed = max(0.0, now - health.last_refill_mono)
            health.tokens = min(
                health.burst_capacity,
                health.tokens + elapsed * health.refill_per_sec,
            )
            health.last_refill_mono = now
            if health.tokens >= 1.0:
                health.tokens -= 1.0
                return
            wait_sec = (1.0 - health.tokens) / health.refill_per_sec
            await asyncio.sleep(min(wait_sec, 1.0))

    def _record_success(self, provider_id: str, latency_ms: float) -> None:
        with self._lock:
            health = self._health[provider_id]
            health.consecutive_failures = 0
            health.cooldown_until_mono = 0.0
            if health.ema_latency_ms <= 0.0:
                health.ema_latency_ms = latency_ms
            else:
                health.ema_latency_ms = 0.3 * latency_ms + 0.7 * health.ema_latency_ms

    def _record_quota_failure(self, provider_id: str) -> float:
        """Enfriamiento prolongado (300 s) cuando el proveedor agota créditos / presupuesto (HTTP 402)."""
        delay = 300.0
        with self._lock:
            health = self._health[provider_id]
            health.consecutive_failures += 1
            health.cooldown_until_mono = time.monotonic() + delay
        logger.warning(
            "Proveedor '%s' en enfriamiento %.1f s: sin créditos disponibles (HTTP 402).",
            provider_id,
            delay,
        )
        return delay

    def _record_retryable_failure(self, provider_id: str, status_code: int | None) -> float:
        with self._lock:
            health = self._health[provider_id]
            health.consecutive_failures += 1
            delay = min(
                self._cooldown_max_sec,
                self._cooldown_base_sec * (2 ** (health.consecutive_failures - 1)),
            )
            health.cooldown_until_mono = time.monotonic() + delay
        err_desc = f"código {status_code}" if status_code is not None else "timeout o error de red"
        logger.warning(
            "Proveedor '%s' en enfriamiento %.1f s tras %s",
            provider_id,
            delay,
            err_desc,
        )
        return float(delay)

    def record_failure(self, provider_id: str, status_code: int | None = None) -> float:
        if status_code == 402:
            return self._record_quota_failure(provider_id)
        if status_code == 404:
            delay = 300.0
            with self._lock:
                health = self._health[provider_id]
                health.consecutive_failures += 1
                health.cooldown_until_mono = time.monotonic() + delay
            logger.warning(
                "Proveedor '%s' en enfriamiento %.1f s tras error 404 sin modelos válidos.",
                provider_id,
                delay,
            )
            return delay
        return self._record_retryable_failure(provider_id, status_code)

    async def generate(self, provider_id: str, request: LLMRequest) -> LLMResponse:
        provider = self.get_provider(provider_id)
        with self._lock:
            health = self._health[provider.provider_id]
            remaining = health.cooldown_until_mono - time.monotonic()

        if remaining > 0.0:
            raise ProviderError(
                f"Proveedor '{provider.provider_id}' en enfriamiento anti-429 ({remaining:.1f} s restantes).",
                provider_id=provider.provider_id,
                status_code=429,
                retryable=True,
            )

        await self._acquire_burst_token(health)
        async with health.semaphore:
            started = time.perf_counter()
            try:
                response = await provider.generate(request)
            except ProviderError as exc:
                if exc.status_code == 404:
                    record_invalid_model(provider.provider_id, request.model)
                    logger.warning(
                        "Modelo '%s' no encontrado (HTTP 404) en proveedor '%s'. Registrado como inválido.",
                        request.model,
                        provider.provider_id,
                    )
                elif exc.status_code == 402:
                    self.record_failure(provider.provider_id, 402)
                elif exc.retryable or exc.status_code in (429, 503):
                    self.record_failure(provider.provider_id, exc.status_code)
                raise
            elapsed_ms = response.latency_ms or ((time.perf_counter() - started) * 1000.0)
            self._record_success(provider.provider_id, elapsed_ms)
            return response

    async def close_all(self) -> None:
        with self._lock:
            providers = list(self._providers.values())
        for provider in providers:
            try:
                await provider.close()
            except Exception as exc:
                logger.debug("Error cerrando proveedor '%s': %s", provider.provider_id, exc)


class ModelCatalogService:
    """
    Antídoto #1 definitivo contra POSTMORTEM_V2:
    `refresh(provider_id: str)` resuelve internamente la instancia del proveedor desde `ProviderPool`
    y cachea el catálogo real en SQLite WAL con TTL de 24 h.
    """

    def __init__(
        self,
        pool: ProviderPool,
        db: Database,
        *,
        ttl: timedelta = _DEFAULT_CACHE_TTL,
    ) -> None:
        self._pool = pool
        self._db = db
        self._ttl = ttl
        self._lock = threading.RLock()
        self._memory_cache: dict[str, tuple[datetime, list[ModelInfo]]] = {}

    def _read_db_cache(self, provider_id: str) -> tuple[datetime, list[ModelInfo]] | None:
        row = self._db.fetchone(
            "SELECT models_json, fetched_at FROM model_catalog_cache WHERE provider_id = ?",
            (provider_id,),
        )
        if row is None:
            return None
        try:
            fetched_at = datetime.fromisoformat(str(row["fetched_at"]))
            raw_list = json.loads(str(row["models_json"]))
            if not isinstance(raw_list, list):
                return None
            models = [ModelInfo.from_dict(item) for item in raw_list if isinstance(item, dict)]
            return fetched_at, models
        except (ValueError, KeyError, TypeError):
            return None

    def _write_db_cache(self, provider_id: str, models: list[ModelInfo], fetched_at: datetime) -> None:
        serialized = json.dumps([m.to_dict() for m in models], ensure_ascii=False)
        self._db.execute(
            """
            INSERT INTO model_catalog_cache (provider_id, models_json, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(provider_id) DO UPDATE SET
                models_json = excluded.models_json,
                fetched_at = excluded.fetched_at
            """,
            (provider_id, serialized, fetched_at.isoformat()),
        )

    def get_cached_models(self, provider_id: str, *, allow_expired: bool = True) -> list[ModelInfo]:
        clean_id = provider_id.strip().lower()
        now = datetime.now(UTC)

        with self._lock:
            entry = self._memory_cache.get(clean_id)
        if entry is not None:
            fetched_at, models = entry
            if allow_expired or (now - fetched_at) <= self._ttl:
                return list(models)

        db_entry = self._read_db_cache(clean_id)
        if db_entry is not None:
            fetched_at, models = db_entry
            with self._lock:
                self._memory_cache[clean_id] = (fetched_at, models)
            if allow_expired or (now - fetched_at) <= self._ttl:
                return list(models)
        return []

    async def refresh(self, provider_id: str, *, force: bool = False) -> list[ModelInfo]:
        """Antídoto #1: recibe exclusivamente `provider_id: str` y resuelve el proveedor internamente."""
        if not isinstance(provider_id, str):
            raise TypeError(
                f"ModelCatalogService.refresh exige provider_id: str, recibió {type(provider_id).__name__}"
            )

        clean_id = provider_id.strip().lower()
        provider = self._pool.get_provider(clean_id)

        if not force:
            fresh = self.get_cached_models(clean_id, allow_expired=False)
            if fresh:
                return fresh

        if not provider.is_configured():
            logger.info("Proveedor '%s' sin clave API configurada; devolviendo lista vacía.", clean_id)
            return []

        raw_models = await provider.list_models()
        cleaned = sorted(
            [m for m in raw_models if is_usable_chat_model(m.id)],
            key=lambda m: m.id,
        )
        now = datetime.now(UTC)
        with self._lock:
            self._memory_cache[clean_id] = (now, cleaned)
        self._write_db_cache(clean_id, cleaned, now)
        logger.info("Catálogo '%s' actualizado con %d modelos reales.", clean_id, len(cleaned))
        return list(cleaned)

    def select_models(
        self,
        provider_id: str,
        *,
        manual_mode: bool = False,
        manual_fast: str = "",
        manual_deep: str = "",
    ) -> ModelSelection:
        clean_id = provider_id.strip().lower()
        invalid = get_invalid_models(clean_id)
        cached = self.get_cached_models(clean_id)
        auto = ModelAutoSelector.select(clean_id, cached, excluded_models=invalid)

        fast_chosen = (
            manual_fast.strip()
            if (manual_mode and manual_fast.strip() and manual_fast.strip() not in invalid)
            else auto.fast_model
        )
        deep_chosen = (
            manual_deep.strip()
            if (manual_mode and manual_deep.strip() and manual_deep.strip() not in invalid)
            else auto.deep_model
        )

        if not fast_chosen and not deep_chosen:
            self._pool.record_failure(clean_id, 404)

        return ModelSelection(
            provider_id=clean_id,
            fast_model=fast_chosen,
            deep_model=deep_chosen,
        )

    def invalidate_model(self, provider_id: str, model_id: str) -> None:
        record_invalid_model(provider_id, model_id)

    def get_invalid_models(self, provider_id: str) -> frozenset[str]:
        return get_invalid_models(provider_id)
