from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.core.permissions import Permission, permissions_for_module
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard, SettingRow, ToggleSwitch


@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    module_id: str
    title: str
    description: str
    slice_label: str


MODULE_CATALOG: tuple[ModuleDescriptor, ...] = (
    ModuleDescriptor(
        module_id="pc",
        title="Control Local del PC",
        description="Abrir/cerrar apps (shell:AppsFolder), volumen, brillo, ventanas, archivos con Undo y telemetría.",
        slice_label="Rebanada 2",
    ),
    ModuleDescriptor(
        module_id="network",
        title="Red Local y Smart TV",
        description="Escaneo híbrido LAN, Samsung Tizen/WoL, Chromecast, Miracast, YouTube TV y Xbox.",
        slice_label="Rebanada 3",
    ),
    ModuleDescriptor(
        module_id="voice",
        title="Voz, Orbe y Escudo Contextual",
        description="Wake-Word Vosk offline, Push-to-Talk, biometría vocal, Edge-TTS y Audio Ducking.",
        slice_label="Rebanada 4",
    ),
    ModuleDescriptor(
        module_id="intel",
        title="Inteligencia Web (0 Claves)",
        description="Búsqueda web, Open-Meteo gratis, noticias RSS adaptativas y extractores PDF/YouTube.",
        slice_label="Rebanada 5",
    ),
    ModuleDescriptor(
        module_id="vision",
        title="Visión y Guía Live",
        description="Captura multi-monitor DPI-aware, Guía Live por turnos y envío de capturas.",
        slice_label="Rebanada 5",
    ),
    ModuleDescriptor(
        module_id="music",
        title="Música Universal",
        description="Windows SMTC, Spotify PKCE, TIDAL PKCE, YouTube Music, Apple Music y biblioteca local.",
        slice_label="Rebanada 7",
    ),
    ModuleDescriptor(
        module_id="workspace",
        title="Google Workspace",
        description="Gmail, Google Calendar, Google Tasks, Drive y creación encadenada de Google Docs.",
        slice_label="Rebanada 7",
    ),
    ModuleDescriptor(
        module_id="discord_chat",
        title="Discord · Chat y Control Remoto",
        description="Avisos del sistema, aprobaciones remotas de SafetyGate y control por DM del Dueño.",
        slice_label="Rebanada 8",
    ),
    ModuleDescriptor(
        module_id="discord_music",
        title="Discord · Música en Voz",
        description="Audio DAVE E2EE, yt-dlp con búfer rodante, rangos Dueño/DJ/Invitado y !shuffle manual.",
        slice_label="Rebanada 8",
    ),
    ModuleDescriptor(
        module_id="whatsapp",
        title="WhatsApp (Baileys)",
        description="Vinculación visual por QR, mensajes nuevos en tiempo real y resumen de notas de voz.",
        slice_label="Rebanada 8",
    ),
    ModuleDescriptor(
        module_id="bridge",
        title="App Móvil y Smartwatch",
        description="Puente WebSocket LAN con emparejamiento QR (cero ADB) y Health Connect.",
        slice_label="Rebanada 9",
    ),
)

_STATE_LABELS: dict[ModuleState, tuple[str, str]] = {
    ModuleState.DISABLED: ("Apagado", "idle"),
    ModuleState.STARTING: ("Iniciando...", "warning"),
    ModuleState.ACTIVE: ("Activo", "active"),
    ModuleState.DEGRADED: ("Degradado", "warning"),
    ModuleState.ERROR: ("Error", "danger"),
    ModuleState.STOPPING: ("Deteniendo...", "warning"),
}


class ModulesView(QWidget):
    """Cuadrícula Fluent 2 columnas de módulos enchufables + panel lateral (drawer) de permisos."""

    _toggle_completed = Signal(str, bool)

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._app = app
        self._bridge = bridge
        self._syncing = False
        self._selected_module_id: str | None = None
        self._switches: dict[str, ToggleSwitch] = {}
        self._status_pills: dict[str, QLabel] = {}

        self._build_ui()
        self._toggle_completed.connect(lambda _mid, _ok: self.refresh())
        self._bridge.module_state_changed.connect(lambda _e: self.refresh())
        self._bridge.settings_updated.connect(lambda _e: self.refresh())
        self.refresh()

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(16)

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(12)

        title = QLabel("Módulos Enchufables", self)
        title.setObjectName("ViewTitle")
        left_col.addWidget(title)

        subtitle = QLabel(
            "Zero-Base en ejecución: cada módulo apagado consume 0 RAM y 0 CPU. Configura permisos granulares desde su panel lateral.",
            self,
        )
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        left_col.addWidget(subtitle)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        grid_host = QWidget(scroll)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 2, 6, 8)
        grid.setSpacing(12)

        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        for idx, desc in enumerate(MODULE_CATALOG):
            card = self._build_module_card(desc, accent)
            grid.addWidget(card, idx // 2, idx % 2)

        grid.setRowStretch((len(MODULE_CATALOG) + 1) // 2, 1)
        scroll.setWidget(grid_host)
        left_col.addWidget(scroll, 1)

        outer.addLayout(left_col, 1)

        # Panel lateral deslizable (drawer) de permisos y detalles
        self._drawer = QFrame(self)
        self._drawer.setObjectName("FluentCard")
        self._drawer.setFixedWidth(315)
        self._drawer.setVisible(False)

        drawer_layout = QVBoxLayout(self._drawer)
        drawer_layout.setContentsMargins(16, 14, 16, 14)
        drawer_layout.setSpacing(10)

        drawer_header = QHBoxLayout()
        self._drawer_title = QLabel("Configurar módulo", self._drawer)
        self._drawer_title.setObjectName("CardTitle")
        drawer_header.addWidget(self._drawer_title, 1)

        close_drawer_btn = QPushButton("✕", self._drawer)
        close_drawer_btn.setObjectName("ModuleDrawerCloseButton")
        close_drawer_btn.setProperty("variant", "ghost")
        close_drawer_btn.setFixedSize(30, 30)
        close_drawer_btn.setToolTip("Cerrar panel")
        close_drawer_btn.clicked.connect(self.close_drawer)
        drawer_header.addWidget(close_drawer_btn)
        drawer_layout.addLayout(drawer_header)

        self._drawer_desc = QLabel("", self._drawer)
        self._drawer_desc.setObjectName("MutedLabel")
        self._drawer_desc.setWordWrap(True)
        drawer_layout.addWidget(self._drawer_desc)

        self._drawer_state = QLabel("", self._drawer)
        self._drawer_state.setObjectName("StatusPill")
        drawer_layout.addWidget(self._drawer_state)

        perm_header = QLabel("Permisos granulares (Permission)", self._drawer)
        perm_header.setObjectName("CardTitle")
        drawer_layout.addWidget(perm_header)

        self._drawer_perms_layout = QVBoxLayout()
        self._drawer_perms_layout.setContentsMargins(0, 0, 0, 0)
        self._drawer_perms_layout.setSpacing(8)
        drawer_layout.addLayout(self._drawer_perms_layout)
        drawer_layout.addStretch(1)

        outer.addWidget(self._drawer)

    def _build_module_card(self, desc: ModuleDescriptor, accent_hex: str) -> FluentCard:
        card = FluentCard(parent=self)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        title_lbl = QLabel(desc.title, card)
        title_lbl.setObjectName("CardTitle")
        title_lbl.setWordWrap(True)
        top.addWidget(title_lbl, 1)

        switch = ToggleSwitch(card, checked=False, accent_hex=accent_hex)
        switch.setToolTip(f"Activar o desactivar {desc.title}")
        switch.toggled.connect(
            lambda checked, mid=desc.module_id: self._on_switch_toggled(mid, checked)
        )
        self._switches[desc.module_id] = switch
        top.addWidget(switch)
        card.body_layout.addLayout(top)

        desc_lbl = QLabel(desc.description, card)
        desc_lbl.setObjectName("MutedLabel")
        desc_lbl.setWordWrap(True)
        card.body_layout.addWidget(desc_lbl)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 4, 0, 0)
        footer.setSpacing(8)

        status_pill = QLabel("Apagado", card)
        status_pill.setObjectName("StatusPill")
        self._status_pills[desc.module_id] = status_pill
        footer.addWidget(status_pill)

        footer.addStretch(1)

        cfg_btn = QPushButton("Configurar", card)
        cfg_btn.setProperty("variant", "ghost")
        cfg_btn.setToolTip("Abrir permisos y detalles de este módulo")
        cfg_btn.clicked.connect(lambda _checked=False, mid=desc.module_id: self.toggle_drawer(mid))
        footer.addWidget(cfg_btn)

        card.body_layout.addLayout(footer)
        return card

    def _on_switch_toggled(self, module_id: str, checked: bool) -> None:
        if self._syncing:
            return
        fut = self._app.set_module_enabled(module_id, checked)

        def _done(f: concurrent.futures.Future[bool]) -> None:
            self._emit_toggle_done(module_id, f)

        fut.add_done_callback(_done)

    def _emit_toggle_done(self, module_id: str, fut: concurrent.futures.Future[bool]) -> None:
        try:
            ok = fut.result()
        except Exception:
            ok = False
        self._toggle_completed.emit(module_id, ok)

    def open_drawer(self, module_id: str) -> None:
        self._selected_module_id = module_id
        desc = next((d for d in MODULE_CATALOG if d.module_id == module_id), None)
        if desc is None:
            return

        self._drawer_title.setText(desc.title)
        self._drawer_desc.setText(desc.description)

        installed = self._app.engine.modules.get(module_id)
        if installed is None:
            self._drawer_state.setText(f"Disponible en {desc.slice_label}")
            self._drawer_state.setProperty("state", "idle")
        else:
            label_text, state_prop = _STATE_LABELS.get(installed.state, ("Apagado", "idle"))
            err = f" · {installed.last_error}" if installed.last_error else ""
            self._drawer_state.setText(f"Estado: {label_text}{err}")
            self._drawer_state.setProperty("state", state_prop)
        self._drawer_state.style().unpolish(self._drawer_state)
        self._drawer_state.style().polish(self._drawer_state)

        while self._drawer_perms_layout.count() > 0:
            item = self._drawer_perms_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()

        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        for meta in permissions_for_module(module_id):
            perm_sw = ToggleSwitch(
                self._drawer,
                checked=self._app.settings.is_permission_enabled(meta.permission),
                accent_hex=accent,
            )
            perm_sw.toggled.connect(
                lambda checked, p=meta.permission: self._on_permission_toggled(p, checked)
            )
            row = SettingRow(
                title=meta.label_es,
                description=meta.description_es,
                control=perm_sw,
                parent=self._drawer,
            )
            self._drawer_perms_layout.addWidget(row)

        self._drawer.setVisible(True)

    def _on_permission_toggled(self, perm: Permission, checked: bool) -> None:
        self._app.set_permission(perm, checked)

    def toggle_drawer(self, module_id: str) -> None:
        """Abre o cierra el drawer si ya estaba seleccionado el mismo módulo (toggle)."""
        if self._selected_module_id == module_id and self._drawer.isVisible():
            self.close_drawer()
        else:
            self.open_drawer(module_id)

    def close_drawer(self) -> None:
        self._selected_module_id = None
        self._drawer.setVisible(False)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drawer.isVisible():
            self.close_drawer()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_accent_color(self, accent_hex: str) -> None:
        for sw in self._switches.values():
            sw.set_accent_color(accent_hex)

    def refresh(self) -> None:
        self._syncing = True
        try:
            for desc in MODULE_CATALOG:
                mid = desc.module_id
                sw = self._switches[mid]
                pill = self._status_pills[mid]
                installed = self._app.engine.modules.get(mid)
                enabled_in_cfg = self._app.settings.modules.is_enabled(mid)

                if installed is None:
                    sw.setEnabled(False)
                    sw.setChecked(False)
                    pill.setText(f"Pendiente ({desc.slice_label})")
                    pill.setProperty("state", "idle")
                else:
                    sw.setEnabled(True)
                    sw.setChecked(enabled_in_cfg)
                    label_text, state_prop = _STATE_LABELS.get(installed.state, ("Apagado", "idle"))
                    pill.setText(label_text)
                    pill.setProperty("state", state_prop)
                pill.style().unpolish(pill)
                pill.style().polish(pill)

            if self._selected_module_id is not None and self._drawer.isVisible():
                self.open_drawer(self._selected_module_id)
        finally:
            self._syncing = False
