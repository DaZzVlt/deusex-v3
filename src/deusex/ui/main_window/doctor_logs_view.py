from __future__ import annotations

import platform
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deusex import __version__
from deusex.app import DeusExApplication
from deusex.core.contracts import DiagnosticItem, DiagnosticSeverity
from deusex.core.logging_setup import anonymize_log_text, get_logging_state
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard

_LOG_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("all", "Todo"),
    ("error", "Errores"),
    ("fast_path", "Fast-Path"),
    ("ai", "IA"),
    ("system", "Sistema"),
)


class DoctorLogsView(QWidget):
    """Centro de Diagnóstico (Doctor con semáforos) + Visor legible de Latest Debug.txt."""

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._active_log_cat = "all"
        self._log_cat_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self.run_diagnostics()
        self.refresh_logs()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        title = QLabel("Doctor y Registro de Sesión", self)
        title.setObjectName("ViewTitle")
        root.addWidget(title)

        sub = QLabel(
            "Diagnóstico integral en español claro y copia de soporte anonimizada (sin claves ni rutas personales).",
            self,
        )
        sub.setObjectName("ViewSubtitle")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Pestañas por píldora
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(8)

        self._tab_doctor_btn = QPushButton("Salud del Sistema (Doctor)", self)
        self._tab_doctor_btn.setProperty("variant", "pill")
        self._tab_doctor_btn.setProperty("selected", True)
        self._tab_doctor_btn.clicked.connect(lambda: self._switch_tab(0))
        tabs_row.addWidget(self._tab_doctor_btn)

        self._tab_logs_btn = QPushButton("Registro (Latest Debug.txt)", self)
        self._tab_logs_btn.setProperty("variant", "pill")
        self._tab_logs_btn.setProperty("selected", False)
        self._tab_logs_btn.clicked.connect(lambda: self._switch_tab(1))
        tabs_row.addWidget(self._tab_logs_btn)

        tabs_row.addStretch(1)
        root.addLayout(tabs_row)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_doctor_tab())
        self._stack.addWidget(self._build_logs_tab())
        root.addWidget(self._stack, 1)

    def _build_doctor_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        run_btn = QPushButton("Ejecutar diagnóstico ahora", page)
        run_btn.setProperty("variant", "primary")
        run_btn.clicked.connect(self.run_diagnostics)
        bar.addWidget(run_btn)
        bar.addStretch(1)
        self._doctor_summary_pill = QLabel("", page)
        self._doctor_summary_pill.setObjectName("StatusPill")
        bar.addWidget(self._doctor_summary_pill)
        layout.addLayout(bar)

        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._diag_container = QWidget(scroll)
        self._diag_layout = QVBoxLayout(self._diag_container)
        self._diag_layout.setContentsMargins(0, 2, 6, 8)
        self._diag_layout.setSpacing(10)
        self._diag_layout.addStretch(1)
        scroll.setWidget(self._diag_container)
        layout.addWidget(scroll, 1)
        return page

    def _build_logs_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.setSpacing(8)

        self.log_search_edit = QLineEdit(page)
        self.log_search_edit.setPlaceholderText("Filtrar entradas del registro...")
        self.log_search_edit.textChanged.connect(lambda _t: self.refresh_logs())
        controls.addWidget(self.log_search_edit, 1)

        for cat_key, label in _LOG_CATEGORIES:
            btn = QPushButton(label, page)
            btn.setProperty("variant", "pill")
            btn.setProperty("selected", cat_key == self._active_log_cat)
            btn.clicked.connect(lambda _checked=False, c=cat_key: self._select_log_cat(c))
            self._log_cat_buttons[cat_key] = btn
            controls.addWidget(btn)

        layout.addLayout(controls)

        actions = QHBoxLayout()
        refresh_btn = QPushButton("Actualizar", page)
        refresh_btn.clicked.connect(self.refresh_logs)
        actions.addWidget(refresh_btn)

        open_btn = QPushButton("Abrir archivo completo", page)
        open_btn.setToolTip("Abre Latest Debug.txt en el editor predeterminado de Windows")
        open_btn.clicked.connect(self._open_full_log_file)
        actions.addWidget(open_btn)

        self.copy_support_btn = QPushButton("Copiar reporte para soporte", page)
        self.copy_support_btn.setProperty("variant", "primary")
        self.copy_support_btn.setToolTip(
            "Anonimiza claves, tokens, IPs LAN, MACs y rutas de usuario antes de copiar"
        )
        self.copy_support_btn.clicked.connect(self.copy_anonymized_support_report)
        actions.addWidget(self.copy_support_btn)

        actions.addStretch(1)
        self._copy_feedback_lbl = QLabel("", page)
        self._copy_feedback_lbl.setObjectName("MutedLabel")
        actions.addWidget(self._copy_feedback_lbl)
        layout.addLayout(actions)

        self.log_viewer = QPlainTextEdit(page)
        self.log_viewer.setReadOnly(True)
        layout.addWidget(self.log_viewer, 1)
        return page

    def _switch_tab(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._tab_doctor_btn.setProperty("selected", index == 0)
        self._tab_logs_btn.setProperty("selected", index == 1)
        for btn in (self._tab_doctor_btn, self._tab_logs_btn):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if index == 0:
            self.run_diagnostics()
        else:
            self.refresh_logs()

    def collect_diagnostics(self) -> list[DiagnosticItem]:
        items: list[DiagnosticItem] = [
            DiagnosticItem(
                code="env_python",
                area="Sistema",
                title=f"Entorno DeusEx v{__version__}",
                severity=DiagnosticSeverity.OK,
                message=f"Python {sys.version.split()[0]} en {platform.system()} {platform.release()}",
            ),
            DiagnosticItem(
                code="db_wal",
                area="Persistencia",
                title="Base de datos SQLite WAL",
                severity=(
                    DiagnosticSeverity.OK
                    if self._app.db.is_open
                    and self._app.db.journal_mode() == "wal"
                    and self._app.db.integrity_ok()
                    else DiagnosticSeverity.ERROR
                ),
                message=(
                    f"Modo {self._app.db.journal_mode().upper()} · Esquema v{self._app.db.schema_version()}"
                    if self._app.db.is_open
                    else "Base de datos cerrada"
                ),
                remediation="Reinicia DeusEx para reabrir el almacenamiento WAL.",
            ),
            DiagnosticItem(
                code="runtime_threads",
                area="Concurrencia",
                title="Hilos del motor (AsyncCore / ComSTA / Audio)",
                severity=(
                    DiagnosticSeverity.OK
                    if self._app.runtime.async_core.is_running
                    and self._app.runtime.com_sta.is_running
                    and self._app.runtime.audio_input.is_running
                    else DiagnosticSeverity.ERROR
                ),
                message="Los 3 hilos secundarios están operativos y aislados del hilo gráfico.",
                remediation="Reinicia la aplicación si algún hilo secundario se detuvo.",
            ),
        ]

        configured_providers = self._app.pool.list_configured_provider_ids()
        items.append(
            DiagnosticItem(
                code="ai_provider",
                area="Cerebro IA",
                title="Proveedores de IA configurados",
                severity=(
                    DiagnosticSeverity.OK if configured_providers else DiagnosticSeverity.WARNING
                ),
                message=(
                    f"Activos: {', '.join(configured_providers)}"
                    if configured_providers
                    else "Modo local sin clave IA (Fast-Path activo)."
                ),
                remediation="Pega tu GEMINI_API_KEY en Ajustes → IA y Claves para conversar con la nube.",
            )
        )

        vosk_ok = (self._app.paths.vosk_es_dir / "am" / "final.mdl").is_file()
        ffmpeg_ok = self._app.paths.ffmpeg_exe.is_file()
        wa_ok = (
            self._app.paths.whatsapp_runtime_dir / "node_modules" / "@whiskeysockets" / "baileys"
        ).is_dir()
        all_rt = vosk_ok and ffmpeg_ok and wa_ok
        items.append(
            DiagnosticItem(
                code="local_runtimes",
                area="Runtimes",
                title="Runtimes offline pre-empaquetados",
                severity=DiagnosticSeverity.OK if all_rt else DiagnosticSeverity.WARNING,
                message=(
                    f"Vosk ES: {'OK' if vosk_ok else 'Falta'} · "
                    f"FFmpeg: {'OK' if ffmpeg_ok else 'Falta'} · "
                    f"WhatsApp Baileys: {'OK' if wa_ok else 'Falta'}"
                ),
                remediation="Ejecuta `uv run python scripts/setup_runtimes.py` si falta algún runtime.",
            )
        )

        for mod in self._app.engine.modules.list_modules():
            items.extend(mod.diagnose())

        return items

    def run_diagnostics(self) -> list[DiagnosticItem]:
        while self._diag_layout.count() > 1:
            item = self._diag_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()

        diagnostics = self.collect_diagnostics()
        errors = sum(1 for d in diagnostics if d.severity == DiagnosticSeverity.ERROR)
        warnings = sum(1 for d in diagnostics if d.severity == DiagnosticSeverity.WARNING)

        if errors > 0:
            self._doctor_summary_pill.setText(f"{errors} error(es) · {warnings} aviso(s)")
            self._doctor_summary_pill.setProperty("state", "danger")
        elif warnings > 0:
            self._doctor_summary_pill.setText(f"Operativo con {warnings} aviso(s)")
            self._doctor_summary_pill.setProperty("state", "warning")
        else:
            self._doctor_summary_pill.setText("Todo en verde")
            self._doctor_summary_pill.setProperty("state", "active")
        self._doctor_summary_pill.style().unpolish(self._doctor_summary_pill)
        self._doctor_summary_pill.style().polish(self._doctor_summary_pill)

        severity_style = {
            DiagnosticSeverity.OK: ("OK", "active"),
            DiagnosticSeverity.WARNING: ("Aviso", "warning"),
            DiagnosticSeverity.ERROR: ("Error", "danger"),
        }

        for diag in diagnostics:
            card = FluentCard(parent=self._diag_container)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)

            badge_text, state_prop = severity_style[diag.severity]
            pill = QLabel(badge_text, card)
            pill.setObjectName("StatusPill")
            pill.setProperty("state", state_prop)
            row.addWidget(pill)

            col = QVBoxLayout()
            col.setContentsMargins(0, 0, 0, 0)
            col.setSpacing(2)
            t_lbl = QLabel(f"[{diag.area}] {diag.title}", card)
            t_lbl.setObjectName("CardTitle")
            col.addWidget(t_lbl)

            m_lbl = QLabel(diag.message, card)
            m_lbl.setWordWrap(True)
            col.addWidget(m_lbl)

            if diag.severity != DiagnosticSeverity.OK and diag.remediation:
                r_lbl = QLabel(f"Solución sugerida: {diag.remediation}", card)
                r_lbl.setObjectName("MutedLabel")
                r_lbl.setWordWrap(True)
                col.addWidget(r_lbl)

            row.addLayout(col, 1)
            card.body_layout.addLayout(row)
            self._diag_layout.insertWidget(max(0, self._diag_layout.count() - 1), card)

        return diagnostics

    def _select_log_cat(self, cat: str) -> None:
        self._active_log_cat = cat
        for key, btn in self._log_cat_buttons.items():
            btn.setProperty("selected", key == cat)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self.refresh_logs()

    def refresh_logs(self) -> None:
        state = get_logging_state()
        if state is None:
            self.log_viewer.setPlainText("El sistema de logs no está activo.")
            return
        entries = state.ring_buffer.snapshot(
            category=self._active_log_cat,
            query=self.log_search_edit.text(),
        )
        lines = [entry.formatted for entry in entries[-300:]]
        self.log_viewer.setPlainText("\n".join(lines) if lines else "Sin entradas para este filtro.")

    def _open_full_log_file(self) -> None:
        log_path = self._app.paths.latest_debug_log
        if log_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_path)))

    def build_anonymized_support_report(self) -> str:
        log_path = self._app.paths.latest_debug_log
        raw = (
            log_path.read_text(encoding="utf-8", errors="replace")
            if log_path.is_file()
            else self.log_viewer.toPlainText()
        )
        extra_secrets = [
            val
            for key in list(self._app.secrets.keys())
            if (val := self._app.secrets.get(key)) is not None
        ]
        header = (
            f"=== DeusEx v{__version__} · Reporte de Soporte Anonimizado ===\n"
            f"OS: {platform.system()} {platform.release()} | Python: {sys.version.split()[0]}\n"
            "==========================================================\n"
        )
        return header + anonymize_log_text(raw[-20_000:], extra_secrets=extra_secrets)

    def copy_anonymized_support_report(self) -> str:
        report = self.build_anonymized_support_report()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(report)
        self._copy_feedback_lbl.setText("Reporte anonimizado copiado al portapapeles.")
        return report
