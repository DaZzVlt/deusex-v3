from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deusex import __version__
from deusex.app import DeusExApplication
from deusex.core.events import CostUsageUpdatedEvent, ProfileChangedEvent
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.chat_view import ChatView
from deusex.ui.main_window.doctor_logs_view import DoctorLogsView
from deusex.ui.main_window.integrations_view import IntegrationsView
from deusex.ui.main_window.memory_view import MemoryView
from deusex.ui.main_window.modules_view import ModulesView
from deusex.ui.main_window.routines_view import RoutinesView
from deusex.ui.main_window.settings_view import SettingsView
from deusex.ui.theme import ThemeMode

_SIDEBAR_EXPANDED_WIDTH = 224
_SIDEBAR_COLLAPSED_WIDTH = 68


@dataclass(frozen=True, slots=True)
class _NavItem:
    key: str
    icon: str
    label: str
    page_index: int


_NAV_ITEMS: tuple[_NavItem, ...] = (
    _NavItem("chat", "💬", "Chat", 0),
    _NavItem("modules", "🧩", "Módulos", 1),
    _NavItem("routines", "⏱", "Rutinas y Agenda", 2),
    _NavItem("memory", "🧠", "Memoria", 3),
    _NavItem("integrations", "🔗", "Integraciones", 4),
    _NavItem("settings", "⚙", "Sistema y Ajustes", 5),
)


class MainWindow(QMainWindow):
    """Ventana Principal Fluent con Sidebar colapsable, 6 secciones + Doctor y pie CostGuard."""

    theme_refresh_requested = Signal()
    open_minichat_requested = Signal()

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        *,
        on_theme_refresh: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._on_theme_refresh = on_theme_refresh
        self._allow_close = False
        self._sidebar_collapsed = bool(self._app.settings.ui.sidebar_collapsed)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._active_nav_key = "chat"

        self.setWindowTitle(f"DeusEx v{__version__}")
        self.resize(1160, 740)
        self.setMinimumSize(860, 580)

        self._build_ui()
        self._connect_signals()
        self._apply_sidebar_collapse(self._sidebar_collapsed, persist=False)
        self.navigate_to("chat")
        self._refresh_footer_cost()

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("MainRoot")
        self.setCentralWidget(central)

        shell_layout = QHBoxLayout(central)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        # Barra lateral colapsable
        self.sidebar = QWidget(central)
        self.sidebar.setObjectName("Sidebar")
        sb_layout = QVBoxLayout(self.sidebar)
        sb_layout.setContentsMargins(10, 14, 10, 14)
        sb_layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(4, 0, 2, 8)
        brand_row.setSpacing(6)

        self._brand_lbl = QLabel("DeusEx", self.sidebar)
        self._brand_lbl.setObjectName("SidebarBrand")
        brand_row.addWidget(self._brand_lbl, 1)

        self.collapse_btn = QPushButton("☰", self.sidebar)
        self.collapse_btn.setProperty("variant", "ghost")
        self.collapse_btn.setFixedSize(34, 34)
        self.collapse_btn.setToolTip("Expandir o contraer la barra lateral")
        self.collapse_btn.clicked.connect(self.toggle_sidebar)
        brand_row.addWidget(self.collapse_btn)
        sb_layout.addLayout(brand_row)

        for item in _NAV_ITEMS:
            btn = QPushButton(f"{item.icon}  {item.label}", self.sidebar)
            btn.setObjectName("SidebarNavButton")
            btn.setToolTip(item.label)
            btn.clicked.connect(lambda _checked=False, k=item.key: self.navigate_to(k))
            self._nav_buttons[item.key] = btn
            sb_layout.addWidget(btn)

        sb_layout.addStretch(1)

        # Pie del sidebar: Cost Guard + accesos rápidos Doctor y Tema
        self._footer_card = QFrame(self.sidebar)
        self._footer_card.setObjectName("SidebarFooterCard")
        footer_layout = QVBoxLayout(self._footer_card)
        footer_layout.setContentsMargins(10, 10, 10, 10)
        footer_layout.setSpacing(6)

        self.cost_footer_lbl = QLabel("0 órdenes instantáneas hoy · 0 tokens", self._footer_card)
        self.cost_footer_lbl.setObjectName("MutedLabel")
        self.cost_footer_lbl.setWordWrap(True)
        footer_layout.addWidget(self.cost_footer_lbl)

        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(0, 0, 0, 0)
        quick_row.setSpacing(6)

        self.doctor_quick_btn = QPushButton("🩺 Doctor", self._footer_card)
        self.doctor_quick_btn.setProperty("variant", "ghost")
        self.doctor_quick_btn.setToolTip("Abrir Centro de Diagnóstico y Logs")
        self.doctor_quick_btn.clicked.connect(lambda: self.navigate_to("doctor"))
        quick_row.addWidget(self.doctor_quick_btn, 1)

        self.theme_quick_btn = QPushButton("◐", self._footer_card)
        self.theme_quick_btn.setProperty("variant", "ghost")
        self.theme_quick_btn.setFixedWidth(36)
        self.theme_quick_btn.setToolTip("Alternar tema (Oscuro / Claro / Cyber-HUD)")
        self.theme_quick_btn.clicked.connect(self._cycle_quick_theme)
        quick_row.addWidget(self.theme_quick_btn)

        footer_layout.addLayout(quick_row)
        sb_layout.addWidget(self._footer_card)

        shell_layout.addWidget(self.sidebar)

        # Stack principal de vistas
        self.stack = QStackedWidget(central)
        self.chat_view = ChatView(self._app, self._bridge, self.stack)
        self.modules_view = ModulesView(self._app, self._bridge, self.stack)
        self.routines_view = RoutinesView(self._app, self._bridge, self.stack)
        self.memory_view = MemoryView(self._app, self._bridge, self.stack)
        self.integrations_view = IntegrationsView(self._app, self._bridge, self.stack)
        self.settings_view = SettingsView(self._app, self._bridge, self.stack)
        self.doctor_logs_view = DoctorLogsView(self._app, self._bridge, self.stack)

        self.stack.addWidget(self.chat_view)          # 0
        self.stack.addWidget(self.modules_view)       # 1
        self.stack.addWidget(self.routines_view)      # 2
        self.stack.addWidget(self.memory_view)        # 3
        self.stack.addWidget(self.integrations_view)  # 4
        self.stack.addWidget(self.settings_view)      # 5
        self.stack.addWidget(self.doctor_logs_view)   # 6

        shell_layout.addWidget(self.stack, 1)

    def _connect_signals(self) -> None:
        self.settings_view.theme_changed.connect(self._emit_theme_refresh)
        self.settings_view.open_doctor_requested.connect(lambda: self.navigate_to("doctor"))

        self._bridge.cost_usage_updated.connect(self._on_cost_usage_updated)
        self._bridge.profile_changed.connect(self._on_profile_changed)

    def _emit_theme_refresh(self) -> None:
        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        self.modules_view.set_accent_color(accent)
        self.settings_view.set_accent_color(accent)
        self.theme_refresh_requested.emit()
        if self._on_theme_refresh is not None:
            self._on_theme_refresh()

    def _on_profile_changed(self, event: object) -> None:
        if isinstance(event, ProfileChangedEvent):
            self._emit_theme_refresh()

    def _on_cost_usage_updated(self, event: object) -> None:
        if isinstance(event, CostUsageUpdatedEvent):
            self._refresh_footer_cost()

    def _refresh_footer_cost(self) -> None:
        summary = self._app.cost_guard.today_summary()
        self.cost_footer_lbl.setText(summary.sidebar_label_es)

    def _cycle_quick_theme(self) -> None:
        order: tuple[ThemeMode, ...] = ("fluent_dark", "fluent_light", "cyber_hud")
        current = self._app.settings.ui.theme
        next_mode = order[0] if current not in order else order[(order.index(current) + 1) % len(order)]
        self._app.settings.ui.theme = next_mode
        self._app.save_settings(section="ui")
        self.settings_view.refresh_from_app()
        self._emit_theme_refresh()

    def toggle_sidebar(self) -> None:
        self._apply_sidebar_collapse(not self._sidebar_collapsed, persist=True)

    def _apply_sidebar_collapse(self, collapsed: bool, *, persist: bool) -> None:
        self._sidebar_collapsed = bool(collapsed)
        self.sidebar.setFixedWidth(
            _SIDEBAR_COLLAPSED_WIDTH if self._sidebar_collapsed else _SIDEBAR_EXPANDED_WIDTH
        )
        self._brand_lbl.setVisible(not self._sidebar_collapsed)
        self.cost_footer_lbl.setVisible(not self._sidebar_collapsed)
        self.doctor_quick_btn.setText("🩺" if self._sidebar_collapsed else "🩺 Doctor")
        for item in _NAV_ITEMS:
            btn = self._nav_buttons[item.key]
            btn.setText(item.icon if self._sidebar_collapsed else f"{item.icon}  {item.label}")
        if persist:
            self._app.settings.ui.sidebar_collapsed = self._sidebar_collapsed
            self._app.save_settings(section="ui")

    def navigate_to(self, section_key: str) -> None:
        self._active_nav_key = section_key
        if section_key == "doctor":
            self.stack.setCurrentIndex(6)
            self.doctor_logs_view.run_diagnostics()
        else:
            target = next((item for item in _NAV_ITEMS if item.key == section_key), _NAV_ITEMS[0])
            self.stack.setCurrentIndex(target.page_index)
            if target.key == "memory":
                self.memory_view.refresh()
            elif target.key == "routines":
                self.routines_view.refresh_timers()
            elif target.key == "modules":
                self.modules_view.refresh()
            elif target.key == "settings":
                self.settings_view.refresh_from_app()
                self.settings_view.check_clipboard_for_key()

        for key, btn in self._nav_buttons.items():
            btn.setProperty("active", key == section_key)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def prepare_for_exit(self) -> None:
        """Autoriza el cierre real de la ventana (desde bandeja o apagado limpio)."""
        self._allow_close = True

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._allow_close:
            event.ignore()
            self.hide()
            return
        self.routines_view.close()
        super().closeEvent(event)
