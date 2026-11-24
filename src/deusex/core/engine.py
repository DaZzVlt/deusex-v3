from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import re
import threading
import time
from typing import Any
import unicodedata
import uuid

from deusex.core.config import DeusExSettings
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
from deusex.core.events import (
    EventBus,
    FastPathExecutedEvent,
    ModuleStateChangedEvent,
    SafetyConfirmationRequestedEvent,
    SafetyConfirmationResolvedEvent,
)
from deusex.core.permissions import Permission

logger = logging.getLogger(__name__)

# Únicos permisos elegibles para bypass opcional si el usuario lo activa explícitamente.
# Las acciones destructivas de PC/borrados JAMÁS pueden saltarse SafetyGate.
_TRUSTED_MESSAGING_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.WORKSPACE_GMAIL_SEND,
        Permission.WHATSAPP_SEND,
        Permission.DISCORD_CHAT,
    }
)

_LEADING_WAKE_RE = re.compile(
    r"^(?:(?:oye|hey|ok|hola|por favor)\s+)*"
    r"(?:(?:deusex|deus ex|raphael|rafael|jarvis|friday|magi|glados)[,\s]+)+",
    re.IGNORECASE,
)
_LEADING_POLITE_RE = re.compile(r"^(?:por favor|oye|hey|disculpa)[,\s]+", re.IGNORECASE)
_TRAILING_POLITE_RE = re.compile(r"[,\s]+(?:por favor|pls|please)$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_command_text(text: str) -> str:
    """Normaliza órdenes en español (sin acentos, minúsculas y sin muletillas) para Fast-Path."""
    decomposed = unicodedata.normalize("NFKD", text.strip())
    ascii_like = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = ascii_like.lower().strip(" \t\r\n¿?¡!.,;:")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _LEADING_WAKE_RE.sub("", cleaned)
    cleaned = _LEADING_POLITE_RE.sub("", cleaned)
    cleaned = _TRAILING_POLITE_RE.sub("", cleaned)
    return cleaned.strip(" \t\r\n¿?¡!.,;:")


def _format_args_preview(args: dict[str, Any]) -> str:
    if not args:
        return "Sin parámetros"
    pairs = [f"{k}={v!r}" for k, v in list(args.items())[:5]]
    preview = ", ".join(pairs)
    return preview[:220] + ("..." if len(preview) > 220 else "")


@dataclass(slots=True)
class PendingConfirmation:
    confirmation_id: str
    tool_name: str
    module_id: str
    summary_es: str
    args_preview: str
    timeout_sec: int
    created_at: datetime
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[bool]


@dataclass(frozen=True, slots=True)
class PendingConfirmationView:
    confirmation_id: str
    tool_name: str
    module_id: str
    summary_es: str
    args_preview: str
    timeout_sec: int
    created_at_iso: str


class SafetyGate:
    """Freno físico obligatorio para acciones CRITICAL con resolución thread-safe desde UI/Discord/APK."""

    def __init__(self, events: EventBus, settings: DeusExSettings) -> None:
        self._events = events
        self._settings = settings
        self._lock = threading.RLock()
        self._pending: dict[str, PendingConfirmation] = {}

    def can_bypass_trusted_messaging(self, permission: Permission | None) -> bool:
        return (
            self._settings.security.allow_trusted_messaging_bypass
            and permission in _TRUSTED_MESSAGING_PERMISSIONS
        )

    def list_pending(self) -> list[PendingConfirmationView]:
        with self._lock:
            return [
                PendingConfirmationView(
                    confirmation_id=item.confirmation_id,
                    tool_name=item.tool_name,
                    module_id=item.module_id,
                    summary_es=item.summary_es,
                    args_preview=item.args_preview,
                    timeout_sec=item.timeout_sec,
                    created_at_iso=item.created_at.isoformat(),
                )
                for item in self._pending.values()
            ]

    async def request_confirmation(
        self,
        *,
        tool_name: str,
        module_id: str,
        summary_es: str,
        args: dict[str, Any] | None = None,
        timeout_override: int | None = None,
    ) -> tuple[bool, str]:
        timeout_sec = (
            timeout_override
            if timeout_override is not None
            else self._settings.security.safety_timeout_sec
        )
        confirmation_id = uuid.uuid4().hex[:10]
        args_preview = _format_args_preview(args or {})
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        pending = PendingConfirmation(
            confirmation_id=confirmation_id,
            tool_name=tool_name,
            module_id=module_id,
            summary_es=summary_es,
            args_preview=args_preview,
            timeout_sec=timeout_sec,
            created_at=datetime.now(UTC),
            loop=loop,
            future=future,
        )
        with self._lock:
            self._pending[confirmation_id] = pending

        self._events.publish(
            SafetyConfirmationRequestedEvent(
                confirmation_id=confirmation_id,
                tool_name=tool_name,
                module_id=module_id,
                summary_es=summary_es,
                args_preview=args_preview,
                timeout_sec=timeout_sec,
            )
        )

        try:
            approved = await asyncio.wait_for(future, timeout=float(timeout_sec))
            return approved, confirmation_id
        except TimeoutError:
            with self._lock:
                self._pending.pop(confirmation_id, None)
            self._events.publish(
                SafetyConfirmationResolvedEvent(
                    confirmation_id=confirmation_id,
                    approved=False,
                    reason="timeout",
                )
            )
            return False, confirmation_id
        except asyncio.CancelledError:
            with self._lock:
                self._pending.pop(confirmation_id, None)
            self._events.publish(
                SafetyConfirmationResolvedEvent(
                    confirmation_id=confirmation_id,
                    approved=False,
                    reason="cancelled",
                )
            )
            raise

    def resolve(self, confirmation_id: str, *, approved: bool, reason: str = "user") -> bool:
        """Resuelve una confirmación pendiente desde cualquier hilo (QtMainThread, Discord, APK)."""
        with self._lock:
            pending = self._pending.pop(confirmation_id, None)
        if pending is None:
            return False

        def _set_result() -> None:
            if not pending.future.done():
                pending.future.set_result(bool(approved))

        if not pending.loop.is_closed():
            pending.loop.call_soon_threadsafe(_set_result)

        self._events.publish(
            SafetyConfirmationResolvedEvent(
                confirmation_id=confirmation_id,
                approved=bool(approved),
                reason=reason,
            )
        )
        return True

    def cancel_all(self) -> None:
        with self._lock:
            ids = list(self._pending.keys())
        for cid in ids:
            self.resolve(cid, approved=False, reason="shutdown")


class FastPathEngine:
    """Motor de reflejos locales (<50 ms, 0 tokens) con normalización en español."""

    def __init__(
        self,
        settings: DeusExSettings,
        safety_gate: SafetyGate,
        events: EventBus,
        db: Database,
        tools: ToolExecutor | None = None,
    ) -> None:
        self._settings = settings
        self._safety_gate = safety_gate
        self._events = events
        self._db = db
        self._tools = tools
        self._lock = threading.RLock()
        self._rules: list[FastPathRule] = []

    def set_tools(self, tools: ToolExecutor) -> None:
        with self._lock:
            self._tools = tools

    def register_rules(self, rules: list[FastPathRule]) -> None:
        with self._lock:
            existing_ids = {r.rule_id for r in rules}
            self._rules = [r for r in self._rules if r.rule_id not in existing_ids]
            self._rules.extend(rules)
            self._rules.sort(key=lambda r: (r.priority, r.rule_id))

    def unregister_module_rules(self, module_id: str) -> None:
        with self._lock:
            self._rules = [r for r in self._rules if r.module_id != module_id]

    def list_rules(self) -> list[FastPathRule]:
        with self._lock:
            return list(self._rules)

    def find_match(self, raw_text: str) -> tuple[FastPathRule, re.Match[str]] | None:
        """Encuentra la primera regla Fast-Path que coincide con el texto normalizado y su Match."""
        normalized = normalize_command_text(raw_text)
        if not normalized:
            return None
        with self._lock:
            snapshot = list(self._rules)
        for rule in snapshot:
            match = rule.pattern.search(normalized)
            if match is not None:
                return rule, match
        return None

    def has_match(self, raw_text: str) -> bool:
        """Determina de forma síncrona si el texto coincide con alguna regla Fast-Path registrada."""
        return self.find_match(raw_text) is not None

    async def try_execute(self, raw_text: str) -> ToolResult | None:
        normalized = normalize_command_text(raw_text)
        if not normalized:
            return None

        with self._lock:
            snapshot = list(self._rules)

        for rule in snapshot:
            match = rule.pattern.search(normalized)
            if match is None:
                continue

            started = time.perf_counter()

            if rule.blocked_in_privacy and self._settings.general.privacy_mode:
                return ToolResult(
                    ok=False,
                    summary="Acción bloqueada mientras el Modo Privacidad está activo.",
                    error_code="privacy_blocked",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

            if rule.permission is not None and not self._settings.is_permission_enabled(
                rule.permission
            ):
                return ToolResult(
                    ok=False,
                    summary=f"Permiso desactivado ({rule.permission.value}) para esta acción.",
                    error_code="permission_denied",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

            if self._settings.developer.dry_run:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                dry_res = ToolResult(
                    ok=True,
                    summary=f"[Simulación Dry-Run] Fast-Path '{rule.rule_id}' habría ejecutado: {raw_text}",
                    dry_run=True,
                    elapsed_ms=elapsed_ms,
                )
                self._events.publish(
                    FastPathExecutedEvent(
                        rule_id=rule.rule_id,
                        module_id=rule.module_id,
                        summary=dry_res.summary,
                        elapsed_ms=elapsed_ms,
                        earcon=rule.earcon,
                    )
                )
                return dry_res

            if (
                rule.risk_level == RiskLevel.CRITICAL
                and not self._safety_gate.can_bypass_trusted_messaging(rule.permission)
            ):
                approved, cid = await self._safety_gate.request_confirmation(
                    tool_name=rule.rule_id,
                    module_id=rule.module_id,
                    summary_es=f"Confirmar acción rápida crítica: {raw_text}",
                    args={"comando": raw_text},
                )
                if not approved:
                    return ToolResult(
                        ok=False,
                        summary="Acción crítica cancelada en SafetyGate.",
                        error_code="safety_rejected",
                        confirmation_id=cid,
                        elapsed_ms=(time.perf_counter() - started) * 1000.0,
                    )

            try:
                result = await rule.handler(match, raw_text)
            except Exception as exc:
                logger.exception("Error ejecutando regla Fast-Path %s", rule.rule_id)
                return ToolResult(
                    ok=False,
                    summary=f"Error al ejecutar comando rápido: {exc}",
                    error_code="fast_path_error",
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

            # Escalación dinámica a ToolExecutor si la regla requiere confirmación física (SafetyGate)
            if (
                result.requires_confirmation
                and isinstance(result.data, dict)
                and isinstance(result.data.get("confirm_tool"), str)
                and self._tools is not None
            ):
                confirm_tool = str(result.data["confirm_tool"])
                confirm_args = (
                    dict(result.data["confirm_args"])
                    if isinstance(result.data.get("confirm_args"), dict)
                    else {}
                )
                return await self._tools.execute(confirm_tool, confirm_args)

            elapsed_ms = (time.perf_counter() - started) * 1000.0
            enriched = ToolResult(
                ok=result.ok,
                summary=result.summary,
                data=result.data,
                error_code=result.error_code,
                spoken_text=result.spoken_text,
                sources=result.sources,
                audit_id=result.audit_id,
                dry_run=result.dry_run,
                elapsed_ms=elapsed_ms,
                requires_confirmation=result.requires_confirmation,
                confirmation_id=result.confirmation_id,
            )

            if rule.risk_level != RiskLevel.SAFE:
                _record_audit(
                    db=self._db,
                    tool_name=rule.rule_id,
                    module_id=rule.module_id,
                    risk_level=rule.risk_level,
                    status="ok" if enriched.ok else "error",
                    args={"raw_text": raw_text},
                    undo_plan=enriched.data.get("undo_plan"),
                )

            self._events.publish(
                FastPathExecutedEvent(
                    rule_id=rule.rule_id,
                    module_id=rule.module_id,
                    summary=enriched.summary,
                    elapsed_ms=elapsed_ms,
                    earcon=rule.earcon if enriched.ok else "error",
                )
            )
            return enriched

        return None


def _record_audit(
    *,
    db: Database,
    tool_name: str,
    module_id: str,
    risk_level: RiskLevel,
    status: str,
    args: dict[str, Any],
    undo_plan: Any = None,
) -> str:
    audit_id = uuid.uuid4().hex
    undo_json = json.dumps(undo_plan, ensure_ascii=False) if undo_plan is not None else None
    db.execute(
        """
        INSERT INTO action_audit
            (id, tool_name, module_id, risk_level, status, args_json, undo_plan_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            audit_id,
            tool_name,
            module_id,
            risk_level.value,
            status,
            json.dumps(args, ensure_ascii=False, default=str),
            undo_json,
            datetime.now(UTC).isoformat(),
        ),
    )
    return audit_id


class ToolExecutor:
    """Ejecutor central de herramientas con validación tipada de permisos, Dry-Run y SafetyGate."""

    def __init__(
        self,
        settings: DeusExSettings,
        safety_gate: SafetyGate,
        db: Database,
    ) -> None:
        self._settings = settings
        self._safety_gate = safety_gate
        self._db = db
        self._lock = threading.RLock()
        self._tools: dict[str, ToolSpec] = {}

    def register_tools(self, tools: list[ToolSpec]) -> None:
        with self._lock:
            for spec in tools:
                self._tools[spec.name] = spec

    def unregister_module_tools(self, module_id: str) -> None:
        with self._lock:
            to_remove = [name for name, spec in self._tools.items() if spec.module_id == module_id]
            for name in to_remove:
                del self._tools[name]

    def get_tool(self, name: str) -> ToolSpec | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        with self._lock:
            return list(self._tools.values())

    async def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        started = time.perf_counter()
        payload = dict(args or {})
        spec = self.get_tool(name)
        if spec is None:
            return ToolResult(
                ok=False,
                summary=f"La herramienta '{name}' no está disponible o su módulo está apagado.",
                error_code="tool_not_found",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        if spec.blocked_in_privacy and self._settings.general.privacy_mode:
            return ToolResult(
                ok=False,
                summary="Herramienta bloqueada mientras el Modo Privacidad está activo.",
                error_code="privacy_blocked",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        if spec.permission is not None and not self._settings.is_permission_enabled(spec.permission):
            return ToolResult(
                ok=False,
                summary=f"Permiso desactivado ({spec.permission.value}) para '{spec.name}'.",
                error_code="permission_denied",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        if self._settings.developer.dry_run and spec.supports_dry_run:
            return ToolResult(
                ok=True,
                summary=f"[Simulación Dry-Run] Se ejecutaría '{spec.name}' con {_format_args_preview(payload)}.",
                data={"simulated_tool": spec.name, "args": payload},
                dry_run=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        confirmation_id: str | None = None
        audit_id: str | None = None
        if (
            spec.risk_level == RiskLevel.CRITICAL
            and not self._safety_gate.can_bypass_trusted_messaging(spec.permission)
        ):
            approved, confirmation_id = await self._safety_gate.request_confirmation(
                tool_name=spec.name,
                module_id=spec.module_id,
                summary_es=spec.description,
                args=payload,
            )
            if not approved:
                audit_id = _record_audit(
                    db=self._db,
                    tool_name=spec.name,
                    module_id=spec.module_id,
                    risk_level=spec.risk_level,
                    status="rejected",
                    args=payload,
                )
                return ToolResult(
                    ok=False,
                    summary="Acción cancelada en SafetyGate.",
                    error_code="safety_rejected",
                    audit_id=audit_id,
                    confirmation_id=confirmation_id,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

        try:
            result = await spec.handler(payload)
        except Exception as exc:
            logger.exception("Error ejecutando herramienta %s", spec.name)
            audit_id = _record_audit(
                db=self._db,
                tool_name=spec.name,
                module_id=spec.module_id,
                risk_level=spec.risk_level,
                status="error",
                args=payload,
            )
            return ToolResult(
                ok=False,
                summary=f"Error al ejecutar '{spec.name}': {exc}",
                error_code="tool_execution_error",
                audit_id=audit_id,
                confirmation_id=confirmation_id,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )

        audit_id = result.audit_id
        undo_plan = result.data.get("undo_plan")
        if spec.risk_level != RiskLevel.SAFE or undo_plan is not None:
            audit_id = _record_audit(
                db=self._db,
                tool_name=spec.name,
                module_id=spec.module_id,
                risk_level=spec.risk_level,
                status="ok" if result.ok else "error",
                args=payload,
                undo_plan=undo_plan,
            )

        return ToolResult(
            ok=result.ok,
            summary=result.summary,
            data=result.data,
            error_code=result.error_code,
            spoken_text=result.spoken_text,
            sources=result.sources,
            audit_id=audit_id,
            dry_run=result.dry_run,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            requires_confirmation=result.requires_confirmation,
            confirmation_id=confirmation_id or result.confirmation_id,
        )


class ModuleRegistry:
    """Registro de módulos enchufables con reconciliación real de settings.json (Antídoto #2)."""

    def __init__(
        self,
        *,
        ctx: ModuleContext,
        tools: ToolExecutor,
        fast_path: FastPathEngine,
    ) -> None:
        self._ctx = ctx
        self._tools = tools
        self._fast_path = fast_path
        self._lock = threading.RLock()
        self._modules: dict[str, BaseModule] = {}

    def register(self, module: BaseModule) -> None:
        with self._lock:
            self._modules[module.module_id] = module

    def get(self, module_id: str) -> BaseModule | None:
        with self._lock:
            return self._modules.get(module_id)

    def list_modules(self) -> list[BaseModule]:
        with self._lock:
            return list(self._modules.values())

    def _emit_state_change(
        self,
        module_id: str,
        old_state: ModuleState,
        new_state: ModuleState,
        error: str | None = None,
    ) -> None:
        self._ctx.events.publish(
            ModuleStateChangedEvent(
                module_id=module_id,
                old_state=old_state.value,
                new_state=new_state.value,
                error=error,
            )
        )

    async def enable_module(self, module_id: str) -> bool:
        module = self.get(module_id)
        if module is None:
            logger.warning("Intento de habilitar módulo no registrado: %s", module_id)
            return False

        if module.state == ModuleState.ACTIVE:
            return True

        prev_state = module.state
        module.set_state(ModuleState.STARTING)
        self._emit_state_change(module_id, prev_state, ModuleState.STARTING)

        try:
            await module.start(self._ctx)
            self._tools.register_tools(module.get_tools())
            self._fast_path.register_rules(module.get_fast_path_rules())
            final_state = (
                module.state
                if module.state in (ModuleState.ACTIVE, ModuleState.DEGRADED)
                else ModuleState.ACTIVE
            )
            module.set_state(final_state, error=module.last_error)
            self._emit_state_change(module_id, ModuleState.STARTING, final_state, module.last_error)
            logger.info("Módulo '%s' habilitado (%s)", module_id, final_state.value)
            return True
        except Exception as exc:
            logger.exception("Fallo al iniciar módulo '%s'", module_id)
            self._tools.unregister_module_tools(module_id)
            self._fast_path.unregister_module_rules(module_id)
            module.set_state(ModuleState.ERROR, error=str(exc))
            self._emit_state_change(module_id, ModuleState.STARTING, ModuleState.ERROR, str(exc))
            return False

    async def disable_module(self, module_id: str) -> bool:
        module = self.get(module_id)
        if module is None:
            return False

        prev_state = module.state
        if prev_state == ModuleState.DISABLED:
            return True

        module.set_state(ModuleState.STOPPING)
        self._emit_state_change(module_id, prev_state, ModuleState.STOPPING)
        self._tools.unregister_module_tools(module_id)
        self._fast_path.unregister_module_rules(module_id)

        try:
            await module.stop()
        except Exception as exc:
            logger.exception("Error deteniendo módulo '%s'", module_id)
            module.set_state(ModuleState.ERROR, error=str(exc))
            self._emit_state_change(module_id, ModuleState.STOPPING, ModuleState.ERROR, str(exc))
            return False

        module.set_state(ModuleState.DISABLED)
        self._emit_state_change(module_id, ModuleState.STOPPING, ModuleState.DISABLED)
        logger.info("Módulo '%s' deshabilitado limpiamente", module_id)
        return True

    async def reconcile_from_settings(self) -> list[str]:
        """Antídoto #2: reconcilia módulos y Fast-Path contra settings.json en cada arranque."""
        activated: list[str] = []
        for module_id in self._ctx.settings.modules.enabled_module_ids():
            if self.get(module_id) is None:
                logger.info(
                    "Módulo '%s' marcado en settings.json pero aún no instalado en esta versión.",
                    module_id,
                )
                continue
            ok = await self.enable_module(module_id)
            if ok:
                activated.append(module_id)
        return activated

    async def stop_all(self) -> None:
        active_ids = [
            mod.module_id for mod in self.list_modules() if mod.state != ModuleState.DISABLED
        ]
        for module_id in reversed(active_ids):
            await self.disable_module(module_id)


class CoreEngine:
    """Fachada cohesionada del motor central (ModuleRegistry + ToolExecutor + SafetyGate + FastPath)."""

    def __init__(self, ctx: ModuleContext) -> None:
        self.ctx = ctx
        self.safety_gate = SafetyGate(ctx.events, ctx.settings)
        self.tools = ToolExecutor(ctx.settings, self.safety_gate, ctx.db)
        self.fast_path = FastPathEngine(
            ctx.settings, self.safety_gate, ctx.events, ctx.db, tools=self.tools
        )
        self.modules = ModuleRegistry(ctx=ctx, tools=self.tools, fast_path=self.fast_path)

    async def shutdown(self) -> None:
        self.safety_gate.cancel_all()
        await self.modules.stop_all()
