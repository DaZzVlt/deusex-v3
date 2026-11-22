from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

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
from deusex.core.permissions import Permission
from deusex.modules.pc.apps import (
    AppScanner,
    CloseCategory,
    classify_close_target,
    close_application,
)
from deusex.modules.pc.audio_ctrl import AudioBrightnessController
from deusex.modules.pc.clipboard import ClipboardService
from deusex.modules.pc.files import FileManagerService
from deusex.modules.pc.routines import RoutineManager
from deusex.modules.pc.telemetry import PowerAction, TelemetryService
from deusex.modules.pc.windows import WindowManager

logger = logging.getLogger(__name__)

_RE_OPEN_APP = re.compile(
    r"^(?:abre|abrir|inicia|iniciar|lanza|lanzar|ejecuta|ejecutar)\s+(?!la\s+rutina\b|rutina\b)(.+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_CLOSE_APP = re.compile(
    r"^(?:cierra|cerr[aá]|cerrar|termina|mata)\s+(.+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_VOL_SET = re.compile(
    r"^(?:pon|poner|ajusta|ajustar|fija|fijar|cambia|cambiar|deja|dejar)?\s*"
    r"(?:el\s+)?volumen(?:\s+de\s+(?P<app>[a-zA-Z0-9áéíóúñÁÉÍÓÚÑ ._-]+?))?"
    r"\s+(?:al?|en)\s+(?P<val>\d{1,3})\s*%?\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_VOL_STEP = re.compile(
    r"^(?P<dir>sube|subir|aumenta|baja|bajar|reduce)\s+(?:el\s+)?volumen"
    r"(?:\s+(?:en\s+)?(?P<delta>\d{1,2})\s*%?)?\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_VOL_MUTE = re.compile(
    r"^(?P<act>silencia|silenciar|mutea|mutear|quita\s+el\s+silencio|desmutea|desmutear|activa\s+el\s+sonido)"
    r"(?:\s+(?:el\s+pc|el\s+equipo|el\s+sistema|el\s+audio|el\s+sonido|a\s+(?P<app1>.+)|(?P<app2>.+)))?\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_BRIGHTNESS = re.compile(
    r"^(?:(?P<dir>sube|subir|baja|bajar)\s+(?:el\s+)?brillo(?:\s+(?:en\s+)?(?P<delta>\d{1,2})\s*%?)?|"
    r"(?:pon|poner|ajusta|ajustar|cambia|cambiar)?\s*(?:el\s+)?brillo\s+(?:al?|en)\s+(?P<val>\d{1,3})\s*%?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_MOVE_WINDOW = re.compile(
    r"^(?:mueve|mover|pasa|pasar|env[ií]a|enviar)\s+"
    r"(?:(?:esta|la)\s+ventana|(?P<app>[a-zA-Z0-9áéíóúñÁÉÍÓÚÑ ._-]+?))\s+"
    r"(?:al?\s+otro\s+monitor|a\s+la\s+otra\s+pantalla|al?\s+monitor\s+(?P<mon>\d))\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_WIN_STATE = re.compile(
    r"^(?P<act>maximiza|maximizar|minimiza|minimizar|enfoca|enfocar)\s+"
    r"(?:(?:esta|la)\s+ventana|(?P<app>.+?))\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_LOCK_SCREEN = re.compile(
    r"^(?:bloquea|bloquear)\s+(?:la\s+pantalla|el\s+pc|el\s+equipo|windows|sesi[oó]n)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_EMPTY_TRASH = re.compile(
    r"^(?:vac[ií]a|vaciar|limpia|limpiar)\s+(?:la\s+)?papelera(?:\s+de\s+reciclaje)?\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_POWER = re.compile(
    r"^(?P<act>apaga|apagar|reinicia|reiniciar|suspende|suspender|hiberna|hibernar)\s+"
    r"(?:el\s+pc|el\s+equipo|la\s+computadora|el\s+computador|windows)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_RUN_ROUTINE = re.compile(
    r"^(?:(?:activa|activar|ejecuta|ejecutar|inicia|iniciar|pon)\s+"
    r"(?:la\s+rutina|rutina|el\s+modo|modo)|modo)\s+"
    r"(?!privacidad\b|silencio\b|oscuro\b|claro\b)(?P<name>.+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RE_TELEMETRY = re.compile(
    r"^(?:estado\s+del\s+pc|telemetr[ií]a(?:\s+del\s+pc)?|uso\s+de\s+(?:cpu|ram|gpu)|"
    r"c[oó]mo\s+est[aá]\s+(?:el\s+pc|la\s+cpu|la\s+ram|la\s+gpu|la\s+bater[ií]a))\s*[.!?]*$",
    re.IGNORECASE,
)


class PCModule(BaseModule):
    """Módulo enchufable de Control Local del PC (Rebanada 2)."""

    module_id = "pc"
    display_name = "Control Local del PC"
    description = (
        "Apps nativas (shell:AppsFolder), volumen maestro y por app, brillo, ventanas "
        "multi-monitor, archivos con Deshacer, portapapeles, telemetría y rutinas."
    )
    required_permissions = (
        Permission.PC_BASIC,
        Permission.PC_CLIPBOARD,
        Permission.PC_FILES,
        Permission.PC_ADVANCED,
    )

    def __init__(
        self,
        *,
        app_scanner: AppScanner | None = None,
        window_manager: WindowManager | None = None,
        telemetry_service: TelemetryService | None = None,
    ) -> None:
        super().__init__()
        self._ctx: ModuleContext | None = None
        self._apps = app_scanner or AppScanner()
        self._windows = window_manager or WindowManager()
        self._telemetry = telemetry_service or TelemetryService()
        self._audio: AudioBrightnessController | None = None
        self._files: FileManagerService | None = None
        self._clipboard: ClipboardService | None = None
        self._routines: RoutineManager | None = None

    @property
    def apps(self) -> AppScanner:
        return self._apps

    @property
    def windows(self) -> WindowManager:
        return self._windows

    @property
    def telemetry(self) -> TelemetryService:
        return self._telemetry

    @property
    def audio(self) -> AudioBrightnessController | None:
        return self._audio

    @property
    def files(self) -> FileManagerService | None:
        return self._files

    @property
    def clipboard(self) -> ClipboardService | None:
        return self._clipboard

    @property
    def routines(self) -> RoutineManager | None:
        return self._routines

    def _is_dry_run(self) -> bool:
        return bool(self._ctx is not None and self._ctx.settings.developer.dry_run)

    async def start(self, ctx: ModuleContext) -> None:
        self.set_state(ModuleState.STARTING)
        self._ctx = ctx
        self._audio = AudioBrightnessController(ctx.runtime.com_sta)
        self._files = FileManagerService(ctx.db)
        self._clipboard = ClipboardService(
            is_privacy_active=lambda: ctx.settings.general.privacy_mode
        )
        self._routines = RoutineManager(ctx.db, self._apps, self._audio, self._windows)

        # Calienta el índice shell:AppsFolder sin bloquear AsyncCoreThread
        if not self._apps.is_scanned:
            await asyncio.to_thread(self._apps.scan)

        self.set_state(ModuleState.ACTIVE)
        logger.info("PCModule activo (%d apps indexadas).", self._apps.entry_count)

    async def stop(self) -> None:
        self.set_state(ModuleState.STOPPING)
        self._telemetry.close()
        self._audio = None
        self._files = None
        self._clipboard = None
        self._routines = None
        self._ctx = None
        self.set_state(ModuleState.DISABLED)

    def diagnose(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = []
        app_count = self._apps.entry_count
        items.append(
            DiagnosticItem(
                code="pc.apps_index",
                area="pc",
                title="Índice de aplicaciones (shell:AppsFolder)",
                severity=DiagnosticSeverity.OK if app_count > 0 else DiagnosticSeverity.WARNING,
                message=f"{app_count} aplicaciones indexadas." if app_count > 0 else "Índice vacío.",
                remediation="" if app_count > 0 else "Activa el módulo PC para escanear aplicaciones.",
            )
        )
        monitors = self._windows.list_monitors()
        items.append(
            DiagnosticItem(
                code="pc.monitors",
                area="pc",
                title="Monitores DPI-aware",
                severity=DiagnosticSeverity.OK if monitors else DiagnosticSeverity.WARNING,
                message=f"{len(monitors)} monitor(es) detectado(s).",
            )
        )
        if self._audio is not None:
            bright = self._audio.get_brightness()
            items.append(
                DiagnosticItem(
                    code="pc.brightness",
                    area="pc",
                    title="Control de brillo",
                    severity=DiagnosticSeverity.OK if bright.supported else DiagnosticSeverity.WARNING,
                    message=bright.summary,
                    remediation=(
                        ""
                        if bright.supported
                        else "Activa DDC/CI en el menú OSD de tu monitor externo si deseas brillo por software."
                    ),
                )
            )
        return items

    # ------------------------------------------------------------------
    # Herramientas (LLM + CoreEngine)
    # ------------------------------------------------------------------
    def get_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="pc_open_app",
                module_id=self.module_id,
                description="Abre una aplicación instalada en Windows (UWP, .exe o app predeterminada como navegador/correo).",
                parameters_schema={
                    "type": "object",
                    "properties": {"app_name": {"type": "string", "description": "Nombre o alias de la app."}},
                    "required": ["app_name"],
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_open_app,
            ),
            ToolSpec(
                name="pc_close_app",
                module_id=self.module_id,
                description="Cierra una aplicación cotidiana o juego en ejecución (no cierra procesos del SO ni editores productivos).",
                parameters_schema={
                    "type": "object",
                    "properties": {"app_name": {"type": "string"}},
                    "required": ["app_name"],
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_close_app,
            ),
            ToolSpec(
                name="pc_close_productivity_app",
                module_id=self.module_id,
                description="Cierra una aplicación productiva o editor (VS Code, Word, Bloc de notas, OBS, etc.) previa confirmación.",
                parameters_schema={
                    "type": "object",
                    "properties": {"app_name": {"type": "string"}},
                    "required": ["app_name"],
                },
                permission=Permission.PC_ADVANCED,
                risk_level=RiskLevel.CRITICAL,
                handler=self._tool_close_productivity_app,
            ),
            ToolSpec(
                name="pc_set_volume",
                module_id=self.module_id,
                description="Ajusta el volumen maestro o de una app específica (nivel 0-100, delta relativo o silencio).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "level_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                        "delta_pct": {"type": "integer", "minimum": -100, "maximum": 100},
                        "muted": {"type": "boolean"},
                        "app_name": {"type": "string"},
                    },
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_set_volume,
            ),
            ToolSpec(
                name="pc_set_brightness",
                module_id=self.module_id,
                description="Lee o ajusta el brillo de pantalla (WMI en laptops y DDC/CI en monitores externos).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "level_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                        "delta_pct": {"type": "integer", "minimum": -100, "maximum": 100},
                    },
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_set_brightness,
            ),
            ToolSpec(
                name="pc_window_control",
                module_id=self.module_id,
                description="Controla ventanas multi-monitor: list, focus, minimize, maximize o move_monitor.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "focus", "minimize", "maximize", "move_monitor"],
                        },
                        "app_name": {"type": "string"},
                        "monitor_index": {"type": "integer", "minimum": 0},
                    },
                    "required": ["action"],
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_window_control,
            ),
            ToolSpec(
                name="pc_search_files",
                module_id=self.module_id,
                description="Busca archivos en Descargas, Documentos, Escritorio o una carpeta específica.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "folder": {"type": "string"},
                        "extension": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_search_files,
            ),
            ToolSpec(
                name="pc_organize_folder",
                module_id=self.module_id,
                description="Organiza archivos sueltos de una carpeta por tipo y guarda plan de deshacer en 1 clic.",
                parameters_schema={
                    "type": "object",
                    "properties": {"folder": {"type": "string"}},
                    "required": ["folder"],
                },
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_organize_folder,
            ),
            ToolSpec(
                name="pc_undo_file_action",
                module_id=self.module_id,
                description="Deshace de forma idempotente la última organización de archivos o el audit_id indicado.",
                parameters_schema={
                    "type": "object",
                    "properties": {"audit_id": {"type": "string"}},
                },
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_undo_file_action,
            ),
            ToolSpec(
                name="pc_send_to_trash",
                module_id=self.module_id,
                description="Envía un archivo o carpeta no protegida a la papelera de reciclaje.",
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_send_to_trash,
            ),
            ToolSpec(
                name="pc_empty_recycle_bin",
                module_id=self.module_id,
                description="Vacía permanentemente la papelera de reciclaje de Windows (requiere confirmación SafetyGate).",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.CRITICAL,
                handler=self._tool_empty_recycle_bin,
            ),
            ToolSpec(
                name="pc_clipboard_read",
                module_id=self.module_id,
                description="Lee e inspecciona el contenido de texto del portapapeles (bloqueado en Modo Privacidad).",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_CLIPBOARD,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_clipboard_read,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="pc_clipboard_write",
                module_id=self.module_id,
                description="Copia texto en el portapapeles de Windows (bloqueado en Modo Privacidad).",
                parameters_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                permission=Permission.PC_CLIPBOARD,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_clipboard_write,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="pc_system_telemetry",
                module_id=self.module_id,
                description="Devuelve telemetría local sin UAC: CPU, RAM, disco, batería y GPU NVIDIA.",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_telemetry,
            ),
            ToolSpec(
                name="pc_lock_screen",
                module_id=self.module_id,
                description="Bloquea la pantalla de Windows inmediatamente (equivalente a Win+L).",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_lock_screen,
            ),
            ToolSpec(
                name="pc_power_action",
                module_id=self.module_id,
                description="Apaga, reinicia, suspende o hiberna el equipo (requiere confirmación SafetyGate).",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["shutdown", "restart", "sleep", "hibernate"],
                        },
                        "delay_sec": {"type": "integer", "minimum": 0, "maximum": 300},
                    },
                    "required": ["action"],
                },
                permission=Permission.PC_ADVANCED,
                risk_level=RiskLevel.CRITICAL,
                handler=self._tool_power_action,
            ),
            ToolSpec(
                name="pc_routine_run",
                module_id=self.module_id,
                description="Ejecuta una rutina o modo guardado por nombre o ID.",
                parameters_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_routine_run,
            ),
            ToolSpec(
                name="pc_routine_capture",
                module_id=self.module_id,
                description="Captura ventanas, monitores y volúmenes actuales como una nueva rutina.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "trigger_phrase": {"type": "string"},
                    },
                    "required": ["name"],
                },
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_routine_capture,
            ),
        ]

    # ------------------------------------------------------------------
    # Reglas Fast-Path (<50 ms, 0 tokens)
    # ------------------------------------------------------------------
    def get_fast_path_rules(self) -> list[FastPathRule]:
        return [
            FastPathRule(
                rule_id="pc_fp_lock_screen",
                module_id=self.module_id,
                priority=20,
                pattern=_RE_LOCK_SCREEN,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_lock_screen,
            ),
            FastPathRule(
                rule_id="pc_fp_empty_trash",
                module_id=self.module_id,
                priority=21,
                pattern=_RE_EMPTY_TRASH,
                permission=Permission.PC_FILES,
                risk_level=RiskLevel.CRITICAL,
                handler=self._fp_empty_trash,
            ),
            FastPathRule(
                rule_id="pc_fp_power",
                module_id=self.module_id,
                priority=22,
                pattern=_RE_POWER,
                permission=Permission.PC_ADVANCED,
                risk_level=RiskLevel.CRITICAL,
                handler=self._fp_power,
            ),
            FastPathRule(
                rule_id="pc_fp_volume_set",
                module_id=self.module_id,
                priority=25,
                pattern=_RE_VOL_SET,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_volume_set,
            ),
            FastPathRule(
                rule_id="pc_fp_volume_step",
                module_id=self.module_id,
                priority=26,
                pattern=_RE_VOL_STEP,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_volume_step,
            ),
            FastPathRule(
                rule_id="pc_fp_volume_mute",
                module_id=self.module_id,
                priority=27,
                pattern=_RE_VOL_MUTE,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_volume_mute,
            ),
            FastPathRule(
                rule_id="pc_fp_brightness",
                module_id=self.module_id,
                priority=28,
                pattern=_RE_BRIGHTNESS,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_brightness,
            ),
            FastPathRule(
                rule_id="pc_fp_move_window",
                module_id=self.module_id,
                priority=29,
                pattern=_RE_MOVE_WINDOW,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_move_window,
            ),
            FastPathRule(
                rule_id="pc_fp_window_state",
                module_id=self.module_id,
                priority=30,
                pattern=_RE_WIN_STATE,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_window_state,
            ),
            FastPathRule(
                rule_id="pc_fp_close_app",
                module_id=self.module_id,
                priority=32,
                pattern=_RE_CLOSE_APP,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.MODERATE,
                handler=self._fp_close_app,
            ),
            FastPathRule(
                rule_id="pc_fp_run_routine",
                module_id=self.module_id,
                priority=33,
                pattern=_RE_RUN_ROUTINE,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.MODERATE,
                handler=self._fp_run_routine,
            ),
            FastPathRule(
                rule_id="pc_fp_telemetry",
                module_id=self.module_id,
                priority=34,
                pattern=_RE_TELEMETRY,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_telemetry,
            ),
            FastPathRule(
                rule_id="pc_fp_open_app",
                module_id=self.module_id,
                priority=35,
                pattern=_RE_OPEN_APP,
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=self._fp_open_app,
            ),
        ]

    # ------------------------------------------------------------------
    # Handlers de herramientas
    # ------------------------------------------------------------------
    async def _tool_open_app(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("app_name") or "").strip()
        if not query:
            return ToolResult(ok=False, summary="Indica qué aplicación deseas abrir.", error_code="BAD_ARGS")
        outcome = await asyncio.to_thread(self._apps.launch_app, query, dry_run=self._is_dry_run())
        return ToolResult(
            ok=outcome.ok,
            summary=outcome.summary,
            spoken_text=outcome.summary,
            error_code=outcome.error_code,
            dry_run=outcome.dry_run,
            data={
                "app": outcome.entry.name if outcome.entry else query,
                "source": outcome.entry.source if outcome.entry else None,
            },
        )

    async def _tool_close_app(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("app_name") or "").strip()
        decision = await asyncio.to_thread(classify_close_target, query, app_scanner=self._apps)
        if decision.category == CloseCategory.REQUIRES_SAFETY_GATE:
            return ToolResult(
                ok=False,
                requires_confirmation=True,
                summary=decision.reason,
                error_code="REQUIRES_PRODUCTIVITY_CONFIRMATION",
                data={
                    "confirm_tool": "pc_close_productivity_app",
                    "confirm_args": {"app_name": query},
                    "processes": list(decision.matched_processes),
                },
            )
        ok, msg, dec = await asyncio.to_thread(
            close_application,
            query,
            allow_productivity=False,
            dry_run=self._is_dry_run(),
            app_scanner=self._apps,
        )
        return ToolResult(
            ok=ok,
            summary=msg,
            spoken_text=msg,
            error_code=None if ok else dec.category.value,
            dry_run=self._is_dry_run(),
            data={"processes": list(dec.matched_processes), "category": dec.category.value},
        )

    async def _tool_close_productivity_app(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("app_name") or "").strip()
        ok, msg, dec = await asyncio.to_thread(
            close_application,
            query,
            allow_productivity=True,
            dry_run=self._is_dry_run(),
            app_scanner=self._apps,
        )
        return ToolResult(
            ok=ok,
            summary=msg,
            spoken_text=msg,
            error_code=None if ok else dec.category.value,
            dry_run=self._is_dry_run(),
            data={"processes": list(dec.matched_processes), "category": dec.category.value},
        )

    async def _tool_set_volume(self, args: dict[str, Any]) -> ToolResult:
        if self._audio is None:
            return ToolResult(ok=False, summary="Control de audio no inicializado.", error_code="NOT_READY")
        dry = self._is_dry_run()
        app_name = str(args.get("app_name") or "").strip()

        if "muted" in args and args["muted"] is not None:
            muted = bool(args["muted"])
            if app_name:
                ok, msg = await self._audio.set_app_mute(app_name, muted, dry_run=dry)
            else:
                ok, msg = await self._audio.set_master_mute(muted, dry_run=dry)
            return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=dry)

        if "level_pct" in args and args["level_pct"] is not None:
            level = int(args["level_pct"])
            if app_name:
                ok, msg, actual = await self._audio.set_app_volume(app_name, level, dry_run=dry)
            else:
                ok, msg, actual = await self._audio.set_master_volume(level, dry_run=dry)
            return ToolResult(ok=ok, summary=msg, spoken_text=msg, data={"volume_pct": actual}, dry_run=dry)

        if "delta_pct" in args and args["delta_pct"] is not None:
            delta = int(args["delta_pct"])
            ok, msg, actual = await self._audio.adjust_master_volume(delta, dry_run=dry)
            return ToolResult(ok=ok, summary=msg, spoken_text=msg, data={"volume_pct": actual}, dry_run=dry)

        cur, is_muted = await self._audio.get_master_volume()
        state = " (silenciado)" if is_muted else ""
        summary = f"El volumen actual es {cur}%{state}."
        return ToolResult(ok=True, summary=summary, spoken_text=summary, data={"volume_pct": cur, "muted": is_muted})

    async def _tool_set_brightness(self, args: dict[str, Any]) -> ToolResult:
        if self._audio is None:
            return ToolResult(ok=False, summary="Control de brillo no inicializado.", error_code="NOT_READY")
        dry = self._is_dry_run()
        if "level_pct" in args and args["level_pct"] is not None:
            res = await asyncio.to_thread(self._audio.set_brightness, int(args["level_pct"]), dry_run=dry)
        elif "delta_pct" in args and args["delta_pct"] is not None:
            res = await asyncio.to_thread(self._audio.adjust_brightness, int(args["delta_pct"]), dry_run=dry)
        else:
            res = await asyncio.to_thread(self._audio.get_brightness)
        return ToolResult(
            ok=res.ok,
            summary=res.summary,
            spoken_text=res.summary,
            error_code=None if res.ok else "BRIGHTNESS_UNSUPPORTED",
            dry_run=res.dry_run,
            data={"level_pct": res.level_pct, "backend": res.backend, "supported": res.supported},
        )

    async def _tool_window_control(self, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "list").strip().lower()
        app_name = str(args.get("app_name") or "").strip() or None
        dry = self._is_dry_run()

        if action == "list":
            wins = await asyncio.to_thread(self._windows.list_windows, include_minimized=True)
            mons = await asyncio.to_thread(self._windows.list_monitors)
            return ToolResult(
                ok=True,
                summary=f"{len(wins)} ventana(s) abierta(s) en {len(mons)} monitor(es).",
                data={
                    "monitors": len(mons),
                    "windows": [
                        {"title": w.title, "process": w.process_name, "monitor": w.monitor_index}
                        for w in wins[:20]
                    ],
                },
            )
        if action == "focus":
            ok, msg = await asyncio.to_thread(self._windows.focus_window, app_name, dry_run=dry)
        elif action == "minimize":
            ok, msg = await asyncio.to_thread(self._windows.minimize_window, app_name, dry_run=dry)
        elif action == "maximize":
            ok, msg = await asyncio.to_thread(self._windows.maximize_window, app_name, dry_run=dry)
        elif action == "move_monitor":
            mon_arg = args.get("monitor_index")
            target_idx = int(mon_arg) if mon_arg is not None else None
            ok, msg = await asyncio.to_thread(
                self._windows.move_window_to_monitor,
                query=app_name,
                target_monitor_index=target_idx,
                dry_run=dry,
            )
        else:
            return ToolResult(ok=False, summary=f"Acción de ventana desconocida: {action}.", error_code="BAD_ARGS")

        return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=dry)

    async def _tool_search_files(self, args: dict[str, Any]) -> ToolResult:
        if self._files is None:
            return ToolResult(ok=False, summary="Servicio de archivos no inicializado.", error_code="NOT_READY")
        query = str(args.get("query") or "")
        folder = str(args.get("folder") or "").strip() or None
        ext = str(args.get("extension") or "").strip() or None
        max_res = int(args.get("max_results") or 20)
        hits = await asyncio.to_thread(
            self._files.search_files,
            query,
            root_hint=folder,
            extension=ext,
            max_results=max_res,
        )
        if not hits:
            return ToolResult(ok=True, summary=f"No encontré archivos que coincidan con '{query}'.", data={"hits": []})
        preview = ", ".join(h.name for h in hits[:5])
        return ToolResult(
            ok=True,
            summary=f"Encontré {len(hits)} archivo(s): {preview}.",
            data={"hits": [{"name": h.name, "path": h.path, "size_bytes": h.size_bytes} for h in hits]},
        )

    async def _tool_organize_folder(self, args: dict[str, Any]) -> ToolResult:
        if self._files is None:
            return ToolResult(ok=False, summary="Servicio de archivos no inicializado.", error_code="NOT_READY")
        folder = str(args.get("folder") or "").strip()
        outcome = await asyncio.to_thread(self._files.organize_folder, folder, dry_run=self._is_dry_run())
        return ToolResult(
            ok=outcome.ok,
            summary=outcome.summary,
            spoken_text=outcome.summary,
            audit_id=outcome.audit_id,
            error_code=outcome.error_code,
            dry_run=outcome.dry_run,
            data={"moved_count": outcome.moved_count, "audit_id": outcome.audit_id},
        )

    async def _tool_undo_file_action(self, args: dict[str, Any]) -> ToolResult:
        if self._files is None:
            return ToolResult(ok=False, summary="Servicio de archivos no inicializado.", error_code="NOT_READY")
        audit_id = str(args.get("audit_id") or "").strip() or None
        outcome = await asyncio.to_thread(self._files.undo_action, audit_id)
        return ToolResult(
            ok=outcome.ok,
            summary=outcome.summary,
            spoken_text=outcome.summary,
            error_code=outcome.error_code,
            data={"restored_count": outcome.restored_count, "already_undone": outcome.already_undone},
        )

    async def _tool_send_to_trash(self, args: dict[str, Any]) -> ToolResult:
        if self._files is None:
            return ToolResult(ok=False, summary="Servicio de archivos no inicializado.", error_code="NOT_READY")
        path_str = str(args.get("path") or "").strip()
        ok, msg = await asyncio.to_thread(self._files.send_to_trash, path_str, dry_run=self._is_dry_run())
        return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=self._is_dry_run())

    async def _tool_empty_recycle_bin(self, _args: dict[str, Any]) -> ToolResult:
        if self._files is None:
            return ToolResult(ok=False, summary="Servicio de archivos no inicializado.", error_code="NOT_READY")
        ok, msg = await asyncio.to_thread(self._files.empty_recycle_bin, dry_run=self._is_dry_run())
        return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=self._is_dry_run())

    async def _tool_clipboard_read(self, _args: dict[str, Any]) -> ToolResult:
        if self._clipboard is None:
            return ToolResult(ok=False, summary="Portapapeles no inicializado.", error_code="NOT_READY")
        try:
            info = await asyncio.to_thread(self._clipboard.inspect)
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="PRIVACY_BLOCKED")
        if not info.has_text:
            return ToolResult(ok=True, summary="El portapapeles está vacío o no contiene texto.", data={"kind": "empty"})
        return ToolResult(
            ok=True,
            summary=f"Portapapeles ({info.kind}, {info.char_count} caracteres): {info.preview}",
            data={"kind": info.kind, "text": info.full_text, "char_count": info.char_count},
        )

    async def _tool_clipboard_write(self, args: dict[str, Any]) -> ToolResult:
        if self._clipboard is None:
            return ToolResult(ok=False, summary="Portapapeles no inicializado.", error_code="NOT_READY")
        text = str(args.get("text") or "")
        try:
            ok, msg = await asyncio.to_thread(self._clipboard.write_text, text, dry_run=self._is_dry_run())
            return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=self._is_dry_run())
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="PRIVACY_BLOCKED")

    async def _tool_telemetry(self, _args: dict[str, Any]) -> ToolResult:
        snap = await asyncio.to_thread(self._telemetry.snapshot)
        summary = self._telemetry.format_summary_es(snap)
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=summary,
            data={
                "cpu_percent": snap.cpu_percent,
                "ram_percent": snap.ram_percent,
                "ram_used_gb": snap.ram_used_gb,
                "ram_total_gb": snap.ram_total_gb,
                "gpu_count": len(snap.gpus),
            },
        )

    async def _tool_lock_screen(self, _args: dict[str, Any]) -> ToolResult:
        ok, msg = await asyncio.to_thread(self._telemetry.lock_workstation, dry_run=self._is_dry_run())
        return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=self._is_dry_run())

    async def _tool_power_action(self, args: dict[str, Any]) -> ToolResult:
        raw_action = str(args.get("action") or "sleep").strip().lower()
        if raw_action not in ("shutdown", "restart", "sleep", "hibernate"):
            return ToolResult(ok=False, summary=f"Acción de energía inválida: {raw_action}.", error_code="BAD_ARGS")
        action: PowerAction = raw_action  # type: ignore[assignment]
        delay_sec = int(args.get("delay_sec") or 5)
        ok, msg = await asyncio.to_thread(
            self._telemetry.execute_power_action,
            action,
            delay_sec=delay_sec,
            dry_run=self._is_dry_run(),
        )
        return ToolResult(ok=ok, summary=msg, spoken_text=msg, dry_run=self._is_dry_run())

    async def _tool_routine_run(self, args: dict[str, Any]) -> ToolResult:
        if self._routines is None:
            return ToolResult(ok=False, summary="Gestor de rutinas no inicializado.", error_code="NOT_READY")
        name = str(args.get("name") or "").strip()
        res = await self._routines.execute_routine(name, dry_run=self._is_dry_run())
        return ToolResult(
            ok=res.ok,
            summary=res.summary,
            spoken_text=res.summary,
            dry_run=res.dry_run,
            data={"routine": res.routine_name, "steps": list(res.step_logs)},
        )

    async def _tool_routine_capture(self, args: dict[str, Any]) -> ToolResult:
        if self._routines is None:
            return ToolResult(ok=False, summary="Gestor de rutinas no inicializado.", error_code="NOT_READY")
        name = str(args.get("name") or "").strip() or "Estado actual"
        trigger = str(args.get("trigger_phrase") or "").strip()
        rec = await self._routines.capture_current_state(name=name, trigger_phrase=trigger, save=True)
        msg = f"Rutina '{rec.name}' capturada con {len(rec.steps)} paso(s)."
        return ToolResult(
            ok=True,
            summary=msg,
            spoken_text=msg,
            data={"routine_id": rec.id, "name": rec.name, "steps_count": len(rec.steps)},
        )

    # ------------------------------------------------------------------
    # Handlers Fast-Path (<50 ms, 0 tokens)
    # ------------------------------------------------------------------
    async def _fp_open_app(self, match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_open_app({"app_name": match.group(1).strip()})

    async def _fp_close_productivity(self, match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_close_productivity_app({"app_name": match.group(1).strip()})

    async def _fp_close_app(self, match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_close_app({"app_name": match.group(1).strip()})

    async def _fp_volume_set(self, match: re.Match[str], _raw: str) -> ToolResult:
        payload: dict[str, Any] = {"level_pct": int(match.group("val"))}
        app_name = match.group("app")
        if app_name and app_name.strip():
            payload["app_name"] = app_name.strip()
        return await self._tool_set_volume(payload)

    async def _fp_volume_step(self, match: re.Match[str], _raw: str) -> ToolResult:
        direction = match.group("dir").lower()
        step = int(match.group("delta") or 10)
        delta = step if direction.startswith(("sub", "aum")) else -step
        return await self._tool_set_volume({"delta_pct": delta})

    async def _fp_volume_mute(self, match: re.Match[str], _raw: str) -> ToolResult:
        act = match.group("act").lower()
        muted = act.startswith(("silen", "mute"))
        app_target = (match.group("app1") or match.group("app2") or "").strip()
        payload: dict[str, Any] = {"muted": muted}
        if app_target and app_target.lower() not in ("el pc", "el equipo", "el sistema", "pc", "todo"):
            payload["app_name"] = app_target
        return await self._tool_set_volume(payload)

    async def _fp_brightness(self, match: re.Match[str], _raw: str) -> ToolResult:
        if match.group("val") is not None:
            return await self._tool_set_brightness({"level_pct": int(match.group("val"))})
        direction = (match.group("dir") or "sube").lower()
        step = int(match.group("delta") or 15)
        delta = step if direction.startswith("sub") else -step
        return await self._tool_set_brightness({"delta_pct": delta})

    async def _fp_move_window(self, match: re.Match[str], _raw: str) -> ToolResult:
        payload: dict[str, Any] = {"action": "move_monitor"}
        app_name = match.group("app")
        if app_name and app_name.strip():
            payload["app_name"] = app_name.strip()
        mon = match.group("mon")
        if mon is not None:
            payload["monitor_index"] = max(0, int(mon) - 1)
        return await self._tool_window_control(payload)

    async def _fp_window_state(self, match: re.Match[str], _raw: str) -> ToolResult:
        act = match.group("act").lower()
        if act.startswith("max"):
            action = "maximize"
        elif act.startswith("min"):
            action = "minimize"
        else:
            action = "focus"
        payload: dict[str, Any] = {"action": action}
        app_name = match.group("app")
        if app_name and app_name.strip():
            payload["app_name"] = app_name.strip()
        return await self._tool_window_control(payload)

    async def _fp_lock_screen(self, _match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_lock_screen({})

    async def _fp_empty_trash(self, _match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_empty_recycle_bin({})

    async def _fp_power(self, match: re.Match[str], _raw: str) -> ToolResult:
        act = match.group("act").lower()
        if act.startswith("apag"):
            action = "shutdown"
        elif act.startswith("rein"):
            action = "restart"
        elif act.startswith("hib"):
            action = "hibernate"
        else:
            action = "sleep"
        return await self._tool_power_action({"action": action})

    async def _fp_run_routine(self, match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_routine_run({"name": match.group("name").strip()})

    async def _fp_telemetry(self, _match: re.Match[str], _raw: str) -> ToolResult:
        return await self._tool_telemetry({})
