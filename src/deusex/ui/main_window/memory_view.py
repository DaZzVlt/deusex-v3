from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.brain.memory import MemoryRecord
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard

_FILTER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("all", "Todo"),
    ("fact", "Hechos"),
    ("preference", "Preferencias"),
    ("interest", "Intereses"),
    ("contact", "Contactos"),
)

_CATEGORY_LABELS: dict[str, str] = {
    "fact": "Hecho",
    "preference": "Preferencia",
    "interest": "Interés",
    "contact": "Contacto",
}


class MemoryView(QWidget):
    """Vista de Memoria Adaptativa (Capa 2): búsqueda instantánea, filtros, pin, edición y borrado."""

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._active_category: str = "all"
        self._filter_buttons: dict[str, QPushButton] = {}

        self._build_ui()
        self._bridge.memory_saved.connect(lambda _e: self.refresh())
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)

        title = QLabel("Memoria Adaptativa", self)
        title.setObjectName("ViewTitle")
        root.addWidget(title)

        subtitle = QLabel(
            "Recuerdos guardados localmente en SQLite WAL. Se congelan automáticamente en Modo Privacidad.",
            self,
        )
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # Barra de búsqueda + filtros por píldora
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(8)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Buscar recuerdos por palabra clave...")
        self.search_edit.setMinimumHeight(36)
        self.search_edit.textChanged.connect(lambda _t: self.refresh())
        search_row.addWidget(self.search_edit, 1)

        for cat_key, label in _FILTER_OPTIONS:
            btn = QPushButton(label, self)
            btn.setProperty("variant", "pill")
            btn.setProperty("selected", cat_key == self._active_category)
            btn.clicked.connect(lambda _checked=False, c=cat_key: self._select_category(c))
            self._filter_buttons[cat_key] = btn
            search_row.addWidget(btn)

        root.addLayout(search_row)

        # Tarjeta de alta manual rápida
        add_card = FluentCard(
            title="Añadir recuerdo manual",
            subtitle="Agrega preferencias, intereses o datos útiles que quieras que DeusEx recuerde.",
            parent=self,
        )
        form_row = QHBoxLayout()
        form_row.setContentsMargins(0, 0, 0, 0)
        form_row.setSpacing(8)

        self.cat_combo = QComboBox(add_card)
        for cat_key, label in _CATEGORY_LABELS.items():
            self.cat_combo.addItem(label, cat_key)
        self.cat_combo.setMinimumWidth(120)
        form_row.addWidget(self.cat_combo)

        self.key_edit = QLineEdit(add_card)
        self.key_edit.setPlaceholderText("Etiqueta (ej. música, ciudad)")
        self.key_edit.setMaximumWidth(200)
        form_row.addWidget(self.key_edit)

        self.val_edit = QLineEdit(add_card)
        self.val_edit.setPlaceholderText("Valor a recordar...")
        self.val_edit.returnPressed.connect(self._on_add_memory)
        form_row.addWidget(self.val_edit, 1)

        add_btn = QPushButton("Guardar", add_card)
        add_btn.setProperty("variant", "primary")
        add_btn.clicked.connect(self._on_add_memory)
        form_row.addWidget(add_btn)

        add_card.body_layout.addLayout(form_row)
        root.addWidget(add_card)

        # Lista desplazable de recuerdos
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._list_container = QWidget(self._scroll)
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 6, 8)
        self._list_layout.setSpacing(10)
        self._list_layout.addStretch(1)

        self._scroll.setWidget(self._list_container)
        root.addWidget(self._scroll, 1)

    def _select_category(self, category: str) -> None:
        self._active_category = category
        for cat_key, btn in self._filter_buttons.items():
            btn.setProperty("selected", cat_key == category)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.refresh()

    def _on_add_memory(self) -> None:
        key_text = self.key_edit.text().strip() or "nota"
        val_text = self.val_edit.text().strip()
        if not val_text:
            return
        cat_data = self.cat_combo.currentData()
        cat = str(cat_data) if isinstance(cat_data, str) else "fact"
        self._app.memory.save_memory(
            category=cat,
            key=key_text,
            value=val_text,
            auto_extracted=False,
        )
        self.key_edit.clear()
        self.val_edit.clear()
        self.refresh()

    def _clear_list(self) -> None:
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()

    def refresh(self) -> None:
        self._clear_list()
        records = self._app.memory.list_memories(
            category=self._active_category,
            query=self.search_edit.text(),
        )
        if not records:
            empty = FluentCard(
                title="Sin recuerdos en esta vista",
                subtitle="Cuando converses con DeusEx o guardes un dato manualmente aparecerá aquí.",
                parent=self._list_container,
            )
            self._list_layout.insertWidget(0, empty)
            return

        for rec in records:
            self._list_layout.insertWidget(
                max(0, self._list_layout.count() - 1),
                self._build_memory_card(rec),
            )

    def _build_memory_card(self, rec: MemoryRecord) -> FluentCard:
        card = FluentCard(parent=self._list_container)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        cat_pill = QLabel(_CATEGORY_LABELS.get(rec.category, rec.category), card)
        cat_pill.setObjectName("StatusPill")
        if rec.pinned:
            cat_pill.setProperty("state", "active")
        row.addWidget(cat_pill)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        key_lbl = QLabel(rec.key.capitalize(), card)
        key_lbl.setObjectName("CardTitle")
        text_col.addWidget(key_lbl)

        val_lbl = QLabel(rec.value, card)
        val_lbl.setWordWrap(True)
        text_col.addWidget(val_lbl)

        row.addLayout(text_col, 1)

        pin_btn = QPushButton("Fijado" if rec.pinned else "Fijar", card)
        pin_btn.setProperty("variant", "pill")
        pin_btn.setProperty("selected", rec.pinned)
        pin_btn.setToolTip("Fijar este recuerdo al inicio del contexto")
        pin_btn.clicked.connect(lambda _checked=False, mid=rec.id: self._on_toggle_pin(mid))
        row.addWidget(pin_btn)

        edit_btn = QPushButton("Editar", card)
        edit_btn.setProperty("variant", "ghost")
        edit_btn.clicked.connect(lambda _checked=False, r=rec: self._on_edit_memory(r))
        row.addWidget(edit_btn)

        del_btn = QPushButton("Borrar", card)
        del_btn.setProperty("variant", "ghost")
        del_btn.clicked.connect(lambda _checked=False, mid=rec.id: self._on_delete_memory(mid))
        row.addWidget(del_btn)

        card.body_layout.addLayout(row)
        return card

    def _on_toggle_pin(self, memory_id: str) -> None:
        self._app.memory.toggle_pin(memory_id)
        self.refresh()

    def _on_edit_memory(self, rec: MemoryRecord) -> None:
        new_value, accepted = QInputDialog.getText(
            self,
            "Editar recuerdo",
            f"Nuevo valor para '{rec.key}':",
            text=rec.value,
        )
        if accepted and new_value.strip():
            self._app.memory.update_memory(rec.id, value=new_value.strip())
            self.refresh()

    def _on_delete_memory(self, memory_id: str) -> None:
        self._app.memory.delete_memory(memory_id)
        self.refresh()
