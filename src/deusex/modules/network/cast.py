from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import threading
from typing import Any
import uuid

from deusex.modules.network.adapters import (
    AdapterActionResult,
    DeviceCapability,
    DeviceType,
    NetworkDeviceAdapter,
    NetworkDeviceRecord,
)

logger = logging.getLogger(__name__)


def guess_cast_mime_type(media_url: str) -> str:
    """Infiere el MIME type estándar para reproducir una URL en Chromecast."""
    lower = media_url.lower().split("?", 1)[0]
    if lower.endswith(".m3u8"):
        return "application/x-mpegURL"
    if lower.endswith(".mpd"):
        return "application/dash+xml"
    guessed, _ = mimetypes.guess_type(lower)
    if guessed:
        return guessed
    return "video/mp4"


def _parse_optional_uuid(raw: str) -> uuid.UUID | None:
    clean = raw.strip().removeprefix("uuid:")
    if not clean:
        return None
    try:
        return uuid.UUID(clean)
    except ValueError:
        return None


class ChromecastAdapter(NetworkDeviceAdapter):
    """
    Adaptador Chromecast / Google TV sobre `pychromecast` con degradación limpia:
    - Reproducción de media por URL, play/pause/stop y control de volumen/silencio.
    - Todas las llamadas bloqueantes de `pychromecast` corren en `asyncio.to_thread`
      con timeouts cortos y desconexión limpia en `close()` (<3 s).
    """

    adapter_id = "chromecast"
    supported_types = (DeviceType.CHROMECAST, DeviceType.ANDROID_TV)
    capabilities = frozenset(
        {
            DeviceCapability.VOLUME,
            DeviceCapability.MUTE,
            DeviceCapability.MEDIA_PLAYBACK,
            DeviceCapability.CAST_MEDIA,
        }
    )

    def __init__(self, *, connect_timeout_sec: float = 3.0) -> None:
        self._connect_timeout_sec = max(1.0, connect_timeout_sec)
        self._lock = threading.RLock()
        self._sessions: dict[str, tuple[str, Any]] = {}

    @staticmethod
    def is_library_available() -> bool:
        try:
            import pychromecast  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_or_connect_sync(self, device: NetworkDeviceRecord) -> Any:
        import pychromecast

        with self._lock:
            cached = self._sessions.get(device.device_uid)
            if cached is not None:
                cached_ip, cached_cast = cached
                if cached_ip == device.last_ip:
                    return cached_cast
                with contextlib.suppress(Exception):
                    cached_cast.disconnect(timeout=0.4)
                self._sessions.pop(device.device_uid, None)

        port = device.port if device.port > 0 else 8009
        dev_uuid = _parse_optional_uuid(str(device.metadata.get("native_id") or "")) or uuid.UUID(int=0)
        model = str(device.metadata.get("model") or "Chromecast")
        host_tuple = (device.last_ip, port, dev_uuid, model, device.alias)

        cast_obj: Any = pychromecast.get_chromecast_from_host(
            host_tuple,
            tries=1,
            retry_wait=0.3,
            timeout=self._connect_timeout_sec,
        )
        cast_obj.wait(timeout=self._connect_timeout_sec)
        with self._lock:
            self._sessions[device.device_uid] = (device.last_ip, cast_obj)
        return cast_obj

    def _drop_session_sync(self, device_uid: str) -> None:
        with self._lock:
            cached = self._sessions.pop(device_uid, None)
        if cached is not None:
            _, cast_obj = cached
            with contextlib.suppress(Exception):
                cast_obj.disconnect(timeout=0.4)

    async def _run_cast_op(
        self,
        device: NetworkDeviceRecord,
        op: Any,
    ) -> AdapterActionResult:
        if not device.last_ip:
            return AdapterActionResult(
                ok=False,
                message=f"No se conoce la IP de {device.alias}. Escanea la red primero.",
                error_code="missing_ip",
            )
        if not self.is_library_available():
            return AdapterActionResult(
                ok=False,
                message="La librería pychromecast no está disponible en este entorno.",
                error_code="dependency_missing",
            )

        def _worker() -> AdapterActionResult:
            try:
                cast_obj = self._get_or_connect_sync(device)
                res: AdapterActionResult = op(cast_obj)
                return res
            except Exception as exc:
                self._drop_session_sync(device.device_uid)
                return AdapterActionResult(
                    ok=False,
                    message=f"No se pudo controlar {device.alias} por Cast: {exc}",
                    error_code="cast_error",
                )

        try:
            result: AdapterActionResult = await asyncio.wait_for(
                asyncio.to_thread(_worker),
                timeout=self._connect_timeout_sec + 2.5,
            )
            return result
        except TimeoutError:
            await asyncio.to_thread(self._drop_session_sync, device.device_uid)
            return AdapterActionResult(
                ok=False,
                message=f"Tiempo de espera agotado al conectar con {device.alias}.",
                error_code="timeout",
            )

    async def set_volume(
        self,
        device: NetworkDeviceRecord,
        *,
        level: int | None = None,
        delta: int | None = None,
        mute: bool | None = None,
    ) -> AdapterActionResult:
        def _op(cast_obj: Any) -> AdapterActionResult:
            if mute is not None:
                cast_obj.set_volume_muted(bool(mute))
                state_txt = "silenciado" if mute else "con sonido"
                return AdapterActionResult(
                    ok=True,
                    message=f"{device.alias} {state_txt}.",
                    data={"muted": bool(mute)},
                )

            if level is not None:
                clamped = max(0, min(100, int(level)))
                cast_obj.set_volume(clamped / 100.0)
                return AdapterActionResult(
                    ok=True,
                    message=f"Volumen de {device.alias} ajustado al {clamped}%.",
                    data={"volume_pct": clamped},
                )

            if delta is not None:
                status = getattr(cast_obj, "status", None)
                current = float(getattr(status, "volume_level", 0.5) or 0.5)
                target = max(0.0, min(1.0, current + (int(delta) / 100.0)))
                cast_obj.set_volume(target)
                pct = round(target * 100)
                return AdapterActionResult(
                    ok=True,
                    message=f"Volumen de {device.alias} ajustado al {pct}%.",
                    data={"volume_pct": pct},
                )

            return AdapterActionResult(
                ok=False,
                message="Debes indicar nivel, cambio relativo o silencio para Cast.",
                error_code="invalid_args",
            )

        return await self._run_cast_op(device, _op)

    async def media_control(
        self,
        device: NetworkDeviceRecord,
        action: str,
        *,
        media_url: str = "",
        title: str = "",
    ) -> AdapterActionResult:
        clean_action = action.strip().lower()

        def _op(cast_obj: Any) -> AdapterActionResult:
            mc = cast_obj.media_controller
            if clean_action in ("play_media", "cast", "load") and media_url:
                mime = guess_cast_mime_type(media_url)
                mc.play_media(media_url, mime, title=title or None)
                with contextlib.suppress(Exception):
                    mc.block_until_active(timeout=2.0)
                return AdapterActionResult(
                    ok=True,
                    message=f"Reproduciendo contenido en {device.alias}.",
                    data={"media_url": media_url, "mime_type": mime},
                )

            if clean_action in ("play", "resume"):
                mc.play()
                return AdapterActionResult(ok=True, message=f"Reproducción reanudada en {device.alias}.")
            if clean_action == "pause":
                mc.pause()
                return AdapterActionResult(ok=True, message=f"Reproducción pausada en {device.alias}.")
            if clean_action == "stop":
                mc.stop()
                return AdapterActionResult(ok=True, message=f"Reproducción detenida en {device.alias}.")
            if clean_action in ("quit", "disconnect"):
                cast_obj.quit_app()
                return AdapterActionResult(ok=True, message=f"Sesión Cast cerrada en {device.alias}.")

            return AdapterActionResult(
                ok=False,
                message=f"Acción multimedia no soportada en {device.alias}: {action}",
                error_code="unsupported_action",
            )

        return await self._run_cast_op(device, _op)

    async def close(self) -> None:
        def _close_all() -> None:
            with self._lock:
                sessions = list(self._sessions.values())
                self._sessions.clear()
            for _, cast_obj in sessions:
                with contextlib.suppress(Exception):
                    cast_obj.disconnect(timeout=0.35)

        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(asyncio.to_thread(_close_all), timeout=0.9)
