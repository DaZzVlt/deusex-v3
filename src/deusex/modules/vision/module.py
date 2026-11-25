from __future__ import annotations

from collections.abc import Awaitable, Callable
import importlib.util
import logging
import re
from typing import Any, Literal

from deusex.brain.providers import MediaAttachment
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
from deusex.core.events import PrivacyModeChangedEvent, ScreenCapturedEvent
from deusex.core.permissions import Permission
from deusex.modules.vision.capture import (
    CapturedFrame,
    MonitorInfo,
    MonitorLister,
    MonitorSelector,
    ScreenCaptureService,
    ScreenGrabber,
    ShareTarget,
    build_share_event,
)
from deusex.modules.vision.live_guide import (
    LiveGuideController,
    LiveGuideSnapshot,
    LiveGuideState,
)

logger = logging.getLogger(__name__)

# Callback inyectado desde app.py: (prompt, attachment, mode) -> respuesta textual multimodal
type ImageAnalyzerCallback = Callable[[str, MediaAttachment, str], Awaitable[str]]

_FP_DESCRIBE_SCREEN_RE = re.compile(
    r"^(?:qu[eé]\s+(?:ves|hay|aparece|tengo)\s+en\s+(?:mi\s+|la\s+)?pantalla|"
    r"(?:analiza|mira|describe|revisa|lee)\s+(?:mi\s+|la\s+)?pantalla(?:\s+(.+))?)$",
    re.IGNORECASE,
)
_FP_CAPTURE_SCREEN_RE = re.compile(
    r"^(?:(?:toma|saca|haz|hacer|tomar)\s+(?:una\s+)?)?"
    r"captura(?:\s+de\s+(?:mi\s+|la\s+)?pantalla)?"
    r"(?:\s+(?:del\s+)?monitor\s+(\d+)|(?:\s+de\s+todos\s+los\s+monitores))?$",
    re.IGNORECASE,
)
_FP_LIVE_GUIDE_START_RE = re.compile(
    r"^(?:inicia|iniciar|activa|activar|empieza|comienza|abre)\s+"
    r"(?:el\s+)?(?:modo\s+)?gu[ií]a\s+live(?:\s+(?:en\s+(?:el\s+)?monitor\s+(\d+)))?$",
    re.IGNORECASE,
)
_FP_LIVE_GUIDE_PAUSE_RE = re.compile(
    r"^(?:pausa|pausar|pon\s+en\s+pausa)\s+(?:el\s+)?(?:modo\s+)?gu[ií]a\s+live$",
    re.IGNORECASE,
)
_FP_LIVE_GUIDE_RESUME_RE = re.compile(
    r"^(?:reanuda|reanudar|contin[uú]a|continuar|sigue|seguir)\s+"
    r"(?:el\s+)?(?:modo\s+)?gu[ií]a\s+live$",
    re.IGNORECASE,
)
_FP_LIVE_GUIDE_STOP_RE = re.compile(
    r"^(?:det[eé]n|detener|para|parar|termina|terminar|apaga|apagar|cierra|cerrar)\s+"
    r"(?:el\s+)?(?:modo\s+)?gu[ií]a\s+live$",
    re.IGNORECASE,
)


def _parse_monitor_selector(raw: Any) -> MonitorSelector:
    if raw is None or raw == "":
        return "active"
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().lower()
    if text in ("active", "activo", "actual"):
        return "active"
    if text in ("all", "todos", "todas"):
        return "all"
    if text in ("primary", "principal", "primario"):
        return "primary"
    if text.isdigit():
        return int(text)
    raise ValueError(f"Selector de monitor no válido: {raw!r}. Usa 'active', 'all' o un índice numérico.")


class VisionModule(BaseModule):
    """
    Módulo de Visión y Guía Live (Rebanada 5):
    - Captura puntual multi-monitor DPI-aware (mss + Pillow).
    - Modo Guía Live por turnos (0 capturas y 0 tokens en pausa/apagado).
    - Contrato `VisionShareRequestedEvent` (`Permission.VISION_SHARE`) para Rebanadas 8-10.
    - Bloqueo estricto en Modo Privacidad.
    """

    module_id = "vision"
    display_name = "Visión y Pantalla"
    description = (
        "Captura multi-monitor DPI-aware, análisis visual multimodal, Modo Guía Live por turnos "
        "(0 consumo en pausa) y envío de capturas."
    )
    required_permissions = (Permission.VISION_CAPTURE, Permission.VISION_SHARE)

    def __init__(
        self,
        *,
        image_analyzer: ImageAnalyzerCallback | None = None,
        monitor_lister: MonitorLister | None = None,
        screen_grabber: ScreenGrabber | None = None,
        focus_point_provider: Callable[[], tuple[int, int] | None] | None = None,
    ) -> None:
        super().__init__()
        self._ctx: ModuleContext | None = None
        self._image_analyzer = image_analyzer
        self._unsub_privacy: Callable[[], None] | None = None
        self.capture_service = ScreenCaptureService(
            monitor_lister=monitor_lister,
            screen_grabber=screen_grabber,
            focus_point_provider=focus_point_provider,
        )
        self.live_guide = LiveGuideController(self.capture_service)

    async def start(self, ctx: ModuleContext) -> None:
        self._ctx = ctx
        self.capture_service.reopen()
        self.live_guide.bind_events(ctx.events)
        self._unsub_privacy = ctx.events.subscribe(
            PrivacyModeChangedEvent,
            self._on_privacy_mode_changed,
        )
        self.set_state(ModuleState.ACTIVE)
        logger.info("VisionModule iniciado (Guía Live en estado off, 0 capturas en reposo).")

    async def stop(self) -> None:
        self.set_state(ModuleState.STOPPING)
        if self._unsub_privacy is not None:
            self._unsub_privacy()
            self._unsub_privacy = None
        self.live_guide.stop()
        self.live_guide.bind_events(None)
        await self.capture_service.close()
        self._ctx = None
        self.set_state(ModuleState.DISABLED)
        logger.info("VisionModule detenido y recursos de captura liberados.")

    def _on_privacy_mode_changed(self, event: PrivacyModeChangedEvent) -> None:
        if event.enabled:
            self.live_guide.handle_privacy_changed(True)
            self.capture_service.clear_last_capture()

    def _is_privacy_mode(self) -> bool:
        return bool(self._ctx is not None and self._ctx.settings.general.privacy_mode)

    def _has_permission(self, permission: Permission) -> bool:
        if self._ctx is None:
            return False
        return self._ctx.settings.is_permission_enabled(permission)

    def _publish_capture_event(self, frame: CapturedFrame) -> None:
        if self._ctx is None:
            return
        self._ctx.events.publish(
            ScreenCapturedEvent(
                capture_id=frame.capture_id,
                monitor_index=frame.monitor_index,
                width=frame.width,
                height=frame.height,
                mime_type=frame.mime_type,
                size_bytes=frame.size_bytes,
                source=frame.source,
            )
        )

    # ------------------------------------------------------------------
    # Fachada para `app.py` y UI (MiniChat / ChatView / Voice)
    # ------------------------------------------------------------------
    @property
    def live_guide_state(self) -> LiveGuideState:
        return self.live_guide.state

    def live_guide_snapshot(self) -> LiveGuideSnapshot:
        return self.live_guide.snapshot()

    async def capture_quick(
        self,
        *,
        monitor: MonitorSelector = "active",
        source: str = "manual",
    ) -> CapturedFrame:
        if not self._has_permission(Permission.VISION_CAPTURE):
            raise PermissionError("El permiso 'vision_capture' está desactivado.")
        frame = await self.capture_service.capture(
            monitor=monitor,
            privacy_mode=self._is_privacy_mode(),
            source=source,
        )
        self._publish_capture_event(frame)
        return frame

    async def capture_for_live_turn(self) -> MediaAttachment | None:
        """
        Usado por `app.py` al procesar un turno de chat o voz:
        - Si Guía Live está `off` o `paused`: devuelve `None` (0 capturas, 0 tokens).
        - Si está `active` y hay permiso/no privacidad: toma 1 captura puntual para el turno.
        """
        if self.state != ModuleState.ACTIVE or not self.live_guide.is_active:
            return None
        if not self._has_permission(Permission.VISION_CAPTURE):
            return None
        frame = await self.live_guide.capture_for_turn(privacy_mode=self._is_privacy_mode())
        return frame.to_media_attachment() if frame is not None else None

    def start_live_guide(self, *, monitor: MonitorSelector = "active") -> LiveGuideSnapshot:
        if not self._has_permission(Permission.VISION_CAPTURE):
            raise PermissionError("El permiso 'vision_capture' está desactivado.")
        return self.live_guide.start(monitor=monitor, privacy_mode=self._is_privacy_mode())

    def pause_live_guide(self) -> LiveGuideSnapshot:
        return self.live_guide.pause()

    def resume_live_guide(self) -> LiveGuideSnapshot:
        if not self._has_permission(Permission.VISION_CAPTURE):
            raise PermissionError("El permiso 'vision_capture' está desactivado.")
        return self.live_guide.resume(privacy_mode=self._is_privacy_mode())

    def stop_live_guide(self) -> LiveGuideSnapshot:
        return self.live_guide.stop()

    # ------------------------------------------------------------------
    # Handlers de Herramientas
    # ------------------------------------------------------------------
    async def _tool_capture_screen(self, args: dict[str, Any]) -> ToolResult:
        try:
            monitor = _parse_monitor_selector(args.get("monitor", "active"))
            question = str(args.get("question") or "").strip()
            frame = await self.capture_service.capture(
                monitor=monitor,
                privacy_mode=self._is_privacy_mode(),
                source="tool",
            )
            self._publish_capture_event(frame)
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="privacy_mode_blocked")
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_monitor")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo capturar la pantalla: {exc}",
                error_code="capture_failed",
            )

        if question and self._image_analyzer is not None:
            try:
                analysis = await self._image_analyzer(
                    question, frame.to_media_attachment(), "snapshot"
                )
                return ToolResult(
                    ok=True,
                    summary=analysis,
                    spoken_text=analysis,
                    data={
                        "capture_id": frame.capture_id,
                        "monitor_index": frame.monitor_index,
                        "width": frame.width,
                        "height": frame.height,
                        "size_bytes": frame.size_bytes,
                        "analyzed": True,
                    },
                )
            except Exception as exc:
                logger.warning("Fallo análisis multimodal tras captura: %s", exc)

        mon_label = (
            "todos los monitores"
            if frame.monitor_index == 0
            else f"el monitor {frame.monitor_index}"
        )
        summary = (
            f"Captura lista de {mon_label} "
            f"({frame.width}x{frame.height}, {frame.size_bytes // 1024} KB)."
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=f"Capturé {mon_label}.",
            data={
                "capture_id": frame.capture_id,
                "monitor_index": frame.monitor_index,
                "width": frame.width,
                "height": frame.height,
                "mime_type": frame.mime_type,
                "size_bytes": frame.size_bytes,
                "filename": frame.filename,
                "analyzed": False,
            },
        )

    async def _tool_analyze_screen(self, args: dict[str, Any]) -> ToolResult:
        question = str(args.get("question") or "").strip() or (
            "Describe de forma clara y concisa qué hay actualmente en mi pantalla "
            "y destaca lo más relevante."
        )
        try:
            monitor = _parse_monitor_selector(args.get("monitor", "active"))
            frame = await self.capture_service.capture(
                monitor=monitor,
                privacy_mode=self._is_privacy_mode(),
                source="tool",
            )
            self._publish_capture_event(frame)
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="privacy_mode_blocked")
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_monitor")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudo capturar la pantalla: {exc}",
                error_code="capture_failed",
            )

        if self._image_analyzer is None:
            return ToolResult(
                ok=True,
                summary=(
                    f"Captura tomada ({frame.width}x{frame.height}), pero no hay un proveedor "
                    "multimodal configurado para analizar la imagen."
                ),
                spoken_text="Tomé la captura, pero no hay un modelo visual configurado.",
                data={
                    "capture_id": frame.capture_id,
                    "monitor_index": frame.monitor_index,
                    "width": frame.width,
                    "height": frame.height,
                    "analyzed": False,
                },
            )

        try:
            analysis = await self._image_analyzer(question, frame.to_media_attachment(), "snapshot")
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"Capturé la pantalla, pero falló el análisis visual: {exc}",
                error_code="vision_analysis_failed",
            )

        return ToolResult(
            ok=True,
            summary=analysis,
            spoken_text=analysis,
            data={
                "capture_id": frame.capture_id,
                "monitor_index": frame.monitor_index,
                "width": frame.width,
                "height": frame.height,
                "size_bytes": frame.size_bytes,
                "analyzed": True,
            },
        )

    async def _tool_list_monitors(self, _args: dict[str, Any]) -> ToolResult:
        try:
            monitors: list[MonitorInfo] = await self.capture_service.list_monitors()
        except Exception as exc:
            return ToolResult(
                ok=False,
                summary=f"No se pudieron consultar los monitores: {exc}",
                error_code="monitors_failed",
            )

        physical = [m for m in monitors if not m.is_combined]
        lines = [f"Detecté {len(physical)} monitor(es):"]
        for m in physical:
            primary_tag = " (principal)" if m.is_primary else ""
            lines.append(
                f"- Monitor {m.index}{primary_tag}: {m.width}x{m.height} en ({m.left}, {m.top})"
            )
        return ToolResult(
            ok=True,
            summary="\n".join(lines),
            spoken_text=f"Detecté {len(physical)} monitores conectados.",
            data={
                "count": len(physical),
                "monitors": [
                    {
                        "index": m.index,
                        "width": m.width,
                        "height": m.height,
                        "left": m.left,
                        "top": m.top,
                        "is_primary": m.is_primary,
                    }
                    for m in physical
                ],
            },
        )

    async def _tool_live_guide_control(self, args: dict[str, Any]) -> ToolResult:
        action = str(args.get("action") or "status").strip().lower()
        try:
            if action == "start":
                monitor = _parse_monitor_selector(args.get("monitor", "active"))
                snap = self.live_guide.start(
                    monitor=monitor,
                    privacy_mode=self._is_privacy_mode(),
                )
                msg = (
                    "Modo Guía Live activado por turnos. Capturaré tu pantalla solo "
                    "cuando me hagas una pregunta o envíes un mensaje."
                )
                spoken = "Modo Guía Live activado por turnos."
            elif action == "pause":
                snap = self.live_guide.pause()
                msg = "Modo Guía Live en pausa (0 capturas y 0 tokens hasta que lo reanudes)."
                spoken = "Guía Live en pausa."
            elif action == "resume":
                snap = self.live_guide.resume(privacy_mode=self._is_privacy_mode())
                msg = "Modo Guía Live reanudado."
                spoken = msg
            elif action == "stop":
                snap = self.live_guide.stop()
                msg = "Modo Guía Live detenido."
                spoken = msg
            elif action == "status":
                snap = self.live_guide.snapshot()
                msg = (
                    f"Guía Live está en estado '{snap.state.value}' "
                    f"(turnos: {snap.turns_count}, capturas: {snap.captures_count})."
                )
                spoken = f"Guía Live está {snap.state.value}."
            else:
                return ToolResult(
                    ok=False,
                    summary=f"Acción de Guía Live desconocida: '{action}'.",
                    error_code="invalid_live_guide_action",
                )
        except PermissionError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="privacy_mode_blocked")
        except ValueError as exc:
            return ToolResult(ok=False, summary=str(exc), error_code="invalid_monitor")

        return ToolResult(
            ok=True,
            summary=msg,
            spoken_text=spoken,
            data={
                "state": snap.state.value,
                "session_id": snap.session_id,
                "turns_count": snap.turns_count,
                "captures_count": snap.captures_count,
            },
        )

    async def _tool_share_capture(self, args: dict[str, Any]) -> ToolResult:
        raw_target = str(args.get("target") or "").strip().lower()
        if raw_target not in ("discord", "whatsapp", "mobile"):
            return ToolResult(
                ok=False,
                summary="Destino inválido. Indica 'discord', 'whatsapp' o 'mobile'.",
                error_code="invalid_share_target",
            )
        target: Literal["discord", "whatsapp", "mobile"] = (
            "discord"
            if raw_target == "discord"
            else ("whatsapp" if raw_target == "whatsapp" else "mobile")
        )
        recipient = str(args.get("recipient") or "").strip()
        caption = str(args.get("caption") or "").strip()
        capture_new = bool(args.get("capture_new", False))

        if self._is_privacy_mode():
            return ToolResult(
                ok=False,
                summary="El envío de capturas está bloqueado mientras el Modo Privacidad está activo.",
                error_code="privacy_mode_blocked",
            )

        frame = None if capture_new else self.capture_service.last_capture
        if frame is None:
            if not self._has_permission(Permission.VISION_CAPTURE):
                return ToolResult(
                    ok=False,
                    summary="No hay una captura previa y el permiso 'vision_capture' está desactivado.",
                    error_code="permission_denied",
                )
            try:
                monitor = _parse_monitor_selector(args.get("monitor", "active"))
                frame = await self.capture_service.capture(
                    monitor=monitor,
                    privacy_mode=False,
                    source="tool",
                )
                self._publish_capture_event(frame)
            except Exception as exc:
                return ToolResult(
                    ok=False,
                    summary=f"No se pudo tomar una captura para compartir: {exc}",
                    error_code="capture_failed",
                )

        share_target: ShareTarget = target
        event = build_share_event(
            frame,
            target=share_target,
            recipient=recipient,
            caption=caption,
        )
        if self._ctx is not None:
            self._ctx.events.publish(event)

        summary = (
            f"Captura ({frame.width}x{frame.height}) enviada al contrato de entrega '{target}' "
            f"(ID {event.share_id})."
        )
        return ToolResult(
            ok=True,
            summary=summary,
            spoken_text=f"Envié la captura hacia {target}.",
            data={
                "share_id": event.share_id,
                "target": event.target,
                "recipient": event.recipient,
                "filename": event.filename,
                "size_bytes": len(event.image_bytes),
                "event_published": self._ctx is not None,
            },
        )

    # ------------------------------------------------------------------
    # Registro de Herramientas y Fast-Path
    # ------------------------------------------------------------------
    def get_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="vision_capture_screen",
                module_id=self.module_id,
                description=(
                    "Toma una captura multi-monitor DPI-aware ('active', 'all' o índice 1..N). "
                    "Bloqueada en Modo Privacidad."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "monitor": {
                            "type": "string",
                            "description": "'active', 'all', 'primary' o índice numérico.",
                        },
                        "question": {
                            "type": "string",
                            "description": "Pregunta opcional para analizar la captura de inmediato.",
                        },
                    },
                },
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_capture_screen,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="vision_analyze_screen",
                module_id=self.module_id,
                description=(
                    "Captura la pantalla actual y la analiza con el modelo multimodal "
                    "para responder qué se ve o resolver una duda visual."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "monitor": {"type": "string"},
                    },
                },
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_analyze_screen,
                blocked_in_privacy=True,
            ),
            ToolSpec(
                name="vision_list_monitors",
                module_id=self.module_id,
                description="Lista los monitores conectados y sus resoluciones.",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_list_monitors,
                blocked_in_privacy=False,
            ),
            ToolSpec(
                name="vision_live_guide_control",
                module_id=self.module_id,
                description=(
                    "Controla el Modo Guía Live por turnos ('start', 'pause', 'resume', 'stop', 'status'). "
                    "En pausa o apagado no captura ni consume tokens."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start", "pause", "resume", "stop", "status"],
                        },
                        "monitor": {"type": "string"},
                    },
                    "required": ["action"],
                },
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=self._tool_live_guide_control,
                blocked_in_privacy=False,
            ),
            ToolSpec(
                name="vision_share_capture",
                module_id=self.module_id,
                description=(
                    "Emite una solicitud de envío de captura de pantalla hacia Discord, "
                    "WhatsApp o la app móvil Companion."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "enum": ["discord", "whatsapp", "mobile"]},
                        "recipient": {"type": "string"},
                        "caption": {"type": "string"},
                        "capture_new": {"type": "boolean"},
                        "monitor": {"type": "string"},
                    },
                    "required": ["target"],
                },
                permission=Permission.VISION_SHARE,
                risk_level=RiskLevel.MODERATE,
                handler=self._tool_share_capture,
                blocked_in_privacy=True,
            ),
        ]

    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp_describe(match: re.Match[str], _raw: str) -> ToolResult:
            extra = (match.group(1) or "").strip() if match.lastindex else ""
            question = (
                f"Analiza mi pantalla enfocándote en: {extra}"
                if extra
                else "Describe de forma clara y concisa qué ves actualmente en mi pantalla."
            )
            return await self._tool_analyze_screen({"question": question, "monitor": "active"})

        async def _fp_capture(match: re.Match[str], raw: str) -> ToolResult:
            if "todos los monitores" in raw.lower():
                monitor: str = "all"
            elif match.lastindex and match.group(1):
                monitor = match.group(1)
            else:
                monitor = "active"
            return await self._tool_capture_screen({"monitor": monitor})

        async def _fp_lg_start(match: re.Match[str], _raw: str) -> ToolResult:
            monitor = match.group(1) if (match.lastindex and match.group(1)) else "active"
            return await self._tool_live_guide_control({"action": "start", "monitor": monitor})

        async def _fp_lg_pause(_match: re.Match[str], _raw: str) -> ToolResult:
            return await self._tool_live_guide_control({"action": "pause"})

        async def _fp_lg_resume(_match: re.Match[str], _raw: str) -> ToolResult:
            return await self._tool_live_guide_control({"action": "resume"})

        async def _fp_lg_stop(_match: re.Match[str], _raw: str) -> ToolResult:
            return await self._tool_live_guide_control({"action": "stop"})

        return [
            FastPathRule(
                rule_id="vision.fp.describe_screen",
                module_id=self.module_id,
                priority=22,
                pattern=_FP_DESCRIBE_SCREEN_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_describe,
                blocked_in_privacy=True,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="vision.fp.capture_screen",
                module_id=self.module_id,
                priority=23,
                pattern=_FP_CAPTURE_SCREEN_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_capture,
                blocked_in_privacy=True,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="vision.fp.live_guide_start",
                module_id=self.module_id,
                priority=20,
                pattern=_FP_LIVE_GUIDE_START_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_lg_start,
                blocked_in_privacy=True,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="vision.fp.live_guide_pause",
                module_id=self.module_id,
                priority=20,
                pattern=_FP_LIVE_GUIDE_PAUSE_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_lg_pause,
                blocked_in_privacy=False,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="vision.fp.live_guide_resume",
                module_id=self.module_id,
                priority=20,
                pattern=_FP_LIVE_GUIDE_RESUME_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_lg_resume,
                blocked_in_privacy=True,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="vision.fp.live_guide_stop",
                module_id=self.module_id,
                priority=20,
                pattern=_FP_LIVE_GUIDE_STOP_RE,
                permission=Permission.VISION_CAPTURE,
                risk_level=RiskLevel.SAFE,
                handler=_fp_lg_stop,
                blocked_in_privacy=False,
                earcon="confirm",
            ),
        ]

    def diagnose(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = []

        missing = [
            pkg
            for pkg in ("mss", "PIL")
            if importlib.util.find_spec(pkg) is None
        ]
        if missing:
            items.append(
                DiagnosticItem(
                    code="vision.deps.missing",
                    area="vision",
                    title="Dependencias de Visión",
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Faltan paquetes para captura de pantalla: {', '.join(missing)}.",
                    remediation="Ejecuta `uv sync` para instalar mss y Pillow.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="vision.deps.ok",
                    area="vision",
                    title="Dependencias de Visión",
                    severity=DiagnosticSeverity.OK,
                    message="mss y Pillow disponibles para captura multi-monitor DPI-aware.",
                )
            )

        if self._image_analyzer is not None:
            items.append(
                DiagnosticItem(
                    code="vision.analyzer.ok",
                    area="vision",
                    title="Analizador multimodal",
                    severity=DiagnosticSeverity.OK,
                    message="Canal de análisis multimodal conectado al Cerebro IA.",
                )
            )
        else:
            items.append(
                DiagnosticItem(
                    code="vision.analyzer.unset",
                    area="vision",
                    title="Analizador multimodal",
                    severity=DiagnosticSeverity.WARNING,
                    message="Sin callback de análisis multimodal; solo se tomarán capturas locales.",
                    remediation="Configura un proveedor IA compatible con visión (ej. Gemini).",
                )
            )

        snap = self.live_guide.snapshot()
        items.append(
            DiagnosticItem(
                code="vision.live_guide.state",
                area="vision",
                title="Estado de Guía Live",
                severity=DiagnosticSeverity.OK,
                message=(
                    f"Guía Live: {snap.state.value} "
                    f"(turnos={snap.turns_count}, capturas={snap.captures_count})."
                ),
            )
        )
        return items
