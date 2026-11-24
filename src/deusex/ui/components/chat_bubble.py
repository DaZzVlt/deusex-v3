from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ChatBubble(QFrame):
    """Burbuja de conversación limpia con insignia de origen, botón copiar y fuentes clickeables."""

    def __init__(
        self,
        *,
        role: str,
        sender_label: str,
        content: str,
        model_used: str = "",
        fast_path: bool = False,
        sources: Sequence[dict[str, str]] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ChatBubble")
        self.setProperty("role", role if role in ("user", "assistant", "system") else "assistant")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self._content = content

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        sender_lbl = QLabel(sender_label, self)
        sender_lbl.setObjectName("CardTitle")
        header.addWidget(sender_lbl)

        if fast_path:
            badge = QLabel("Fast-Path · 0 tokens", self)
            badge.setObjectName("StatusPill")
            badge.setProperty("state", "active")
            header.addWidget(badge)
        elif model_used:
            badge = QLabel(model_used, self)
            badge.setObjectName("StatusPill")
            header.addWidget(badge)

        header.addStretch(1)

        if role == "assistant" and content.strip():
            copy_btn = QPushButton("Copiar", self)
            copy_btn.setProperty("variant", "ghost")
            copy_btn.setToolTip("Copiar respuesta al portapapeles")
            copy_btn.clicked.connect(self._copy_text)
            header.addWidget(copy_btn)

        layout.addLayout(header)

        self.content_label = QLabel(content, self)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        layout.addWidget(self.content_label)

        if sources:
            sources_row = QHBoxLayout()
            sources_row.setContentsMargins(0, 4, 0, 0)
            sources_row.setSpacing(6)
            for idx, src in enumerate(sources[:5], start=1):
                url = str(src.get("url") or "").strip()
                title = str(src.get("title") or f"Fuente {idx}").strip()
                if not (url.startswith("https://") or url.startswith("http://")):
                    continue
                short_title = title[:36] + ("…" if len(title) > 36 else "")
                btn = QPushButton(f"↗ {short_title}", self)
                btn.setProperty("variant", "pill")
                btn.setToolTip(url)
                btn.clicked.connect(lambda _checked=False, target=url: _open_safe_url(target))
                sources_row.addWidget(btn)
            sources_row.addStretch(1)
            layout.addLayout(sources_row)

    def _copy_text(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._content)


def _open_safe_url(url: str) -> None:
    if url.startswith(("https://", "http://")):
        QDesktopServices.openUrl(QUrl(url))


class MemorySavedPill(QFrame):
    """Píldora transparente bajo respuestas cuando DeusEx guarda un recuerdo adaptativo."""

    undo_requested = Signal(str)  # memory_id

    def __init__(
        self,
        *,
        memory_id: str,
        summary_es: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MemoryPill")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.memory_id = memory_id

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 10, 6)
        layout.setSpacing(10)

        self._label = QLabel(f"Guardado en memoria: {summary_es}", self)
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)

        self.undo_btn = QPushButton("Deshacer", self)
        self.undo_btn.setProperty("variant", "ghost")
        self.undo_btn.setToolTip("Eliminar este recuerdo guardado automáticamente")
        self.undo_btn.clicked.connect(self._on_undo)
        layout.addWidget(self.undo_btn)

    def _on_undo(self) -> None:
        self.mark_undone()
        self.undo_requested.emit(self.memory_id)

    def mark_undone(self) -> None:
        self.undo_btn.setEnabled(False)
        self._label.setText("Recuerdo descartado de la memoria.")
