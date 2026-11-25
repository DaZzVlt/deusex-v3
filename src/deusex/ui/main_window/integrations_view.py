from __future__ import annotations

from collections.abc import Coroutine
import concurrent.futures
import contextlib
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState, ToolResult
from deusex.modules.network import DeviceAliasStore, DeviceType, NetworkDeviceRecord
from deusex.modules.workspace import GoogleAuthSessionInfo
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard

_DEVICE_TYPE_LABELS: dict[DeviceType, str] = {
    DeviceType.SAMSUNG_TV: "Samsung TV",
    DeviceType.CHROMECAST: "Chromecast",
    DeviceType.MIRACAST: "Miracast",
    DeviceType.YOUTUBE_TV: "YouTube TV",
    DeviceType.XBOX: "Xbox",
    DeviceType.LG_WEBOS: "LG WebOS",
    DeviceType.ANDROID_TV: "Android TV",
    DeviceType.SMART_LIGHT: "Luz Smart",
    DeviceType.HOME_ASSISTANT: "Home Assistant",
    DeviceType.GENERIC: "Dispositivo LAN",
}


@dataclass(frozen=True, slots=True)
class _IntegrationCardSpec:
    key: str
    title: str
    subtitle: str
    action_label: str
    slice_note: str


_INTEGRATIONS: tuple[_IntegrationCardSpec, ...] = (
    _IntegrationCardSpec(
        key="google",
        title="Google Workspace y YouTube Music",
        subtitle="Gmail, Calendar, Tasks, Drive, Google Docs y playlists de YouTube Music mediante OAuth 2.0 PKCE en 1 clic.",
        action_label="Iniciar sesión con Google",
        slice_note="OAuth 2.0 PKCE 1-clic activo (127.0.0.1:8888). Módulos Workspace en Rebanada 7.",
    ),
    _IntegrationCardSpec(
        key="spotify",
        title="Spotify (Premium y Free)",
        subtitle="Control de reproducción, búsqueda, Canciones que te gustan y puente hacia Discord (puerto fijo 127.0.0.1:8888).",
        action_label="Iniciar sesión con Spotify",
        slice_note="Se activa en Rebanadas 6-7 (OAuth PKCE 1-clic).",
    ),
    _IntegrationCardSpec(
        key="tidal",
        title="TIDAL",
        subtitle="Catálogo oficial y playlists vía OAuth 2.0 PKCE estándar con control local SMTC.",
        action_label="Iniciar sesión con TIDAL",
        slice_note="Se activa en Rebanadas 6-7 (OAuth PKCE 1-clic).",
    ),
    _IntegrationCardSpec(
        key="discord",
        title="Bot Privado de Discord",
        subtitle="1 bot por usuario (sin choques entre PCs) para avisos, control remoto Dueño y música DAVE E2EE con !shuffle manual.",
        action_label="Configurar bot de Discord",
        slice_note="Se activa en Rebanada 8 (con generador automático de enlace de invitación).",
    ),
    _IntegrationCardSpec(
        key="whatsapp",
        title="WhatsApp (Baileys Pre-Empaquetado)",
        subtitle="Vinculación visual por código QR en esta ventana, solo mensajes nuevos y resumen de notas de voz vía Gemini.",
        action_label="Mostrar QR de WhatsApp",
        slice_note="Runtime Node + Baileys ya preparado; vinculación UI en Rebanada 8.",
    ),
    _IntegrationCardSpec(
        key="mobile",
        title="DeusEx Companion (.apk) y Smartwatch",
        subtitle="Emparejamiento por QR en Wi-Fi local (cero ADB) para capturas, SafetyGate y Health Connect.",
        action_label="Generar QR móvil",
        slice_note="Servidor WebSocket v1 en Rebanada 9 y APK Android en Rebanada 10.",
    ),
)


class IntegrationsView(QWidget):
    """Vista unificada de Integraciones (panel LAN activo + tarjetas para OAuth, Discord, WhatsApp y APK)."""

    _lan_scan_completed = Signal(object, str)
    _lan_action_completed = Signal(bool, str)
    _module_toggle_completed = Signal(bool, str)
    _oauth_action_completed = Signal(bool, str)
    _setup_sync_opened = Signal(bool, str)

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._closing = False
        self._scanning = False
        self._google_busy = False
        self._populating_lan_table = False
        self._lan_devices: list[NetworkDeviceRecord] = []
        self._status_pills: dict[str, QLabel] = {}

        self._lan_scan_completed.connect(self._on_lan_scan_completed)
        self._lan_action_completed.connect(self._on_lan_action_completed)
        self._module_toggle_completed.connect(self._on_module_toggle_completed)
        self._oauth_action_completed.connect(self._on_oauth_action_completed)
        self._setup_sync_opened.connect(self._on_setup_sync_opened)

        self._bridge.module_state_changed.connect(self._on_module_state_changed)
        self._bridge.settings_updated.connect(self._on_settings_updated)
        self._bridge.oauth_completed.connect(self._on_oauth_completed_event)
        self._bridge.oauth_error.connect(self._on_oauth_error_event)
        self._bridge.setup_sync_received.connect(self._on_setup_sync_received_event)

        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        title = QLabel("Integraciones y Dispositivos", self)
        title.setObjectName("ViewTitle")
        root.addWidget(title)

        subtitle = QLabel(
            "Conecta cuentas externas en 1 clic, vincula WhatsApp por QR y gestiona tus dispositivos LAN y móvil.",
            self,
        )
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        host = QWidget(scroll)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 4, 6, 8)
        grid.setSpacing(12)

        # Panel Setup Sync y Portal Web ocupando las 2 columnas en fila 0
        setup_card = self._build_setup_sync_card(host)
        grid.addWidget(setup_card, 0, 0, 1, 2)

        # Panel LAN principal ocupando las 2 columnas en fila 1
        lan_card = self._build_lan_card(host)
        grid.addWidget(lan_card, 1, 0, 1, 2)

        # Tarjetas de integraciones a partir de fila 2
        for idx, spec in enumerate(_INTEGRATIONS):
            card = FluentCard(title=spec.title, subtitle=spec.subtitle, parent=host)

            footer = QHBoxLayout()
            footer.setContentsMargins(0, 6, 0, 0)
            footer.setSpacing(8)

            pill = QLabel("Listo en próxima rebanada", card)
            pill.setObjectName("StatusPill")
            self._status_pills[spec.key] = pill
            footer.addWidget(pill)

            footer.addStretch(1)

            if spec.key == "google":
                self._google_disconnect_btn = QPushButton("Desconectar", card)
                self._google_disconnect_btn.setObjectName("GoogleDisconnectButton")
                self._google_disconnect_btn.setVisible(False)
                self._google_disconnect_btn.clicked.connect(self._on_google_disconnect_clicked)
                footer.addWidget(self._google_disconnect_btn)

                self._google_connect_btn = QPushButton(spec.action_label, card)
                self._google_connect_btn.setObjectName("GoogleOAuthButton")
                self._google_connect_btn.setProperty("variant", "primary")
                self._google_connect_btn.setToolTip(spec.slice_note)
                self._google_connect_btn.clicked.connect(self._on_google_oauth_clicked)
                footer.addWidget(self._google_connect_btn)

                card.body_layout.addLayout(footer)

                self._google_feedback = QLabel("", card)
                self._google_feedback.setObjectName("GoogleFeedbackLabel")
                self._google_feedback.setProperty("class", "ViewSubtitle")
                self._google_feedback.setWordWrap(True)
                card.body_layout.addWidget(self._google_feedback)
            else:
                btn = QPushButton(spec.action_label, card)
                btn.setProperty("variant", "primary")
                btn.setEnabled(False)
                btn.setToolTip(spec.slice_note)
                footer.addWidget(btn)
                card.body_layout.addLayout(footer)

            grid.addWidget(card, 2 + (idx // 2), idx % 2)

        grid.setRowStretch(2 + (len(_INTEGRATIONS) + 1) // 2, 1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

    def _build_setup_sync_card(self, host: QWidget) -> FluentCard:
        card = FluentCard(
            title="Portal Web y Setup Sync Local (127.0.0.1)",
            subtitle=(
                "Abre las guías oficiales en GitHub Pages y envía claves API directo a tu DeusEx "
                "local mediante token efímero (sin editar .env manualmente)."
            ),
            parent=host,
        )

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 2)
        row.setSpacing(8)

        pill = QLabel("Servidor local listo bajo demanda", card)
        pill.setObjectName("SetupSyncStatusPill")
        self._status_pills["setup_sync"] = pill
        row.addWidget(pill)

        row.addStretch(1)

        self._open_portal_btn = QPushButton("Abrir Portal Web", card)
        self._open_portal_btn.setObjectName("OpenPortalButton")
        self._open_portal_btn.clicked.connect(self._on_open_portal_clicked)
        row.addWidget(self._open_portal_btn)

        self._setup_sync_btn = QPushButton("Abrir Setup Sync", card)
        self._setup_sync_btn.setObjectName("SetupSyncButton")
        self._setup_sync_btn.setProperty("variant", "primary")
        self._setup_sync_btn.clicked.connect(self._on_open_setup_sync_clicked)
        row.addWidget(self._setup_sync_btn)

        card.body_layout.addLayout(row)

        self._sync_feedback = QLabel("", card)
        self._sync_feedback.setObjectName("SetupSyncFeedbackLabel")
        self._sync_feedback.setProperty("class", "ViewSubtitle")
        self._sync_feedback.setWordWrap(True)
        card.body_layout.addWidget(self._sync_feedback)

        return card

    def _build_lan_card(self, host: QWidget) -> FluentCard:
        card = FluentCard(
            title="Red Local / Dispositivos LAN",
            subtitle="Descubrimiento híbrido mDNS/SSDP/ARP, alias en SQLite y control remoto directo.",
            parent=host,
        )

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 4, 0, 4)
        top_row.setSpacing(8)

        pill = QLabel("Verificando...", card)
        pill.setObjectName("StatusPill")
        self._status_pills["lan"] = pill
        top_row.addWidget(pill)

        top_row.addStretch(1)

        self._lan_enable_btn = QPushButton("Activar Red Local", card)
        self._lan_enable_btn.setObjectName("LanEnableButton")
        self._lan_enable_btn.clicked.connect(self._on_enable_network_clicked)
        self._lan_enable_btn.setVisible(False)
        top_row.addWidget(self._lan_enable_btn)

        self._lan_scan_btn = QPushButton("Escanear red", card)
        self._lan_scan_btn.setObjectName("LanScanButton")
        self._lan_scan_btn.setProperty("variant", "primary")
        self._lan_scan_btn.clicked.connect(self._on_scan_clicked)
        top_row.addWidget(self._lan_scan_btn)

        card.body_layout.addLayout(top_row)

        self._lan_table = QTableWidget(0, 6, card)
        self._lan_table.setObjectName("LanDevicesTable")
        self._lan_table.setHorizontalHeaderLabels(["Fav", "Alias", "Tipo", "IP", "MAC", "Estado"])
        self._lan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._lan_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._lan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._lan_table.verticalHeader().setVisible(False)
        header = self._lan_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in range(2, 6):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self._lan_table.setMinimumHeight(165)
        self._lan_table.itemSelectionChanged.connect(self._on_lan_selection_changed)
        self._lan_table.itemChanged.connect(self._on_lan_table_item_changed)
        card.body_layout.addWidget(self._lan_table)

        alias_row = QHBoxLayout()
        alias_row.setContentsMargins(0, 4, 0, 2)
        alias_row.setSpacing(8)

        self._alias_input = QLineEdit(card)
        self._alias_input.setObjectName("LanAliasInput")
        self._alias_input.setPlaceholderText("Alias amigable (ej. TV Sala)")
        self._alias_input.textChanged.connect(lambda _t: self._update_action_buttons_state())
        alias_row.addWidget(self._alias_input, 1)

        self._save_alias_btn = QPushButton("Guardar alias", card)
        self._save_alias_btn.setObjectName("LanSaveAliasButton")
        self._save_alias_btn.clicked.connect(self._on_save_alias_clicked)
        alias_row.addWidget(self._save_alias_btn)

        card.body_layout.addLayout(alias_row)

        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(0, 2, 0, 4)
        ctrl_row.setSpacing(6)

        self._btn_power_on = QPushButton("Encender", card)
        self._btn_power_on.clicked.connect(self._on_power_on_clicked)
        ctrl_row.addWidget(self._btn_power_on)

        self._btn_power_off = QPushButton("Apagar", card)
        self._btn_power_off.clicked.connect(self._on_power_off_clicked)
        ctrl_row.addWidget(self._btn_power_off)

        self._btn_vol_down = QPushButton("Vol -", card)
        self._btn_vol_down.clicked.connect(self._on_vol_down_clicked)
        ctrl_row.addWidget(self._btn_vol_down)

        self._btn_vol_up = QPushButton("Vol +", card)
        self._btn_vol_up.clicked.connect(self._on_vol_up_clicked)
        ctrl_row.addWidget(self._btn_vol_up)

        self._btn_mute = QPushButton("Silenciar", card)
        self._btn_mute.clicked.connect(self._on_mute_clicked)
        ctrl_row.addWidget(self._btn_mute)

        self._btn_play_pause = QPushButton("Pausa / Play", card)
        self._btn_play_pause.clicked.connect(self._on_play_pause_clicked)
        ctrl_row.addWidget(self._btn_play_pause)

        self._btn_project = QPushButton("Proyectar", card)
        self._btn_project.clicked.connect(self._on_project_clicked)
        ctrl_row.addWidget(self._btn_project)

        card.body_layout.addLayout(ctrl_row)

        self._lan_feedback = QLabel("", card)
        self._lan_feedback.setObjectName("ViewSubtitle")
        self._lan_feedback.setWordWrap(True)
        card.body_layout.addWidget(self._lan_feedback)

        return card

    def _get_selected_device(self) -> NetworkDeviceRecord | None:
        selected_items = self._lan_table.selectedItems()
        row = selected_items[0].row() if selected_items else self._lan_table.currentRow()

        if row < 0 or row >= len(self._lan_devices):
            return None

        for col in (1, 0):
            item = self._lan_table.item(row, col)
            if item is not None:
                uid = str(item.data(Qt.ItemDataRole.UserRole) or "")
                if uid:
                    for dev in self._lan_devices:
                        if dev.device_uid == uid:
                            return dev

        return self._lan_devices[row]

    def _update_action_buttons_state(self) -> None:
        net_mod = getattr(self._app, "network_module", None)
        is_active = net_mod is not None and net_mod.state == ModuleState.ACTIVE
        selected = self._get_selected_device()
        has_sel = selected is not None
        alias_text = self._alias_input.text().strip()

        self._save_alias_btn.setEnabled(has_sel and bool(alias_text))
        controls_enabled = is_active and has_sel and not self._scanning
        self._btn_power_on.setEnabled(controls_enabled)
        self._btn_power_off.setEnabled(controls_enabled)
        self._btn_vol_down.setEnabled(controls_enabled)
        self._btn_vol_up.setEnabled(controls_enabled)
        self._btn_mute.setEnabled(controls_enabled)
        self._btn_play_pause.setEnabled(controls_enabled)
        self._btn_project.setEnabled(controls_enabled)

    def refresh(self) -> None:
        wa_ready = (
            self._app.paths.whatsapp_runtime_dir / "node_modules" / "@whiskeysockets" / "baileys"
        ).is_dir()
        wa_pill = self._status_pills.get("whatsapp")
        if wa_pill is not None:
            wa_pill.setText("Runtime Baileys listo (Rebanada 8)" if wa_ready else "Runtime pendiente")
            wa_pill.setProperty("state", "active" if wa_ready else "warning")
            wa_pill.style().unpolish(wa_pill)
            wa_pill.style().polish(wa_pill)

        self.refresh_oauth_section()
        self.refresh_lan_section()

    def refresh_lan_section(self) -> None:
        net_mod = getattr(self._app, "network_module", None)
        is_active = net_mod is not None and net_mod.state == ModuleState.ACTIVE

        pill = self._status_pills.get("lan")
        if pill is not None:
            pill.setText("Activo" if is_active else "Desactivado")
            pill.setProperty("state", "active" if is_active else "inactive")
            pill.style().unpolish(pill)
            pill.style().polish(pill)

        self._lan_enable_btn.setVisible(not is_active)
        self._lan_scan_btn.setEnabled(is_active and not self._scanning)

        selected_uid = ""
        sel_dev = self._get_selected_device()
        if sel_dev is not None:
            selected_uid = sel_dev.device_uid

        if is_active and net_mod is not None:
            self._lan_devices = net_mod.list_known_devices()
        elif getattr(self._app, "db", None) is not None and self._app.db.is_open:
            store = DeviceAliasStore(self._app.db, self._app.secrets)
            self._lan_devices = store.list_devices()
        else:
            self._lan_devices = []

        self._populating_lan_table = True
        self._lan_table.blockSignals(True)
        try:
            self._lan_table.setRowCount(len(self._lan_devices))
            restore_row = -1

            for r, dev in enumerate(self._lan_devices):
                item_fav = QTableWidgetItem()
                item_fav.setData(Qt.ItemDataRole.UserRole, dev.device_uid)
                item_fav.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item_fav.setCheckState(
                    Qt.CheckState.Checked if dev.is_favorite else Qt.CheckState.Unchecked
                )
                item_fav.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._lan_table.setItem(r, 0, item_fav)

                item_alias = QTableWidgetItem(dev.alias)
                item_alias.setData(Qt.ItemDataRole.UserRole, dev.device_uid)
                self._lan_table.setItem(r, 1, item_alias)

                type_label = _DEVICE_TYPE_LABELS.get(dev.device_type, dev.device_type.value)
                self._lan_table.setItem(r, 2, QTableWidgetItem(type_label))
                self._lan_table.setItem(r, 3, QTableWidgetItem(dev.last_ip or "—"))
                self._lan_table.setItem(r, 4, QTableWidgetItem(dev.mac_address or "—"))
                status_text = "En línea" if dev.online else "Guardado"
                self._lan_table.setItem(r, 5, QTableWidgetItem(status_text))

                if dev.device_uid == selected_uid:
                    restore_row = r

            if restore_row >= 0:
                self._lan_table.selectRow(restore_row)
            else:
                self._alias_input.setText("")
        finally:
            self._lan_table.blockSignals(False)
            self._populating_lan_table = False
        self._update_action_buttons_state()

    def _on_lan_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._populating_lan_table or self._closing:
            return
        if item.column() != 0:
            return

        uid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not uid:
            return

        is_checked = (item.checkState() == Qt.CheckState.Checked)
        net_mod = getattr(self._app, "network_module", None)
        if net_mod is not None and net_mod.state == ModuleState.ACTIVE:
            self._submit_lan_action(net_mod.set_favorite_device(uid, favorite=is_checked))
        elif getattr(self._app, "db", None) is not None and self._app.db.is_open:
            store = DeviceAliasStore(self._app.db, self._app.secrets)
            updated = store.set_favorite(uid, favorite=is_checked)
            if updated is not None:
                action_lbl = "marcado como dispositivo favorito" if is_checked else "desmarcado de favoritos"
                self._lan_feedback.setText(f"'{updated.alias}' {action_lbl}.")
            self.refresh_lan_section()

    def _submit_lan_action(self, coro: Coroutine[Any, Any, ToolResult]) -> None:
        if self._closing or not hasattr(self._app, "runtime"):
            return
        self._lan_feedback.setText("Enviando comando...")

        def _done_cb(fut: concurrent.futures.Future[ToolResult]) -> None:
            if self._closing:
                return
            try:
                res = fut.result()
                self._lan_action_completed.emit(res.ok, res.summary)
            except Exception as exc:
                self._lan_action_completed.emit(False, str(exc))

        try:
            fut = self._app.runtime.submit_async(coro)
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._lan_action_completed.emit(False, str(exc))

    def _on_scan_clicked(self) -> None:
        if self._scanning or self._closing:
            return
        net_mod = getattr(self._app, "network_module", None)
        if net_mod is None:
            self._lan_feedback.setText("Módulo de red no disponible.")
            return

        self._scanning = True
        self._lan_scan_btn.setEnabled(False)
        self._lan_scan_btn.setText("Escaneando...")
        self._lan_feedback.setText("Escaneando la red local en busca de dispositivos...")

        def _done_cb(fut: concurrent.futures.Future[ToolResult]) -> None:
            if self._closing:
                return
            try:
                res = fut.result()
                self._lan_scan_completed.emit(res, res.summary)
            except Exception as exc:
                self._lan_scan_completed.emit(None, str(exc))

        try:
            fut = self._app.runtime.submit_async(net_mod.scan_network())
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._scanning = False
            self._lan_scan_btn.setEnabled(True)
            self._lan_scan_btn.setText("Escanear red")
            self._lan_feedback.setText(str(exc))

    def _on_lan_scan_completed(self, _res: Any, msg: str) -> None:
        self._scanning = False
        self._lan_scan_btn.setText("Escanear red")
        self._lan_feedback.setText(msg)
        self.refresh_lan_section()

    def _on_lan_action_completed(self, _ok: bool, msg: str) -> None:
        self._lan_feedback.setText(msg)
        self.refresh_lan_section()

    def _on_lan_selection_changed(self) -> None:
        dev = self._get_selected_device()
        if dev is not None:
            self._alias_input.setText(dev.alias)
        else:
            self._alias_input.setText("")
        self._update_action_buttons_state()

    def _on_save_alias_clicked(self) -> None:
        dev = self._get_selected_device()
        if dev is None:
            return
        new_alias = self._alias_input.text().strip()
        if not new_alias:
            return

        net_mod = getattr(self._app, "network_module", None)
        if net_mod is not None and net_mod.state == ModuleState.ACTIVE:
            self._submit_lan_action(net_mod.rename_device(dev.device_uid, new_alias))
        elif getattr(self._app, "db", None) is not None and self._app.db.is_open:
            store = DeviceAliasStore(self._app.db, self._app.secrets)
            store.rename_alias(dev.device_uid, new_alias)
            self.refresh_lan_section()
            self._lan_feedback.setText(f"Alias guardado como '{new_alias}'.")

    def _on_enable_network_clicked(self) -> None:
        self._lan_enable_btn.setEnabled(False)
        self._lan_feedback.setText("Activando módulo de Red Local...")

        def _cb(fut: concurrent.futures.Future[bool]) -> None:
            if self._closing:
                return
            try:
                ok = fut.result()
                msg = "Módulo de Red Local activado." if ok else "No se pudo activar el módulo."
                self._module_toggle_completed.emit(ok, msg)
            except Exception as exc:
                self._module_toggle_completed.emit(False, str(exc))

        try:
            fut = self._app.set_module_enabled("network", True)
            fut.add_done_callback(_cb)
        except RuntimeError as exc:
            self._module_toggle_completed.emit(False, str(exc))

    def _on_module_toggle_completed(self, _success: bool, msg: str) -> None:
        self._lan_enable_btn.setEnabled(True)
        self._lan_feedback.setText(msg)
        self.refresh_lan_section()

    def _on_module_state_changed(self, event: Any) -> None:
        if getattr(event, "module_id", "") == "network":
            self.refresh_lan_section()

    def _on_settings_updated(self, _event: Any) -> None:
        self.refresh_oauth_section()
        self.refresh_lan_section()

    def refresh_oauth_section(self) -> None:
        session = self._app.get_google_auth_status()
        google_pill = self._status_pills.get("google")
        if google_pill is not None:
            if session.connected:
                label = f"Conectado ({session.email})" if session.email else "Conectado"
                google_pill.setText(label)
                google_pill.setProperty("state", "active")
            elif session.client_configured:
                google_pill.setText("Desconectado")
                google_pill.setProperty("state", "inactive")
            else:
                google_pill.setText("Falta GOOGLE_OAUTH_CLIENT_ID")
                google_pill.setProperty("state", "warning")
            google_pill.style().unpolish(google_pill)
            google_pill.style().polish(google_pill)

        self._google_connect_btn.setEnabled(not self._google_busy)
        self._google_connect_btn.setText(
            "Esperando navegador..."
            if self._google_busy
            else ("Reconectar Google" if session.connected else "Iniciar sesión con Google")
        )
        self._google_disconnect_btn.setVisible(session.connected)
        self._google_disconnect_btn.setEnabled(session.connected and not self._google_busy)

        sync_pill = self._status_pills.get("setup_sync")
        if sync_pill is not None:
            srv = self._app.oauth_server
            if srv.is_running:
                sync_pill.setText(f"Escuchando en 127.0.0.1:{srv.port}")
                sync_pill.setProperty("state", "active")
            else:
                port = self._app.settings.developer.oauth_callback_port
                sync_pill.setText(f"Listo bajo demanda (:{port})")
                sync_pill.setProperty("state", "inactive")
            sync_pill.style().unpolish(sync_pill)
            sync_pill.style().polish(sync_pill)

    def _on_google_oauth_clicked(self) -> None:
        if self._google_busy or self._closing:
            return
        if not self._app.google_auth.is_client_configured():
            self._google_feedback.setText(
                "Configura GOOGLE_OAUTH_CLIENT_ID mediante Setup Sync o .env antes de iniciar sesión."
            )
            return

        self._google_busy = True
        self._google_feedback.setText("Abriendo navegador para iniciar sesión con Google (PKCE)...")
        self.refresh_oauth_section()

        def _done_cb(fut: concurrent.futures.Future[GoogleAuthSessionInfo]) -> None:
            if self._closing:
                return
            try:
                info = fut.result()
                label = info.email or info.name or "Google"
                self._oauth_action_completed.emit(True, f"Cuenta conectada: {label}.")
            except Exception as exc:
                self._oauth_action_completed.emit(False, str(exc))

        try:
            fut = self._app.start_google_oauth_flow()
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._oauth_action_completed.emit(False, str(exc))

    def _on_google_disconnect_clicked(self) -> None:
        if self._google_busy or self._closing:
            return
        self._google_busy = True
        self._google_feedback.setText("Desconectando cuenta de Google...")
        self.refresh_oauth_section()

        def _done_cb(fut: concurrent.futures.Future[bool]) -> None:
            if self._closing:
                return
            try:
                fut.result()
                self._oauth_action_completed.emit(True, "Cuenta de Google desconectada.")
            except Exception as exc:
                self._oauth_action_completed.emit(False, str(exc))

        try:
            fut = self._app.disconnect_google_account(revoke_remote=False)
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._oauth_action_completed.emit(False, str(exc))

    def _on_oauth_action_completed(self, _ok: bool, message: str) -> None:
        self._google_busy = False
        self._google_feedback.setText(message)
        self.refresh_oauth_section()

    def _on_open_portal_clicked(self) -> None:
        url = self._app.open_web_portal(page="index.html")
        self._sync_feedback.setText(f"Portal abierto en el navegador: {url}")

    def _on_open_setup_sync_clicked(self) -> None:
        if self._closing:
            return
        self._setup_sync_btn.setEnabled(False)
        self._sync_feedback.setText("Iniciando servidor local 127.0.0.1 y abriendo Setup Sync...")

        def _done_cb(fut: concurrent.futures.Future[str]) -> None:
            if self._closing:
                return
            try:
                fut.result()
                port = self._app.oauth_server.port
                self._setup_sync_opened.emit(
                    True,
                    f"Setup Sync activo en 127.0.0.1:{port}. Envía tus claves desde la pestaña abierta.",
                )
            except Exception as exc:
                self._setup_sync_opened.emit(False, str(exc))

        try:
            fut = self._app.open_setup_sync_portal(page="guides.html")
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._setup_sync_opened.emit(False, str(exc))

    def _on_setup_sync_opened(self, _ok: bool, message: str) -> None:
        self._setup_sync_btn.setEnabled(True)
        self._sync_feedback.setText(message)
        self.refresh_oauth_section()

    def _on_oauth_completed_event(self, event: Any) -> None:
        if getattr(event, "provider", "") == "google":
            msg = str(getattr(event, "message", "") or "")
            if msg:
                self._google_feedback.setText(msg)
            self.refresh_oauth_section()

    def _on_oauth_error_event(self, event: Any) -> None:
        if getattr(event, "provider", "") == "google":
            msg = str(getattr(event, "message", "") or "")
            if msg:
                self._google_feedback.setText(msg)
            self.refresh_oauth_section()

    def _on_setup_sync_received_event(self, event: Any) -> None:
        msg = str(getattr(event, "message", "") or "Setup Sync recibido.")
        self._sync_feedback.setText(msg)
        self.refresh_oauth_section()

    def _on_power_on_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            self._submit_lan_action(net_mod.control_power(dev.device_uid, "on"))

    def _on_power_off_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            self._submit_lan_action(net_mod.control_power(dev.device_uid, "off"))

    def _on_vol_down_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            step = 10 if dev.device_type == DeviceType.CHROMECAST else 3
            self._submit_lan_action(net_mod.control_volume(dev.device_uid, delta=-step))

    def _on_vol_up_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            step = 10 if dev.device_type == DeviceType.CHROMECAST else 3
            self._submit_lan_action(net_mod.control_volume(dev.device_uid, delta=step))

    def _on_mute_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            self._submit_lan_action(net_mod.control_volume(dev.device_uid, mute=True))

    def _on_play_pause_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            action = "play_pause" if dev.device_type == DeviceType.SAMSUNG_TV else "pause"
            self._submit_lan_action(net_mod.control_media(dev.device_uid, action=action))

    def _on_project_clicked(self) -> None:
        dev = self._get_selected_device()
        net_mod = getattr(self._app, "network_module", None)
        if dev is not None and net_mod is not None:
            self._submit_lan_action(net_mod.project_screen(dev.device_uid, action="connect"))

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        with contextlib.suppress(RuntimeError, TypeError):
            self._bridge.module_state_changed.disconnect(self._on_module_state_changed)
        with contextlib.suppress(RuntimeError, TypeError):
            self._bridge.settings_updated.disconnect(self._on_settings_updated)
        with contextlib.suppress(RuntimeError, TypeError):
            self._bridge.oauth_completed.disconnect(self._on_oauth_completed_event)
        with contextlib.suppress(RuntimeError, TypeError):
            self._bridge.oauth_error.disconnect(self._on_oauth_error_event)
        with contextlib.suppress(RuntimeError, TypeError):
            self._bridge.setup_sync_received.disconnect(self._on_setup_sync_received_event)
        super().closeEvent(event)
