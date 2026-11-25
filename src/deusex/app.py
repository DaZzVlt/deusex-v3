from __future__ import annotations

import asyncio
from collections.abc import Callable
import concurrent.futures
import contextlib
import logging
from pathlib import Path
from typing import Final
import webbrowser

from deusex.brain.catalog import ModelCatalogService, ProviderPool
from deusex.brain.cost_guard import CostGuard
from deusex.brain.memory import MemoryManager
from deusex.brain.orchestrator import BrainOrchestrator, TurnResult
from deusex.brain.personality import ProfileManager
from deusex.brain.providers import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    MediaAttachment,
    ModelInfo,
    ModelTier,
    is_vision_model_id,
)
from deusex.core.config import DeusExSettings, SecretsManager, SettingsStore
from deusex.core.contracts import BaseModule, ModuleContext, ModuleState
from deusex.core.db import Database
from deusex.core.engine import CoreEngine
from deusex.core.events import (
    AppStartedEvent,
    AppStoppingEvent,
    EventBus,
    FastPathExecutedEvent,
    MicMuteChangedEvent,
    OAuthCompletedEvent,
    OAuthErrorEvent,
    OrbStateChangedEvent,
    PrivacyModeChangedEvent,
    ProfileChangedEvent,
    ScheduledJobFiredEvent,
    SettingsUpdatedEvent,
    SetupSyncReceivedEvent,
    VoiceMuteChangedEvent,
)
from deusex.core.logging_setup import setup_logging, teardown_logging
from deusex.core.paths import AppPaths, resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import RuntimeServices
from deusex.core.scheduler import LocalScheduler
from deusex.core.system_tools import EarconPlayer, GlobalHotkeyManager
from deusex.modules.intel.module import IntelModule
from deusex.modules.intel.web_search import SearchHit, SearchReport
from deusex.modules.network.module import NetworkModule
from deusex.modules.pc.module import PCModule
from deusex.modules.vision.capture import MonitorSelector
from deusex.modules.vision.live_guide import LiveGuideSnapshot
from deusex.modules.vision.module import VisionModule
from deusex.modules.voice.module import ProfileVoiceInfo, VoiceCommandReply, VoiceModule
from deusex.modules.voice.stt import AudioInputDevice, transcribe_wav_cascade
from deusex.modules.workspace import (
    GoogleAuthService,
    GoogleAuthSessionInfo,
    OAuthCallbackServer,
    SetupSyncPayload,
    load_oauth_defaults,
)

logger = logging.getLogger(__name__)

_OPEN_WILDCARD_RULES: Final[frozenset[str]] = frozenset(
    {
        "pc_fp_open_app",
        "pc_fp_close_app",
        "pc_fp_close_productivity",
        "pc_fp_run_routine",
        "network.send_youtube",
    }
)

_SETUP_SYNC_PROVIDER_MAP: Final[dict[str, str]] = {
    "GEMINI_API_KEY": "gemini",
    "OPENAI_API_KEY": "openai",
    "XAI_API_KEY": "xai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GROQ_API_KEY": "groq",
    "DEEPSEEK_API_KEY": "deepseek",
    "OPENROUTER_API_KEY": "openrouter",
}


class DeusExApplication:
    """
    Composition Root delgado de DeusEx v3.
    - Antídoto #1: `refresh_models(provider_id: str)` delega en `ModelCatalogService.refresh(str)`.
    - Antídoto #2: `start()` reconcilia `ModuleRegistry` y `FastPathEngine` contra `settings.json`.
    """

    def __init__(
        self,
        *,
        user_data_dir: Path | None = None,
        enable_hotkeys: bool = True,
        enable_earcons: bool = True,
        fallback_to_os_env: bool = True,
        providers_override: dict[str, LLMProvider] | None = None,
    ) -> None:
        self.paths: AppPaths = resolve_app_paths(user_data_override=user_data_dir)
        self.paths.ensure_user_dirs()
        self.logging_state = setup_logging(self.paths)

        self.settings_store = SettingsStore(self.paths)
        self.settings: DeusExSettings = self.settings_store.load()
        self.secrets = SecretsManager(self.paths, fallback_to_os_env=fallback_to_os_env)

        self.events = EventBus()
        self.db = Database(self.paths.db_file)
        self.runtime = RuntimeServices(self.events)

        self.module_ctx = ModuleContext(
            paths=self.paths,
            settings=self.settings,
            secrets=self.secrets,
            db=self.db,
            events=self.events,
            runtime=self.runtime,
        )
        self.engine = CoreEngine(self.module_ctx)
        self.scheduler = LocalScheduler(self.db, self.events)
        self.earcons = EarconPlayer(self.paths, muted=not enable_earcons)
        self.hotkeys = GlobalHotkeyManager(self.events)
        self._enable_hotkeys = enable_hotkeys

        self.pool = ProviderPool(self.secrets, providers=providers_override)
        self.catalog = ModelCatalogService(self.pool, self.db)
        self.cost_guard = CostGuard(self.db, self.settings, self.events)
        self.memory = MemoryManager(self.db, self.settings, self.events)
        self.profiles = ProfileManager(self.paths)

        self.orchestrator = BrainOrchestrator(
            settings=self.settings,
            engine=self.engine,
            pool=self.pool,
            catalog=self.catalog,
            cost_guard=self.cost_guard,
            memory=self.memory,
            profiles=self.profiles,
            events=self.events,
            on_set_privacy=self.set_privacy_mode,
            on_set_profile=self.set_active_profile,
            on_set_voice_muted=self.set_voice_muted,
        )

        # Registro de módulos enchufables disponibles (Zero-Base: se activan según settings.json)
        self.pc_module = PCModule()
        self.register_module(self.pc_module)
        self.network_module = NetworkModule()
        self.register_module(self.network_module)
        self.voice_module = VoiceModule(
            on_voice_command=self._handle_voice_command,
            profile_info_provider=self._current_profile_voice_info,
            on_set_voice_muted=self.set_voice_muted,
            on_save_settings=lambda section: self.save_settings(section=section),
            fast_path_matcher=self._has_fast_path_match,
            audio_transcriber=self._transcribe_voice_wav,
            vocabulary_provider=self.get_indexed_app_names,
            on_resolve_safety=lambda cid, apprv, rsn: self.resolve_safety_confirmation(
                cid, approved=apprv, reason=rsn
            ),
        )
        self.register_module(self.voice_module)

        self.intel_module = IntelModule(
            interests_provider=self._get_memory_interests,
            gemini_search_fn=self._search_with_gemini,
        )
        self.register_module(self.intel_module)

        self.vision_module = VisionModule(
            image_analyzer=self._analyze_screen_image,
        )
        self.register_module(self.vision_module)

        # Base OAuth 2.0 PKCE + Setup Sync local (Rebanada 6 — arranque bajo demanda, cierre <0.5 s)
        oauth_defaults = load_oauth_defaults(self.paths, self.secrets)
        self.google_auth = GoogleAuthService(self.paths, self.secrets)
        self.oauth_server = OAuthCallbackServer(
            port=self.settings.developer.oauth_callback_port,
            allowed_origins=oauth_defaults.allowed_cors_origins,
            on_setup_sync=self._apply_setup_sync_payload,
        )

        self._started = False
        self._unsubscribers: list[object] = []

    @property
    def is_started(self) -> bool:
        return self._started

    def register_module(self, module: BaseModule) -> None:
        self.engine.modules.register(module)

    def _wire_core_event_reactions(self) -> None:
        def _on_fast_path(event: FastPathExecutedEvent) -> None:
            if event.earcon:
                self.earcons.play(event.earcon)

        def _on_job_fired(_event: ScheduledJobFiredEvent) -> None:
            self.earcons.play("timer")

        self._unsubscribers.append(self.events.subscribe(FastPathExecutedEvent, _on_fast_path))
        self._unsubscribers.append(self.events.subscribe(ScheduledJobFiredEvent, _on_job_fired))

    def start(self) -> list[str]:
        """Inicia DB WAL, 4 hilos, Fast-Path universal, LocalScheduler y reconcilia módulos."""
        if self._started:
            return self.settings.modules.enabled_module_ids()

        self.db.open()
        self.runtime.start()
        self._wire_core_event_reactions()

        # Registrar reflejos y herramientas siempre activos (Zero-Base)
        self.engine.fast_path.register_rules(self.orchestrator.build_universal_fast_path_rules())
        self.engine.fast_path.register_rules(self.scheduler.get_fast_path_rules())
        self.engine.tools.register_tools(self.scheduler.get_tools())

        async def _async_bootstrap() -> list[str]:
            await self.scheduler.start()
            # Antídoto #2: reconciliar todos los módulos habilitados en settings.json
            return await self.engine.modules.reconcile_from_settings()

        reconciled = self.runtime.async_core.run_sync(_async_bootstrap(), timeout=10.0)

        if self._enable_hotkeys:
            try:
                self.hotkeys.add_hotkey("minichat", self.settings.ui.minichat_hotkey)
                self.hotkeys.add_hotkey("privacy", self.settings.ui.privacy_hotkey)
                self.hotkeys.start()
            except Exception as exc:
                logger.warning("No se pudieron iniciar los atajos globales Win32: %s", exc)

        self._started = True
        self.events.publish(AppStartedEvent(reconciled_modules=tuple(reconciled)))
        logger.info("DeusExApplication iniciada. Módulos reconciliados: %s", reconciled)
        return reconciled

    def shutdown(self) -> float:
        """Apagado determinista <3 s (hotkeys -> módulos/scheduler/pool -> hilos -> WAL checkpoint)."""
        if not self._started:
            self.db.close()
            teardown_logging()
            return 0.0

        self.events.publish(AppStoppingEvent(reason="shutdown"))
        self.hotkeys.stop(timeout=0.5)

        if self.runtime.async_core.is_running:

            async def _async_stop() -> None:
                await self.scheduler.stop()
                await self.engine.shutdown()
                await self.oauth_server.stop(stop_timeout_sec=0.35)
                await self.google_auth.close()
                await self.pool.close_all()

            try:
                self.runtime.async_core.run_sync(_async_stop(), timeout=1.5)
            except Exception as exc:
                logger.warning("Aviso durante detención asíncrona: %s", exc)

        elapsed = self.runtime.shutdown(total_budget_sec=2.2)
        self.db.close()
        self._started = False
        teardown_logging()
        return elapsed

    # ------------------------------------------------------------------
    # Fachada para UI / Hotkeys (thread-safe desde QtMainThread)
    # ------------------------------------------------------------------
    def save_settings(self, *, section: str = "all") -> None:
        self.settings_store.save(self.settings)
        self.events.publish(SettingsUpdatedEvent(section=section))

    def set_privacy_mode(self, enabled: bool) -> None:
        new_val = bool(enabled)
        if self.settings.general.privacy_mode == new_val:
            return
        self.settings.general.privacy_mode = new_val
        self.save_settings(section="general")
        self.earcons.play("privacy_on" if new_val else "privacy_off")
        self.events.publish(PrivacyModeChangedEvent(enabled=new_val))

    def toggle_privacy_mode(self) -> bool:
        target = not self.settings.general.privacy_mode
        self.set_privacy_mode(target)
        return target

    def set_voice_muted(self, muted: bool) -> None:
        new_val = bool(muted)
        self.settings.voice.tts_muted = new_val
        self.save_settings(section="voice")
        self.events.publish(VoiceMuteChangedEvent(muted=new_val))

    def set_mic_muted(self, muted: bool) -> None:
        new_val = bool(muted)
        self.settings.voice.mic_muted = new_val
        self.save_settings(section="voice")
        self.events.publish(MicMuteChangedEvent(muted=new_val))

    def toggle_mic_muted(self) -> bool:
        target = not self.settings.voice.mic_muted
        self.set_mic_muted(target)
        return target

    def set_active_profile(self, profile_id: str, tone: str | None = None) -> None:
        prof = self.profiles.get(profile_id)
        self.settings.ui.active_profile_id = prof.id
        if tone is not None:
            self.settings.ui.active_tone = tone.strip().lower()
        self.save_settings(section="ui")
        self.events.publish(
            ProfileChangedEvent(
                profile_id=prof.id,
                tone=self.settings.ui.active_tone,
                accent_hex=prof.accent_hex,
            )
        )

    def set_permission(self, permission: Permission, enabled: bool) -> None:
        self.settings.set_permission(permission, enabled)
        self.save_settings(section="permissions")

    def set_module_enabled(self, module_id: str, enabled: bool) -> concurrent.futures.Future[bool]:
        self.settings.modules.set_enabled(module_id, enabled)
        self.save_settings(section="modules")

        async def _apply() -> bool:
            if enabled:
                return await self.engine.modules.enable_module(module_id)
            return await self.engine.modules.disable_module(module_id)

        return self.runtime.submit_async(_apply())

    def save_api_key(self, env_key: str, value: str, *, provider_id: str | None = None) -> None:
        clean_val = value.strip()
        if clean_val:
            self.secrets.set(env_key, clean_val)
        else:
            self.secrets.delete(env_key)
        if provider_id:
            self.pool.reset_cooldown(provider_id)
        self.events.publish(SettingsUpdatedEvent(section="secrets"))

    def refresh_models(
        self,
        provider_id: str,
        *,
        force: bool = True,
    ) -> concurrent.futures.Future[list[ModelInfo]]:
        """Antídoto #1: siempre pasa `provider_id: str` a `ModelCatalogService.refresh`."""
        return self.runtime.submit_async(self.catalog.refresh(provider_id, force=force))

    def submit_user_message(
        self,
        text: str,
        *,
        conversation_id: str | None = None,
        attachments: tuple[MediaAttachment, ...] = (),
        requested_tier: ModelTier | None = None,
    ) -> concurrent.futures.Future[TurnResult]:
        async def _run() -> TurnResult:
            effective_attachments = attachments
            if not effective_attachments:
                effective_attachments = await self._maybe_capture_live_guide_attachment(text)
            return await self.orchestrator.handle_user_input(
                text,
                conversation_id=conversation_id,
                attachments=effective_attachments,
                requested_tier=requested_tier,
            )

        return self.runtime.submit_async(_run())

    def resolve_safety_confirmation(
        self,
        confirmation_id: str,
        *,
        approved: bool,
        reason: str = "user",
    ) -> bool:
        return self.engine.safety_gate.resolve(confirmation_id, approved=approved, reason=reason)

    def _current_profile_voice_info(self) -> ProfileVoiceInfo:
        prof = self.profiles.get(self.settings.ui.active_profile_id)
        return ProfileVoiceInfo(
            profile_id=prof.id,
            wake_words=prof.wake_words,
            default_voice=prof.default_voice,
            accent_hex=prof.accent_hex,
        )

    async def _handle_voice_command(
        self,
        transcript: str,
        _is_follow_up: bool,
    ) -> VoiceCommandReply | None:
        attachments = await self._maybe_capture_live_guide_attachment(transcript)
        turn: TurnResult = await self.orchestrator.handle_user_input(
            transcript,
            attachments=attachments,
        )
        reply_text = turn.reply_text.strip()
        spoken_text = turn.spoken_text.strip() or None
        if not reply_text and spoken_text is None:
            return None
        return VoiceCommandReply(
            text=reply_text,
            spoken_text=spoken_text,
            should_speak=not self.settings.voice.tts_muted,
        )

    def trigger_orb_preview(self, *, duration_sec: float = 4.0) -> None:
        """Dispara la vista previa fija de 4 s del Orbe (bloqueada en Modo Privacidad)."""
        if self.settings.general.privacy_mode:
            return
        prof = self.profiles.get(self.settings.ui.active_profile_id)
        self.events.publish(
            OrbStateChangedEvent(
                state="preview",
                visible=True,
                accent_hex=prof.accent_hex,
                preview_sec=max(1.0, float(duration_sec)),
            )
        )

    def list_microphones(self) -> list[AudioInputDevice]:
        return self.voice_module.list_microphones()

    def set_input_device(self, device_index: int | None) -> None:
        self.settings.voice.input_device_index = device_index
        self.save_settings(section="voice")
        if self.voice_module.stt is not None:
            self.voice_module.stt.request_device_change(device_index)

    def start_voiceprint_calibration(self, *, duration_sec: float = 6.0) -> bool:
        return self.voice_module.start_voiceprint_calibration(duration_sec=duration_sec)

    def cancel_voiceprint_calibration(self) -> None:
        self.voice_module.cancel_voiceprint_calibration()

    def _has_fast_path_match(self, text: str) -> bool:
        if self.engine is None or self.engine.fast_path is None:
            return False
        match_info = self.engine.fast_path.find_match(text)
        if match_info is None:
            return False

        rule, match = match_info
        if rule.rule_id not in _OPEN_WILDCARD_RULES:
            return True

        if rule.rule_id in ("pc_fp_open_app", "pc_fp_close_app", "pc_fp_close_productivity"):
            app_query = (match.group(1) or "").strip() if match.lastindex and match.lastindex >= 1 else ""
            if app_query and hasattr(self, "pc_module") and self.pc_module.apps is not None:
                if not self.pc_module.apps.is_scanned:
                    with contextlib.suppress(Exception):
                        self.pc_module.apps.scan()
                found = self.pc_module.apps.find_app(app_query)
                if found is not None:
                    return True
        return False

    async def _transcribe_with_gemini_audio(
        self,
        wav_bytes: bytes,
        extra_vocabulary: tuple[str, ...] = (),
    ) -> str | None:
        try:
            provider = self.pool.get_provider("gemini")
            if not provider.is_configured():
                return None
            if self.pool.is_in_cooldown("gemini"):
                return None

            selection = self.catalog.select_models("gemini")
            fast_model = selection.fast_model

            apps_hint = (
                ", ".join(extra_vocabulary[:60])
                if extra_vocabulary
                else "YouTube, Spotify, Hearthstone, Visual Studio Code, Discord, Xbox, Chromecast"
            )
            prompt = (
                "Transcribe de forma literal y exacta el comando de voz en español chileno grabado en este audio. "
                f"Presta especial atención a nombres propios de aplicaciones y juegos en inglés como: {apps_hint}. "
                "Devuelve ÚNICAMENTE el texto reconocido, sin explicaciones, sin comillas y sin puntuación extra."
            )
            req = LLMRequest(
                messages=(
                    LLMMessage(
                        role="user",
                        content=prompt,
                        attachments=(MediaAttachment(mime_type="audio/wav", data=wav_bytes),),
                    ),
                ),
                model=fast_model,
                temperature=0.0,
                max_output_tokens=128,
            )
            resp = await asyncio.wait_for(self.pool.generate("gemini", req), timeout=5.5)
            text = resp.text.strip()
            return text or None
        except Exception as exc:
            logger.debug("Error en Gemini Audio STT: %s", exc)
            return None

    def get_indexed_app_names(self) -> tuple[str, ...]:
        if hasattr(self, "pc_module") and self.pc_module.apps is not None:
            if not self.pc_module.apps.is_scanned:
                with contextlib.suppress(Exception):
                    self.pc_module.apps.scan()
            return self.pc_module.apps.app_names
        return ()

    async def _transcribe_voice_wav(self, wav_bytes: bytes) -> tuple[str, str] | None:
        wake_matcher = (
            self.voice_module.stt.matcher
            if (self.voice_module and self.voice_module.stt is not None)
            else None
        )
        return await transcribe_wav_cascade(
            wav_bytes,
            secrets=self.secrets,
            gemini_transcriber=self._transcribe_with_gemini_audio,
            extra_vocabulary=self.get_indexed_app_names(),
            fast_path_matcher=self._has_fast_path_match,
            wake_matcher=wake_matcher,
        )

    # ------------------------------------------------------------------
    # Fachada Rebanada 5: Inteligencia Web y Visión / Guía Live
    # ------------------------------------------------------------------
    def _get_memory_interests(self) -> list[str]:
        """Lee intereses/preferencias desde MemoryManager (vacío en Modo Privacidad)."""
        if self.settings.general.privacy_mode:
            return []
        try:
            rows = self.memory.list_memories()
        except Exception as exc:
            logger.debug("No se pudieron consultar intereses en memoria: %s", exc)
            return []
        interests = [
            rec.value.strip()
            for rec in rows
            if rec.category in ("interest", "preference") and rec.value.strip()
        ]
        return interests[:5]

    async def _search_with_gemini(self, query: str) -> SearchReport | None:
        """Búsqueda web opcional vía Gemini Grounding solo si hay clave activa y sin cooldown."""
        provider = self.pool.get_provider("gemini")
        if not provider.is_configured() or self.pool.is_in_cooldown("gemini"):
            return None
        selection = self.catalog.select_models("gemini")
        req = LLMRequest(
            messages=(
                LLMMessage(
                    role="user",
                    content=(
                        f"Busca información actualizada en la web sobre: {query}. "
                        "Responde en español de forma concisa y precisa."
                    ),
                ),
            ),
            model=selection.fast_model,
            temperature=0.2,
            max_output_tokens=700,
            enable_web_search=True,
            timeout_sec=12.0,
            tier=ModelTier.FAST,
        )
        resp = await self.pool.generate("gemini", req)
        self.cost_guard.record_cloud_usage(
            provider_id="gemini",
            model_id=selection.fast_model,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
        )
        hits = tuple(
            SearchHit(title=src.get("title") or src.get("url", ""), url=src.get("url", ""))
            for src in resp.sources
            if src.get("url")
        )
        return SearchReport(
            query=query,
            provider="gemini_search",
            mode="fast",
            hits=hits,
            answer_summary=resp.text.strip(),
        )

    async def _analyze_screen_image(
        self,
        prompt: str,
        attachment: MediaAttachment,
        mode: str,
    ) -> str:
        """Analiza una captura usando el primer proveedor configurado con soporte visual."""
        configured = self.pool.list_configured_provider_ids()
        primary = self.settings.brain.primary_provider.strip().lower()
        ordered: list[str] = []
        if primary in configured:
            ordered.append(primary)
        for pid in configured:
            if pid not in ordered:
                ordered.append(pid)

        vision_candidates = [
            pid
            for pid in ordered
            if self.pool.get_provider(pid).metadata.supports_vision
            and not self.pool.is_in_cooldown(pid)
        ]
        if not vision_candidates:
            raise RuntimeError(
                "No hay un proveedor IA con visión configurado. Agrega tu GEMINI_API_KEY en Ajustes."
            )

        tier = self.cost_guard.resolve_effective_tier(
            ModelTier.FAST if mode == "live_guide" else ModelTier.DEEP
        )
        confirmed_vision: list[str] = []
        fallback_vision: list[str] = []
        for pid in vision_candidates:
            sel = self.catalog.select_models(pid, require_vision=True)
            mid = sel.for_tier(tier)
            if is_vision_model_id(pid, mid):
                confirmed_vision.append(pid)
            else:
                fallback_vision.append(pid)
        vision_providers = confirmed_vision + fallback_vision

        system_prompt = self.profiles.compose_system_prompt(
            profile_id=self.settings.ui.active_profile_id,
            tone=self.settings.ui.active_tone,
            user_name=self.settings.general.user_name,
            city=self.settings.general.city,
            country=self.settings.general.country,
            memory_context="",
            privacy_mode=self.settings.general.privacy_mode,
        )

        last_err: Exception | None = None
        for provider_id in vision_providers:
            selection = self.catalog.select_models(provider_id, require_vision=True)
            model_id = selection.for_tier(tier)
            req = LLMRequest(
                messages=(
                    LLMMessage(
                        role="user",
                        content=prompt,
                        attachments=(attachment,),
                    ),
                ),
                model=model_id,
                system_prompt=system_prompt,
                temperature=0.3,
                max_output_tokens=900,
                timeout_sec=20.0,
                tier=tier,
            )
            try:
                resp = await self.pool.generate(provider_id, req)
            except Exception as exc:
                last_err = exc
                continue

            self.cost_guard.record_cloud_usage(
                provider_id=provider_id,
                model_id=model_id,
                tokens_in=resp.tokens_in,
                tokens_out=resp.tokens_out,
            )
            return resp.text.strip() or "No pude extraer detalles visibles de la captura."

        raise RuntimeError(f"Todos los proveedores visuales fallaron: {last_err}")

    async def _maybe_capture_live_guide_attachment(
        self,
        text: str,
    ) -> tuple[MediaAttachment, ...]:
        if self.vision_module.state != ModuleState.ACTIVE:
            return ()
        if not self.vision_module.live_guide.is_active:
            return ()
        if self.settings.general.privacy_mode:
            self.vision_module.live_guide.handle_privacy_changed(True)
            return ()
        # Comandos Fast-Path (ej. "pausa guía live", "detén guía live", "qué hora es") no capturan
        if self.engine.fast_path.find_match(text) is not None:
            return ()
        attachment = await self.vision_module.capture_for_live_turn()
        return (attachment,) if attachment is not None else ()

    def capture_screen_and_analyze(
        self,
        prompt: str = "",
        *,
        monitor: MonitorSelector = "active",
        conversation_id: str | None = None,
    ) -> concurrent.futures.Future[TurnResult]:
        """Captura puntual iniciada desde UI y enviada como turno multimodal al historial."""

        async def _run() -> TurnResult:
            frame = await self.vision_module.capture_quick(monitor=monitor, source="manual")
            question = prompt.strip() or "📷 Analizar pantalla"
            return await self.orchestrator.handle_user_input(
                question,
                conversation_id=conversation_id,
                attachments=(frame.to_media_attachment(),),
            )

        return self.runtime.submit_async(_run())

    def start_live_guide(self, *, monitor: MonitorSelector = "active") -> LiveGuideSnapshot:
        return self.vision_module.start_live_guide(monitor=monitor)

    def pause_live_guide(self) -> LiveGuideSnapshot:
        return self.vision_module.pause_live_guide()

    def resume_live_guide(self) -> LiveGuideSnapshot:
        return self.vision_module.resume_live_guide()

    def stop_live_guide(self) -> LiveGuideSnapshot:
        return self.vision_module.stop_live_guide()

    def get_live_guide_state(self) -> str:
        return self.vision_module.live_guide_state.value

    # ------------------------------------------------------------------
    # Fachada Rebanada 6: Portal Web, OAuth 2.0 PKCE y Setup Sync Local
    # ------------------------------------------------------------------
    def _apply_setup_sync_payload(
        self,
        payload: SetupSyncPayload,
    ) -> tuple[list[str], list[str]]:
        saved_secrets: list[str] = []
        for env_key, raw_value in payload.secrets.items():
            clean_val = raw_value.strip()
            if not clean_val:
                continue
            provider_id = _SETUP_SYNC_PROVIDER_MAP.get(env_key)
            self.save_api_key(env_key, clean_val, provider_id=provider_id)
            saved_secrets.append(env_key)

        updated_settings: list[str] = []
        for setting_key, raw_value in payload.settings.items():
            clean_val = raw_value.strip()
            if not clean_val:
                continue
            if setting_key == "general.user_name":
                self.settings.general.user_name = clean_val
                updated_settings.append(setting_key)
            elif setting_key == "general.city":
                self.settings.general.city = clean_val
                updated_settings.append(setting_key)
            elif setting_key == "general.country":
                self.settings.general.country = clean_val
                updated_settings.append(setting_key)
            elif setting_key == "general.language":
                self.settings.general.language = clean_val
                updated_settings.append(setting_key)

        if updated_settings:
            self.save_settings(section="general")

        saved_secrets.sort()
        updated_settings.sort()
        msg = (
            f"Setup Sync aplicado: {len(saved_secrets)} clave(s) y "
            f"{len(updated_settings)} ajuste(s) guardados localmente."
        )
        self.events.publish(
            SetupSyncReceivedEvent(
                saved_secrets=tuple(saved_secrets),
                updated_settings=tuple(updated_settings),
                source_origin=payload.source_origin,
                message=msg,
            )
        )
        return saved_secrets, updated_settings

    def get_google_auth_status(self) -> GoogleAuthSessionInfo:
        return self.google_auth.get_session_info()

    def get_portal_base_url(self) -> str:
        return self.google_auth.get_portal_base_url()

    def ensure_oauth_server_started(self) -> concurrent.futures.Future[int]:
        async def _start() -> int:
            if not self.oauth_server.is_running:
                self.oauth_server.set_port(self.settings.developer.oauth_callback_port)
            return await self.oauth_server.start()

        return self.runtime.submit_async(_start())

    def stop_oauth_server(self) -> concurrent.futures.Future[None]:
        return self.runtime.submit_async(self.oauth_server.stop(stop_timeout_sec=0.35))

    def start_google_oauth_flow(
        self,
        *,
        open_browser: Callable[[str], bool] | None = None,
        timeout_sec: float = 180.0,
    ) -> concurrent.futures.Future[GoogleAuthSessionInfo]:
        async def _run() -> GoogleAuthSessionInfo:
            if not self.oauth_server.is_running:
                self.oauth_server.set_port(self.settings.developer.oauth_callback_port)
            try:
                info = await self.google_auth.complete_oauth_flow(
                    self.oauth_server,
                    open_browser=open_browser,
                    timeout_sec=timeout_sec,
                )
            except Exception as exc:
                self.events.publish(
                    OAuthErrorEvent(
                        provider="google",
                        error_code="oauth_failed",
                        message=str(exc),
                    )
                )
                raise

            account_label = info.email or info.name or "cuenta Google"
            self.events.publish(
                OAuthCompletedEvent(
                    provider="google",
                    connected=True,
                    account_email=info.email,
                    account_name=info.name,
                    message=f"Cuenta de Google conectada ({account_label}).",
                )
            )
            return info

        return self.runtime.submit_async(_run())

    def disconnect_google_account(
        self,
        *,
        revoke_remote: bool = False,
    ) -> concurrent.futures.Future[bool]:
        async def _run() -> bool:
            deleted = await self.google_auth.disconnect(revoke_remote=revoke_remote)
            self.events.publish(
                OAuthCompletedEvent(
                    provider="google",
                    connected=False,
                    message="Cuenta de Google desconectada localmente.",
                )
            )
            return deleted

        return self.runtime.submit_async(_run())

    def prepare_setup_sync_session(
        self,
        *,
        page: str = "guides.html",
        ttl_sec: float = 600.0,
    ) -> concurrent.futures.Future[str]:
        async def _run() -> str:
            if not self.oauth_server.is_running:
                self.oauth_server.set_port(self.settings.developer.oauth_callback_port)
            port = await self.oauth_server.start()
            sync_token = self.oauth_server.issue_sync_token(ttl_sec=ttl_sec)
            return self.google_auth.build_setup_sync_url(
                port=port,
                sync_token=sync_token,
                page=page,
            )

        return self.runtime.submit_async(_run())

    def open_setup_sync_portal(
        self,
        *,
        page: str = "guides.html",
        open_browser: Callable[[str], bool] | None = None,
    ) -> concurrent.futures.Future[str]:
        opener = open_browser if open_browser is not None else webbrowser.open

        async def _run() -> str:
            if not self.oauth_server.is_running:
                self.oauth_server.set_port(self.settings.developer.oauth_callback_port)
            port = await self.oauth_server.start()
            sync_token = self.oauth_server.issue_sync_token(ttl_sec=600.0)
            url = self.google_auth.build_setup_sync_url(
                port=port,
                sync_token=sync_token,
                page=page,
            )
            opener(url)
            return url

        return self.runtime.submit_async(_run())

    def open_web_portal(
        self,
        *,
        page: str = "index.html",
        open_browser: Callable[[str], bool] | None = None,
    ) -> str:
        opener = open_browser if open_browser is not None else webbrowser.open
        base = self.get_portal_base_url().rstrip("/")
        clean_page = page.lstrip("/")
        url = f"{base}/{clean_page}" if clean_page else base
        opener(url)
        return url

