from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys

ENV_USER_DATA_DIR = "DEUSEX_USER_DATA_DIR"
APP_FOLDER_NAME = "DeusEx"


def _detect_app_dir() -> Path:
    """Resuelve la raíz de solo lectura del paquete (compatible con PyInstaller --onedir)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if isinstance(meipass, str) and meipass:
            return Path(meipass).resolve()
        return Path(sys.executable).resolve().parent

    # Desarrollo: src/deusex/core/paths.py -> parents[3] == raíz del repositorio
    candidate = Path(__file__).resolve().parents[3]
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return Path.cwd().resolve()


def _detect_user_data_dir() -> Path:
    """Resuelve la raíz escribible de datos del usuario sin rutas hardcodeadas."""
    override = os.environ.get(ENV_USER_DATA_DIR, "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return (Path(local_app_data) / APP_FOLDER_NAME).resolve()
        return (Path.home() / "AppData" / "Local" / APP_FOLDER_NAME).resolve()

    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return (Path(xdg_data) / APP_FOLDER_NAME).resolve()
    return (Path.home() / ".local" / "share" / APP_FOLDER_NAME).resolve()


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Contrato de doble raíz estricta: APP_DIR (solo lectura) y USER_DATA_DIR (escritura)."""

    app_dir: Path
    user_data_dir: Path

    # --- APP_DIR (solo lectura) ---
    @property
    def assets_dir(self) -> Path:
        return self.app_dir / "assets"

    @property
    def bundled_profiles_dir(self) -> Path:
        return self.assets_dir / "profiles"

    @property
    def themes_dir(self) -> Path:
        return self.assets_dir / "themes"

    @property
    def earcons_dir(self) -> Path:
        return self.assets_dir / "earcons"

    @property
    def icons_dir(self) -> Path:
        return self.assets_dir / "icons"

    @property
    def oauth_defaults_file(self) -> Path:
        return self.assets_dir / "oauth_app_defaults.json"

    # --- USER_DATA_DIR (persistencia escribible) ---
    @property
    def settings_file(self) -> Path:
        return self.user_data_dir / "settings.json"

    @property
    def env_file(self) -> Path:
        return self.user_data_dir / ".env"

    @property
    def db_file(self) -> Path:
        return self.user_data_dir / "deusex.db"

    @property
    def secrets_dir(self) -> Path:
        return self.user_data_dir / "secrets"

    @property
    def logs_dir(self) -> Path:
        return self.user_data_dir / "logs"

    @property
    def latest_debug_log(self) -> Path:
        return self.logs_dir / "Latest Debug.txt"

    @property
    def profiles_dir(self) -> Path:
        return self.user_data_dir / "profiles"

    @property
    def models_dir(self) -> Path:
        return self.user_data_dir / "models"

    @property
    def vosk_es_dir(self) -> Path:
        return self.models_dir / "vosk-es"

    @property
    def runtimes_dir(self) -> Path:
        return self.user_data_dir / "runtimes"

    @property
    def ffmpeg_dir(self) -> Path:
        return self.runtimes_dir / "ffmpeg"

    @property
    def ffmpeg_exe(self) -> Path:
        exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        return self.ffmpeg_dir / "bin" / exe_name

    @property
    def whatsapp_runtime_dir(self) -> Path:
        return self.runtimes_dir / "whatsapp"

    @property
    def runtimes_manifest_file(self) -> Path:
        return self.runtimes_dir / "manifest.json"

    @property
    def cache_dir(self) -> Path:
        return self.user_data_dir / "cache"

    @property
    def backups_dir(self) -> Path:
        return self.user_data_dir / "backups"

    def ensure_user_dirs(self) -> None:
        """Crea de forma idempotente toda la jerarquía de directorios de USER_DATA_DIR."""
        for directory in (
            self.user_data_dir,
            self.secrets_dir,
            self.logs_dir,
            self.profiles_dir,
            self.models_dir,
            self.runtimes_dir,
            self.cache_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def is_secret_path(self, target: Path) -> bool:
        """Indica si una ruta corresponde a .env o secrets/ (excluida de backups y logs)."""
        resolved = target.resolve()
        if resolved == self.env_file.resolve():
            return True
        secrets_root = self.secrets_dir.resolve()
        return resolved == secrets_root or secrets_root in resolved.parents


def resolve_app_paths(
    *,
    user_data_override: Path | None = None,
    app_dir_override: Path | None = None,
) -> AppPaths:
    """Construye la instancia canónica de AppPaths."""
    app_dir = app_dir_override.resolve() if app_dir_override is not None else _detect_app_dir()
    user_data_dir = (
        user_data_override.resolve()
        if user_data_override is not None
        else _detect_user_data_dir()
    )
    return AppPaths(app_dir=app_dir, user_data_dir=user_data_dir)
