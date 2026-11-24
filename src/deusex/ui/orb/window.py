from __future__ import annotations

from typing import Final

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from deusex.ui.orb.renderers import (
    ORB_STYLES,
    OrbRenderContext,
    OrbRenderer,
    create_orb_renderer,
    parse_accent_color,
)

DEFAULT_PREVIEW_DURATION_SEC: Final[float] = 4.0
_ORB_SIZE_PX: Final[int] = 152
_SCREEN_MARGIN_PX: Final[int] = 28
_ANIM_INTERVAL_MS: Final[int] = 33  # ~30 FPS solo cuando el Orbe está visible
_WATCHDOG_TIMEOUT_MS: Final[int] = 30_000


class FloatingOrbWindow(QWidget):
    """
    Orbe Flotante Click-Through de DeusEx v3:
    - Ventana frameless, `WindowTransparentForInput` y `WindowStaysOnTopHint`.
    - Oculto por defecto en `IDLE` (salvo `always_subtle`) y siempre oculto en `PRIVACY`.
    - Visible al escuchar, pensar, hablar, calibrar o en vista previa fija de 4 s.
    - 5 renderers intercambiables (`reactor`, `waves`, `sphere`, `bars`, `avatar`) reactivos al RMS.
    """

    def __init__(
        self,
        *,
        style: str = "reactor",
        accent_hex: str = "#38BDF8",
        visibility_mode: str = "speaking_only",
        corner: str = "bottom_right",
        avatar_path: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._style_id = style if style in ORB_STYLES else "reactor"
        self._accent_hex = accent_hex
        self._accent = parse_accent_color(accent_hex)
        self._visibility_mode = (
            visibility_mode if visibility_mode in ("speaking_only", "always_subtle") else "speaking_only"
        )
        self._corner = (
            corner
            if corner in ("bottom_right", "bottom_left", "top_right", "top_left")
            else "bottom_right"
        )
        self._avatar_path = avatar_path

        self._state = "idle"
        self._privacy_mode = False
        self._preview_active = False
        self._target_level = 0.0
        self._smoothed_level = 0.0
        self._phase = 0.0

        self._renderer: OrbRenderer = create_orb_renderer(
            self._style_id,
            avatar_path=self._avatar_path,
        )

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._on_tick)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._on_preview_finished)

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setSingleShot(True)
        self._watchdog_timer.setInterval(_WATCHDOG_TIMEOUT_MS)
        self._watchdog_timer.timeout.connect(self._on_watchdog_timeout)

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.setFixedSize(_ORB_SIZE_PX, _ORB_SIZE_PX)
        self.reposition()
        self._sync_visibility()

    @property
    def style_id(self) -> str:
        return self._style_id

    @property
    def current_state(self) -> str:
        return self._state

    @property
    def is_preview_active(self) -> bool:
        return self._preview_active

    @property
    def corner(self) -> str:
        return self._corner

    def set_style(self, style: str, *, avatar_path: str | None = None) -> None:
        normalized = style.strip().lower()
        if normalized not in ORB_STYLES:
            normalized = "reactor"
        if avatar_path is not None:
            self._avatar_path = avatar_path

        if normalized != self._style_id:
            self._renderer.cleanup()
            self._style_id = normalized
            self._renderer = create_orb_renderer(
                self._style_id,
                avatar_path=self._avatar_path,
            )
            self._renderer.set_active(self.isVisible())
        elif avatar_path is not None:
            self._renderer.set_avatar_path(self._avatar_path)

        if self.isVisible():
            self.update()

    def set_accent_color(self, accent_hex: str) -> None:
        self._accent_hex = accent_hex
        self._accent = parse_accent_color(accent_hex)
        if self.isVisible():
            self.update()

    def set_visibility_mode(self, mode: str) -> None:
        self._visibility_mode = (
            mode if mode in ("speaking_only", "always_subtle") else "speaking_only"
        )
        self._sync_visibility()

    def set_corner(self, corner: str) -> None:
        if corner in ("bottom_right", "bottom_left", "top_right", "top_left"):
            self._corner = corner
            self.reposition()

    def set_privacy_mode(self, enabled: bool) -> None:
        self._privacy_mode = bool(enabled)
        if self._privacy_mode:
            self._preview_timer.stop()
            self._watchdog_timer.stop()
            self._preview_active = False
            self._state = "privacy"
            self._target_level = 0.0
        elif self._state == "privacy":
            self._state = "idle"
        self._sync_visibility()

    def set_audio_level(self, level: float) -> None:
        self._target_level = max(0.0, min(1.0, float(level)))

    def show_preview(self, duration_sec: float = DEFAULT_PREVIEW_DURATION_SEC) -> None:
        """Muestra el Orbe fijo durante `duration_sec` (4 s por defecto) salvo en Modo Privacidad."""
        if self._privacy_mode:
            self._sync_visibility()
            return
        self._watchdog_timer.stop()
        self._preview_active = True
        self._state = "preview"
        self._target_level = 0.65
        ms = max(500, round(float(duration_sec) * 1000.0))
        self._preview_timer.start(ms)
        self._sync_visibility()

    def apply_orb_state(
        self,
        state: str,
        *,
        visible: bool | None = None,
        accent_hex: str = "",
        preview_sec: float = 0.0,
    ) -> None:
        """Aplica una transición de estado emitida por `OrbStateChangedEvent`."""
        if accent_hex:
            self.set_accent_color(accent_hex)

        norm_state = state.strip().lower() or "idle"
        if norm_state == "privacy" or self._privacy_mode:
            self._preview_timer.stop()
            self._watchdog_timer.stop()
            self._preview_active = False
            self._state = "privacy"
            self._target_level = 0.0
            self._sync_visibility()
            return

        if norm_state == "preview":
            dur = preview_sec if preview_sec > 0.0 else DEFAULT_PREVIEW_DURATION_SEC
            self.show_preview(duration_sec=dur)
            return

        # Si el usuario está viendo una vista previa explícita y llega un `idle`, respetar la vista previa
        if self._preview_active and norm_state == "idle":
            return

        self._preview_timer.stop()
        self._preview_active = False
        self._state = norm_state
        if norm_state in ("listening", "thinking", "calibrating"):
            self._watchdog_timer.start(_WATCHDOG_TIMEOUT_MS)
        else:
            self._watchdog_timer.stop()

        if norm_state == "idle":
            self._target_level = 0.0
        elif norm_state == "thinking":
            self._target_level = 0.45

        if visible is False and norm_state == "idle" and self._visibility_mode != "always_subtle":
            self._hide_orb()
            return
        self._sync_visibility()

    def _on_watchdog_timeout(self) -> None:
        """Seguridad: fuerza retorno a idle si un estado activo no transiciona en 30 s."""
        self.apply_orb_state("idle", visible=False)

    def _on_preview_finished(self) -> None:
        self._preview_active = False
        if self._state == "preview":
            self._state = "idle"
            self._target_level = 0.0
        self._sync_visibility()

    def _should_be_visible(self) -> bool:
        if self._privacy_mode or self._state == "privacy":
            return False
        if self._preview_active:
            return True
        if self._state in ("listening", "thinking", "speaking", "calibrating"):
            return True
        return self._visibility_mode == "always_subtle"

    def _show_orb(self) -> None:
        self.reposition()
        if not self.isVisible():
            self.show()
        self._renderer.set_active(True)
        if not self._anim_timer.isActive():
            self._anim_timer.start()
        self.update()

    def _hide_orb(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self._renderer.set_active(False)
        if self.isVisible():
            self.hide()

    def _sync_visibility(self) -> None:
        if self._should_be_visible():
            self._show_orb()
        else:
            self._hide_orb()

    def reposition(self) -> None:
        """Posiciona el Orbe en la esquina configurada del área útil del monitor principal."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        if self._corner in ("bottom_left", "top_left"):
            x = avail.x() + _SCREEN_MARGIN_PX
        else:
            x = avail.x() + avail.width() - self.width() - _SCREEN_MARGIN_PX

        if self._corner in ("top_left", "top_right"):
            y = avail.y() + _SCREEN_MARGIN_PX
        else:
            y = avail.y() + avail.height() - self.height() - _SCREEN_MARGIN_PX
        self.move(x, y)

    def _on_tick(self) -> None:
        if self._preview_active:
            # Simulación de pulso orgánico durante vista previa de 4 s
            self._target_level = 0.55 + 0.30 * abs(float((self._phase * 0.6) % 2.0 - 1.0))

        self._smoothed_level = 0.72 * self._smoothed_level + 0.28 * self._target_level
        speed = 0.05 if self._state == "idle" else (0.13 if self._state == "thinking" else 0.10)
        self._phase += speed
        self.update()

    def prepare_for_exit(self) -> None:
        """Detiene temporizadores y libera recursos gráficos antes del cierre."""
        self._preview_timer.stop()
        self._watchdog_timer.stop()
        self._anim_timer.stop()
        self._preview_active = False
        self._renderer.cleanup()
        self.hide()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        subtle = self._state == "idle" and self._visibility_mode == "always_subtle"
        ctx = OrbRenderContext(
            rect=QRectF(0.0, 0.0, float(self.width()), float(self.height())),
            phase=self._phase,
            level=self._smoothed_level,
            state=self._state,
            accent=self._accent,
            subtle=subtle,
        )
        self._renderer.paint(painter, ctx)
        painter.end()
