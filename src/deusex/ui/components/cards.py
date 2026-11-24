from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class FluentCard(QFrame):
    """Tarjeta plana redondeada (10 px) estilo Windows 11 (reemplazo limpio de QGroupBox)."""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("FluentCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(16, 14, 16, 14)
        self.body_layout.setSpacing(10)

        self.title_label: QLabel | None = None
        self.subtitle_label: QLabel | None = None

        if title:
            self.title_label = QLabel(title, self)
            self.title_label.setObjectName("CardTitle")
            self.title_label.setWordWrap(True)
            self.body_layout.addWidget(self.title_label)

        if subtitle:
            self.subtitle_label = QLabel(subtitle, self)
            self.subtitle_label.setObjectName("MutedLabel")
            self.subtitle_label.setWordWrap(True)
            self.body_layout.addWidget(self.subtitle_label)


class SettingRow(QFrame):
    """Fila de ajuste estilo Windows 11 Settings: ícono + título/descripción a la izq, control a la der."""

    def __init__(
        self,
        *,
        title: str,
        description: str = "",
        control: QWidget | None = None,
        icon_text: str = "",
        keywords: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingRow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(58)

        self._search_blob = f"{title} {description} {keywords}".lower()

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(16, 12, 16, 12)
        row_layout.setSpacing(14)

        if icon_text:
            icon_lbl = QLabel(icon_text, self)
            icon_lbl.setFixedWidth(26)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row_layout.addWidget(icon_lbl)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(3)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("CardTitle")
        self.title_label.setWordWrap(True)
        text_box.addWidget(self.title_label)

        self.description_label: QLabel | None = None
        if description:
            self.description_label = QLabel(description, self)
            self.description_label.setObjectName("MutedLabel")
            self.description_label.setWordWrap(True)
            text_box.addWidget(self.description_label)

        row_layout.addLayout(text_box, 1)

        self._control_container = QHBoxLayout()
        self._control_container.setContentsMargins(0, 0, 0, 0)
        self._control_container.setSpacing(8)
        row_layout.addLayout(self._control_container)

        if control is not None:
            self.set_control(control)

    def set_control(self, widget: QWidget) -> None:
        self._control_container.addWidget(widget)

    def matches_search(self, query: str) -> bool:
        needle = query.strip().lower()
        return not needle or needle in self._search_blob


class SettingExpander(QFrame):
    """Tarjeta expandible estilo Windows 11 para agrupar ajustes relacionados sin QGroupBox."""

    def __init__(
        self,
        *,
        title: str,
        description: str = "",
        icon_text: str = "",
        expanded: bool = False,
        keywords: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingExpander")
        self.setFrameShape(QFrame.Shape.StyledPanel)

        self._expanded = bool(expanded)
        self._search_blob = f"{title} {description} {keywords}".lower()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 14, 12)
        header_layout.setSpacing(12)

        if icon_text:
            icon_lbl = QLabel(icon_text, header)
            icon_lbl.setFixedWidth(26)
            icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header_layout.addWidget(icon_lbl)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)

        self.title_label = QLabel(title, header)
        self.title_label.setObjectName("CardTitle")
        title_col.addWidget(self.title_label)

        if description:
            desc_lbl = QLabel(description, header)
            desc_lbl.setObjectName("MutedLabel")
            desc_lbl.setWordWrap(True)
            title_col.addWidget(desc_lbl)

        header_layout.addLayout(title_col, 1)

        self._chevron_btn = QPushButton("▾" if self._expanded else "▸", header)
        self._chevron_btn.setProperty("variant", "ghost")
        self._chevron_btn.setFixedSize(32, 32)
        self._chevron_btn.setToolTip("Expandir o contraer sección")
        self._chevron_btn.clicked.connect(self.toggle_expanded)
        header_layout.addWidget(self._chevron_btn)

        outer.addWidget(header)

        self.content_widget = QWidget(self)
        self.content_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(16, 4, 16, 14)
        self.content_layout.setSpacing(10)
        self.content_widget.setVisible(self._expanded)
        outer.addWidget(self.content_widget)

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self.content_widget.setVisible(self._expanded)
        self._chevron_btn.setText("▾" if self._expanded else "▸")

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def add_content_widget(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget)

    def matches_search(self, query: str) -> bool:
        needle = query.strip().lower()
        return not needle or needle in self._search_blob
