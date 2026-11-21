from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deusex.core.paths import AppPaths
from deusex.core.permissions import Permission, default_permissions_map, get_permission_meta

logger = logging.getLogger(__name__)

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class GeneralSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_name: str = ""
    language: str = "es-CL"
    city: str = ""
    country: str = ""
    start_with_windows: bool = False
    check_updates: bool = True
    privacy_mode: bool = False
    onboarding_completed: bool = False


class UISettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    theme: Literal["auto", "fluent_dark", "fluent_light", "cyber_hud"] = "auto"
    use_mica: bool = True
    use_windows_accent: bool = False
    sidebar_collapsed: bool = False
    active_profile_id: str = "raphael"
    active_tone: str = "equilibrado"
    minichat_hotkey: str = "Alt+Space"
    privacy_hotkey: str = "Ctrl+Shift+P"
    orb_style: Literal["reactor", "waves", "sphere", "bars", "avatar"] = "reactor"
    orb_visibility: Literal["speaking_only", "always_subtle"] = "speaking_only"
    orb_corner: Literal["bottom_right", "bottom_left", "top_right", "top_left"] = "bottom_right"
    orb_avatar_path: str = ""


class BrainSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    primary_provider: str = "gemini"
    model_selection_mode: Literal["auto", "manual"] = "auto"
    manual_fast_model: str = ""
    manual_deep_model: str = ""
    daily_token_budget: int = Field(default=0, ge=0)
    memory_auto_extract: bool = True
    show_model_selector_in_chat: bool = False


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    input_device_index: int | None = None
    wake_word_enabled: bool = True
    custom_wake_words: list[str] = Field(default_factory=list)
    ptt_enabled: bool = True
    ptt_hotkey: str = "Ctrl+Alt+V"
    tts_muted: bool = False
    tts_rate: float = Field(default=1.0, ge=0.5, le=2.0)
    voice_by_profile: dict[str, str] = Field(default_factory=dict)
    follow_up_window_sec: float = Field(default=6.0, ge=0.0, le=30.0)
    voiceprint_enabled: bool = False
    ducking_level_pct: int = Field(default=22, ge=0, le=100)
    in_call_text_only: bool = False


class ModulesToggleSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Zero-Base en primer arranque: todos los módulos enchufables inician en False.
    pc: bool = False
    network: bool = False
    voice: bool = False
    intel: bool = False
    vision: bool = False
    music: bool = False
    workspace: bool = False
    discord_chat: bool = False
    discord_music: bool = False
    whatsapp: bool = False
    bridge: bool = False

    def enabled_module_ids(self) -> list[str]:
        return [
            field_name
            for field_name, is_enabled in self.model_dump().items()
            if bool(is_enabled)
        ]

    def is_enabled(self, module_id: str) -> bool:
        if module_id not in type(self).model_fields:
            raise KeyError(f"Módulo desconocido en configuración: {module_id!r}")
        return bool(self.model_dump()[module_id])

    def set_enabled(self, module_id: str, enabled: bool) -> None:
        if module_id not in type(self).model_fields:
            raise KeyError(f"Módulo desconocido en configuración: {module_id!r}")
        setattr(self, module_id, bool(enabled))


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    safety_timeout_sec: int = Field(default=45, ge=5, le=300)
    allow_trusted_messaging_bypass: bool = False


class DiscordSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command_prefix: str = "!"
    notify_safety_gate: bool = True
    notify_timers: bool = True
    notify_errors: bool = True
    owner_user_ids: list[str] = Field(default_factory=list)
    dj_user_ids: list[str] = Field(default_factory=list)


class DeveloperSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    dry_run: bool = False
    oauth_callback_port: int = Field(default=8888, ge=1024, le=65535)
    bridge_ws_port: int = Field(default=8765, ge=1024, le=65535)


class DeusExSettings(BaseModel):
    """Único documento de configuración tipado (`USER_DATA_DIR/settings.json`)."""

    model_config = ConfigDict(extra="ignore")
    schema_version: int = 1
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    ui: UISettings = Field(default_factory=UISettings)
    brain: BrainSettings = Field(default_factory=BrainSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    modules: ModulesToggleSettings = Field(default_factory=ModulesToggleSettings)
    permissions: dict[Permission, bool] = Field(default_factory=default_permissions_map)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    developer: DeveloperSettings = Field(default_factory=DeveloperSettings)

    def ensure_all_permissions(self) -> None:
        """Completa cualquier permiso nuevo añadido al catálogo sin sobrescribir los existentes."""
        for perm, default_value in default_permissions_map().items():
            self.permissions.setdefault(perm, default_value)

    def is_permission_enabled(self, permission: Permission) -> bool:
        if permission in self.permissions:
            return self.permissions[permission]
        return get_permission_meta(permission).default_enabled

    def set_permission(self, permission: Permission, enabled: bool) -> None:
        self.permissions[permission] = bool(enabled)


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


class SettingsStore:
    """Persistencia thread-safe y atómica de `settings.json`."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._lock = threading.RLock()

    @property
    def file_path(self) -> Path:
        return self._paths.settings_file

    def load(self) -> DeusExSettings:
        with self._lock:
            self._paths.ensure_user_dirs()
            if not self.file_path.is_file():
                settings = DeusExSettings()
                settings.ensure_all_permissions()
                self.save(settings)
                return settings

            try:
                raw_text = self.file_path.read_text(encoding="utf-8")
                settings = DeusExSettings.model_validate_json(raw_text)
                settings.ensure_all_permissions()
                return settings
            except (OSError, ValidationError, ValueError) as exc:
                logger.warning(
                    "settings.json inválido o ilegible (%s); regenerando respaldo y valores por defecto.",
                    exc,
                )
                corrupt_backup = self.file_path.with_suffix(".corrupt.json")
                with contextlib.suppress(OSError):
                    os.replace(self.file_path, corrupt_backup)
                settings = DeusExSettings()
                settings.ensure_all_permissions()
                self.save(settings)
                return settings

    def save(self, settings: DeusExSettings) -> None:
        with self._lock:
            settings.ensure_all_permissions()
            serialized = settings.model_dump_json(indent=2) + "\n"
            _atomic_write_text(self.file_path, serialized)


def _escape_env_value(value: str) -> str:
    if value == "":
        return ""
    needs_quotes = any(ch in value for ch in (" ", "\t", "#", '"', "'", "\n", "\r"))
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return f'"{escaped}"'


def _unescape_env_value(raw: str) -> str:
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        inner = stripped[1:-1]
        return inner.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    if len(stripped) >= 2 and stripped[0] == "'" and stripped[-1] == "'":
        return stripped[1:-1]
    return stripped


class SecretsManager:
    """Gestor atómico de `.env` y archivos JSON aislados en `USER_DATA_DIR/secrets/`."""

    def __init__(self, paths: AppPaths, *, fallback_to_os_env: bool = True) -> None:
        self._paths = paths
        self._fallback_to_os_env = fallback_to_os_env
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}
        self.reload()

    @property
    def env_file(self) -> Path:
        return self._paths.env_file

    def reload(self) -> None:
        with self._lock:
            self._paths.ensure_user_dirs()
            parsed: dict[str, str] = {}
            if self.env_file.is_file():
                for line in self.env_file.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key_raw, val_raw = stripped.split("=", 1)
                    key = key_raw.strip()
                    if _ENV_KEY_RE.match(key):
                        parsed[key] = _unescape_env_value(val_raw)
            self._cache = parsed

    def _flush_locked(self) -> None:
        lines = [
            "# DeusEx v3 — Secretos locales (nunca compartir ni subir a Git)",
        ]
        for key in sorted(self._cache):
            lines.append(f"{key}={_escape_env_value(self._cache[key])}")
        _atomic_write_text(self.env_file, "\n".join(lines) + "\n")

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            if key in self._cache and self._cache[key] != "":
                return self._cache[key]
            if self._fallback_to_os_env:
                env_val = os.environ.get(key)
                if env_val is not None and env_val != "":
                    return env_val
            return default

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def set(self, key: str, value: str) -> None:
        clean_key = key.strip()
        if not _ENV_KEY_RE.match(clean_key):
            raise ValueError(f"Nombre de clave .env inválido: {key!r}")
        with self._lock:
            self._cache[clean_key] = value.strip()
            self._flush_locked()

    def delete(self, key: str) -> bool:
        with self._lock:
            if key not in self._cache:
                return False
            del self._cache[key]
            self._flush_locked()
            return True

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._cache)

    def masked_items(self) -> dict[str, str]:
        """Devuelve las claves de `.env` con valores enmascarados para la UI de Desarrollador."""
        with self._lock:
            result: dict[str, str] = {}
            for key in sorted(self._cache):
                val = self._cache[key]
                if not val:
                    result[key] = ""
                elif len(val) <= 6:
                    result[key] = "*" * len(val)
                else:
                    result[key] = f"{val[:3]}{'*' * (len(val) - 6)}{val[-3:]}"
            return result

    def _resolve_secret_json_path(self, name: str) -> Path:
        if not _SECRET_NAME_RE.match(name) or ".." in name:
            raise ValueError(f"Nombre de archivo secreto inválido: {name!r}")
        filename = name if name.endswith(".json") else f"{name}.json"
        target = (self._paths.secrets_dir / filename).resolve()
        secrets_root = self._paths.secrets_dir.resolve()
        if secrets_root not in target.parents:
            raise ValueError(f"Ruta fuera de secrets/: {name!r}")
        return target

    def save_secret_json(self, name: str, payload: dict[str, Any]) -> Path:
        with self._lock:
            self._paths.ensure_user_dirs()
            target = self._resolve_secret_json_path(name)
            _atomic_write_text(
                target,
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
            return target

    def load_secret_json(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            target = self._resolve_secret_json_path(name)
            if not target.is_file():
                return None
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return None
            return loaded

    def delete_secret_json(self, name: str) -> bool:
        with self._lock:
            target = self._resolve_secret_json_path(name)
            if not target.is_file():
                return False
            target.unlink()
            return True
