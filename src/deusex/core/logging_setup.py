from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading

from deusex.core.paths import AppPaths

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(threadName)-16s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_ARCHIVED_SESSIONS = 5
_RING_BUFFER_CAPACITY = 1000

_WINDOWS_USER_PATH_RE = re.compile(r"([A-Za-z]:\\Users\\)([^\\/\s\"']+)", re.IGNORECASE)
_POSIX_USER_PATH_RE = re.compile(r"(/(?:home|Users)/)([^/\s\"']+)")
_SECRET_TOKEN_RE = re.compile(
    r"(?:"
    r"AIza[0-9A-Za-z\-_]{20,}"
    r"|sk-[0-9A-Za-z\-_]{16,}"
    r"|xai-[0-9A-Za-z\-_]{16,}"
    r"|gsk_[0-9A-Za-z\-_]{16,}"
    r"|ya29\.[0-9A-Za-z\-_]+"
    r"|[MN][A-Za-z\d]{23,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"
    r")"
)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_ENV_ASSIGN_RE = re.compile(
    r"((?:API_KEY|TOKEN|SECRET|PASSWORD)\s*[=:]\s*)([^\s,;\"']+)",
    re.IGNORECASE,
)
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3})\b"
)
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\w)\+\d{8,15}\b")


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Entrada estructurada para el visor de Logs en la UI (filtros por píldora)."""

    timestamp: str
    level: str
    thread_name: str
    logger_name: str
    category: str
    message: str
    formatted: str


def _classify_category(logger_name: str, level_name: str) -> str:
    if level_name in ("ERROR", "CRITICAL"):
        return "error"
    lowered = logger_name.lower()
    if "fast_path" in lowered or "engine" in lowered or "scheduler" in lowered:
        return "fast_path"
    if "brain" in lowered or "provider" in lowered or "catalog" in lowered or "orchestrator" in lowered:
        return "ai"
    return "system"


class LogRingBufferHandler(logging.Handler):
    """Buffer circular thread-safe en memoria para la vista de Logs sin leer disco continuamente."""

    def __init__(self, capacity: int = _RING_BUFFER_CAPACITY) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatted = self.format(record)
            entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S"),
                level=record.levelname,
                thread_name=record.threadName or "",
                logger_name=record.name,
                category=_classify_category(record.name, record.levelname),
                message=record.getMessage(),
                formatted=formatted,
            )
            with self._lock:
                self._entries.append(entry)
        except Exception:
            self.handleError(record)

    def snapshot(self, *, category: str | None = None, query: str = "") -> list[LogEntry]:
        needle = query.strip().lower()
        with self._lock:
            items = list(self._entries)
        if category and category != "all":
            items = [item for item in items if item.category == category]
        if needle:
            items = [
                item
                for item in items
                if needle in item.message.lower() or needle in item.logger_name.lower()
            ]
        return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


@dataclass(slots=True)
class LoggingState:
    log_file: Path
    ring_buffer: LogRingBufferHandler
    file_handler: RotatingFileHandler
    console_handler: logging.Handler | None


_ACTIVE_STATE: LoggingState | None = None
_SETUP_LOCK = threading.RLock()


def _archive_previous_latest_log(paths: AppPaths) -> None:
    latest = paths.latest_debug_log
    if not latest.is_file():
        return
    with suppress(OSError):
        if latest.stat().st_size == 0:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        archive_path = paths.logs_dir / f"deusex_session_{stamp}.log"
        latest.replace(archive_path)

    archives = sorted(
        paths.logs_dir.glob("deusex_session_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in archives[_MAX_ARCHIVED_SESSIONS:]:
        with suppress(OSError):
            stale.unlink()


def setup_logging(
    paths: AppPaths,
    *,
    level: int = logging.INFO,
    console: bool = False,
) -> LoggingState:
    """Configura `Latest Debug.txt` con rotación, archivo de sesiones previas y buffer en memoria."""
    with _SETUP_LOCK:
        teardown_logging()
        paths.ensure_user_dirs()
        _archive_previous_latest_log(paths)

        formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

        file_handler = RotatingFileHandler(
            filename=str(paths.latest_debug_log),
            mode="w",
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)

        ring_buffer = LogRingBufferHandler(capacity=_RING_BUFFER_CAPACITY)
        ring_buffer.setFormatter(formatter)
        ring_buffer.setLevel(level)

        console_handler: logging.Handler | None = None
        if console:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            console_handler.setLevel(level)

        root_logger = logging.getLogger("deusex")
        root_logger.setLevel(level)
        root_logger.propagate = False
        root_logger.addHandler(file_handler)
        root_logger.addHandler(ring_buffer)
        if console_handler is not None:
            root_logger.addHandler(console_handler)

        state = LoggingState(
            log_file=paths.latest_debug_log,
            ring_buffer=ring_buffer,
            file_handler=file_handler,
            console_handler=console_handler,
        )
        globals()["_ACTIVE_STATE"] = state
        root_logger.info("Sistema de registro iniciado en Latest Debug.txt")
        return state


def get_logging_state() -> LoggingState | None:
    with _SETUP_LOCK:
        return _ACTIVE_STATE


def teardown_logging() -> None:
    """Cierra handlers activos liberando descriptores de archivo (clave para cierre limpio y tests)."""
    with _SETUP_LOCK:
        state = _ACTIVE_STATE
        globals()["_ACTIVE_STATE"] = None
        root_logger = logging.getLogger("deusex")
        if state is None:
            return
        for handler in (state.file_handler, state.ring_buffer, state.console_handler):
            if handler is None:
                continue
            root_logger.removeHandler(handler)
            with suppress(Exception):
                handler.flush()
            with suppress(Exception):
                handler.close()


def anonymize_log_text(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Anonimiza claves API, rutas personales, IPs LAN, MACs, correos y teléfonos para soporte."""
    sanitized = text
    for secret in extra_secrets:
        token = secret.strip()
        if len(token) >= 4:
            sanitized = sanitized.replace(token, "<REDACTED_SECRET>")

    sanitized = _SECRET_TOKEN_RE.sub("<REDACTED_TOKEN>", sanitized)
    sanitized = _BEARER_RE.sub(r"\1<REDACTED_TOKEN>", sanitized)
    sanitized = _ENV_ASSIGN_RE.sub(r"\1<REDACTED_SECRET>", sanitized)
    sanitized = _WINDOWS_USER_PATH_RE.sub(r"\1<USER>", sanitized)
    sanitized = _POSIX_USER_PATH_RE.sub(r"\1<USER>", sanitized)
    sanitized = _MAC_RE.sub("<REDACTED_MAC>", sanitized)
    sanitized = _PRIVATE_IP_RE.sub("<LAN_IP>", sanitized)
    sanitized = _EMAIL_RE.sub("<REDACTED_EMAIL>", sanitized)
    sanitized = _PHONE_RE.sub("<REDACTED_PHONE>", sanitized)
    return sanitized


def translate_error_es(error: BaseException | str) -> str:
    """Traduce errores técnicos comunes a español claro para usuarios finales."""
    raw = str(error).strip()
    lowered = raw.lower()

    if "429" in lowered or "resource_exhausted" in lowered or "rate limit" in lowered:
        return "El proveedor de IA alcanzó temporalmente su límite de peticiones. Espera unos segundos e inténtalo de nuevo."
    if "api_key_invalid" in lowered or "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "La clave API configurada no es válida o expiró. Revísala en Ajustes → IA y Claves."
    if "403" in lowered or "permission_denied" in lowered or "forbidden" in lowered:
        return "No tienes permiso para realizar esta acción o la cuenta no cuenta con el plan requerido."
    if "503" in lowered or "service unavailable" in lowered or "overloaded" in lowered:
        return "El servicio externo está temporalmente saturado. Reintentaremos automáticamente en unos instantes."
    if isinstance(error, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return "La operación tardó demasiado en responder. Verifica tu conexión o el dispositivo objetivo."
    if isinstance(error, ConnectionError) or "connecterror" in lowered or "getaddrinfo failed" in lowered:
        return "No se pudo conectar a internet o al servicio local. Revisa tu conexión de red."
    if isinstance(error, PermissionError):
        return "Windows denegó el acceso al archivo o recurso solicitado."
    if isinstance(error, FileNotFoundError):
        return "No se encontró el archivo o programa solicitado."
    if not raw:
        return "Ocurrió un error inesperado al ejecutar la acción."
    return f"Ocurrió un problema inesperado: {raw}"
