from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from deusex.app import DeusExApplication
from deusex.ui.bridge_qt import QtEventBridge


def create_programmatic_app_icon(accent_hex: str = "#38BDF8") -> QIcon:
    """Genera un ícono nítido en memoria (sin depender de archivos .ico externos)."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#111318"))
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)

    painter.setBrush(QColor(accent_hex))
    painter.drawEllipse(12, 12, 40, 40)

    painter.setPen(QColor("#0B1120"))
    font = QFont("Segoe UI", 22, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "D")
    painter.end()
    return QIcon(pixmap)


class SystemTrayController(QObject):
    """Bandeja del sistema con menú rápido y cierre limpio garantizado."""

    open_main_requested = Signal()
    toggle_minichat_requested = Signal()
    exit_requested = Signal()

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge

        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        self._tray = QSystemTrayIcon(create_programmatic_app_icon(accent), self)
        self._tray.setToolTip("DeusEx v3")

        self._menu = QMenu()
        self._open_action = QAction("Abrir DeusEx", self._menu)
        self._open_action.triggered.connect(self.open_main_requested.emit)
        self._menu.addAction(self._open_action)

        self._minichat_action = QAction("Mini-Chat rápido (Alt+Espacio)", self._menu)
        self._minichat_action.triggered.connect(self.toggle_minichat_requested.emit)
        self._menu.addAction(self._minichat_action)

        self._menu.addSeparator()

        self._privacy_action = QAction("Modo Privacidad (Ctrl+Shift+P)", self._menu)
        self._privacy_action.setCheckable(True)
        self._privacy_action.triggered.connect(lambda checked: self._app.set_privacy_mode(checked))
        self._menu.addAction(self._privacy_action)

        self._mute_action = QAction("Silenciar voz", self._menu)
        self._mute_action.setCheckable(True)
        self._mute_action.triggered.connect(lambda checked: self._app.set_voice_muted(checked))
        self._menu.addAction(self._mute_action)

        self._menu.addSeparator()

        self._exit_action = QAction("Salir de DeusEx", self._menu)
        self._exit_action.triggered.connect(self.exit_requested.emit)
        self._menu.addAction(self._exit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

        self._bridge.privacy_mode_changed.connect(lambda _e: self.sync_state())
        self._bridge.voice_mute_changed.connect(lambda _m: self.sync_state())
        self._bridge.profile_changed.connect(lambda _p: self.refresh_icon())
        self._bridge.scheduled_job_fired.connect(self._on_scheduled_job_fired)
        self.sync_state()

    @property
    def tray_icon(self) -> QSystemTrayIcon:
        return self._tray

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def refresh_icon(self) -> None:
        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        self._tray.setIcon(create_programmatic_app_icon(accent))

    def sync_state(self) -> None:
        self._privacy_action.setChecked(self._app.settings.general.privacy_mode)
        self._mute_action.setChecked(self._app.settings.voice.tts_muted)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_main_requested.emit()

    def show_notification(
        self,
        title: str,
        message: str,
        *,
        duration_ms: int = 6000,
    ) -> bool:
        """Muestra una notificación nativa en la bandeja del sistema si está disponible."""
        if not QSystemTrayIcon.isSystemTrayAvailable() or not self._tray.supportsMessages():
            return False
        if not self._tray.isVisible():
            self._tray.show()
        self._tray.showMessage(
            title,
            message,
            QSystemTrayIcon.MessageIcon.Information,
            max(1000, int(duration_ms)),
        )
        return True

    def _on_scheduled_job_fired(self, event: object) -> None:
        label = str(getattr(event, "label", "") or "Temporizador").strip()
        kind = str(getattr(event, "kind", "timer") or "timer").strip().lower()
        prefix = (
            "Alarma"
            if kind == "alarm"
            else "Recordatorio"
            if kind == "reminder"
            else "Temporizador"
        )
        self.show_notification(
            f"DeusEx · {prefix} finalizado",
            f"{label} ha llegado a su fin.",
        )
