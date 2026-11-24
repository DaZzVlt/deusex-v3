from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget


class ToggleSwitch(QAbstractButton):
    """Interruptor animado estilo Windows 11 Fluent con color de acento dinámico."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        checked: bool = False,
        accent_hex: str = "#38BDF8",
    ) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._accent_color = QColor(accent_hex)
        self._progress: float = 1.0 if checked else 0.0

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(130)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_value)

        self.setChecked(checked)
        self.toggled.connect(self._on_toggled)

    def sizeHint(self) -> QSize:
        return QSize(44, 24)

    def set_accent_color(self, accent_hex: str) -> None:
        color = QColor(accent_hex)
        if color.isValid():
            self._accent_color = color
            self.update()

    def _on_anim_value(self, value: Any) -> None:
        try:
            self._progress = float(value)
        except (TypeError, ValueError):
            return
        self.update()

    def _on_toggled(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        self._anim.stop()
        if not self.isVisible():
            self._progress = target
            self.update()
            return
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(1.0, 1.0, float(self.width() - 2), float(self.height() - 2))
        radius = rect.height() / 2.0

        if not self.isEnabled():
            track_color = QColor(100, 116, 139, 90)
            border_color = QColor(148, 163, 184, 70)
            thumb_color = QColor(203, 213, 225, 160)
        elif self._progress > 0.5:
            track_color = QColor(self._accent_color)
            border_color = QColor(self._accent_color)
            thumb_color = QColor("#FFFFFF")
        else:
            track_color = QColor(100, 116, 139, 75)
            border_color = QColor(148, 163, 184, 150)
            thumb_color = QColor("#E2E8F0")

        painter.setPen(QPen(border_color, 1.2))
        painter.setBrush(track_color)
        painter.drawRoundedRect(rect, radius, radius)

        thumb_diameter = rect.height() - 6.0
        min_x = rect.left() + 3.0
        max_x = rect.right() - thumb_diameter - 3.0
        thumb_x = min_x + (max_x - min_x) * self._progress
        thumb_y = rect.top() + 3.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(thumb_color)
        painter.drawEllipse(QRectF(thumb_x, thumb_y, thumb_diameter, thumb_diameter))
        painter.end()
