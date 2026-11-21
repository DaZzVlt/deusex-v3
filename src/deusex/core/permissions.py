from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    """Único catálogo tipado de permisos de DeusEx v3 (cero strings mágicos con punto)."""

    # PC Local
    PC_BASIC = "pc_basic"
    PC_CLIPBOARD = "pc_clipboard"
    PC_FILES = "pc_files"
    PC_ADVANCED = "pc_advanced"

    # Red Local
    NETWORK_DISCOVERY = "network_discovery"
    NETWORK_CONTROL = "network_control"

    # Voz y Audio
    VOICE_LISTEN = "voice_listen"
    VOICE_SPEAK = "voice_speak"

    # Inteligencia Web y Visión
    INTEL_WEB = "intel_web"
    VISION_CAPTURE = "vision_capture"
    VISION_SHARE = "vision_share"

    # Música
    MUSIC_LOCAL = "music_local"
    MUSIC_STREAMING = "music_streaming"

    # Google Workspace
    WORKSPACE_GMAIL_READ = "workspace_gmail_read"
    WORKSPACE_GMAIL_SEND = "workspace_gmail_send"
    WORKSPACE_CALENDAR = "workspace_calendar"
    WORKSPACE_TASKS = "workspace_tasks"
    WORKSPACE_DRIVE_DOCS = "workspace_drive_docs"

    # Discord
    DISCORD_CHAT = "discord_chat"
    DISCORD_REMOTE = "discord_remote"
    DISCORD_MUSIC = "discord_music"

    # WhatsApp
    WHATSAPP_READ = "whatsapp_read"
    WHATSAPP_SEND = "whatsapp_send"

    # Puente Móvil y Smartwatch (Cero ADB)
    BRIDGE_MOBILE = "bridge_mobile"
    BRIDGE_HEALTH = "bridge_health"


@dataclass(frozen=True, slots=True)
class PermissionMeta:
    permission: Permission
    module_id: str
    label_es: str
    description_es: str
    default_enabled: bool = True
    sensitive: bool = False


PERMISSION_CATALOG: dict[Permission, PermissionMeta] = {
    Permission.PC_BASIC: PermissionMeta(
        permission=Permission.PC_BASIC,
        module_id="pc",
        label_es="Control básico del PC",
        description_es="Abrir apps, ajustar volumen, brillo, ventanas, telemetría y bloquear pantalla.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.PC_CLIPBOARD: PermissionMeta(
        permission=Permission.PC_CLIPBOARD,
        module_id="pc",
        label_es="Portapapeles inteligente",
        description_es="Leer y escribir el portapapeles (bloqueado automáticamente en Modo Privacidad).",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.PC_FILES: PermissionMeta(
        permission=Permission.PC_FILES,
        module_id="pc",
        label_es="Gestión de archivos",
        description_es="Buscar, organizar carpetas con deshacer en 1 clic y gestionar la papelera.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.PC_ADVANCED: PermissionMeta(
        permission=Permission.PC_ADVANCED,
        module_id="pc",
        label_es="Control avanzado y energía",
        description_es="Cerrar aplicaciones productivas y apagar, reiniciar o suspender el equipo.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.NETWORK_DISCOVERY: PermissionMeta(
        permission=Permission.NETWORK_DISCOVERY,
        module_id="network",
        label_es="Escaneo de red local",
        description_es="Descubrir televisores, Chromecast y consolas en la red local.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.NETWORK_CONTROL: PermissionMeta(
        permission=Permission.NETWORK_CONTROL,
        module_id="network",
        label_es="Control de TV y dispositivos",
        description_es="Encender por WoL, proyectar por Miracast y controlar TV, Cast y Xbox.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.VOICE_LISTEN: PermissionMeta(
        permission=Permission.VOICE_LISTEN,
        module_id="voice",
        label_es="Escucha por micrófono",
        description_es="Detección offline de palabra clave (Vosk), Push-to-Talk y biometría vocal.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.VOICE_SPEAK: PermissionMeta(
        permission=Permission.VOICE_SPEAK,
        module_id="voice",
        label_es="Respuesta por voz",
        description_es="Síntesis de voz neural, subtítulos sincronizados y atenuación de música.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.INTEL_WEB: PermissionMeta(
        permission=Permission.INTEL_WEB,
        module_id="intel",
        label_es="Búsqueda e inteligencia web",
        description_es="Consultar clima, noticias, buscar en la web, abrir enlaces y leer PDFs/YouTube.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.VISION_CAPTURE: PermissionMeta(
        permission=Permission.VISION_CAPTURE,
        module_id="vision",
        label_es="Captura y análisis de pantalla",
        description_es="Capturas puntuales y Modo Guía Live por turnos (bloqueado en Modo Privacidad).",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.VISION_SHARE: PermissionMeta(
        permission=Permission.VISION_SHARE,
        module_id="vision",
        label_es="Compartir capturas",
        description_es="Enviar capturas de pantalla a Discord, WhatsApp o la app móvil.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.MUSIC_LOCAL: PermissionMeta(
        permission=Permission.MUSIC_LOCAL,
        module_id="music",
        label_es="Control multimedia local",
        description_es="Controlar reproducción de Windows (SMTC) e indexar biblioteca musical local.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.MUSIC_STREAMING: PermissionMeta(
        permission=Permission.MUSIC_STREAMING,
        module_id="music",
        label_es="Servicios de streaming",
        description_es="Conectar y reproducir desde Spotify, TIDAL, YouTube Music y Apple Music.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.WORKSPACE_GMAIL_READ: PermissionMeta(
        permission=Permission.WORKSPACE_GMAIL_READ,
        module_id="workspace",
        label_es="Leer y resumir Gmail",
        description_es="Buscar, leer correos y crear borradores en tu cuenta de Google.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.WORKSPACE_GMAIL_SEND: PermissionMeta(
        permission=Permission.WORKSPACE_GMAIL_SEND,
        module_id="workspace",
        label_es="Enviar correos por Gmail",
        description_es="Enviar y responder correos (protegido por confirmación SafetyGate).",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.WORKSPACE_CALENDAR: PermissionMeta(
        permission=Permission.WORKSPACE_CALENDAR,
        module_id="workspace",
        label_es="Google Calendar",
        description_es="Consultar, crear, editar y eliminar eventos en tu calendario.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.WORKSPACE_TASKS: PermissionMeta(
        permission=Permission.WORKSPACE_TASKS,
        module_id="workspace",
        label_es="Google Tasks",
        description_es="Consultar, crear, completar y borrar pendientes.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.WORKSPACE_DRIVE_DOCS: PermissionMeta(
        permission=Permission.WORKSPACE_DRIVE_DOCS,
        module_id="workspace",
        label_es="Google Drive y Docs",
        description_es="Buscar archivos en Drive y crear documentos en Google Docs.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.DISCORD_CHAT: PermissionMeta(
        permission=Permission.DISCORD_CHAT,
        module_id="discord_chat",
        label_es="Avisos y chat de Discord",
        description_es="Enviar notificaciones, recordatorios y mensajes a través de tu bot privado.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.DISCORD_REMOTE: PermissionMeta(
        permission=Permission.DISCORD_REMOTE,
        module_id="discord_chat",
        label_es="Control remoto por Discord",
        description_es="Ejecutar órdenes del PC y aprobar acciones críticas remotamente (solo Dueño).",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.DISCORD_MUSIC: PermissionMeta(
        permission=Permission.DISCORD_MUSIC,
        module_id="discord_music",
        label_es="Música en voz de Discord",
        description_es="Conectar tu bot a canales de voz DAVE E2EE y gestionar la cola musical.",
        default_enabled=True,
        sensitive=False,
    ),
    Permission.WHATSAPP_READ: PermissionMeta(
        permission=Permission.WHATSAPP_READ,
        module_id="whatsapp",
        label_es="Lectura y resumen de WhatsApp",
        description_es="Leer mensajes nuevos y transcribir notas de voz recibidas.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.WHATSAPP_SEND: PermissionMeta(
        permission=Permission.WHATSAPP_SEND,
        module_id="whatsapp",
        label_es="Envío de mensajes WhatsApp",
        description_es="Enviar mensajes o capturas por WhatsApp (protegido por SafetyGate).",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.BRIDGE_MOBILE: PermissionMeta(
        permission=Permission.BRIDGE_MOBILE,
        module_id="bridge",
        label_es="App móvil (DeusEx Companion)",
        description_es="Vincular por QR en Wi-Fi local sin ADB para control remoto y capturas.",
        default_enabled=True,
        sensitive=True,
    ),
    Permission.BRIDGE_HEALTH: PermissionMeta(
        permission=Permission.BRIDGE_HEALTH,
        module_id="bridge",
        label_es="Telemetría Smartwatch (Health Connect)",
        description_es="Recibir pasos, frecuencia cardíaca y sueño desde tu teléfono emparejado.",
        default_enabled=True,
        sensitive=True,
    ),
}


def get_permission_meta(permission: Permission) -> PermissionMeta:
    """Devuelve los metadatos oficiales de un permiso tipado."""
    return PERMISSION_CATALOG[permission]


def default_permissions_map() -> dict[Permission, bool]:
    """Genera el mapa completo de permisos con sus valores por defecto."""
    return {perm: meta.default_enabled for perm, meta in PERMISSION_CATALOG.items()}


def permissions_for_module(module_id: str) -> tuple[PermissionMeta, ...]:
    """Devuelve todos los permisos pertenecientes a un módulo concreto."""
    return tuple(meta for meta in PERMISSION_CATALOG.values() if meta.module_id == module_id)
