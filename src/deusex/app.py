from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path

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
from deusex.modules.pc.module import PCModule

logger = logging.getLogger(__name__)


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
        self.register_module(PCModule())

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

