from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from deusex.core.events import SafetyConfirmationRequestedEvent


class SafetyCard(QFrame):
    """Tarjeta visual física de SafetyGate con cuenta regresiva y botones Aprobar / Rechazar."""

    decision_made = Signal(str, bool)  # (confirmation_id, approved)

    def __init__(
        self,
        event: SafetyConfirmationRequestedEvent,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SafetyCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.confirmation_id = event.confirmation_id
        self._total_sec = max(1, int(event.timeout_sec))
        self._remaining_sec = self._total_sec
        self._resolved = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Confirmación de seguridad (SafetyGate)", self)
        title.setObjectName("CardTitle")
        header_row.addWidget(title, 1)

        self._countdown_label = QLabel(f"{self._remaining_sec} s", self)
        self._countdown_label.setObjectName("StatusPill")
        self._countdown_label.setProperty("state", "warning")
        header_row.addWidget(self._countdown_label)
        layout.addLayout(header_row)

        summary_lbl = QLabel(event.summary_es or event.tool_name, self)
        summary_lbl.setWordWrap(True)
        layout.addWidget(summary_lbl)

        detail_text = f"Acción: {event.tool_name}"
        if event.args_preview:
            detail_text += f" · {event.args_preview}"
        detail_lbl = QLabel(detail_text, self)
        detail_lbl.setObjectName("MutedLabel")
        detail_lbl.setWordWrap(True)
        layout.addWidget(detail_lbl)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, self._total_sec)
        self._progress.setValue(self._total_sec)
        self._progress.setTextVisible(False)
        layout.addWidget(self._progress)

        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 4, 0, 0)
        actions_row.setSpacing(10)

        self.approve_btn = QPushButton("Aprobar acción", self)
        self.approve_btn.setProperty("variant", "primary")
        self.approve_btn.setToolTip("Autoriza la ejecución de esta acción crítica")
        self.approve_btn.clicked.connect(lambda: self._emit_decision(True))
        actions_row.addWidget(self.approve_btn)

        self.reject_btn = QPushButton("Rechazar", self)
        self.reject_btn.setProperty("variant", "danger")
        self.reject_btn.setToolTip("Cancela la acción de forma segura")
        self.reject_btn.clicked.connect(lambda: self._emit_decision(False))
        actions_row.addWidget(self.reject_btn)

        actions_row.addStretch(1)

        self._status_lbl = QLabel("Esperando tu decisión...", self)
        self._status_lbl.setObjectName("MutedLabel")
        actions_row.addWidget(self._status_lbl)

        layout.addLayout(actions_row)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    def _emit_decision(self, approved: bool) -> None:
        if self._resolved:
            return
        self.mark_resolved(approved=approved, reason="user")
        self.decision_made.emit(self.confirmation_id, approved)

    def _on_tick(self) -> None:
        if self._resolved:
            self._timer.stop()
            return
        self._remaining_sec = max(0, self._remaining_sec - 1)
        self._progress.setValue(self._remaining_sec)
        self._countdown_label.setText(f"{self._remaining_sec} s")
        if self._remaining_sec <= 0:
            self.mark_resolved(approved=False, reason="timeout")

    def mark_resolved(self, *, approved: bool, reason: str = "") -> None:
        if self._resolved:
            return
        self._resolved = True
        self._timer.stop()
        self.approve_btn.setEnabled(False)
        self.reject_btn.setEnabled(False)
        if approved:
            self._status_lbl.setText("Acción aprobada")
            self._countdown_label.setText("Aprobada")
            self._countdown_label.setProperty("state", "active")
        elif reason == "timeout":
            self._status_lbl.setText("Cancelada por tiempo agotado")
            self._countdown_label.setText("Expirada")
            self._countdown_label.setProperty("state", "danger")
        else:
            self._status_lbl.setText("Acción rechazada")
            self._countdown_label.setText("Rechazada")
            self._countdown_label.setProperty("state", "danger")
        self._countdown_label.style().unpolish(self._countdown_label)
        self._countdown_label.style().polish(self._countdown_label)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        super().closeEvent(event)
