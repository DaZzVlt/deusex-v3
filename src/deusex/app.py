from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from pathlib import Path
from typing import Final

from deusex.brain.catalog import ModelCatalogService, ProviderPool
from deusex.brain.cost_guard import CostGuard
from deusex.brain.memory import MemoryManager
from deusex.brain.orchestrator import BrainOrchestrator, TurnResult
from deusex.brain.personality import ProfileManager
from deusex.brain.providers import LLMProvider, MediaAttachment, ModelInfo, ModelTier
from deusex.core.config import DeusExSettings, SecretsManager, SettingsStore
from deusex.core.contracts import BaseModule, ModuleContext
from deusex.core.db import Database
from deusex.core.engine import CoreEngine
from deusex.core.events import (
    AppStartedEvent,
    AppStoppingEvent,
    EventBus,
    FastPathExecutedEvent,
    MicMuteChangedEvent,
    OrbStateChangedEvent,
    PrivacyModeChangedEvent,
    ProfileChangedEvent,
    ScheduledJobFiredEvent,
    SettingsUpdatedEvent,
    VoiceMuteChangedEvent,
)
from deusex.core.logging_setup import setup_logging, teardown_logging
from deusex.core.paths import AppPaths, resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import RuntimeServices
from deusex.core.scheduler import LocalScheduler
from deusex.core.system_tools import EarconPlayer, GlobalHotkeyManager
from deusex.modules.network.module import NetworkModule
from deusex.modules.pc.module import PCModule
from deusex.modules.voice.module import ProfileVoiceInfo, VoiceCommandReply, VoiceModule
from deusex.modules.voice.stt import AudioInputDevice, transcribe_wav_cascade

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
        return self.runtime.submit_async(
            self.orchestrator.handle_user_input(
                text,
                conversation_id=conversation_id,
                attachments=attachments,
                requested_tier=requested_tier,
            )
        )

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
        turn: TurnResult = await self.orchestrator.handle_user_input(transcript)
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

            from deusex.brain.providers import LLMMessage, LLMRequest, MediaAttachment

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

