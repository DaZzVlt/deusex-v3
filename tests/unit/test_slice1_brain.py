from __future__ import annotations

import asyncio
from pathlib import Path
import re
import threading
from typing import Any

import pytest

from deusex.app import DeusExApplication
from deusex.brain.catalog import ModelAutoSelector, ModelCatalogService, ProviderPool
from deusex.brain.providers import (
    PROVIDER_METADATA,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ModelInfo,
    ModelTier,
    ProviderError,
    ToolCall,
    detect_api_key_from_text,
)
from deusex.core.config import SecretsManager
from deusex.core.contracts import (
    BaseModule,
    FastPathRule,
    ModuleContext,
    ModuleState,
    RiskLevel,
    ToolResult,
    ToolSpec,
)
from deusex.core.db import Database
from deusex.core.events import SafetyConfirmationRequestedEvent
from deusex.core.permissions import Permission


class _FakeGeminiProvider(LLMProvider):
    """Proveedor determinista offline para probar Antídoto #1, EMA/429 y tool-calling."""

    def __init__(self, secrets: SecretsManager, *, fail_429_times: int = 0) -> None:
        super().__init__(PROVIDER_METADATA["gemini"], secrets)
        self.fail_429_times = fail_429_times
        self.calls = 0
        self.next_tool_call: ToolCall | None = None

    async def list_models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id="gemini-3.5-flash", provider_id="gemini", display_name="Gemini 3.5 Flash"),
            ModelInfo(id="gemini-3.8-flash", provider_id="gemini", display_name="Gemini 3.8 Flash"),
            ModelInfo(id="text-embedding-004", provider_id="gemini", display_name="Embedding"),
        ]

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.fail_429_times > 0:
            self.fail_429_times -= 1
            raise ProviderError(
                "429 RESOURCE_EXHAUSTED",
                provider_id="gemini",
                status_code=429,
                retryable=True,
            )
        if self.next_tool_call is not None:
            tc = self.next_tool_call
            self.next_tool_call = None
            return LLMResponse(
                text="",
                provider_id="gemini",
                model=request.model,
                tokens_in=20,
                tokens_out=10,
                tool_calls=(tc,),
                latency_ms=42.0,
            )
        return LLMResponse(
            text=f"Respuesta simulada ({request.model}): OK",
            provider_id="gemini",
            model=request.model,
            tokens_in=30,
            tokens_out=15,
            latency_ms=55.0,
        )


class _ReconcileDummyModule(BaseModule):
    module_id = "pc"
    display_name = "PC Dummy"
    description = "Valida Antídoto #2 (reconciliación desde settings.json)"
    required_permissions = (Permission.PC_BASIC,)

    def __init__(self) -> None:
        super().__init__()
        self.started_count = 0

    async def start(self, _ctx: ModuleContext) -> None:
        self.started_count += 1
        self.set_state(ModuleState.ACTIVE)

    async def stop(self) -> None:
        self.set_state(ModuleState.DISABLED)

    def get_tools(self) -> list[ToolSpec]:
        async def _danger(_args: dict[str, Any]) -> ToolResult:
            return ToolResult(ok=True, summary="Acción crítica ejecutada.")

        return [
            ToolSpec(
                name="pc.danger_test",
                module_id=self.module_id,
                description="Acción crítica de prueba",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_ADVANCED,
                risk_level=RiskLevel.CRITICAL,
                handler=_danger,
            )
        ]

    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp(_match: re.Match[str], _raw: str) -> ToolResult:
            return ToolResult(ok=True, summary="Reflejo PC activo")

        return [
            FastPathRule(
                rule_id="pc.fp.ping",
                module_id=self.module_id,
                priority=20,
                pattern=re.compile(r"^ping pc$"),
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=_fp,
            )
        ]


def test_antidote1_model_catalog_refresh_by_provider_id_and_cache(
    secrets_manager: SecretsManager,
    db: Database,
) -> None:
    secrets_manager.set("GEMINI_API_KEY", "AIza" + "X" * 35)
    fake = _FakeGeminiProvider(secrets_manager)
    pool = ProviderPool(secrets_manager, providers={"gemini": fake})
    catalog = ModelCatalogService(pool, db)

    models = asyncio.run(catalog.refresh("gemini", force=True))
    ids = [m.id for m in models]
    assert ids == ["gemini-3.5-flash", "gemini-3.8-flash"]

    sel = ModelAutoSelector.select("gemini", models)
    assert sel.fast_model == "gemini-3.5-flash"
    assert sel.deep_model == "gemini-3.8-flash"

    # Debe rechazar explícitamente objetos que no sean str
    with pytest.raises(TypeError):
        asyncio.run(catalog.refresh(fake))  # type: ignore[arg-type]


def test_provider_pool_cooldown_and_ema_on_429(secrets_manager: SecretsManager) -> None:
    secrets_manager.set("GEMINI_API_KEY", "AIza" + "Y" * 35)
    fake = _FakeGeminiProvider(secrets_manager, fail_429_times=1)
    pool = ProviderPool(
        secrets_manager,
        providers={"gemini": fake},
        cooldown_base_sec=5.0,
    )
    req = LLMRequest(messages=(), model="gemini-2.5-flash")

    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(pool.generate("gemini", req))
    assert exc_info.value.status_code == 429
    assert pool.is_in_cooldown("gemini") is True

    pool.reset_cooldown("gemini")
    assert pool.is_in_cooldown("gemini") is False
    resp = asyncio.run(pool.generate("gemini", req))
    assert resp.text.startswith("Respuesta simulada")
    assert pool.health_snapshot("gemini").ema_latency_ms > 0.0


def test_antidote2_reconciliation_fast_path_safety_gate_and_cost_guard(tmp_path: Path) -> None:
    user_dir = tmp_path / "app_data"

    # Primera sesión: activamos 'pc' en settings.json y cerramos
    app1 = DeusExApplication(
        user_data_dir=user_dir,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app1.settings.modules.set_enabled("pc", True)
    app1.save_settings(section="modules")
    app1.shutdown()

    # Segunda sesión: start() debe reconciliar 'pc' y sus reglas Fast-Path automáticamente
    app2 = DeusExApplication(
        user_data_dir=user_dir,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    dummy = _ReconcileDummyModule()
    app2.register_module(dummy)
    reconciled = app2.start()
    try:
        assert "pc" in reconciled
        assert dummy.state == ModuleState.ACTIVE
        assert dummy.started_count == 1

        # Fast-Path inyectado del módulo + Fast-Path universal en orden encadenada
        turn = app2.submit_user_message("ping pc y dime la hora").result(timeout=5.0)
        assert turn.fast_path is True
        assert "Reflejo PC activo" in turn.reply_text
        assert app2.cost_guard.today_summary().fast_path_hits >= 2

        # SafetyGate físico para herramienta CRITICAL (rechazo desde otro hilo)
        requested = threading.Event()
        captured_cid: list[str] = []

        def _on_safety(ev: SafetyConfirmationRequestedEvent) -> None:
            captured_cid.append(ev.confirmation_id)
            requested.set()

        app2.events.subscribe(SafetyConfirmationRequestedEvent, _on_safety)
        fut = app2.runtime.submit_async(app2.engine.tools.execute("pc.danger_test", {}))
        assert requested.wait(timeout=2.0) is True
        assert app2.resolve_safety_confirmation(captured_cid[0], approved=False) is True
        rejected_res = fut.result(timeout=3.0)
        assert rejected_res.ok is False
        assert rejected_res.error_code == "safety_rejected"

        # Modo Dry-Run simula sin disparar SafetyGate
        app2.settings.developer.enabled = True
        app2.settings.developer.dry_run = True
        dry_res = app2.runtime.async_core.run_sync(app2.engine.tools.execute("pc.danger_test", {}))
        assert dry_res.ok is True
        assert dry_res.dry_run is True
    finally:
        assert app2.shutdown() < 3.0


def test_memory_two_layers_privacy_freeze_and_chat_persistence(tmp_path: Path) -> None:
    user_dir = tmp_path / "mem_data"
    secrets = SecretsManager(
        DeusExApplication(
            user_data_dir=user_dir,
            enable_hotkeys=False,
            enable_earcons=False,
            fallback_to_os_env=False,
        ).paths,
        fallback_to_os_env=False,
    )
    secrets.set("GEMINI_API_KEY", "AIza" + "Z" * 35)
    fake_gemini = _FakeGeminiProvider(secrets)

    app = DeusExApplication(
        user_data_dir=user_dir,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
        providers_override={"gemini": fake_gemini},
    )
    app.start()
    try:
        turn = app.submit_user_message(
            "Hola, vivo en Concepción y me gusta mucho el rock clásico."
        ).result(timeout=5.0)
        assert turn.fast_path is False
        assert "Respuesta simulada" in turn.reply_text
        assert len(turn.saved_memories) >= 1
        assert app.cost_guard.today_summary().cloud_calls == 1

        # En Modo Privacidad se congela la extracción automática de memoria
        app.set_privacy_mode(True)
        turn_priv = app.submit_user_message(
            "Me gusta el ajedrez y vivo en Valdivia."
        ).result(timeout=5.0)
        assert turn_priv.saved_memories == ()

        # Degradación CostGuard DEEP -> FAST cuando se supera presupuesto
        app.settings.brain.daily_token_budget = 10
        assert app.cost_guard.resolve_effective_tier(ModelTier.DEEP) == ModelTier.FAST
    finally:
        assert app.shutdown() < 3.0


def test_smart_clipboard_catcher_detection() -> None:
    assert detect_api_key_from_text("texto cualquiera") is None
    det_g = detect_api_key_from_text("AIza" + "A" * 35)
    assert det_g is not None and det_g[0] == "gemini" and det_g[1] == "GEMINI_API_KEY"
    det_a = detect_api_key_from_text("sk-ant-" + "B" * 30)
    assert det_a is not None and det_a[0] == "anthropic"
    det_x = detect_api_key_from_text("xai-" + "C" * 30)
    assert det_x is not None and det_x[0] == "xai"


def test_groq_404_model_invalidation_and_chat_fallback(tmp_path: Path) -> None:
    from deusex.brain.catalog import (
        ModelAutoSelector,
        clear_invalid_models,
        get_invalid_models,
        record_invalid_model,
    )

    clear_invalid_models()
    try:
        # a) Registro de 404 para llama-3.3-70b-versatile
        record_invalid_model("groq", "llama-3.3-70b-versatile")
        assert "llama-3.3-70b-versatile" in get_invalid_models("groq")

        # Catálogo simulado de Groq con modelos de chat y modelos que no son de chat (whisper, guard)
        groq_models = [
            ModelInfo(id="whisper-large-v3", provider_id="groq", display_name="Whisper v3"),
            ModelInfo(id="whisper-large-v3-turbo", provider_id="groq", display_name="Whisper Turbo"),
            ModelInfo(id="llama-guard-3-8b", provider_id="groq", display_name="Guard 3 8B"),
            ModelInfo(id="llama-3.3-70b-versatile", provider_id="groq", display_name="Llama 3.3 70B"),
            ModelInfo(id="llama-3.1-8b-instant", provider_id="groq", display_name="Llama 3.1 8B"),
            ModelInfo(id="gemma2-9b-it", provider_id="groq", display_name="Gemma 2 9B"),
        ]

        selection = ModelAutoSelector.select(
            "groq", groq_models, excluded_models=get_invalid_models("groq")
        )
        # Verifica que NO seleccionó el modelo con 404 ni los modelos whisper/guard
        assert selection.fast_model != "llama-3.3-70b-versatile"
        assert not selection.fast_model.startswith("whisper")
        assert not selection.fast_model.startswith("llama-guard")
        assert selection.fast_model == "llama-3.1-8b-instant"

        # Si no hay catálogo en caché pero llama-3.3-70b-versatile está invalidado, usa fallback estático válido
        empty_selection = ModelAutoSelector.select(
            "groq", [], excluded_models=get_invalid_models("groq")
        )
        assert empty_selection.fast_model != "llama-3.3-70b-versatile"
        assert empty_selection.fast_model == "llama-3.1-8b-instant"
    finally:
        clear_invalid_models()


def test_provider_pool_cooldown_on_http_402(tmp_path: Path) -> None:
    from deusex.brain.catalog import ProviderPool
    from deusex.core.config import SecretsManager

    secrets = SecretsManager(
        DeusExApplication(
            user_data_dir=tmp_path,
            enable_hotkeys=False,
            enable_earcons=False,
            fallback_to_os_env=False,
        ).paths,
        fallback_to_os_env=False,
    )
    pool = ProviderPool(secrets=secrets)
    try:
        assert pool.is_in_cooldown("openrouter") is False
        delay = pool.record_failure("openrouter", status_code=402)
        assert delay >= 60.0
        assert pool.is_in_cooldown("openrouter") is True
        snap = pool.health_snapshot("openrouter")
        assert snap.in_cooldown is True
        assert snap.cooldown_remaining_sec >= 60.0
    finally:
        pool.reset_cooldown("openrouter")
