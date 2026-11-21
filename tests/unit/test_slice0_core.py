from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any

import pytest

from deusex.core.config import SecretsManager, SettingsStore
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
from deusex.core.db import Database
from deusex.core.events import DomainEvent, EventBus, PrivacyModeChangedEvent
from deusex.core.logging_setup import (
    anonymize_log_text,
    setup_logging,
    teardown_logging,
    translate_error_es,
)
from deusex.core.paths import AppPaths
from deusex.core.permissions import (
    PERMISSION_CATALOG,
    Permission,
    default_permissions_map,
    permissions_for_module,
)
from deusex.core.runtime import RuntimeServices


def test_app_paths_double_root_and_secret_detection(isolated_paths: AppPaths) -> None:
    assert (isolated_paths.app_dir / "pyproject.toml").is_file()
    assert isolated_paths.user_data_dir.is_dir()
    assert isolated_paths.secrets_dir.is_dir()
    assert isolated_paths.logs_dir.is_dir()
    assert isolated_paths.runtimes_dir.is_dir()

    assert isolated_paths.is_secret_path(isolated_paths.env_file) is True
    assert isolated_paths.is_secret_path(isolated_paths.secrets_dir / "google_token.json") is True
    assert isolated_paths.is_secret_path(isolated_paths.settings_file) is False
    assert isolated_paths.is_secret_path(isolated_paths.db_file) is False


def test_permissions_catalog_complete_and_no_dot_magic_strings() -> None:
    assert set(PERMISSION_CATALOG.keys()) == set(Permission)
    for perm in Permission:
        assert "." not in perm.value
        assert perm.value == perm.value.lower()

    defaults = default_permissions_map()
    assert len(defaults) == len(Permission)

    pc_perms = {meta.permission for meta in permissions_for_module("pc")}
    assert pc_perms == {
        Permission.PC_BASIC,
        Permission.PC_CLIPBOARD,
        Permission.PC_FILES,
        Permission.PC_ADVANCED,
    }


def test_settings_zero_base_persistence_and_corrupt_recovery(
    settings_store: SettingsStore,
) -> None:
    loaded = settings_store.load()
    assert loaded.schema_version == 1
    assert loaded.ui.active_profile_id == "raphael"
    assert loaded.developer.enabled is False
    assert loaded.modules.enabled_module_ids() == []
    assert loaded.is_permission_enabled(Permission.PC_BASIC) is True

    loaded.modules.set_enabled("pc", True)
    loaded.set_permission(Permission.PC_FILES, False)
    loaded.general.user_name = "Tester"
    settings_store.save(loaded)

    reloaded = settings_store.load()
    assert reloaded.modules.is_enabled("pc") is True
    assert reloaded.modules.enabled_module_ids() == ["pc"]
    assert reloaded.is_permission_enabled(Permission.PC_FILES) is False
    assert reloaded.general.user_name == "Tester"

    # Recuperación ante JSON corrupto
    settings_store.file_path.write_text("{json-corrupto", encoding="utf-8")
    recovered = settings_store.load()
    assert recovered.modules.enabled_module_ids() == []
    assert settings_store.file_path.with_suffix(".corrupt.json").is_file()

    with pytest.raises(KeyError):
        recovered.modules.is_enabled("modulo_inexistente")


def test_secrets_manager_atomic_env_and_secret_json(
    isolated_paths: AppPaths,
    secrets_manager: SecretsManager,
) -> None:
    secrets_manager.set("GEMINI_API_KEY", "test_key_1234567890")
    secrets_manager.set("QUOTED_VALUE", 'valor con espacios y # comentario "interno"')

    reloaded = SecretsManager(isolated_paths, fallback_to_os_env=False)
    assert reloaded.get("GEMINI_API_KEY") == "test_key_1234567890"
    assert reloaded.get("QUOTED_VALUE") == 'valor con espacios y # comentario "interno"'
    assert reloaded.has("GEMINI_API_KEY") is True

    masked = reloaded.masked_items()
    assert masked["GEMINI_API_KEY"].startswith("tes")
    assert masked["GEMINI_API_KEY"].endswith("890")
    assert "*" in masked["GEMINI_API_KEY"]

    assert reloaded.delete("QUOTED_VALUE") is True
    assert reloaded.delete("QUOTED_VALUE") is False
    assert reloaded.get("QUOTED_VALUE") is None

    with pytest.raises(ValueError):
        reloaded.set("INVALID-KEY", "abc")

    saved_path = reloaded.save_secret_json("oauth_test", {"access_token": "tok_demo"})
    assert isolated_paths.is_secret_path(saved_path) is True
    assert reloaded.load_secret_json("oauth_test") == {"access_token": "tok_demo"}
    assert reloaded.delete_secret_json("oauth_test") is True
    assert reloaded.load_secret_json("oauth_test") is None

    with pytest.raises(ValueError):
        reloaded.save_secret_json("../escape", {"bad": True})


def test_database_wal_schema_transactions_and_cascade(db: Database) -> None:
    assert db.is_open is True
    assert db.journal_mode() == "wal"
    assert db.schema_version() == 1
    assert db.integrity_ok() is True

    rows = db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = {str(row["name"]) for row in rows}
    expected_tables = {
        "schema_migrations",
        "conversations",
        "messages",
        "memories",
        "scheduled_jobs",
        "routines",
        "action_audit",
        "device_aliases",
        "usage_ledger",
        "model_catalog_cache",
        "local_media_index",
        "paired_devices",
    }
    assert expected_tables.issubset(table_names)

    now = datetime.now(UTC).isoformat()
    with db.transaction():
        db.execute(
            "INSERT INTO conversations (id, title, profile_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("conv_1", "Prueba", "raphael", now, now),
        )
        db.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ("msg_1", "conv_1", "user", "Hola", now),
        )
        with pytest.raises(RuntimeError), db.transaction():
            db.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                ("msg_rollback", "conv_1", "user", "No debe persistir", now),
            )
            raise RuntimeError("forzar rollback savepoint")

    assert db.scalar("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", ("conv_1",)) == 1

    # Foreign key ON DELETE CASCADE
    db.execute("DELETE FROM conversations WHERE id = ?", ("conv_1",))
    assert db.scalar("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", ("conv_1",)) == 0

    db.close()
    assert db.is_open is False


def test_event_bus_routes_async_handlers_to_async_core_thread(
    event_bus: EventBus,
    runtime_services: RuntimeServices,
) -> None:
    """Antídoto #5: publicar desde un hilo sin event loop jamás lanza RuntimeError."""
    done = threading.Event()
    observed_thread_ids: list[int] = []
    sync_received: list[bool] = []

    def _failing_sync(_event: DomainEvent) -> None:
        raise RuntimeError("error aislado en handler sync")

    def _ok_sync(event: PrivacyModeChangedEvent) -> None:
        sync_received.append(event.enabled)

    async def _async_handler(event: PrivacyModeChangedEvent) -> None:
        assert event.enabled is True
        await asyncio.sleep(0.01)
        observed_thread_ids.append(threading.get_ident())
        done.set()

    unsub_fail = event_bus.subscribe(PrivacyModeChangedEvent, _failing_sync)
    event_bus.subscribe(PrivacyModeChangedEvent, _ok_sync)
    event_bus.subscribe(PrivacyModeChangedEvent, _async_handler)

    # Publicamos desde un hilo externo puro (sin asyncio loop corriendo)
    def _publisher_thread() -> None:
        event_bus.publish(PrivacyModeChangedEvent(enabled=True))

    pub_thread = threading.Thread(target=_publisher_thread, name="MockQtMainThread")
    pub_thread.start()
    pub_thread.join(timeout=2.0)

    assert done.wait(timeout=2.0) is True
    assert sync_received == [True]
    assert observed_thread_ids == [runtime_services.async_core.thread_id]

    unsub_fail()
    # Verificar publish_and_wait desde AsyncCoreThread
    runtime_services.async_core.run_sync(
        event_bus.publish_and_wait(PrivacyModeChangedEvent(enabled=True))
    )
    assert len(sync_received) == 2


def test_runtime_services_threads_com_sta_and_fast_shutdown(event_bus: EventBus) -> None:
    runtime = RuntimeServices(event_bus)
    hook_order: list[str] = []
    audio_ran = threading.Event()

    runtime.register_shutdown_hook(lambda: hook_order.append("first"))
    runtime.register_shutdown_hook(lambda: hook_order.append("second"))

    runtime.start()
    try:
        assert runtime.is_started is True
        main_tid = threading.get_ident()
        tids = {
            main_tid,
            runtime.async_core.thread_id,
            runtime.com_sta.thread_id,
            runtime.audio_input.thread_id,
        }
        assert None not in tids
        assert len(tids) == 4

        com_tid = runtime.com_sta.run_sync(threading.get_ident)
        assert com_tid == runtime.com_sta.thread_id

        async def _check_com_async() -> int:
            return await runtime.com_sta.run_async(threading.get_ident)

        assert runtime.async_core.run_sync(_check_com_async()) == runtime.com_sta.thread_id

        def _fake_audio_worker(stop_evt: threading.Event) -> None:
            audio_ran.set()
            stop_evt.wait(timeout=2.0)

        runtime.audio_input.set_worker(_fake_audio_worker)
        assert audio_ran.wait(timeout=1.5) is True
    finally:
        started = time.monotonic()
        elapsed = runtime.shutdown()
        wall = time.monotonic() - started

    assert hook_order == ["second", "first"]
    assert elapsed < 3.0
    assert wall < 3.0
    assert runtime.is_started is False
    assert runtime.async_core.is_running is False
    assert runtime.com_sta.is_running is False
    assert runtime.audio_input.is_running is False


class _DummyModule(BaseModule):
    module_id = "dummy"
    display_name = "Módulo de Prueba"
    description = "Verifica contrato BaseModule"
    required_permissions = (Permission.PC_BASIC,)

    async def start(self, ctx: ModuleContext) -> None:
        assert ctx.settings.schema_version == 1
        self.set_state(ModuleState.ACTIVE)

    async def stop(self) -> None:
        self.set_state(ModuleState.DISABLED)

    def get_tools(self) -> list[ToolSpec]:
        async def _handler(_args: dict[str, Any]) -> ToolResult:
            return ToolResult(ok=True, summary="Listo")

        return [
            ToolSpec(
                name="dummy.ping",
                module_id=self.module_id,
                description="Ping",
                parameters_schema={"type": "object", "properties": {}},
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=_handler,
            )
        ]

    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp(_match: re.Match[str], _raw: str) -> ToolResult:
            return ToolResult(ok=True, summary="Pong")

        return [
            FastPathRule(
                rule_id="dummy.fp.ping",
                module_id=self.module_id,
                priority=10,
                pattern=re.compile(r"^ping$", re.IGNORECASE),
                permission=Permission.PC_BASIC,
                risk_level=RiskLevel.SAFE,
                handler=_fp,
            )
        ]

    def diagnose(self) -> list[DiagnosticItem]:
        return [
            DiagnosticItem(
                code="dummy_ok",
                area="dummy",
                title="Estado Dummy",
                severity=DiagnosticSeverity.OK,
                message="Operativo",
            )
        ]


def test_base_module_contract_lifecycle(
    isolated_paths: AppPaths,
    settings_store: SettingsStore,
    secrets_manager: SecretsManager,
    db: Database,
    event_bus: EventBus,
    runtime_services: RuntimeServices,
) -> None:
    module = _DummyModule()
    assert module.state == ModuleState.DISABLED

    ctx = ModuleContext(
        paths=isolated_paths,
        settings=settings_store.load(),
        secrets=secrets_manager,
        db=db,
        events=event_bus,
        runtime=runtime_services,
    )
    runtime_services.async_core.run_sync(module.start(ctx))
    assert module.state == ModuleState.ACTIVE
    assert len(module.get_tools()) == 1
    assert len(module.get_fast_path_rules()) == 1
    assert module.diagnose()[0].severity == DiagnosticSeverity.OK

    runtime_services.async_core.run_sync(module.stop())
    assert module.state == ModuleState.DISABLED


def test_logging_setup_ring_buffer_anonymizer_and_translator(isolated_paths: AppPaths) -> None:
    state = setup_logging(isolated_paths, level=logging.INFO, console=False)
    assert state.log_file == isolated_paths.latest_debug_log
    assert state.log_file.is_file()

    logging.getLogger("deusex.brain.providers").info("Llamada modelo de prueba")
    logging.getLogger("deusex.core.engine").info("Regla fast-path ejecutada")
    logging.getLogger("deusex.core.runtime").error("Fallo simulado de prueba")

    assert len(state.ring_buffer.snapshot(category="ai")) >= 1
    assert len(state.ring_buffer.snapshot(category="fast_path")) >= 1
    assert len(state.ring_buffer.snapshot(category="error")) >= 1
    assert len(state.ring_buffer.snapshot(query="fast-path")) == 1

    # Reiniciar logging debe archivar la sesión previa
    teardown_logging()
    setup_logging(isolated_paths, level=logging.INFO, console=False)
    archives = list(isolated_paths.logs_dir.glob("deusex_session_*.log"))
    assert len(archives) == 1

    synthetic_user_path = str(Path("D:/Users/demo_tester/Documents/nota.txt"))
    raw_report = (
        f"Path={synthetic_user_path} "
        "Key=AIzaSyDummyTokenValue12345678901234 "
        "Auth=Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        "Host=192.168.50.10 "
        "Mac=AA:BB:CC:DD:EE:FF "
        "Mail=demo.user@example.com "
        "Tel=+56912345678 "
        "Custom=MI_SECRETO_MANUAL"
    )
    cleaned = anonymize_log_text(raw_report, extra_secrets=["MI_SECRETO_MANUAL"])
    assert "demo_tester" not in cleaned
    assert "AIzaSyDummyToken" not in cleaned
    assert "eyJhbGci" not in cleaned
    assert "192.168.50.10" not in cleaned
    assert "AA:BB:CC:DD:EE:FF" not in cleaned
    assert "demo.user@example.com" not in cleaned
    assert "+56912345678" not in cleaned
    assert "MI_SECRETO_MANUAL" not in cleaned

    assert "límite" in translate_error_es("429 RESOURCE_EXHAUSTED").lower()
    assert "clave api" in translate_error_es("401 API_KEY_INVALID").lower()
    assert "tardó demasiado" in translate_error_es(TimeoutError("timeout")).lower()
    assert "no se encontró" in translate_error_es(FileNotFoundError("x")).lower()
