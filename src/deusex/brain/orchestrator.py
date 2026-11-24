from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import re
from typing import Any

from deusex.brain.catalog import ModelCatalogService, ProviderPool
from deusex.brain.cost_guard import CostGuard
from deusex.brain.memory import MemoryManager, MemoryRecord
from deusex.brain.personality import AVAILABLE_TONES, ProfileManager
from deusex.brain.providers import (
    LLMMessage,
    LLMRequest,
    MediaAttachment,
    ModelTier,
    ProviderError,
)
from deusex.core.config import DeusExSettings
from deusex.core.contracts import FastPathRule, RiskLevel, ToolResult
from deusex.core.engine import CoreEngine
from deusex.core.events import ChatMessageEvent, EventBus
from deusex.core.logging_setup import translate_error_es

logger = logging.getLogger(__name__)

_CHAIN_SPLIT_RE = re.compile(
    r"\s*(?:;\s*|\s+y\s+(?:despues|luego|tambien)\s+|\s+y\s+(?=(?:pon|ponme|crea|abre|cierra|sube|baja|silencia|cambia|activa|desactiva|dime|muestra|cancela|vacia|apaga|reinicia)\b))",
    re.IGNORECASE,
)
_DEEP_KEYWORDS: tuple[str, ...] = (
    "analiza",
    "compara",
    "investiga",
    "explica en detalle",
    "arquitectura",
    "código",
    "codigo",
    "resume este pdf",
    "plan detallado",
)

_TIME_RE = re.compile(r"^(?:que hora es|dime la hora|hora actual|que dia es hoy|dime la fecha|fecha de hoy)$")
_SWITCH_PROFILE_RE = re.compile(
    r"^(?:cambia|cambiar|pasa|usa|activa)\s+(?:a\s+|el perfil\s+|la personalidad\s+)?"
    r"(raphael|rafael|jarvis|friday|viernes|magi|mayi|glados)$"
)
_SWITCH_TONE_RE = re.compile(
    r"^(?:cambia|pon|modo)\s+(?:a\s+|el tono\s+|tono\s+)?"
    r"(equilibrado|conciso|formal|serio|relajado|tecnico|sarcastico)$"
)
_PRIVACY_ON_RE = re.compile(r"^(?:activa|enciende|pon)\s+(?:el\s+)?modo privacidad$")
_PRIVACY_OFF_RE = re.compile(r"^(?:desactiva|apaga|quita|sal del)\s+(?:el\s+)?modo privacidad$")
_MUTE_VOICE_RE = re.compile(r"^(?:silencia|callate|silenciar|apaga)\s+(?:la\s+)?(?:voz|audio|habla)$")
_UNMUTE_VOICE_RE = re.compile(r"^(?:activa|enciende|habilita)\s+(?:la\s+)?(?:voz|audio|habla)$")


@dataclass(frozen=True, slots=True)
class TurnResult:
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    reply_text: str
    spoken_text: str
    model_used: str
    fast_path: bool
    sources: tuple[dict[str, str], ...] = ()
    saved_memories: tuple[MemoryRecord, ...] = ()
    dry_run: bool = False
    error_code: str | None = None


def split_chained_commands(raw_text: str) -> list[str]:
    """Divide órdenes compuestas sin romper frases largas de conversación."""
    cleaned = raw_text.strip()
    if not cleaned or len(cleaned) > 260:
        return [cleaned] if cleaned else []
    parts = [p.strip(" ,.;") for p in _CHAIN_SPLIT_RE.split(cleaned) if p.strip(" ,.;")]
    return parts if len(parts) > 1 else [cleaned]


class BrainOrchestrator:
    """
    Orquestador central:
    1) Ejecuta Fast-Path universal/modular en <50 ms (0 tokens) incluyendo órdenes encadenadas.
    2) Si requiere IA, resuelve proveedor/modelo con CostGuard y ProviderPool.
    3) Ejecuta herramientas con ToolExecutor + SafetyGate + Dry-Run.
    4) Persiste historial (Capa 1) y extrae memoria adaptativa transparente (Capa 2).
    """

    def __init__(
        self,
        *,
        settings: DeusExSettings,
        engine: CoreEngine,
        pool: ProviderPool,
        catalog: ModelCatalogService,
        cost_guard: CostGuard,
        memory: MemoryManager,
        profiles: ProfileManager,
        events: EventBus,
        on_set_privacy: Callable[[bool], None],
        on_set_profile: Callable[[str, str | None], None],
        on_set_voice_muted: Callable[[bool], None],
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._pool = pool
        self._catalog = catalog
        self._cost_guard = cost_guard
        self._memory = memory
        self._profiles = profiles
        self._events = events
        self._on_set_privacy = on_set_privacy
        self._on_set_profile = on_set_profile
        self._on_set_voice_muted = on_set_voice_muted
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def build_universal_fast_path_rules(self) -> list[FastPathRule]:
        """Reflejos universales siempre activos (0 tokens, sin requerir API key ni módulos)."""

        async def _fp_time(_match: re.Match[str], raw: str) -> ToolResult:
            now = datetime.now()
            if "fecha" in raw.lower() or "dia" in raw.lower() or "día" in raw.lower():
                msg = f"Hoy es {now.strftime('%d/%m/%Y')} y son las {now.strftime('%H:%M')}."
            else:
                msg = f"Son las {now.strftime('%H:%M')}."
            return ToolResult(ok=True, summary=msg, spoken_text=msg)

        async def _fp_profile(match: re.Match[str], _raw: str) -> ToolResult:
            alias = match.group(1)
            prof = self._profiles.find_by_name_or_alias(alias)
            if prof is None:
                return ToolResult(ok=False, summary=f"No encontré el perfil '{alias}'.")
            self._on_set_profile(prof.id, None)
            msg = f"Perfil cambiado a {prof.name}."
            return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"profile_id": prof.id})

        async def _fp_tone(match: re.Match[str], _raw: str) -> ToolResult:
            raw_tone = match.group(1).lower()
            tone = "formal" if raw_tone == "serio" else raw_tone
            if tone not in AVAILABLE_TONES:
                return ToolResult(ok=False, summary=f"Tono '{tone}' no disponible.")
            self._on_set_profile(self._settings.ui.active_profile_id, tone)
            msg = f"Tono ajustado a {tone}."
            return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"tone": tone})

        async def _fp_privacy_on(_match: re.Match[str], _raw: str) -> ToolResult:
            self._on_set_privacy(True)
            msg = "Modo Privacidad activado. Memoria automática, visión y portapapeles pausados."
            return ToolResult(ok=True, summary=msg, spoken_text="Modo Privacidad activado.")

        async def _fp_privacy_off(_match: re.Match[str], _raw: str) -> ToolResult:
            self._on_set_privacy(False)
            msg = "Modo Privacidad desactivado."
            return ToolResult(ok=True, summary=msg, spoken_text=msg)

        async def _fp_mute(_match: re.Match[str], _raw: str) -> ToolResult:
            self._on_set_voice_muted(True)
            msg = "Voz silenciada. Responderé solo por texto."
            return ToolResult(ok=True, summary=msg, spoken_text="")

        async def _fp_unmute(_match: re.Match[str], _raw: str) -> ToolResult:
            self._on_set_voice_muted(False)
            msg = "Voz reactivada."
            return ToolResult(ok=True, summary=msg, spoken_text=msg)

        return [
            FastPathRule(
                rule_id="core.universal.time",
                module_id="core.universal",
                priority=5,
                pattern=_TIME_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_time,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.universal.profile",
                module_id="core.universal",
                priority=6,
                pattern=_SWITCH_PROFILE_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_profile,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.universal.tone",
                module_id="core.universal",
                priority=7,
                pattern=_SWITCH_TONE_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_tone,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.universal.privacy_on",
                module_id="core.universal",
                priority=8,
                pattern=_PRIVACY_ON_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_privacy_on,
                earcon="privacy_on",
            ),
            FastPathRule(
                rule_id="core.universal.privacy_off",
                module_id="core.universal",
                priority=9,
                pattern=_PRIVACY_OFF_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_privacy_off,
                earcon="privacy_off",
            ),
            FastPathRule(
                rule_id="core.universal.voice_mute",
                module_id="core.universal",
                priority=10,
                pattern=_MUTE_VOICE_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_mute,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.universal.voice_unmute",
                module_id="core.universal",
                priority=11,
                pattern=_UNMUTE_VOICE_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_unmute,
                earcon="confirm",
            ),
        ]

    def _infer_tier(self, user_text: str, attachments: tuple[MediaAttachment, ...]) -> ModelTier:
        requested = ModelTier.FAST
        lowered = user_text.lower()
        if attachments or len(user_text) > 360 or any(kw in lowered for kw in _DEEP_KEYWORDS):
            requested = ModelTier.DEEP
        return self._cost_guard.resolve_effective_tier(requested)

    def _pick_provider_order(self) -> list[str]:
        configured = self._pool.list_configured_provider_ids()
        primary = self._settings.brain.primary_provider.strip().lower()
        ordered: list[str] = []
        if primary in configured:
            ordered.append(primary)
        for pid in configured:
            if pid not in ordered:
                ordered.append(pid)
        return ordered

    async def handle_user_input(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        attachments: tuple[MediaAttachment, ...] = (),
        requested_tier: ModelTier | None = None,
    ) -> TurnResult:
        clean_text = text.strip()
        if not clean_text and not attachments:
            raise ValueError("El mensaje del usuario está vacío.")

        conv_id = conversation_id or self._memory.get_or_create_active_conversation().id
        user_record = self._memory.append_message(
            conversation_id=conv_id,
            role="user",
            content=clean_text or "[Adjunto multimedia]",
        )
        self._events.publish(
            ChatMessageEvent(
                conversation_id=conv_id,
                message_id=user_record.id,
                role="user",
                content=user_record.content,
            )
        )

        # 1. Fast-Path (soporta órdenes simples o encadenadas)
        fp_summaries: list[str] = []
        fp_spoken: list[str] = []
        remaining_clauses: list[str] = []
        any_dry_run = False

        if not attachments:
            clauses = split_chained_commands(clean_text)
            for clause in clauses:
                fp_res = await self._engine.fast_path.try_execute(clause)
                if fp_res is not None:
                    self._cost_guard.record_fast_path_hit(rule_id="fast_path")
                    fp_summaries.append(fp_res.summary)
                    fp_spoken.append(fp_res.spoken_text if fp_res.spoken_text is not None else fp_res.summary)
                    any_dry_run = any_dry_run or fp_res.dry_run
                else:
                    remaining_clauses.append(clause)

        if fp_summaries and not remaining_clauses:
            reply = "\n".join(fp_summaries)
            spoken = " ".join(s for s in fp_spoken if s).strip()
            asst_record = self._memory.append_message(
                conversation_id=conv_id,
                role="assistant",
                content=reply,
                model_used="fast-path",
                fast_path=True,
            )
            self._events.publish(
                ChatMessageEvent(
                    conversation_id=conv_id,
                    message_id=asst_record.id,
                    role="assistant",
                    content=reply,
                    model_used="fast-path",
                    fast_path=True,
                )
            )
            return TurnResult(
                conversation_id=conv_id,
                user_message_id=user_record.id,
                assistant_message_id=asst_record.id,
                reply_text=reply,
                spoken_text=spoken,
                model_used="fast-path",
                fast_path=True,
                dry_run=any_dry_run,
            )

        # 2. Extracción ligera de memoria adaptativa (Capa 2, congelada en Modo Privacidad)
        saved_memories = tuple(
            self._memory.extract_from_user_message(clean_text, source_message_id=user_record.id)
        )

        # 3. Llamada al cerebro IA (Multi-Provider + Fallback automático)
        provider_order = self._pick_provider_order()
        if not provider_order:
            no_key_msg = (
                "No hay ninguna clave de IA configurada todavía. "
                "Puedes usar comandos rápidos locales (temporizadores, hora, perfiles) o pegar tu "
                "GEMINI_API_KEY en Ajustes → IA y Claves."
            )
            if fp_summaries:
                no_key_msg = "\n".join(fp_summaries) + "\n\n" + no_key_msg
            asst_record = self._memory.append_message(
                conversation_id=conv_id,
                role="assistant",
                content=no_key_msg,
                model_used="offline",
                fast_path=bool(fp_summaries),
            )
            self._events.publish(
                ChatMessageEvent(
                    conversation_id=conv_id,
                    message_id=asst_record.id,
                    role="assistant",
                    content=no_key_msg,
                    model_used="offline",
                    fast_path=bool(fp_summaries),
                )
            )
            return TurnResult(
                conversation_id=conv_id,
                user_message_id=user_record.id,
                assistant_message_id=asst_record.id,
                reply_text=no_key_msg,
                spoken_text=no_key_msg,
                model_used="offline",
                fast_path=bool(fp_summaries),
                saved_memories=saved_memories,
                error_code="no_provider_configured",
            )

        tier = (
            self._cost_guard.resolve_effective_tier(requested_tier)
            if requested_tier is not None
            else self._infer_tier(clean_text, attachments)
        )

        system_prompt = self._profiles.compose_system_prompt(
            profile_id=self._settings.ui.active_profile_id,
            tone=self._settings.ui.active_tone,
            user_name=self._settings.general.user_name,
            city=self._settings.general.city,
            country=self._settings.general.country,
            memory_context=self._memory.format_prompt_context(),
            privacy_mode=self._settings.general.privacy_mode,
        )

        history = self._memory.recent_llm_messages(conv_id, limit=14)
        if history and history[-1].role == "user":
            prompt_tail = " y ".join(remaining_clauses) if remaining_clauses else clean_text
            if fp_summaries:
                prompt_tail = (
                    f"[Acciones locales ya completadas: {'; '.join(fp_summaries)}]\n"
                    f"Pendiente del usuario: {prompt_tail}"
                )
            history[-1] = LLMMessage(role="user", content=prompt_tail, attachments=attachments)

        available_tools = tuple(self._engine.tools.list_tools())
        last_error: BaseException | None = None

        for provider_id in provider_order:
            if self._pool.is_in_cooldown(provider_id):
                continue
            manual = (
                self._settings.brain.model_selection_mode == "manual"
                and provider_id == self._settings.brain.primary_provider
            )
            selection = self._catalog.select_models(
                provider_id,
                manual_mode=manual,
                manual_fast=self._settings.brain.manual_fast_model,
                manual_deep=self._settings.brain.manual_deep_model,
            )
            model_id = selection.for_tier(tier)

            tier_str = tier.value if hasattr(tier, "value") else str(tier)
            logger.info(
                "LLM intentando proveedor=%s modelo=%s tier=%s",
                provider_id,
                model_id,
                tier_str,
            )
            try:
                reply_text, sources, total_in, total_out, tool_dry_run = await self._run_llm_loop(
                    provider_id=provider_id,
                    model_id=model_id,
                    system_prompt=system_prompt,
                    conversation_messages=list(history),
                    tools=available_tools,
                    tier=tier,
                )
            except ProviderError as exc:
                last_error = exc
                if exc.status_code == 404:
                    with contextlib.suppress(Exception):
                        t = asyncio.create_task(self._catalog.refresh(provider_id, force=True))
                        self._bg_tasks.add(t)
                        t.add_done_callback(self._bg_tasks.discard)
                logger.warning("Proveedor '%s' falló (%s); evaluando respaldo.", provider_id, exc)
                continue

            self._cost_guard.record_cloud_usage(
                provider_id=provider_id,
                model_id=model_id,
                tokens_in=total_in,
                tokens_out=total_out,
            )

            full_reply = (
                "\n".join(fp_summaries) + "\n\n" + reply_text if fp_summaries and reply_text else reply_text
            )
            if not full_reply:
                full_reply = "\n".join(fp_summaries) or "Listo."

            asst_record = self._memory.append_message(
                conversation_id=conv_id,
                role="assistant",
                content=full_reply,
                model_used=f"{provider_id}:{model_id}",
                fast_path=False,
                sources=sources,
                tokens_in=total_in,
                tokens_out=total_out,
            )
            self._events.publish(
                ChatMessageEvent(
                    conversation_id=conv_id,
                    message_id=asst_record.id,
                    role="assistant",
                    content=full_reply,
                    model_used=asst_record.model_used,
                    fast_path=False,
                    sources=sources,
                )
            )
            return TurnResult(
                conversation_id=conv_id,
                user_message_id=user_record.id,
                assistant_message_id=asst_record.id,
                reply_text=full_reply,
                spoken_text=full_reply,
                model_used=asst_record.model_used,
                fast_path=False,
                sources=sources,
                saved_memories=saved_memories,
                dry_run=any_dry_run or tool_dry_run,
            )

        err_es = translate_error_es(last_error or "Sin proveedores disponibles")
        asst_record = self._memory.append_message(
            conversation_id=conv_id,
            role="assistant",
            content=err_es,
            model_used="error",
        )
        self._events.publish(
            ChatMessageEvent(
                conversation_id=conv_id,
                message_id=asst_record.id,
                role="assistant",
                content=err_es,
                model_used="error",
            )
        )
        return TurnResult(
            conversation_id=conv_id,
            user_message_id=user_record.id,
            assistant_message_id=asst_record.id,
            reply_text=err_es,
            spoken_text=err_es,
            model_used="error",
            fast_path=False,
            saved_memories=saved_memories,
            error_code="provider_failed",
        )

    async def _run_llm_loop(
        self,
        *,
        provider_id: str,
        model_id: str,
        system_prompt: str,
        conversation_messages: list[LLMMessage],
        tools: tuple[Any, ...],
        max_rounds: int = 4,
        tier: ModelTier = ModelTier.FAST,
    ) -> tuple[str, tuple[dict[str, str], ...], int, int, bool]:
        messages = list(conversation_messages)
        total_in = 0
        total_out = 0
        all_sources: list[dict[str, str]] = []
        any_dry_run = False
        request_timeout = 25.0 if tier == ModelTier.DEEP else 18.0

        for _ in range(max_rounds):
            req = LLMRequest(
                messages=tuple(messages),
                model=model_id,
                system_prompt=system_prompt,
                tools=tools,
                timeout_sec=request_timeout,
                tier=tier,
            )
            resp = await self._pool.generate(provider_id, req)
            logger.info(
                "LLM respondió proveedor=%s modelo=%s en %.0f ms",
                provider_id,
                resp.model,
                resp.latency_ms,
            )
            total_in += resp.tokens_in
            total_out += resp.tokens_out
            all_sources.extend(resp.sources)

            if not resp.tool_calls:
                return resp.text, tuple(all_sources), total_in, total_out, any_dry_run

            tool_outputs: list[str] = []
            for call in resp.tool_calls:
                t_res = await self._engine.tools.execute(call.name, call.arguments)
                any_dry_run = any_dry_run or t_res.dry_run
                all_sources.extend(t_res.sources)
                tool_outputs.append(
                    json.dumps(
                        {
                            "tool": call.name,
                            "ok": t_res.ok,
                            "summary": t_res.summary,
                            "data": t_res.data,
                            "error_code": t_res.error_code,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                )

            if resp.text:
                messages.append(LLMMessage(role="assistant", content=resp.text))
            messages.append(
                LLMMessage(
                    role="user",
                    content="Resultados de herramientas ejecutadas:\n" + "\n".join(tool_outputs),
                )
            )

        return (
            "Completé las acciones solicitadas.",
            tuple(all_sources),
            total_in,
            total_out,
            any_dry_run,
        )
