
# DeusEx v3 — Arquitectura Técnica y Contratos del Núcleo

**Versión:** 3.0.0  
**Plataforma:** Windows 11 (64-bit) · Python 3.14.10 · PySide6 6.12.0 · `uv`  
**Fuente de Verdad Funcional:** `DESIGN_V3.md`  
**Perfiles Base (Fase Pruebas/Beta):** `raphael`, `jarvis`, `friday`, `magi`, `glados` (`active_profile_id = "raphael"` por defecto; 100% data-driven en `assets/profiles/*.json`).  
**Regla de Evolución:** Este documento vive y se actualiza solo si una rebanada verificada en vivo exige ajustar un contrato. Todo módulo enchufable depende exclusivamente de `deusex.core` y jamás importa otro módulo hermano de forma directa.

---

## 1. Árbol Exacto de Directorios y Archivos (`deusex-v3/`)

```text
deusex-v3/
├── .gitignore                        # Bloquea .env, secrets/, *.db*, runtimes/, models/, dist/, _entrega_*.md
├── pyproject.toml                    # Stack completo Python 3.14 unificado ([project].dependencies + optional dev, voice-azure)
├── DESIGN_V3.md                      # Fuente de verdad de producto (congelada)
├── ARCHITECTURE_V3.md                # Este documento (contratos y fronteras técnicas)
├── README.md                         # Guía breve de desarrollo y compilación
│
├── .github/
│   └── workflows/
│       └── deploy-pages.yml          # Publicación automática de web_portal/ en GitHub Pages (Rebanada 6)
│
├── scripts/
│   └── setup_runtimes.py             # Prepara runtimes locales (Vosk ES, FFmpeg portable, Node.js + Baileys) en USER_DATA_DIR
│
├── assets/
│   ├── oauth_app_defaults.json       # Client IDs públicos oficiales (Google Desktop, Spotify PKCE :8888, TIDAL PKCE)
│   ├── profiles/                     # 5 perfiles JSON editables por defecto (raphael, jarvis, friday, magi, glados)
│   ├── themes/
│   │   ├── fluent_dark.qss           # Tema Fluent Oscuro Windows 11
│   │   ├── fluent_light.qss          # Tema Fluent Claro Windows 11
│   │   └── cyber_hud.qss             # Tema Cyber-HUD
│   ├── earcons/                      # Micro-sonidos WAV + síntesis fallback en memoria
│   └── icons/                        # Íconos SVG/PNG monocromos Fluent + bandeja del sistema
│
├── web_portal/                       # Sitio estático GitHub Pages (Rebanada 6)
│   ├── index.html                    # Landing comercial + enlace a GitHub Releases
│   ├── guides.html                   # Guías ilustradas (Gemini Key, Bot Discord, TV, APK)
│   ├── privacy.html                  # Política Google API Services User Data Policy (Limited Use)
│   ├── terms.html                    # Términos de servicio + atribución Open-Meteo CC BY 4.0
│   ├── callback.html                 # Puente OAuth PKCE (estricto 127.0.0.1 + anti Open-Redirect + Setup Sync)
│   ├── 404.html
│   ├── styles.css
│   └── app.js
│
├── packaging/                        # Empaquetado comercial Windows 11 (Rebanada 9)
│   ├── deusex.spec                   # PyInstaller --onedir
│   └── deusex_installer.iss          # Inno Setup (incluye Vosk ES, FFmpeg portable, Node/Baileys)
│
├── mobile_app/                       # DeusEx Companion.apk — Kotlin + Jetpack Compose, cero ADB (Rebanada 10)
│   └── ...
│
├── src/
│   └── deusex/
│       ├── __init__.py               # Versión semántica (__version__ = "3.0.0")
│       ├── __main__.py               # Entrypoint `python -m deusex`
│       ├── main.py                   # DPI PerMonitorV2, instancia única, arranque Qt + wiring
│       ├── app.py                    # DeusExApplication (Composition Root + reconciliación de settings.json)
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── paths.py              # Doble raíz APP_DIR / USER_DATA_DIR (%LOCALAPPDATA%\DeusEx\)
│       │   ├── permissions.py        # ÚNICO catálogo Permission(StrEnum) + PermissionMeta
│       │   ├── config.py             # DeusExSettings unificado (settings.json) + SecretsManager (.env atómico)
│       │   ├── runtime.py            # AsyncCoreThread, ComStaThread (CoInitializeEx), AudioInputThread, cierre <3 s
│       │   ├── db.py                 # SQLite WAL + RLock + migraciones versionadas
│       │   ├── events.py             # EventBus (enrutamiento estricto a AsyncCoreThread) + eventos de dominio
│       │   ├── contracts.py          # RiskLevel, ModuleState, DiagnosticItem, ToolSpec, ToolResult, FastPathRule, BaseModule
│       │   ├── engine.py             # ModuleRegistry + ToolExecutor + SafetyGate + FastPathEngine
│       │   ├── scheduler.py          # LocalScheduler offline (temporizadores, alarmas y recordatorios SQLite)
│       │   ├── logging_setup.py      # Latest Debug.txt, rotación, traductor de errores ES, anonimizador de soporte
│       │   └── system_tools.py       # EarconPlayer, GlobalHotkeyManager (Win32), Doctor, BackupZip, ReleaseUpdater
│       │
│       ├── brain/
│       │   ├── __init__.py
│       │   ├── providers.py          # LLMProvider, GeminiProvider (google-genai), OpenAICompatProvider, AnthropicProvider
│       │   ├── catalog.py            # ModelCatalogService (Antídoto #1), ModelAutoSelector, ProviderPool (EMA + escudo 429)
│       │   ├── memory.py             # MemoryManager (Capa 1 episódica + Capa 2 declarativa SQLite WAL + Deshacer)
│       │   ├── personality.py        # ProfileManager (5 perfiles editables + tonos + system prompt composer)
│       │   ├── cost_guard.py         # Conteo nube vs. Fast-Path (0 tokens) + degradación DEEP->FAST
│       │   └── orchestrator.py       # IntentRouter, órdenes encadenadas, integración SafetyGate y Dry-Run
│       │
│       ├── modules/
│       │   ├── __init__.py
│       │   ├── pc/                   # Rebanada 2
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # PCModule (BaseModule)
│       │   │   ├── apps.py           # AppScanner (shell:AppsFolder + App Paths + UserChoice + fonética + cierre inteligente)
│       │   │   ├── audio_ctrl.py     # Volumen maestro/por app (pycaw en ComStaThread) + brillo (WMI/DDC-CI)
│       │   │   ├── windows.py        # Gestión multi-monitor DPI-aware
│       │   │   ├── files.py          # Búsqueda, organización con Undo en action_audit, send2trash
│       │   │   ├── clipboard.py      # Portapapeles inteligente (bloqueado en Modo Privacidad)
│       │   │   ├── telemetry.py      # CPU/RAM/disco/batería (psutil) + GPU NVIDIA (nvidia-ml-py) sin UAC
│       │   │   └── routines.py       # Gestor de Rutinas + "Capturar estado actual"
│       │   │
│       │   ├── network/              # Rebanada 3
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # NetworkModule
│       │   │   ├── discovery.py      # Descubrimiento híbrido mDNS/SSDP + sondeo TCP/HTTP sobre ARP + MAC en cascada
│       │   │   ├── store.py          # DeviceAliasStore (SQLite + tokens aislados en .env)
│       │   │   ├── adapters.py       # Interfaz NetworkDeviceAdapter + contratos futuros (LG, Android TV, luces, HA)
│       │   │   ├── samsung.py        # Samsung Tizen WS + Wake-on-LAN
│       │   │   ├── cast.py           # Chromecast / Google TV (pychromecast)
│       │   │   ├── miracast.py       # Win+K UIA en ComStaThread + DisplaySwitch.exe + cadena WoL
│       │   │   ├── youtube_tv.py     # YouTube Lounge API (httpx) + ActiveBrowserUrlReader (UIA en ComStaThread)
│       │   │   └── xbox.py           # Xbox SmartGlass UDP :5050
│       │   │
│       │   ├── voice/                # Rebanada 4
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # VoiceModule
│       │   │   ├── dsp.py            # FarFieldAudioConditioner (>85 Hz, AGC np.tanh, VAD 300-3400 Hz)
│       │   │   ├── voiceprint.py     # OwnerVoiceprintGuard (26D NumPy puro, calibración 6 s)
│       │   │   ├── context_gate.py   # Escudo contextual (is_multimedia_active + is_noise_or_third_person + En Llamada)
│       │   │   ├── ducking.py        # SelectiveAudioDucker (pycaw en ComStaThread, excluye llamadas)
│       │   │   ├── stt.py            # Vosk ES pre-empaquetado + PTT Win32 GetAsyncKeyState + ruta híbrida multimodal
│       │   │   └── tts.py            # Edge-TTS (QtMultimedia WordBoundary) + SAPI fallback (ComStaThread) + barge-in
│       │   │
│       │   ├── intel/                # Rebanada 5
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # IntelModule
│       │   │   ├── web_search.py     # Gemini Search / DuckDuckGo + trafilatura + apertura de links en navegador
│       │   │   ├── weather.py        # Open-Meteo (solo tier gratuito, caché 15 min) + divisas/hora/unidades
│       │   │   ├── news_rss.py       # Google News RSS hiper-contextual (ubicación + Memoria Adaptativa)
│       │   │   └── extractors.py     # URL, PDF (pypdf), TXT/MD, YouTube transcript y portapapeles
│       │   │
│       │   ├── vision/               # Rebanada 5
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # VisionModule
│       │   │   ├── capture.py        # Captura multi-monitor DPI-aware (mss + Pillow) + contrato de envío
│       │   │   └── live_guide.py     # Modo Guía Live por turnos (0 capturas y 0 tokens en pausa)
│       │   │
│       │   ├── music/                # Rebanada 7
│       │   │   ├── __init__.py
│       │   │   ├── module.py         # MusicModule + emisión PlaylistBridgeEvent
│       │   │   ├── smtc.py           # Nivel 0 Windows SMTC (winrt) + fallback teclas multimedia Win32
│       │   │   ├── local_library.py  # Indexador offline mutagen (.mp3/.flac/.wav/.ogg/.m4a/.opus)
│       │   │   ├── spotify.py        # Spotify OAuth 2.0 PKCE (:8888) Premium + fallback Free + Liked Songs
│       │   │   ├── tidal.py          # TIDAL OAuth 2.0 PKCE oficial + URI/SMTC
│       │   │   └── streaming.py      # YouTube Music (Google OAuth + SMTC) y Apple Music (URI music:// + SMTC)
│       │   │
│       │   ├── workspace/            # Rebanadas 6 (OAuth base) y 7 (Workspace)
│       │   │   ├── __init__.py
│       │   │   ├── oauth_server.py   # Servidor local 127.0.0.1 (callback OAuth PKCE + Setup Sync desde GitHub Pages)
│       │   │   ├── google_auth.py    # Flujo OAuth 2.0 PKCE Google + refresh tokens en secrets/
│       │   │   ├── gmail.py          # Gmail (lectura, búsqueda, resúmenes, borradores; envío en SafetyGate)
│       │   │   ├── calendar.py       # Google Calendar CRUD (borrado en SafetyGate)
│       │   │   ├── tasks.py          # Google Tasks CRUD (borrado en SafetyGate)
│       │   │   ├── drive_docs.py     # Google Drive búsqueda/descarga + creación encadenada de Google Docs
│       │   │   └── module.py         # WorkspaceModule
│       │   │
│       │   ├── discord/              # Rebanada 8
│       │   │   ├── __init__.py
│       │   │   ├── gateway.py        # DiscordGatewayService compartido (1 bot por usuario + decoder Base64 invite)
│       │   │   ├── audio_pipeline.py # DAVE E2EE + yt-dlp búfer rodante 3 pistas + MeteredPCMVolumeTransformer (RMS)
│       │   │   ├── chat_module.py    # DiscordChatModule (avisos, control remoto Dueño, !aprobar/!rechazar/!pendientes)
│       │   │   └── music_module.py   # DiscordMusicModule (!play.. !help, rangos Dueño/DJ/Invitado, !shuffle manual)
│       │   │
│       │   ├── whatsapp/             # Rebanada 8
│       │   │   ├── __init__.py
│       │   │   ├── ipc_client.py     # Cliente IPC hacia Node.js + Baileys en USER_DATA_DIR/runtimes/whatsapp
│       │   │   └── module.py         # WhatsAppModule (QR, solo mensajes nuevos, shouldIgnoreJid, audio->Gemini)
│       │   │
│       │   └── bridge/               # Rebanada 9 (servidor) + Rebanada 10 (APK)
│       │       ├── __init__.py
│       │       ├── protocol.py       # Contrato JSON WebSocket v1 (comandos, capturas, SafetyGate, Health Connect)
│       │       ├── ws_server.py      # Servidor WebSocket LAN autenticado por token de emparejamiento
│       │       ├── qr.py             # Generador QR de emparejamiento (sin ADB)
│       │       └── module.py         # BridgeModule
│       │
│       └── ui/
│           ├── __init__.py
│           ├── theme.py              # Design tokens Fluent + motor QSS dinámico + Mica Win11 + acento por perfil
│           ├── bridge_qt.py          # Único puente thread-safe EventBus <-> Qt Signals
│           ├── tray.py               # Bandeja del sistema + menú rápido + cierre limpio
│           ├── onboarding.py         # Asistente inicial de 4 pasos + Smart Clipboard Catcher
│           ├── components/
│           │   ├── __init__.py
│           │   ├── cards.py          # FluentCard, SettingRow, SettingExpander (cero QGroupBox apilados)
│           │   ├── toggle_switch.py  # ToggleSwitch animado Windows 11
│           │   ├── safety_card.py    # Tarjeta inline física de confirmación SafetyGate con cuenta regresiva
│           │   └── chat_bubble.py    # Burbujas limpias, fuentes clickeables, píldora memoria ("Deshacer")
│           ├── main_window/
│           │   ├── __init__.py
│           │   ├── shell.py          # MainWindow con Sidebar colapsable (220px <-> 64px) y pie CostGuard
│           │   ├── chat_view.py      # Historial completo + cabecera limpia + silenciar voz
│           │   ├── modules_view.py   # Cuadrícula de tarjetas Zero-Base + panel lateral deslizable (drawer)
│           │   ├── routines_view.py  # Temporizadores activos + tarjetas de Rutinas + "Capturar estado actual"
│           │   ├── memory_view.py    # Búsqueda instantánea, filtros por categoría, pin y edición
│           │   ├── integrations_view.py # OAuth 1-clic, Discord, WhatsApp QR, tabla LAN + mini controles, QR Móvil
│           │   ├── settings_view.py  # Ajustes buscables + interruptor Modo Desarrollador (Dry-Run, .env, overrides)
│           │   └── doctor_logs_view.py # Doctor con semáforos + Latest Debug.txt + "Copiar reporte para soporte"
│           ├── minichat/
│           │   ├── __init__.py
│           │   └── window.py         # Mini-Chat compacto (Alt+Espacio) enfocado en comandos, SafetyGate y Visión
│           └── orb/
│               ├── __init__.py
│               ├── window.py         # FloatingOrbWindow click-through (oculto en IDLE/PRIVACY, preview 4 s)
│               ├── renderers.py      # 5 renderers intercambiables (Reactor, Ondas, Esfera, Barras, Avatar/GIF)
│               └── subtitle_holobox.py # Subtítulos palabra por palabra (máx. 14 palabras / 85 chars)
│
└── tests/
    ├── conftest.py                   # Fixtures aislamiento USER_DATA_DIR, AsyncCoreThread test, qapp/qtbot cleanup
    ├── unit/                         # Tests offline rápidos (-m "not live")
    ├── ui/                           # Tests pytest-qt estrictos (-m "not live")
    └── live/                         # Smoke tests opt-in contra Windows 11/APIs/LAN reales (-m live)

```

---

## 2. Contratos Python Exactos del Núcleo

Todos los archivos del proyecto comienzan con `from __future__ import annotations` y deben pasar `uv run mypy src` y `uv run ruff check src tests scripts` con **0 errores**.

### 2.1 Catálogo Único de Permisos (`src/deusex/core/permissions.py`) — Antídoto #3

Cero strings mágicos con punto (`"pc.basic"` está prohibido). Este `StrEnum` es la única fuente de verdad compartida entre `DeusExSettings`, `ToolSpec`, `FastPathRule`, `BaseModule` y la UI.

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    # PC Local
    PC_BASIC = "pc_basic"                 # Abrir apps, volumen, brillo, ventanas, telemetría, Win+L
    PC_CLIPBOARD = "pc_clipboard"         # Leer/escribir portapapeles (bloqueado en Modo Privacidad)
    PC_FILES = "pc_files"                 # Buscar, organizar (Undo) y papelera
    PC_ADVANCED = "pc_advanced"           # Cerrar apps productivas, energía (apagar/reiniciar/suspender)

    # Red Local
    NETWORK_DISCOVERY = "network_discovery"  # mDNS/SSDP + sondeo ARP/TCP + MAC
    NETWORK_CONTROL = "network_control"      # Samsung TV, Cast, Miracast, YouTube TV, Xbox, WoL

    # Voz y Audio
    VOICE_LISTEN = "voice_listen"         # Micrófono, Vosk Wake-Word, PTT, biometría vocal
    VOICE_SPEAK = "voice_speak"           # Edge-TTS, SAPI, Audio Ducking

    # Inteligencia Web y Visión
    INTEL_WEB = "intel_web"               # Búsqueda web, Open-Meteo, RSS, extractores, abrir links
    VISION_CAPTURE = "vision_capture"     # Captura puntual multi-monitor y Guía Live
    VISION_SHARE = "vision_share"         # Enviar capturas a Discord, WhatsApp o APK móvil

    # Música
    MUSIC_LOCAL = "music_local"           # Windows SMTC y biblioteca local mutagen
    MUSIC_STREAMING = "music_streaming"   # Spotify, TIDAL, YouTube Music, Apple Music URI

    # Google Workspace
    WORKSPACE_GMAIL_READ = "workspace_gmail_read"
    WORKSPACE_GMAIL_SEND = "workspace_gmail_send"
    WORKSPACE_CALENDAR = "workspace_calendar"
    WORKSPACE_TASKS = "workspace_tasks"
    WORKSPACE_DRIVE_DOCS = "workspace_drive_docs"

    # Discord
    DISCORD_CHAT = "discord_chat"         # Avisos y mensajes del bot
    DISCORD_REMOTE = "discord_remote"     # Control remoto del PC y aprobaciones SafetyGate (Dueño)
    DISCORD_MUSIC = "discord_music"       # Canal de voz DAVE E2EE y cola musical

    # WhatsApp
    WHATSAPP_READ = "whatsapp_read"       # Lectura/resumen de mensajes y audios nuevos
    WHATSAPP_SEND = "whatsapp_send"       # Envío saliente (siempre bajo SafetyGate)

    # Puente Móvil y Smartwatch (Cero ADB)
    BRIDGE_MOBILE = "bridge_mobile"       # WebSocket LAN con DeusEx Companion.apk
    BRIDGE_HEALTH = "bridge_health"       # Recepción de telemetría Health Connect


@dataclass(frozen=True, slots=True)
class PermissionMeta:
    permission: Permission
    module_id: str
    label_es: str
    description_es: str
    default_enabled: bool = True
    sensitive: bool = False

```

### 2.2 Contratos de Módulos, Herramientas y Fast-Path (`src/deusex/core/contracts.py`)

Un único `ToolResult` canónico (prohibidas subclases locales o shims `_construct_compat`).

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import TYPE_CHECKING, Any

from deusex.core.permissions import Permission

if TYPE_CHECKING:
    from deusex.core.config import DeusExSettings, SecretsManager
    from deusex.core.db import Database
    from deusex.core.events import EventBus
    from deusex.core.paths import AppPaths
    from deusex.core.runtime import RuntimeServices


class RiskLevel(StrEnum):
    SAFE = "safe"           # Ejecución directa
    MODERATE = "moderate"   # Ejecución directa con registro en action_audit
    CRITICAL = "critical"   # Freno físico obligatorio en SafetyGate (salvo excepción explícita de mensajería)


class ModuleState(StrEnum):
    DISABLED = "disabled"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPING = "stopping"


class DiagnosticSeverity(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DiagnosticItem:
    code: str
    area: str
    title: str
    severity: DiagnosticSeverity
    message: str
    remediation: str = ""
    auto_fixable: bool = False


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    spoken_text: str | None = None
    sources: tuple[dict[str, str], ...] = ()
    audit_id: str | None = None
    dry_run: bool = False
    elapsed_ms: float = 0.0
    requires_confirmation: bool = False
    confirmation_id: str | None = None


type ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]
type FastPathHandler = Callable[[re.Match[str], str], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    module_id: str
    description: str
    parameters_schema: dict[str, Any]
    permission: Permission | None
    risk_level: RiskLevel
    handler: ToolHandler
    blocked_in_privacy: bool = False
    supports_dry_run: bool = True


@dataclass(frozen=True, slots=True)
class FastPathRule:
    rule_id: str
    module_id: str
    priority: int
    pattern: re.Pattern[str]
    permission: Permission | None
    risk_level: RiskLevel
    handler: FastPathHandler
    blocked_in_privacy: bool = False
    earcon: str | None = "confirm"


@dataclass(frozen=True, slots=True)
class ModuleContext:
    paths: AppPaths
    settings: DeusExSettings
    secrets: SecretsManager
    db: Database
    events: EventBus
    runtime: RuntimeServices


class BaseModule(ABC):
    module_id: str
    display_name: str
    description: str
    required_permissions: tuple[Permission, ...] = ()

    def __init__(self) -> None:
        self._state: ModuleState = ModuleState.DISABLED
        self._last_error: str | None = None

    @property
    def state(self) -> ModuleState:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def set_state(self, state: ModuleState, error: str | None = None) -> None:
        self._state = state
        self._last_error = error

    @abstractmethod
    async def start(self, ctx: ModuleContext) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    def get_tools(self) -> list[ToolSpec]:
        return []

    def get_fast_path_rules(self) -> list[FastPathRule]:
        return []

    def diagnose(self) -> list[DiagnosticItem]:
        return []

```

### 2.3 EventBus con Enrutamiento Estricto de Hilos (`src/deusex/core/events.py`) — Antídoto #5

Regla arquitectónica inquebrantable:

* Ningún componente puede llamar `asyncio.create_task()` desde `QtMainThread`, `ComStaThread` ni `AudioInputThread`.
* `EventBus` recibe el `asyncio.AbstractEventLoop` de `AsyncCoreThread` durante el arranque.
* `publish(event)` es 100% thread-safe desde cualquiera de los 4 hilos:
* Los suscriptores `async def` se encolan **siempre** en `AsyncCoreThread` mediante `asyncio.run_coroutine_threadsafe(...)`.
* Los suscriptores síncronos reciben el evento de forma segura sin asumir un event loop activo; la UI se suscribe únicamente a través de `QtEventBridge` (que reemite hacia `QtMainThread` vía `Signal`).
* Los errores de un suscriptor se aíslan y registran sin tumbar el bus ni otros suscriptores.



```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar
import uuid


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


E = TypeVar("E", bound=DomainEvent)
type SyncHandler[T: DomainEvent] = Callable[[T], None]
type AsyncHandler[T: DomainEvent] = Callable[[T], Awaitable[None]]


class EventBus:
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...
    def unbind_loop(self) -> None: ...
    def subscribe(self, event_type: type[E], handler: SyncHandler[E] | AsyncHandler[E]) -> Callable[[], None]: ...
    def publish(self, event: DomainEvent) -> None: ...
    async def publish_and_wait(self, event: DomainEvent, timeout: float = 5.0) -> None: ...

```

### 2.4 Configuración Unificada (`src/deusex/core/config.py`)

Un único `USER_DATA_DIR/settings.json` validado por Pydantic v2. Todos los secretos viven exclusivamente en `USER_DATA_DIR/.env` (escritura atómica `.tmp` + `os.replace`) y tokens OAuth en `USER_DATA_DIR/secrets/`.

```python
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

from deusex.core.permissions import Permission


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
    daily_token_budget: int = 0           # 0 = sin tope
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
    tts_rate: float = 1.0
    voice_by_profile: dict[str, str] = Field(default_factory=dict)
    follow_up_window_sec: float = 6.0
    voiceprint_enabled: bool = False
    ducking_level_pct: int = 22
    in_call_text_only: bool = False


class ModulesToggleSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Zero-Base en primer arranque: todos los enchufables en False (LocalScheduler vive en core).
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


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    safety_timeout_sec: int = 45
    allow_trusted_messaging_bypass: bool = False  # Destructivas de PC SIEMPRE en SafetyGate


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
    oauth_callback_port: int = 8888
    bridge_ws_port: int = 8765


class DeusExSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")
    schema_version: int = 1
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    ui: UISettings = Field(default_factory=UISettings)
    brain: BrainSettings = Field(default_factory=BrainSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    modules: ModulesToggleSettings = Field(default_factory=ModulesToggleSettings)
    permissions: dict[Permission, bool] = Field(default_factory=dict)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    developer: DeveloperSettings = Field(default_factory=DeveloperSettings)

```

### 2.5 Reconciliación de Arranque y Catálogo de Modelos (Antídotos #1 y #2)

* **Antídoto #1 (`ModelCatalogService.refresh(provider_id: str)`):**
* Firma estricta: `async def refresh(self, provider_id: str, *, force: bool = False) -> list[ModelInfo]`.
* Resuelve internamente `provider = self._pool.get_provider(provider_id)` antes de llamar a `await provider.list_models()`. Jamás recibe un objeto proveedor desde la capa UI/App ni asume duck-typing sobre `str`.


* **Antídoto #2 (`DeusExApplication.start()`):**
* Tras inicializar `Database`, `RuntimeServices` y `EventBus`, itera sobre `settings.modules.model_dump()`: para todo `module_id` con valor `True`, llama a `await self.engine.enable_module(module_id)`, lo cual invoca `module.start(ctx)`, registra sus `ToolSpec` y carga sus `FastPathRule` en `FastPathEngine`.
* Si un módulo falla al reconciliarse, pasa a `ModuleState.ERROR` con diagnóstico legible sin abortar el arranque de la aplicación.



---

## 3. Esquema SQLite WAL v1 (`src/deusex/core/db.py`)

Ubicación: `USER_DATA_DIR/deusex.db`. Acceso serializado mediante `threading.RLock`, conexión única con `isolation_level=None` y transacciones explícitas (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`).

### 3.1 Pragmas Obligatorios

```sql
-- Al abrir la conexión:
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;

-- En el cierre determinista (<3 s):
PRAGMA wal_checkpoint(TRUNCATE);

```

### 3.2 Tablas de Migración v1

| Tabla | Propósito | Columnas Clave |
| --- | --- | --- |
| `schema_migrations` | Control de versiones SQLite | `version INTEGER PRIMARY KEY`, `description TEXT NOT NULL`, `applied_at TEXT NOT NULL` |
| `conversations` | Hilos de chat (Capa 1) | `id TEXT PRIMARY KEY`, `title TEXT NOT NULL`, `profile_id TEXT NOT NULL`, `created_at TEXT`, `updated_at TEXT` |
| `messages` | Mensajes persistidos | `id TEXT PRIMARY KEY`, `conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE`, `role TEXT`, `content TEXT`, `model_used TEXT`, `fast_path INTEGER`, `sources_json TEXT`, `tokens_in INTEGER`, `tokens_out INTEGER`, `created_at TEXT` |
| `memories` | Memoria Adaptativa (Capa 2) | `id TEXT PRIMARY KEY`, `category TEXT` (`fact`/`preference`/`interest`/`contact`), `key TEXT`, `value TEXT`, `confidence REAL`, `pinned INTEGER`, `source_message_id TEXT`, `created_at TEXT`, `updated_at TEXT` |
| `scheduled_jobs` | Temporizadores y alarmas offline | `id TEXT PRIMARY KEY`, `kind TEXT` (`timer`/`alarm`/`reminder`), `label TEXT`, `fire_at TEXT`, `recurrence TEXT`, `payload_json TEXT`, `status TEXT`, `created_at TEXT` |
| `routines` | Modos y Rutinas (incl. estado capturado) | `id TEXT PRIMARY KEY`, `name TEXT UNIQUE`, `trigger_phrase TEXT`, `icon TEXT`, `steps_json TEXT`, `enabled INTEGER`, `created_at TEXT`, `updated_at TEXT` |
| `action_audit` | Auditoría y planes de Undo (1 clic) | `id TEXT PRIMARY KEY`, `tool_name TEXT`, `module_id TEXT`, `risk_level TEXT`, `status TEXT`, `args_json TEXT`, `undo_plan_json TEXT`, `undone_at TEXT`, `created_at TEXT` |
| `device_aliases` | Dispositivos LAN (cero tokens aquí) | `device_uid TEXT PRIMARY KEY`, `alias TEXT`, `device_type TEXT`, `last_ip TEXT`, `mac_address TEXT`, `port INTEGER`, `metadata_json TEXT`, `updated_at TEXT` |
| `usage_ledger` | CostGuard diario (Fast-Path vs. Nube) | `day_utc TEXT`, `provider_id TEXT`, `model_id TEXT`, `fast_path_hits INTEGER`, `cloud_calls INTEGER`, `tokens_in INTEGER`, `tokens_out INTEGER`, `PRIMARY KEY (day_utc, provider_id, model_id)` |
| `model_catalog_cache` | Caché TTL 24 h de modelos reales | `provider_id TEXT PRIMARY KEY`, `models_json TEXT NOT NULL`, `fetched_at TEXT NOT NULL` |
| `local_media_index` | Biblioteca local offline (`mutagen`) | `path TEXT PRIMARY KEY`, `title TEXT`, `artist TEXT`, `album TEXT`, `duration_sec REAL`, `mtime REAL`, `indexed_at TEXT` |
| `paired_devices` | Dispositivos APK móvil emparejados | `device_id TEXT PRIMARY KEY`, `device_name TEXT`, `platform TEXT`, `token_hash TEXT`, `last_seen_at TEXT`, `created_at TEXT` |

Índices v1: `idx_messages_conv_created`, `idx_memories_cat_pinned`, `idx_scheduled_jobs_status_fire`, `idx_action_audit_created`.

---

## 4. Convenciones de Pruebas (Offline vs. `@pytest.mark.live`) y Reglas Antigravity

### 4.1 Clasificación de Tests (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = [
    "live: smoke tests reales contra Windows 11, APIs externas o LAN (opt-in con -m live)",
    "ui: pruebas de interfaz gráfica con pytest-qt",
]

```

| Suite | Comando Exacto | Reglas |
| --- | --- | --- |
| **Offline (Compuerta 3)** | `uv run pytest -v -m "not live"` | 0 red externa, 0 claves reales, `USER_DATA_DIR` aislado en `tmp_path`. **Prohibido usar `object.__setattr__**` para burlar tipos o permisos. |
| **UI (`pytest-qt`)** | Incluida en offline | Usar exclusivamente fixtures `qapp`/`qtbot`. **Prohibido** instanciar `QApplication([])` a mano. Limpiar todo widget con `widget.close(); widget.deleteLater(); qapp.processEvents()`. |
| **En Vivo (Compuerta 4)** | `uv run pytest -v -m live -k slice_N` | Opt-in. Usa el Windows 11 real, APIs reales y LAN real leyendo credenciales desde `.env`/entorno del usuario. Jamás imprime tokens, IPs privadas ni rutas personales en stdout/logs. |
| **Calidad Estática** | `uv run ruff check src tests scripts` y `uv run mypy src` | Salida obligatoria en código `0` antes de pasar a la Compuerta 4. |

### 4.2 Regla de Ejecución en Windows 11 para Google Antigravity

* Todo comando PowerShell (`uv sync`, `pytest`, `ruff`, `mypy`, `scripts/setup_runtimes.py`) se ejecuta **en primer plano con bypass de sandbox** para evitar bloqueos de `ProactorEventLoop` y sockets locales en Windows 11.
* Cero IPs, MACs, teléfonos, tokens ni rutas `C:\Users\...` hardcodeadas en código fuente, scripts o tests.
* Modificaciones sobre archivos existentes mediante **parches quirúrgicos incrementales**; nunca sobrescribir archivos largos completos ni dejar archivos temporales `_entrega_*.md`.

---

## 5. Mapa de Runtimes Pre-Empaquetados (`scripts/setup_runtimes.py`)

El repositorio Git permanece liviano (sin binarios pesados), pero `scripts/setup_runtimes.py` prepara el entorno de desarrollo y el empaquetador `Inno Setup` distribuye los mismos artefactos bajo `USER_DATA_DIR`:

1. **Vosk ES (`vosk-model-small-es-0.42`):** `USER_DATA_DIR/models/vosk-es/`
2. **FFmpeg Portable (Windows x64 estático):** `USER_DATA_DIR/runtimes/ffmpeg/bin/ffmpeg.exe`
3. **Node.js Portable LTS + `@whiskeysockets/baileys`:** `USER_DATA_DIR/runtimes/whatsapp/` (aislado, 0 procesos en ejecución mientras `modules.whatsapp` sea `False`).
