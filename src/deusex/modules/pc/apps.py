from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
import functools
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Literal
import unicodedata

import psutil

logger = logging.getLogger(__name__)

AppSource = Literal["appsfolder", "app_path", "utility", "user_choice"]

# Constantes COM para shell:AppsFolder (Antídoto #4) y ventanas
_COINIT_APARTMENTTHREADED = 0x2
_SIGDN_NORMALDISPLAY = 0x00000000
_SIGDN_PARENTRELATIVEPARSING = 0x80018001
_WM_CLOSE = 0x0010
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_CLOAKED = 14

_HELPER_SERVICE_KEYWORDS: tuple[str, ...] = (
    "webview",
    "gamebar",
    "svc",
    "service",
    "updater",
    "crashhandler",
    "helper",
    "broker",
    "background",
)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(d1: int, d2: int, d3: int, d4: tuple[int, ...]) -> _GUID:
    return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


_IID_ISHELLITEM = _make_guid(
    0x43826D1E, 0xE718, 0x42EE, (0xBC, 0x55, 0xA1, 0xE2, 0x61, 0xC3, 0x7B, 0xFE)
)
_BHID_ENUMITEMS = _make_guid(
    0x94F60519, 0x2850, 0x4924, (0xAA, 0x5A, 0xD1, 0x5E, 0x84, 0x86, 0x80, 0x39)
)
_IID_IENUMSHELLITEMS = _make_guid(
    0x70629033, 0xE363, 0x4A28, (0xA5, 0x67, 0x0D, 0xB7, 0x80, 0x06, 0xE6, 0xD7)
)


def normalize_text(text: str) -> str:
    """Normaliza acentos, mayúsculas y signos para comparación tolerante."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_like = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-zA-Z0-9\s]+", " ", ascii_like.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def phonetic_key(text: str) -> str:
    """Genera clave fonética simple español/inglés (absorbe errores típicos de dictado)."""
    norm = normalize_text(text).replace(" ", "")
    if not norm:
        return ""
    if len(norm) > 2 and norm.startswith("es") and norm[2] in "ptckfmnl":
        norm = norm[1:]
    replacements = (
        ("ph", "f"),
        ("qu", "k"),
        ("ck", "k"),
        ("ch", "x"),
        ("sh", "x"),
        ("ai", "y"),
        ("ay", "y"),
        ("w", "gu"),
        ("v", "b"),
        ("z", "s"),
        ("k", "c"),
        ("h", ""),
    )
    for src, dst in replacements:
        norm = norm.replace(src, dst)
    # Colapsa consonantes repetidas ("spotifyy" -> "spotify")
    return re.sub(r"(.)\1+", r"\1", norm)


_KNOWN_ALIAS_HINTS: dict[str, tuple[str, ...]] = {
    "spotify": ("spotify", "espotify", "espotifai", "spotifai", "spoty"),
    "whatsapp": ("whatsapp", "wasap", "guasap", "wsp", "whatsap"),
    "discord": ("discord", "discor", "dc"),
    "google chrome": ("chrome", "crome", "crom", "google chrome"),
    "microsoft edge": ("edge", "edch", "microsoft edge"),
    "opera gx": ("opera gx", "operagx", "opera"),
    "opera": ("opera", "opera browser"),
    "firefox": ("firefox", "faierfox", "mozilla"),
    "brave": ("brave", "breiv"),
    "visual studio code": ("vscode", "vs code", "vsc", "code"),
    "calculadora": ("calculadora", "calculator", "calc"),
    "calculator": ("calculadora", "calculator", "calc"),
    "bloc de notas": ("bloc de notas", "notepad", "bloc", "notas"),
    "notepad": ("bloc de notas", "notepad", "bloc", "notas"),
    "explorador de archivos": ("explorador", "explorador de archivos", "archivos", "explorer"),
    "administrador de tareas": ("administrador de tareas", "task manager", "taskmgr", "tareas"),
    "configuracion": ("configuracion", "ajustes", "settings"),
    "simbolo del sistema": ("cmd", "consola", "simbolo del sistema"),
    "terminal": ("terminal", "windows terminal", "wt"),
    "powershell": ("powershell", "ps"),
    "word": ("word", "microsoft word"),
    "excel": ("excel", "microsoft excel"),
    "powerpoint": ("powerpoint", "power point", "ppt"),
    "steam": ("steam", "estim"),
    "obs studio": ("obs", "obs studio"),
    "paint": ("paint", "peint"),
    "herramienta recortes": ("recortes", "snipping tool", "captura"),
    "vlc": ("vlc", "vlc media player"),
    "telegram": ("telegram", "tg"),
}

_DEFAULT_ROLE_SYNONYMS: dict[str, str] = {
    "navegador": "browser",
    "el navegador": "browser",
    "navegador web": "browser",
    "browser": "browser",
    "internet": "browser",
    "correo": "mail",
    "el correo": "mail",
    "mail": "mail",
    "email": "mail",
    "cliente de correo": "mail",
    "reproductor": "player",
    "el reproductor": "player",
    "reproductor de musica": "player",
    "reproductor de audio": "player",
    "player": "player",
    "reproductor de video": "video",
    "lector pdf": "pdf",
    "lector de pdf": "pdf",
}

_BUILTIN_UTILITIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("Calculadora", "calc.exe", "calc.exe", ("calculadora", "calculator", "calc")),
    ("Bloc de notas", "notepad.exe", "notepad.exe", ("bloc de notas", "notepad", "bloc", "notas")),
    (
        "Explorador de archivos",
        "explorer.exe",
        "explorer.exe",
        ("explorador", "explorador de archivos", "archivos", "explorer"),
    ),
    (
        "Administrador de tareas",
        "taskmgr.exe",
        "taskmgr.exe",
        ("administrador de tareas", "taskmgr", "task manager", "tareas"),
    ),
    ("Configuración", "ms-settings:", "", ("configuracion", "ajustes", "settings")),
    ("Paint", "mspaint.exe", "mspaint.exe", ("paint", "peint", "mspaint")),
    ("Símbolo del sistema", "cmd.exe", "cmd.exe", ("cmd", "consola", "simbolo del sistema")),
    ("PowerShell", "powershell.exe", "powershell.exe", ("powershell", "ps")),
    ("Panel de control", "control.exe", "control.exe", ("panel de control", "control")),
    ("Recortes", "ms-screenclip:", "", ("recortes", "snipping", "snipping tool")),
)

# Procesos críticos del SO: cierre estrictamente bloqueado
CRITICAL_OS_PROCESSES: frozenset[str] = frozenset(
    {
        "system",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "winlogon.exe",
        "svchost.exe",
        "dwm.exe",
        "explorer.exe",
        "fontdrvhost.exe",
        "sihost.exe",
        "taskhostw.exe",
        "ctfmon.exe",
        "runtimebroker.exe",
        "audiodg.exe",
        "conhost.exe",
        "memory compression",
        "lsaiso.exe",
        "secure system",
        "deusex.exe",
    }
)

# Aplicaciones productivas / editores: cierre retenido por SafetyGate
PRODUCTIVITY_PROCESSES: frozenset[str] = frozenset(
    {
        "code.exe",
        "cursor.exe",
        "antigravity.exe",
        "devenv.exe",
        "pycharm64.exe",
        "idea64.exe",
        "webstorm64.exe",
        "winword.exe",
        "excel.exe",
        "powerpnt.exe",
        "onenote.exe",
        "notepad.exe",
        "notepad++.exe",
        "sublime_text.exe",
        "photoshop.exe",
        "illustrator.exe",
        "afterfx.exe",
        "adobe premiere pro.exe",
        "blender.exe",
        "obs64.exe",
        "resolve.exe",
        "figma.exe",
        "gimp.exe",
        "gimp-2.10.exe",
        "audacity.exe",
        "fl64.exe",
        "acrobat.exe",
        "wordpad.exe",
        "mspaint.exe",
    }
)

_CLOSE_ALIAS_TO_PROCS: dict[str, tuple[str, ...]] = {
    "spotify": ("spotify.exe",),
    "chrome": ("chrome.exe",),
    "google chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "microsoft edge": ("msedge.exe",),
    "opera": ("opera.exe",),
    "opera gx": ("opera.exe",),
    "firefox": ("firefox.exe",),
    "brave": ("brave.exe",),
    "discord": ("discord.exe",),
    "whatsapp": ("whatsapp.exe",),
    "telegram": ("telegram.exe",),
    "calculadora": ("calculatorapp.exe", "calc.exe"),
    "calculator": ("calculatorapp.exe", "calc.exe"),
    "calc": ("calculatorapp.exe", "calc.exe"),
    "bloc de notas": ("notepad.exe",),
    "notepad": ("notepad.exe",),
    "vscode": ("code.exe",),
    "vs code": ("code.exe",),
    "visual studio code": ("code.exe",),
    "word": ("winword.exe",),
    "excel": ("excel.exe",),
    "powerpoint": ("powerpnt.exe",),
    "steam": ("steam.exe",),
    "obs": ("obs64.exe",),
    "vlc": ("vlc.exe",),
    "paint": ("mspaint.exe",),
    "minecraft": ("javaw.exe", "minecraft.exe"),
    "overwatch": ("overwatch.exe",),
    "warframe": ("warframe.x64.exe",),
}


def generate_aliases(name: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Genera alias normalizados, sin espacios, acrónimos y variantes fonéticas."""
    norm = normalize_text(name)
    aliases: set[str] = set()
    if norm:
        aliases.add(norm)
        compact = norm.replace(" ", "")
        if compact:
            aliases.add(compact)
        words = norm.split()
        if len(words) >= 2:
            acronym = "".join(w[0] for w in words if w)
            if len(acronym) >= 2:
                aliases.add(acronym)

    for key, hints in _KNOWN_ALIAS_HINTS.items():
        if norm == key or key in norm:
            for hint in hints:
                normalized_hint = normalize_text(hint)
                if normalized_hint:
                    aliases.add(normalized_hint)

    for item in extra:
        normalized_extra = normalize_text(item)
        if normalized_extra:
            aliases.add(normalized_extra)

    return tuple(sorted(aliases))


@dataclass(frozen=True, slots=True)
class AppEntry:
    name: str
    launch_target: str
    source: AppSource
    aliases: tuple[str, ...]
    executable_name: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    ok: bool
    summary: str
    entry: AppEntry | None = None
    dry_run: bool = False
    error_code: str | None = None


class CloseCategory(StrEnum):
    BLOCKED_SYSTEM = "blocked_system"
    REQUIRES_SAFETY_GATE = "requires_safety_gate"
    DIRECT_SAFE = "direct_safe"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True, slots=True)
class CloseDecision:
    category: CloseCategory
    query: str
    matched_processes: tuple[str, ...] = ()
    matched_pids: tuple[int, ...] = ()
    reason: str = ""


def _extract_executable_from_command(command: str) -> str | None:
    """Extrae la ruta del ejecutable desde un comando del Registro Windows."""
    raw = os.path.expandvars(command.strip())
    if not raw:
        return None
    if raw.startswith('"'):
        end_quote = raw.find('"', 1)
        if end_quote > 1:
            candidate = raw[1:end_quote].strip()
            return candidate if candidate else None
    with suppress(ValueError):
        parts = shlex.split(raw, posix=False)
        if parts:
            return parts[0].strip('"')
    first = raw.split(" ", 1)[0].strip('"')
    return first or None


def _vtable_entry(obj_ptr: int, index: int) -> int:
    vtable_ptr = ctypes.c_void_p.from_address(obj_ptr).value
    if not vtable_ptr:
        raise OSError("Puntero vtable COM nulo.")
    slot_addr = vtable_ptr + index * ctypes.sizeof(ctypes.c_void_p)
    fn_addr = ctypes.c_void_p.from_address(slot_addr).value
    if not fn_addr:
        raise OSError(f"Entrada vtable COM {index} nula.")
    return int(fn_addr)


def _scan_apps_folder_com() -> list[tuple[str, str]]:
    """Enumera `shell:AppsFolder` vía COM nativo (`IShellItem` + `IEnumShellItems`)."""
    if sys.platform != "win32":
        return []
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return []

    ole32 = windll.ole32
    shell32 = windll.shell32

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None

    shell32.SHCreateItemFromParsingName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long

    release_proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
    bind_proto = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    )
    get_name_proto = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    )
    enum_next_proto = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    )

    def _release(ptr: ctypes.c_void_p) -> None:
        if ptr.value:
            with suppress(Exception):
                fn = release_proto(_vtable_entry(ptr.value, 2))
                fn(ptr)

    def _read_display_name(item_ptr: ctypes.c_void_p, sigdn: int) -> str | None:
        if not item_ptr.value:
            return None
        str_ptr = ctypes.c_void_p()
        fn = get_name_proto(_vtable_entry(item_ptr.value, 5))
        hr_name = fn(item_ptr, sigdn, ctypes.byref(str_ptr))
        if hr_name != 0 or not str_ptr.value:
            return None
        try:
            return ctypes.wstring_at(str_ptr.value)
        finally:
            ole32.CoTaskMemFree(str_ptr)

    hr_init = int(ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
    should_uninit = hr_init in (0, 1)
    folder_ptr = ctypes.c_void_p()
    enum_ptr = ctypes.c_void_p()
    results: list[tuple[str, str]] = []

    try:
        hr_folder = int(
            shell32.SHCreateItemFromParsingName(
                "shell:AppsFolder",
                None,
                ctypes.byref(_IID_ISHELLITEM),
                ctypes.byref(folder_ptr),
            )
        )
        if hr_folder != 0 or not folder_ptr.value:
            return []

        bind_fn = bind_proto(_vtable_entry(folder_ptr.value, 3))
        hr_bind = int(
            bind_fn(
                folder_ptr,
                None,
                ctypes.byref(_BHID_ENUMITEMS),
                ctypes.byref(_IID_IENUMSHELLITEMS),
                ctypes.byref(enum_ptr),
            )
        )
        if hr_bind != 0 or not enum_ptr.value:
            return []

        next_fn = enum_next_proto(_vtable_entry(enum_ptr.value, 3))
        while True:
            child_ptr = ctypes.c_void_p()
            fetched = ctypes.c_ulong(0)
            hr_next = int(next_fn(enum_ptr, 1, ctypes.byref(child_ptr), ctypes.byref(fetched)))
            if hr_next != 0 or fetched.value == 0 or not child_ptr.value:
                break
            try:
                display_name = _read_display_name(child_ptr, _SIGDN_NORMALDISPLAY)
                aumid = _read_display_name(child_ptr, _SIGDN_PARENTRELATIVEPARSING)
                if display_name and aumid:
                    results.append((display_name.strip(), aumid.strip()))
            finally:
                _release(child_ptr)
    finally:
        _release(enum_ptr)
        _release(folder_ptr)
        if should_uninit:
            ole32.CoUninitialize()

    return results


def _scan_apps_folder_powershell_fallback() -> list[tuple[str, str]]:
    """Fallback nativo `Get-StartApps` si el enumerador COM devolviera vacío."""
    if sys.platform != "win32":
        return []
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6.0,
            creationflags=creation_flags,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
        items = payload if isinstance(payload, list) else [payload]
        out: list[tuple[str, str]] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            name = str(row.get("Name") or "").strip()
            app_id = str(row.get("AppID") or "").strip()
            if name and app_id:
                out.append((name, app_id))
        return out
    except Exception as exc:
        logger.debug("Fallback Get-StartApps no disponible: %s", exc)
        return []


def _scan_registry_app_paths() -> list[AppEntry]:
    """Lee ejecutables registrados en HKLM/HKCU App Paths de Windows."""
    if sys.platform != "win32":
        return []
    import winreg

    entries: list[AppEntry] = []
    seen_targets: set[str] = set()
    subkey_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, subkey_path) as root_key:
                idx = 0
                while True:
                    try:
                        app_key_name = winreg.EnumKey(root_key, idx)
                    except OSError:
                        break
                    idx += 1
                    try:
                        with winreg.OpenKey(root_key, app_key_name) as app_key:
                            raw_val, _ = winreg.QueryValueEx(app_key, "")
                    except OSError:
                        continue
                    exe_path = _extract_executable_from_command(str(raw_val))
                    if not exe_path:
                        continue
                    path_obj = Path(exe_path)
                    if not path_obj.is_file():
                        continue
                    target_norm = str(path_obj).lower()
                    if target_norm in seen_targets:
                        continue
                    seen_targets.add(target_norm)
                    stem = path_obj.stem
                    display = stem.replace("_", " ").replace("-", " ").title()
                    entries.append(
                        AppEntry(
                            name=display,
                            launch_target=str(path_obj),
                            source="app_path",
                            aliases=generate_aliases(display, (stem, app_key_name)),
                            executable_name=path_obj.name.lower(),
                        )
                    )
        except OSError:
            continue
    return entries


def _read_user_choice_progid(role: str) -> str | None:
    """Lee el ProgId de la aplicación predeterminada en Windows 11 (`UserChoice`)."""
    if sys.platform != "win32":
        return None
    import winreg

    role_keys: dict[str, tuple[str, ...]] = {
        "browser": (
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice",
        ),
        "mail": (
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice",
        ),
        "player": (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.mp3\UserChoice",
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.wav\UserChoice",
        ),
        "video": (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.mp4\UserChoice",
        ),
        "pdf": (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice",
        ),
    }

    for reg_path in role_keys.get(role, ()):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
                val = str(prog_id).strip()
                if val:
                    return val
        except OSError:
            continue
    return None


def _resolve_progid_launch(prog_id: str) -> tuple[str, str, AppSource] | None:
    """Resuelve un ProgId de Windows a un ejecutable o AppUserModelID."""
    if sys.platform != "win32":
        return None
    import winreg

    # 1. Comando directo en HKCR\<ProgId>\shell\open\command
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{prog_id}\\shell\\open\\command") as key:
            raw_cmd, _ = winreg.QueryValueEx(key, "")
            exe_path = _extract_executable_from_command(str(raw_cmd))
            if exe_path and Path(exe_path).is_file():
                stem = Path(exe_path).stem
                return (stem.title(), exe_path, "user_choice")
    except OSError:
        pass

    # 2. App UWP / MSIX vía ApplicationUserModelID
    for sub in (f"{prog_id}\\Application", f"{prog_id}\\shell\\open"):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, sub) as key:
                for value_name in ("AppUserModelID", "PackageRelativeAppId"):
                    try:
                        aumid, _ = winreg.QueryValueEx(key, value_name)
                        clean = str(aumid).strip()
                        if clean and "!" in clean:
                            return (prog_id, clean, "appsfolder")
                    except OSError:
                        continue
        except OSError:
            continue

    return None


class AppScanner:
    """
    Índice nativo de aplicaciones de Windows 11 (Antídoto #4):
    - `shell:AppsFolder` (UWP/MSIX + accesos directos modernos, cero parsers binarios `.lnk`)
    - Registro `App Paths`
    - Apps predeterminadas dinámicas vía `UserChoice` (navegador, correo, reproductor, PDF)
    - Utilidades integradas de Windows y tabla de alias fonéticos
    """

    def __init__(self, *, entries_override: list[AppEntry] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: list[AppEntry] = list(entries_override) if entries_override else []
        self._scanned = entries_override is not None
        self._last_scan_ts: float = time.monotonic() if entries_override is not None else 0.0
        self._default_role_cache: dict[str, AppEntry | None] = {}

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def is_scanned(self) -> bool:
        with self._lock:
            return self._scanned

    def list_entries(self) -> list[AppEntry]:
        with self._lock:
            return list(self._entries)

    def scan(self, *, force: bool = False) -> int:
        """Escanea `shell:AppsFolder`, `App Paths` y utilidades Windows."""
        with self._lock:
            if self._scanned and not force and (time.monotonic() - self._last_scan_ts) < 300.0:
                return len(self._entries)

        collected: list[AppEntry] = []
        seen_keys: set[tuple[str, str]] = set()

        # 1. shell:AppsFolder (prioridad máxima: cubre UWP/MSIX como Spotify y apps modernas)
        folder_items = _scan_apps_folder_com()
        if not folder_items:
            folder_items = _scan_apps_folder_powershell_fallback()

        for display_name, aumid in folder_items:
            key = ("appsfolder", aumid.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            exe_hint: str | None = None
            if aumid.lower().endswith(".exe"):
                exe_hint = Path(aumid).name.lower()
            collected.append(
                AppEntry(
                    name=display_name,
                    launch_target=aumid,
                    source="appsfolder",
                    aliases=generate_aliases(display_name),
                    executable_name=exe_hint,
                )
            )

        # 2. Registry App Paths
        for entry in _scan_registry_app_paths():
            key = ("app_path", entry.launch_target.lower())
            if key not in seen_keys:
                seen_keys.add(key)
                collected.append(entry)

        # 3. Utilidades integradas Windows
        for disp, target, exe_name, extra_aliases in _BUILTIN_UTILITIES:
            key = ("utility", target.lower())
            if key not in seen_keys:
                seen_keys.add(key)
                collected.append(
                    AppEntry(
                        name=disp,
                        launch_target=target,
                        source="utility",
                        aliases=generate_aliases(disp, extra_aliases),
                        executable_name=exe_name or None,
                    )
                )

        with self._lock:
            self._entries = collected
            self._scanned = True
            self._last_scan_ts = time.monotonic()
            self._default_role_cache.clear()
        logger.info("AppScanner indexó %d aplicaciones.", len(collected))
        return len(collected)

    def resolve_default_app(self, role: str) -> AppEntry | None:
        """Resuelve dinámicamente el navegador, correo o reproductor predeterminado."""
        canonical_role = _DEFAULT_ROLE_SYNONYMS.get(normalize_text(role), role.strip().lower())
        with self._lock:
            if canonical_role in self._default_role_cache:
                return self._default_role_cache[canonical_role]

        prog_id = _read_user_choice_progid(canonical_role)
        resolved: AppEntry | None = None
        if prog_id:
            parsed = _resolve_progid_launch(prog_id)
            if parsed is not None:
                name, target, source = parsed
                exe_name = Path(target).name.lower() if source == "user_choice" else None
                resolved = AppEntry(
                    name=name,
                    launch_target=target,
                    source=source,
                    aliases=generate_aliases(name, (canonical_role, prog_id)),
                    executable_name=exe_name,
                )
            else:
                # Si es un ProgId UWP sin comando directo, buscar en AppsFolder
                prog_lower = prog_id.lower()
                with self._lock:
                    for item in self._entries:
                        if item.source == "appsfolder" and prog_lower in item.launch_target.lower():
                            resolved = item
                            break

        # Fallback por rol si el registro UserChoice no entrega ruta directa
        if resolved is None and canonical_role == "browser":
            for browser_name in ("opera gx", "google chrome", "microsoft edge", "firefox", "brave"):
                candidate = self.find_app(browser_name, allow_role_lookup=False)
                if candidate is not None:
                    resolved = candidate
                    break
        elif resolved is None and canonical_role == "player":
            for player_name in ("spotify", "vlc", "reproductor multimedia", "media player"):
                candidate = self.find_app(player_name, allow_role_lookup=False)
                if candidate is not None:
                    resolved = candidate
                    break

        with self._lock:
            self._default_role_cache[canonical_role] = resolved
        return resolved

    def find_app(self, query: str, *, allow_role_lookup: bool = True) -> AppEntry | None:
        """Busca la mejor aplicación coincidente por rol predeterminado, alias o similitud."""
        clean = normalize_text(re.sub(r"\.exe$", "", query.strip(), flags=re.IGNORECASE))
        if not clean:
            return None

        # Limpia artículos iniciales ("el ", "la ", "mi ")
        stripped = re.sub(r"^(?:el|la|los|las|mi|un|una)\s+", "", clean).strip()

        if allow_role_lookup and (clean in _DEFAULT_ROLE_SYNONYMS or stripped in _DEFAULT_ROLE_SYNONYMS):
            role = _DEFAULT_ROLE_SYNONYMS.get(clean) or _DEFAULT_ROLE_SYNONYMS[stripped]
            default_entry = self.resolve_default_app(role)
            if default_entry is not None:
                return default_entry

        with self._lock:
            entries = list(self._entries)

        if not entries:
            self.scan()
            with self._lock:
                entries = list(self._entries)

        target_phonetic = phonetic_key(stripped)
        best_entry: AppEntry | None = None
        best_score = 0.0

        source_bonus: dict[AppSource, float] = {
            "appsfolder": 0.05,
            "user_choice": 0.04,
            "app_path": 0.02,
            "utility": 0.01,
        }

        for entry in entries:
            norm_name = normalize_text(entry.name)
            score = 0.0

            if stripped == norm_name or stripped in entry.aliases:
                score = 1.0
            elif target_phonetic and any(
                phonetic_key(alias) == target_phonetic for alias in entry.aliases
            ):
                score = 0.94
            elif stripped in norm_name and len(stripped) >= 3:
                score = 0.88 - min(0.12, (len(norm_name) - len(stripped)) * 0.005)
            else:
                for alias in entry.aliases:
                    ratio = SequenceMatcher(None, stripped, alias).ratio()
                    if ratio > score:
                        score = ratio

            if score >= 0.78:
                adjusted = score + source_bonus.get(entry.source, 0.0)
                if adjusted > best_score:
                    best_score = adjusted
                    best_entry = entry

        return best_entry

    def launch_app(self, query: str, *, dry_run: bool = False) -> LaunchOutcome:
        """Lanza la aplicación solicitada usando `explorer.exe shell:AppsFolder\\...` o ejecutable."""
        entry = self.find_app(query)
        if entry is None:
            return LaunchOutcome(
                ok=False,
                summary=f"No encontré ninguna aplicación instalada parecida a '{query}'.",
                error_code="APP_NOT_FOUND",
            )

        if dry_run:
            return LaunchOutcome(
                ok=True,
                summary=f"[Dry-Run] Abriría {entry.name} ({entry.source}: {entry.launch_target}).",
                entry=entry,
                dry_run=True,
            )

        try:
            if entry.source == "appsfolder":
                subprocess.Popen(
                    ["explorer.exe", f"shell:AppsFolder\\{entry.launch_target}"],
                    close_fds=True,
                )
            elif entry.source == "utility" and ":" in entry.launch_target and not Path(
                entry.launch_target
            ).drive:
                startfile = getattr(os, "startfile", None)
                if callable(startfile):
                    startfile(entry.launch_target)
                else:
                    subprocess.Popen(["explorer.exe", entry.launch_target], close_fds=True)
            else:
                subprocess.Popen([entry.launch_target], close_fds=True)
        except Exception as exc:
            logger.warning("Error al abrir '%s' (%s): %s", entry.name, entry.launch_target, exc)
            return LaunchOutcome(
                ok=False,
                summary=f"No pude abrir {entry.name}: {exc}",
                entry=entry,
                error_code="LAUNCH_FAILED",
            )

        return LaunchOutcome(
            ok=True,
            summary=f"Abriendo {entry.name}.",
            entry=entry,
        )


@functools.lru_cache(maxsize=512)
def _read_exe_display_names(exe_path: str) -> tuple[str, ...]:
    """Lee FileDescription y ProductName de un .exe usando version.dll nativo."""
    if sys.platform != "win32" or not exe_path:
        return ()
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return ()
    version_dll = getattr(windll, "version", None)
    if version_dll is None:
        return ()

    try:
        size = int(version_dll.GetFileVersionInfoSizeW(exe_path, None))
        if not size:
            return ()
        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(exe_path, 0, size, buf):
            return ()

        lplp_buffer = ctypes.c_void_p()
        pu_len = wintypes.UINT()

        translations: list[tuple[int, int]] = [
            (0x0409, 0x04B0),  # US English Unicode
            (0x0C0A, 0x04B0),  # Spanish Modern Sort Unicode
            (0x040A, 0x04B0),  # Spanish Traditional Sort Unicode
            (0x0000, 0x04B0),  # Neutral Unicode
            (0x0409, 0x04E4),  # US English Windows ANSI
            (0x0C0A, 0x04E4),  # Spanish Windows ANSI
        ]
        if version_dll.VerQueryValueW(
            buf,
            r"\VarFileInfo\Translation",
            ctypes.byref(lplp_buffer),
            ctypes.byref(pu_len),
        ):
            count = pu_len.value // 4
            if count > 0 and lplp_buffer.value is not None:
                trans_arr = (wintypes.WORD * (count * 2)).from_address(lplp_buffer.value)
                for i in range(count):
                    translations.insert(0, (trans_arr[i * 2], trans_arr[i * 2 + 1]))

        names: list[str] = []
        for lang, codepage in translations:
            for field in ("FileDescription", "ProductName"):
                sub_block = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field}"
                lp_str = ctypes.c_wchar_p()
                str_len = wintypes.UINT()
                if (
                    version_dll.VerQueryValueW(
                        buf, sub_block, ctypes.byref(lp_str), ctypes.byref(str_len)
                    )
                    and lp_str.value
                ):
                    val = lp_str.value.strip()
                    if val and val not in names:
                        names.append(val)
        return tuple(names)
    except Exception:
        return ()


def _collect_visible_window_titles() -> dict[int, list[str]]:
    """Recolecta títulos de ventanas visibles agrupados por PID de proceso."""
    if sys.platform != "win32":
        return {}
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return {}
    user32 = getattr(windll, "user32", None)
    if user32 is None:
        return {}
    dwmapi = getattr(windll, "dwmapi", None)

    pid_to_titles: dict[int, list[str]] = {}
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        ex_style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE))
        if ex_style & _WS_EX_TOOLWINDOW:
            return True
        if dwmapi is not None:
            cloaked = ctypes.c_int(0)
            hr = int(
                dwmapi.DwmGetWindowAttribute(
                    wintypes.HWND(hwnd),
                    _DWMWA_CLOAKED,
                    ctypes.byref(cloaked),
                    ctypes.sizeof(cloaked),
                )
            )
            if hr == 0 and cloaked.value != 0:
                return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title == "Program Manager":
            return True
        pid_val = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        pid = int(pid_val.value)
        if pid > 0:
            pid_to_titles.setdefault(pid, []).append(title)
        return True

    callback = cb_type(_enum_cb)
    hdesk = user32.OpenInputDesktop(0, False, 0x0040 | 0x0001)
    if hdesk:
        try:
            user32.EnumDesktopWindows(hdesk, callback, 0)
        finally:
            user32.CloseDesktop(hdesk)
    else:
        user32.EnumWindows(callback, 0)

    return pid_to_titles


def _score_process_candidate(
    *,
    proc_name: str,
    exe_path: str,
    window_titles: list[str] | None,
    stripped: str,
    target_phonetic: str,
    scanner_target_names: set[str],
    scanner_aliases: set[str],
    scanner_names: set[str],
    alias_target_names: set[str],
) -> float:
    pname_low = proc_name.lower()
    stem_norm = normalize_text(Path(pname_low).stem)
    stem_phonetic = phonetic_key(stem_norm)
    version_names = _read_exe_display_names(exe_path) if exe_path else ()
    norm_version_names = [normalize_text(n) for n in version_names]
    window_norm_titles = [normalize_text(t) for t in (window_titles or [])]

    # 1. Coincidencia exacta de ejecutable o alias conocido
    if stripped == stem_norm or f"{stripped}.exe" == pname_low:
        return 1.0
    if pname_low in scanner_target_names or pname_low in alias_target_names:
        return 0.98

    # 2. Coincidencia en metadatos de versión (FileDescription / ProductName)
    if any(stripped == vn for vn in norm_version_names):
        return 0.97
    if any(phonetic_key(vn) == target_phonetic for vn in norm_version_names):
        return 0.94

    # 3. Coincidencia con aplicación resuelta por AppScanner
    if stem_norm in scanner_aliases or stem_norm in scanner_names:
        return 0.96
    if any(vn in scanner_aliases or vn in scanner_names for vn in norm_version_names):
        return 0.96
    if any(phonetic_key(al) == stem_phonetic for al in scanner_aliases):
        return 0.93

    # 4. Coincidencia en título de ventana visible
    if window_norm_titles:
        if any(stripped in wt for wt in window_norm_titles):
            return 0.91
        if target_phonetic and any(phonetic_key(wt) == target_phonetic for wt in window_norm_titles):
            return 0.90

    # 5. Subcadena si longitud >= 4
    if len(stripped) >= 4 and (stripped in stem_norm or any(stripped in vn for vn in norm_version_names)):
        return 0.87

    # 6. Similitud difusa con SequenceMatcher sobre ejecutable y metadatos del candidato
    best_ratio = 0.0
    for cand in [stem_norm, *norm_version_names]:
        if not cand:
            continue
        ratio = SequenceMatcher(None, stripped, cand).ratio()
        if ratio > best_ratio:
            best_ratio = ratio

    if best_ratio >= 0.80:
        return best_ratio

    return 0.0


def _find_matching_running_processes(
    query: str,
    *,
    app_scanner: AppScanner | None = None,
) -> list[psutil.Process]:
    """Encuentra procesos activos del usuario priorizando ventanas visibles y metadatos nativos."""
    clean = normalize_text(re.sub(r"\.exe$", "", query.strip(), flags=re.IGNORECASE))
    stripped = re.sub(r"^(?:cierra|cerr[aá]|cerrar|termina|mata)\s+", "", clean).strip()
    stripped = re.sub(r"^(?:el|la|los|las|mi)\s+", "", stripped).strip()
    if not stripped:
        return []

    target_phonetic = phonetic_key(stripped)
    current_pid = os.getpid()

    scanner_target_names: set[str] = set()
    scanner_aliases: set[str] = set()
    scanner_names: set[str] = set()
    if app_scanner is not None:
        entry = app_scanner.find_app(stripped, allow_role_lookup=False)
        if entry is not None:
            scanner_names.add(normalize_text(entry.name))
            for al in entry.aliases:
                norm_al = normalize_text(al)
                scanner_aliases.add(norm_al)
                if norm_al.endswith(".exe"):
                    scanner_target_names.add(norm_al)
                else:
                    scanner_target_names.add(f"{norm_al}.exe")
            if entry.executable_name:
                scanner_target_names.add(entry.executable_name.lower())

    alias_target_names: set[str] = set()
    if stripped in _CLOSE_ALIAS_TO_PROCS:
        alias_target_names.update(_CLOSE_ALIAS_TO_PROCS[stripped])
    else:
        for alias_key, proc_names in _CLOSE_ALIAS_TO_PROCS.items():
            if stripped == alias_key or (len(stripped) >= 4 and stripped in alias_key):
                alias_target_names.update(proc_names)

    visible_titles_by_pid = _collect_visible_window_titles()

    # Fase 1: Candidatos con ventana visible de usuario
    phase1_candidates: list[tuple[float, psutil.Process]] = []
    for pid, titles in visible_titles_by_pid.items():
        if pid == current_pid:
            continue
        try:
            proc = psutil.Process(pid)
            pname = proc.name()
            if not pname:
                continue
            exe_path = ""
            with suppress(Exception):
                exe_path = proc.exe()
            score = _score_process_candidate(
                proc_name=pname,
                exe_path=exe_path,
                window_titles=titles,
                stripped=stripped,
                target_phonetic=target_phonetic,
                scanner_target_names=scanner_target_names,
                scanner_aliases=scanner_aliases,
                scanner_names=scanner_names,
                alias_target_names=alias_target_names,
            )
            if score >= 0.80:
                phase1_candidates.append((score, proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if phase1_candidates:
        max_score = max(score for score, _ in phase1_candidates)
        top_procs = [proc for score, proc in phase1_candidates if score >= max_score - 0.05]
        return top_procs

    # Fase 2: Fallback sin ventana visible descartando helpers y servicios
    phase2_candidates: list[tuple[float, psutil.Process]] = []
    for proc in psutil.process_iter(attrs=["pid", "name"]):
        try:
            pid = int(proc.info["pid"])
            if pid == current_pid:
                continue
            pname = str(proc.info["name"] or "").strip()
            if not pname:
                continue
            pname_low = pname.lower()
            stem_norm = normalize_text(Path(pname_low).stem)

            is_explicit = stripped == stem_norm or f"{stripped}.exe" == pname_low
            if not is_explicit and any(kw in pname_low for kw in _HELPER_SERVICE_KEYWORDS):
                continue

            exe_path = ""
            with suppress(Exception):
                exe_path = proc.exe()

            score = _score_process_candidate(
                proc_name=pname,
                exe_path=exe_path,
                window_titles=None,
                stripped=stripped,
                target_phonetic=target_phonetic,
                scanner_target_names=scanner_target_names,
                scanner_aliases=scanner_aliases,
                scanner_names=scanner_names,
                alias_target_names=alias_target_names,
            )
            if score >= 0.80:
                phase2_candidates.append((score, proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if phase2_candidates:
        max_score = max(score for score, _ in phase2_candidates)
        top_procs = [proc for score, proc in phase2_candidates if score >= max_score - 0.05]
        return top_procs

    return []


def _send_wm_close_to_pids(pids: set[int]) -> None:
    """Envía WM_CLOSE educado a las ventanas principales de los PIDs indicados."""
    if sys.platform != "win32" or not pids:
        return
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return

    user32 = windll.user32
    enum_cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd: int, _lparam: int) -> bool:
        proc_id = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if int(proc_id.value) in pids and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        return True

    callback = enum_cb_type(_cb)
    with suppress(Exception):
        hdesk = user32.OpenInputDesktop(0, False, 0x0040 | 0x0001)
        if hdesk:
            try:
                user32.EnumDesktopWindows(hdesk, callback, 0)
            finally:
                user32.CloseDesktop(hdesk)
        else:
            user32.EnumWindows(callback, 0)


def _safe_wait_procs(
    procs: list[psutil.Process],
    timeout: float = 1.2,
) -> tuple[list[psutil.Process], list[psutil.Process]]:
    """Espera a que los procesos terminen capturando excepciones por PID."""
    gone: list[psutil.Process] = []
    alive: list[psutil.Process] = []
    deadline = time.monotonic() + timeout
    for proc in procs:
        try:
            remain = max(0.05, deadline - time.monotonic())
            proc.wait(timeout=remain)
            gone.append(proc)
        except psutil.TimeoutExpired:
            alive.append(proc)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            gone.append(proc)
        except (psutil.AccessDenied, PermissionError, OSError):
            alive.append(proc)
    return gone, alive


def classify_close_target(
    query: str,
    *,
    running_process_names: Iterable[str] | None = None,
    app_scanner: AppScanner | None = None,
    visible_process_names: Iterable[str] | None = None,
) -> CloseDecision:
    """
    Política de cierre inteligente en 3 niveles con resolución dinámica en 4 capas:
    1. Procesos críticos de Windows -> BLOCKED_SYSTEM (jamás se cierran)
    2. Editores / productividad -> REQUIRES_SAFETY_GATE (riesgo de pérdida de trabajo)
    3. Apps cotidianas y juegos -> DIRECT_SAFE (cierre directo)
    """
    clean = normalize_text(re.sub(r"\.exe$", "", query.strip(), flags=re.IGNORECASE))
    stripped = re.sub(r"^(?:cierra|cerr[aá]|cerrar|termina|mata)\s+", "", clean).strip()
    stripped = re.sub(r"^(?:el|la|los|las|mi)\s+", "", stripped).strip()
    if not stripped:
        return CloseDecision(
            category=CloseCategory.NOT_RUNNING,
            query=query,
            reason="No se indicó qué aplicación cerrar.",
        )

    # Chequeo temprano contra procesos críticos del SO incluso por nombre directo
    direct_candidates = {stripped, f"{stripped}.exe"}
    if direct_candidates & CRITICAL_OS_PROCESSES or stripped in (
        "explorador",
        "explorador de windows",
        "explorer",
        "windows",
        "sistema",
    ):
        return CloseDecision(
            category=CloseCategory.BLOCKED_SYSTEM,
            query=query,
            matched_processes=tuple(sorted(direct_candidates & CRITICAL_OS_PROCESSES)),
            reason="Proceso crítico del sistema operativo protegido contra cierre.",
        )

    scanner_target_names: set[str] = set()
    scanner_aliases: set[str] = set()
    scanner_names: set[str] = set()
    if app_scanner is not None:
        entry = app_scanner.find_app(stripped, allow_role_lookup=False)
        if entry is not None:
            scanner_names.add(normalize_text(entry.name))
            for al in entry.aliases:
                norm_al = normalize_text(al)
                scanner_aliases.add(norm_al)
                if norm_al.endswith(".exe"):
                    scanner_target_names.add(norm_al)
                else:
                    scanner_target_names.add(f"{norm_al}.exe")
            if entry.executable_name:
                scanner_target_names.add(entry.executable_name.lower())

    alias_target_names: set[str] = set()
    if stripped in _CLOSE_ALIAS_TO_PROCS:
        alias_target_names.update(_CLOSE_ALIAS_TO_PROCS[stripped])
    else:
        for alias_key, proc_names in _CLOSE_ALIAS_TO_PROCS.items():
            if stripped == alias_key or (len(stripped) >= 4 and stripped in alias_key):
                alias_target_names.update(proc_names)

    target_phonetic = phonetic_key(stripped)
    matched_names: list[str] = []
    matched_pids: list[int] = []

    if running_process_names is not None or visible_process_names is not None:
        # Prioridad 1: Ventanas visibles especificadas (pruebas o mocks)
        if visible_process_names is not None:
            scored_visible: list[tuple[float, str]] = []
            for raw_name in visible_process_names:
                low = raw_name.lower()
                s = _score_process_candidate(
                    proc_name=low,
                    exe_path="",
                    window_titles=None,
                    stripped=stripped,
                    target_phonetic=target_phonetic,
                    scanner_target_names=scanner_target_names,
                    scanner_aliases=scanner_aliases,
                    scanner_names=scanner_names,
                    alias_target_names=alias_target_names,
                )
                if s >= 0.80:
                    scored_visible.append((s, low))
            if scored_visible:
                max_s = max(s for s, _ in scored_visible)
                matched_names.extend([n for s, n in scored_visible if s >= max_s - 0.05])

        # Prioridad 2: Fallback en procesos en ejecución si no hubo visible coincidente
        if not matched_names and running_process_names is not None:
            scored_running: list[tuple[float, str]] = []
            for raw_name in running_process_names:
                low = raw_name.lower()
                stem = normalize_text(Path(low).stem)
                is_explicit = stripped == stem or f"{stripped}.exe" == low
                if not is_explicit and any(kw in low for kw in _HELPER_SERVICE_KEYWORDS):
                    continue
                s = _score_process_candidate(
                    proc_name=low,
                    exe_path="",
                    window_titles=None,
                    stripped=stripped,
                    target_phonetic=target_phonetic,
                    scanner_target_names=scanner_target_names,
                    scanner_aliases=scanner_aliases,
                    scanner_names=scanner_names,
                    alias_target_names=alias_target_names,
                )
                if s >= 0.80:
                    scored_running.append((s, low))
            if scored_running:
                max_s = max(s for s, _ in scored_running)
                matched_names.extend([n for s, n in scored_running if s >= max_s - 0.05])
    else:
        # Resolución en vivo contra procesos del sistema operativo
        procs = _find_matching_running_processes(stripped, app_scanner=app_scanner)
        for proc in procs:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                matched_names.append(proc.name().lower())
                matched_pids.append(proc.pid)

    unique_names = tuple(sorted(set(matched_names)))
    if not unique_names:
        # Si la consulta busca una app productiva (por nombre directo, scanner o alias)
        target_prod = (
            direct_candidates | scanner_target_names | alias_target_names
        ) & PRODUCTIVITY_PROCESSES
        if target_prod:
            return CloseDecision(
                category=CloseCategory.REQUIRES_SAFETY_GATE,
                query=query,
                matched_processes=tuple(sorted(target_prod)),
                reason=(
                    "Aplicación de productividad/edición detectada: requiere confirmación "
                    "para evitar pérdida de trabajo no guardado."
                ),
            )
        return CloseDecision(
            category=CloseCategory.NOT_RUNNING,
            query=query,
            reason=f"No encontré '{query}' en ejecución.",
        )

    if any(name in CRITICAL_OS_PROCESSES for name in unique_names):
        return CloseDecision(
            category=CloseCategory.BLOCKED_SYSTEM,
            query=query,
            matched_processes=unique_names,
            matched_pids=tuple(matched_pids),
            reason="Bloqueado: corresponde a un proceso crítico de Windows.",
        )

    if any(name in PRODUCTIVITY_PROCESSES for name in unique_names):
        return CloseDecision(
            category=CloseCategory.REQUIRES_SAFETY_GATE,
            query=query,
            matched_processes=unique_names,
            matched_pids=tuple(matched_pids),
            reason=(
                "Aplicación de productividad/edición detectada: requiere confirmación "
                "para evitar pérdida de trabajo no guardado."
            ),
        )

    return CloseDecision(
        category=CloseCategory.DIRECT_SAFE,
        query=query,
        matched_processes=unique_names,
        matched_pids=tuple(matched_pids),
        reason="Aplicación cotidiana o juego: cierre directo permitido.",
    )


def close_application(
    query: str,
    *,
    allow_productivity: bool = False,
    dry_run: bool = False,
    app_scanner: AppScanner | None = None,
) -> tuple[bool, str, CloseDecision]:
    """Ejecuta el cierre inteligente respetando los 3 niveles de seguridad y permisos de Windows."""
    decision = classify_close_target(query, app_scanner=app_scanner)
    if decision.category == CloseCategory.NOT_RUNNING:
        return False, decision.reason, decision
    if decision.category == CloseCategory.BLOCKED_SYSTEM:
        return False, decision.reason, decision
    if decision.category == CloseCategory.REQUIRES_SAFETY_GATE and not allow_productivity:
        return False, decision.reason, decision

    if dry_run:
        names_str = ", ".join(decision.matched_processes)
        return True, f"[Dry-Run] Cerraría {names_str}.", decision

    pids = set(decision.matched_pids)
    _send_wm_close_to_pids(pids)

    # Esperar hasta 1.2 s por cierre educado antes de terminate()
    procs: list[psutil.Process] = []
    denied_count = 0
    for pid in pids:
        try:
            procs.append(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, PermissionError, OSError):
            denied_count += 1

    if not procs and denied_count > 0:
        names_str = ", ".join(decision.matched_processes)
        return (
            False,
            f"Windows denegó el permiso para cerrar {names_str} (proceso con privilegios elevados o del sistema).",
            decision,
        )

    _, alive = _safe_wait_procs(procs, timeout=1.2)
    terminate_denied = 0
    for proc in alive:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except (psutil.AccessDenied, PermissionError, OSError):
            terminate_denied += 1

    if alive:
        _safe_wait_procs(alive, timeout=0.8)

    still_alive: list[psutil.Process] = []
    for proc in procs:
        try:
            if proc.is_running():
                still_alive.append(proc)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except (psutil.AccessDenied, PermissionError, OSError):
            still_alive.append(proc)

    if still_alive and (denied_count == len(pids) or terminate_denied == len(alive)):
        names_str = ", ".join(decision.matched_processes)
        return (
            False,
            f"Windows denegó el permiso para cerrar {names_str} (proceso con privilegios elevados o del sistema).",
            decision,
        )

    names_str = ", ".join(decision.matched_processes)
    return True, f"Cerré {names_str}.", decision
