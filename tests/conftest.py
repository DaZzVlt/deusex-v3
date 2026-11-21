from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from deusex.core.config import SecretsManager, SettingsStore
from deusex.core.db import Database
from deusex.core.events import EventBus
from deusex.core.logging_setup import teardown_logging
from deusex.core.paths import ENV_USER_DATA_DIR, AppPaths, resolve_app_paths
from deusex.core.runtime import RuntimeServices


@pytest.fixture(autouse=True)
def _cleanup_logging_handlers() -> Iterator[None]:
    """Garantiza que ningún FileHandler quede abierto al finalizar un test en Windows."""
    yield
    teardown_logging()


@pytest.fixture()
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    """Aísla USER_DATA_DIR en tmp_path sin tocar la carpeta real del usuario."""
    user_dir = tmp_path / "user_data"
    monkeypatch.setenv(ENV_USER_DATA_DIR, str(user_dir))
    paths = resolve_app_paths(user_data_override=user_dir)
    paths.ensure_user_dirs()
    return paths


@pytest.fixture()
def settings_store(isolated_paths: AppPaths) -> SettingsStore:
    return SettingsStore(isolated_paths)


@pytest.fixture()
def secrets_manager(isolated_paths: AppPaths) -> SecretsManager:
    return SecretsManager(isolated_paths, fallback_to_os_env=False)


@pytest.fixture()
def db(isolated_paths: AppPaths) -> Iterator[Database]:
    database = Database(isolated_paths.db_file)
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture()
def event_bus() -> EventBus:
    return EventBus()


@pytest.fixture()
def runtime_services(event_bus: EventBus) -> Iterator[RuntimeServices]:
    runtime = RuntimeServices(event_bus)
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.shutdown()


@pytest.fixture()
def qt_widget_cleanup(qapp: Any) -> Iterator[Callable[[Any], Any]]:
    """Helper oficial para pytest-qt: cierra y libera widgets con deleteLater() + processEvents()."""
    tracked: list[Any] = []

    def _track(widget: Any) -> Any:
        tracked.append(widget)
        return widget

    yield _track

    for widget in reversed(tracked):
        widget.close()
        widget.deleteLater()
    qapp.processEvents()
