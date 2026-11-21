from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import TYPE_CHECKING, Any

from deusex.core.permissions import Permission

if TYPE_CHECKING:
    from deusex.core.config import DeusExSettings, SecretsManager
    from deusex.core.db import Database
    from deusex.core.events import EventBus
    from deusex.core.paths import AppPaths
    from deusex.core.runtime import RuntimeServices


class RiskLevel(StrEnum):
    """Nivel de riesgo físico de una herramienta o regla de Fast-Path."""

    SAFE = "safe"
    MODERATE = "moderate"
    CRITICAL = "critical"


class ModuleState(StrEnum):
    """Ciclo de vida oficial de un módulo enchufable."""

    DISABLED = "disabled"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPING = "stopping"


class DiagnosticSeverity(StrEnum):
    """Semáforo del Doctor de sistema."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    code: str
    area: str
    title: str
    severity: DiagnosticSeverity
    message: str
    remediation: str = ""
    auto_fixable: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Único tipo de retorno canónico para herramientas y Fast-Path (cero subclases locales)."""

    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    spoken_text: str | None = None
    sources: tuple[dict[str, str], ...] = ()
    audit_id: str | None = None
    dry_run: bool = False
    elapsed_ms: float = 0.0
    requires_confirmation: bool = False
    confirmation_id: str | None = None


type ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]
type FastPathHandler = Callable[[re.Match[str], str], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    module_id: str
    description: str
    parameters_schema: dict[str, Any]
    permission: Permission | None
    risk_level: RiskLevel
    handler: ToolHandler
    blocked_in_privacy: bool = False
    supports_dry_run: bool = True


@dataclass(frozen=True, slots=True)
class FastPathRule:
    rule_id: str
    module_id: str
    priority: int
    pattern: re.Pattern[str]
    permission: Permission | None
    risk_level: RiskLevel
    handler: FastPathHandler
    blocked_in_privacy: bool = False
    earcon: str | None = "confirm"


@dataclass(frozen=True, slots=True)
class ModuleContext:
    """Dependencias compartidas inyectadas a cada módulo al arrancar."""

    paths: AppPaths
    settings: DeusExSettings
    secrets: SecretsManager
    db: Database
    events: EventBus
    runtime: RuntimeServices


class BaseModule(ABC):
    """Contrato base de todo módulo enchufable (cero imports cruzados entre módulos)."""

    module_id: str
    display_name: str
    description: str
    required_permissions: tuple[Permission, ...] = ()

    def __init__(self) -> None:
        self._state: ModuleState = ModuleState.DISABLED
        self._last_error: str | None = None

    @property
    def state(self) -> ModuleState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_state(self, state: ModuleState, error: str | None = None) -> None:
        self._state = state
        self._last_error = error

    @abstractmethod
    async def start(self, ctx: ModuleContext) -> None:
        """Inicializa recursos del módulo dentro de AsyncCoreThread."""

    @abstractmethod
    async def stop(self) -> None:
        """Libera recursos del módulo de forma idempotente y rápida."""

    def get_tools(self) -> list[ToolSpec]:
        return []

    def get_fast_path_rules(self) -> list[FastPathRule]:
        return []

    def diagnose(self) -> list[DiagnosticItem]:
        return []
