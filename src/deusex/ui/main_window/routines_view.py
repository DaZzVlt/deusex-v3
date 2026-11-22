from __future__ import annotations

import concurrent.futures
from contextlib import suppress
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.core.scheduler import ScheduledJob, format_duration_es, parse_duration_seconds
from deusex.modules.pc.module import PCModule
from deusex.modules.pc.routines import (
    RoutineRecord,
    RoutineStep,
    RoutineStepKind,
    describe_step_es,
)
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard
from deusex.ui.components.toggle_switch import ToggleSwitch


class RoutinesView(QWidget):
    """Vista de Temporizadores/Alarmas offline (arriba) y Modos/Rutinas (abajo)."""

    _async_feedback = Signal(str, bool)

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._draft_steps: list[RoutineStep] = []

        self._build_ui()
        self._bridge.scheduled_jobs_updated.connect(lambda _count: self.refresh_timers())
        self._bridge.scheduled_job_fired.connect(lambda _event: self.refresh_timers())
        self._bridge.module_state_changed.connect(self._on_module_state_changed)
        self._async_feedback.connect(self._on_async_feedback)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        self.refresh_timers()
        self.refresh_routines()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)

        title = QLabel("Rutinas y Agenda Local", self)
        title.setObjectName("ViewTitle")
        root.addWidget(title)

        subtitle = QLabel(
            "Temporizadores y alarmas 100% offline en SQLite WAL + Modos rápidos del PC.",
            self,
        )
        subtitle.setObjectName("ViewSubtitle")
        root.addWidget(subtitle)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget(scroll)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 8)
        content_layout.setSpacing(14)

        # Mitad superior: Temporizadores
        quick_timer_card = FluentCard(
            title="Nuevo temporizador rápido",
            subtitle="Funciona sin internet y sin gastar tokens (también por chat: 'pon un temporizador de 5 segundos').",
            parent=content,
        )
        timer_form = QHBoxLayout()
        timer_form.setContentsMargins(0, 0, 0, 0)
        timer_form.setSpacing(8)

        self.timer_label_edit = QLineEdit(quick_timer_card)
        self.timer_label_edit.setPlaceholderText("Etiqueta (ej. Café, Pausa activa)")
        timer_form.addWidget(self.timer_label_edit, 1)

        self.timer_amount_spin = QSpinBox(quick_timer_card)
        self.timer_amount_spin.setRange(1, 999)
        self.timer_amount_spin.setValue(5)
        self.timer_amount_spin.setMinimumWidth(80)
        timer_form.addWidget(self.timer_amount_spin)

        self.timer_unit_combo = QComboBox(quick_timer_card)
        self.timer_unit_combo.addItem("segundos", "s")
        self.timer_unit_combo.addItem("minutos", "min")
        self.timer_unit_combo.addItem("horas", "h")
        self.timer_unit_combo.setMinimumWidth(110)
        timer_form.addWidget(self.timer_unit_combo)

        create_btn = QPushButton("Iniciar", quick_timer_card)
        create_btn.setProperty("variant", "primary")
        create_btn.clicked.connect(self._on_create_timer)
        timer_form.addWidget(create_btn)

        quick_timer_card.body_layout.addLayout(timer_form)
        content_layout.addWidget(quick_timer_card)

        self._timers_section = FluentCard(
            title="Temporizadores y alarmas activos",
            parent=content,
        )
        self._timers_list_layout = QVBoxLayout()
        self._timers_list_layout.setContentsMargins(0, 0, 0, 0)
        self._timers_list_layout.setSpacing(8)
        self._timers_section.body_layout.addLayout(self._timers_list_layout)
        content_layout.addWidget(self._timers_section)

        # Mitad inferior: Modos y Rutinas (Rebanada 2)
        self._routines_card = FluentCard(
            title="Modos y Rutinas",
            subtitle="Encadena apps, volumen, brillo y monitores con un solo comando o en 1 clic.",
            parent=content,
        )
        routines_header = QHBoxLayout()
        routines_header.setContentsMargins(0, 0, 0, 0)
        routines_header.setSpacing(8)

        self.capture_state_btn = QPushButton("Capturar estado actual", self._routines_card)
        self.capture_state_btn.setProperty("variant", "primary")
        self.capture_state_btn.setToolTip(
            "Guarda el volumen actual y las ventanas abiertas en sus monitores como rutina"
        )
        self.capture_state_btn.clicked.connect(self._on_capture_current_state)
        routines_header.addWidget(self.capture_state_btn)

        self.new_routine_btn = QPushButton("Nueva rutina", self._routines_card)
        self.new_routine_btn.setProperty("variant", "ghost")
        self.new_routine_btn.clicked.connect(self._on_toggle_builder)
        routines_header.addWidget(self.new_routine_btn)

        routines_header.addStretch(1)
        self._routines_card.body_layout.addLayout(routines_header)

        self._routines_status_lbl = QLabel("", self._routines_card)
        self._routines_status_lbl.setObjectName("MutedLabel")
        self._routines_status_lbl.setWordWrap(True)
        self._routines_card.body_layout.addWidget(self._routines_status_lbl)

        self._routines_feedback_lbl = QLabel("", self._routines_card)
        self._routines_feedback_lbl.setObjectName("MutedLabel")
        self._routines_feedback_lbl.setWordWrap(True)
        self._routines_feedback_lbl.setVisible(False)
        self._routines_card.body_layout.addWidget(self._routines_feedback_lbl)

        # Constructor visual colapsable de nueva rutina
        self._builder_panel = QWidget(self._routines_card)
        builder_layout = QVBoxLayout(self._builder_panel)
        builder_layout.setContentsMargins(0, 6, 0, 6)
        builder_layout.setSpacing(8)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 0, 0, 0)
        meta_row.setSpacing(8)

        self.routine_name_edit = QLineEdit(self._builder_panel)
        self.routine_name_edit.setPlaceholderText("Nombre (ej. Modo Estudio)")
        meta_row.addWidget(self.routine_name_edit, 1)

        self.routine_trigger_edit = QLineEdit(self._builder_panel)
        self.routine_trigger_edit.setPlaceholderText("Frase disparadora (ej. estudio)")
        meta_row.addWidget(self.routine_trigger_edit, 1)
        builder_layout.addLayout(meta_row)

        step_row = QHBoxLayout()
        step_row.setContentsMargins(0, 0, 0, 0)
        step_row.setSpacing(8)

        self.step_kind_combo = QComboBox(self._builder_panel)
        self.step_kind_combo.addItem("Abrir app", "open_app")
        self.step_kind_combo.addItem("Cerrar app", "close_app")
        self.step_kind_combo.addItem("Volumen maestro (%)", "set_volume")
        self.step_kind_combo.addItem("Brillo (%)", "set_brightness")
        self.step_kind_combo.addItem("Maximizar ventana", "maximize_window")
        self.step_kind_combo.addItem("Mover al monitor (#)", "move_window")
        self.step_kind_combo.setMinimumWidth(165)
        step_row.addWidget(self.step_kind_combo)

        self.step_value_edit = QLineEdit(self._builder_panel)
        self.step_value_edit.setPlaceholderText("Valor (ej. spotify, 35, 2)")
        step_row.addWidget(self.step_value_edit, 1)

        self.add_step_btn = QPushButton("Añadir paso", self._builder_panel)
        self.add_step_btn.setProperty("variant", "ghost")
        self.add_step_btn.clicked.connect(self._on_add_draft_step)
        step_row.addWidget(self.add_step_btn)
        builder_layout.addLayout(step_row)

        self._draft_steps_lbl = QLabel("Pasos: ninguno añadido todavía.", self._builder_panel)
        self._draft_steps_lbl.setObjectName("MutedLabel")
        self._draft_steps_lbl.setWordWrap(True)
        builder_layout.addWidget(self._draft_steps_lbl)

        builder_actions = QHBoxLayout()
        builder_actions.setContentsMargins(0, 0, 0, 0)
        builder_actions.setSpacing(8)

        self.save_routine_btn = QPushButton("Guardar rutina", self._builder_panel)
        self.save_routine_btn.setProperty("variant", "primary")
        self.save_routine_btn.clicked.connect(self._on_save_routine)
        builder_actions.addWidget(self.save_routine_btn)

        clear_btn = QPushButton("Limpiar", self._builder_panel)
        clear_btn.setProperty("variant", "ghost")
        clear_btn.clicked.connect(self._on_clear_draft)
        builder_actions.addWidget(clear_btn)
        builder_actions.addStretch(1)
        builder_layout.addLayout(builder_actions)

        self._builder_panel.setVisible(False)
        self._routines_card.body_layout.addWidget(self._builder_panel)

        self._routines_list_layout = QVBoxLayout()
        self._routines_list_layout.setContentsMargins(0, 4, 0, 0)
        self._routines_list_layout.setSpacing(8)
        self._routines_card.body_layout.addLayout(self._routines_list_layout)

        content_layout.addWidget(self._routines_card)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _on_create_timer(self) -> None:
        label = self.timer_label_edit.text().strip() or "Temporizador"
        amount = self.timer_amount_spin.value()
        unit = str(self.timer_unit_combo.currentData() or "s")
        seconds = parse_duration_seconds(amount, unit)
        self._app.scheduler.schedule_timer(seconds=seconds, label=label)
        self.timer_label_edit.clear()
        self.refresh_timers()

    def _on_tick(self) -> None:
        if self.isVisible() and self._app.scheduler.list_pending():
            self.refresh_timers()

    def _clear_timers_layout(self) -> None:
        while self._timers_list_layout.count() > 0:
            item = self._timers_list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()

    def refresh_timers(self) -> None:
        self._clear_timers_layout()
        jobs = self._app.scheduler.list_pending()
        if not jobs:
            empty = QLabel("No hay temporizadores pendientes en este momento.", self._timers_section)
            empty.setObjectName("MutedLabel")
            self._timers_list_layout.addWidget(empty)
            return

        for job in jobs:
            self._timers_list_layout.addWidget(self._build_timer_row(job))

    def _build_timer_row(self, job: ScheduledJob) -> QWidget:
        row_widget = QWidget(self._timers_section)
        outer = QVBoxLayout(row_widget)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        lbl = QLabel(job.label, row_widget)
        lbl.setObjectName("CardTitle")
        top.addWidget(lbl, 1)

        remaining = round(job.remaining_seconds())
        rem_pill = QLabel(format_duration_es(remaining), row_widget)
        rem_pill.setObjectName("StatusPill")
        rem_pill.setProperty("state", "active")
        top.addWidget(rem_pill)

        cancel_btn = QPushButton("Cancelar", row_widget)
        cancel_btn.setProperty("variant", "ghost")
        cancel_btn.clicked.connect(lambda _checked=False, jid=job.id: self._cancel_job(jid))
        top.addWidget(cancel_btn)
        outer.addLayout(top)

        total_duration = int((job.payload or {}).get("duration_seconds") or max(1, remaining))
        progress = QProgressBar(row_widget)
        progress.setRange(0, max(1, total_duration))
        progress.setValue(max(0, min(total_duration, remaining)))
        progress.setTextVisible(False)
        outer.addWidget(progress)

        return row_widget

    def _cancel_job(self, job_id: str) -> None:
        self._app.scheduler.cancel_job(job_id)
        self.refresh_timers()

    def _active_pc_module(self) -> PCModule | None:
        mod = self._app.engine.modules.get("pc")
        if (
            isinstance(mod, PCModule)
            and mod.state == ModuleState.ACTIVE
            and mod.routines is not None
        ):
            return mod
        return None

    def _on_module_state_changed(self, event: object) -> None:
        if getattr(event, "module_id", "") == "pc":
            self.refresh_routines()

    def _on_async_feedback(self, message: str, should_refresh: bool) -> None:
        self._routines_feedback_lbl.setText(message)
        self._routines_feedback_lbl.setVisible(bool(message))
        if should_refresh:
            self.refresh_routines()

    def _emit_feedback_safe(self, message: str, should_refresh: bool) -> None:
        with suppress(RuntimeError):
            self._async_feedback.emit(message, should_refresh)

    def _on_toggle_builder(self) -> None:
        if self._active_pc_module() is None:
            return
        self._builder_panel.setVisible(not self._builder_panel.isVisible())

    def _on_add_draft_step(self) -> None:
        kind_raw = str(self.step_kind_combo.currentData() or "open_app")
        raw_val = self.step_value_edit.text().strip()
        if not raw_val:
            return

        kind: RoutineStepKind = kind_raw  # type: ignore[assignment]
        params: dict[str, Any]
        if kind in ("set_volume", "set_brightness"):
            digits = "".join(ch for ch in raw_val if ch.isdigit())
            level = max(0, min(100, int(digits))) if digits else 50
            params = {"level_pct": level}
        elif kind == "move_window":
            if raw_val.isdigit():
                params = {"monitor_index": max(0, int(raw_val) - 1)}
            else:
                params = {"app": raw_val, "monitor_index": 1}
        else:
            params = {"app": raw_val}

        self._draft_steps.append(RoutineStep(kind=kind, params=params))
        self.step_value_edit.clear()
        self._update_draft_steps_label()

    def _on_clear_draft(self) -> None:
        self._draft_steps.clear()
        self.routine_name_edit.clear()
        self.routine_trigger_edit.clear()
        self.step_value_edit.clear()
        self._update_draft_steps_label()

    def _update_draft_steps_label(self) -> None:
        if not self._draft_steps:
            self._draft_steps_lbl.setText("Pasos: ninguno añadido todavía.")
            return
        summary = " · ".join(f"{idx}. {describe_step_es(s)}" for idx, s in enumerate(self._draft_steps, 1))
        self._draft_steps_lbl.setText(f"Pasos ({len(self._draft_steps)}): {summary}")

    def _on_save_routine(self) -> None:
        pc_mod = self._active_pc_module()
        if pc_mod is None or pc_mod.routines is None:
            return
        name = self.routine_name_edit.text().strip()
        if not name or not self._draft_steps:
            self._on_async_feedback("Escribe un nombre y añade al menos un paso.", False)
            return
        trigger = self.routine_trigger_edit.text().strip() or name.lower()
        rec = pc_mod.routines.save_routine(
            name=name,
            steps=list(self._draft_steps),
            trigger_phrase=trigger,
            icon="⚡",
            enabled=True,
        )
        self._on_clear_draft()
        self._builder_panel.setVisible(False)
        self._on_async_feedback(f"Rutina '{rec.name}' guardada ({len(rec.steps)} paso(s)).", True)

    def _on_capture_current_state(self) -> None:
        if self._active_pc_module() is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            "Capturar estado actual",
            "Nombre de la nueva rutina:",
            text="Mi espacio actual",
        )
        if not accepted or not name.strip():
            return
        self.capture_state_with_name(name.strip())

    def capture_state_with_name(self, name: str) -> concurrent.futures.Future[RoutineRecord] | None:
        """Captura ventanas y volúmenes actuales de forma asíncrona sin bloquear Qt."""
        pc_mod = self._active_pc_module()
        if pc_mod is None or pc_mod.routines is None:
            return None
        routines_mgr = pc_mod.routines
        self._on_async_feedback(f"Capturando estado actual para '{name}'...", False)
        fut = self._app.runtime.submit_async(
            routines_mgr.capture_current_state(name=name, save=True)
        )

        def _done(completed: concurrent.futures.Future[RoutineRecord]) -> None:
            try:
                rec = completed.result()
                self._emit_feedback_safe(
                    f"Rutina '{rec.name}' capturada con {len(rec.steps)} paso(s).",
                    True,
                )
            except Exception as exc:
                self._emit_feedback_safe(f"No pude capturar el estado actual: {exc}", False)

        fut.add_done_callback(_done)
        return fut

    def _on_run_routine(self, routine_id: str) -> None:
        pc_mod = self._active_pc_module()
        if pc_mod is None or pc_mod.routines is None:
            return
        routines_mgr = pc_mod.routines
        dry = self._app.settings.developer.dry_run
        fut = self._app.runtime.submit_async(
            routines_mgr.execute_routine(routine_id, dry_run=dry)
        )

        def _done(completed: concurrent.futures.Future[Any]) -> None:
            try:
                res = completed.result()
                self._emit_feedback_safe(str(res.summary), False)
            except Exception as exc:
                self._emit_feedback_safe(f"Error al ejecutar la rutina: {exc}", False)

        fut.add_done_callback(_done)

    def _on_toggle_routine(self, routine_id: str, enabled: bool) -> None:
        pc_mod = self._active_pc_module()
        if pc_mod is None or pc_mod.routines is None:
            return
        pc_mod.routines.set_enabled(routine_id, enabled)

    def _on_delete_routine(self, routine_id: str) -> None:
        pc_mod = self._active_pc_module()
        if pc_mod is None or pc_mod.routines is None:
            return
        if pc_mod.routines.delete_routine(routine_id):
            self._on_async_feedback("Rutina eliminada.", True)

    def _clear_routines_layout(self) -> None:
        while self._routines_list_layout.count() > 0:
            item = self._routines_list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()

    def _build_routine_row(self, rec: RoutineRecord) -> QWidget:
        row_card = FluentCard(parent=self._routines_card)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        title_lbl = QLabel(f"{rec.icon}  {rec.name}", row_card)
        title_lbl.setObjectName("CardTitle")
        top.addWidget(title_lbl, 1)

        trigger_lbl = QLabel(f"«modo {rec.trigger_phrase}»", row_card)
        trigger_lbl.setObjectName("MutedLabel")
        top.addWidget(trigger_lbl)

        toggle = ToggleSwitch(row_card)
        toggle.setChecked(rec.enabled)
        toggle.toggled.connect(
            lambda checked, rid=rec.id: self._on_toggle_routine(rid, checked)
        )
        top.addWidget(toggle)

        run_btn = QPushButton("Ejecutar", row_card)
        run_btn.setProperty("variant", "primary")
        run_btn.clicked.connect(lambda _checked=False, rid=rec.id: self._on_run_routine(rid))
        top.addWidget(run_btn)

        del_btn = QPushButton("Eliminar", row_card)
        del_btn.setProperty("variant", "ghost")
        del_btn.clicked.connect(lambda _checked=False, rid=rec.id: self._on_delete_routine(rid))
        top.addWidget(del_btn)
        row_card.body_layout.addLayout(top)

        steps_txt = " · ".join(describe_step_es(s) for s in rec.steps[:5])
        if len(rec.steps) > 5:
            steps_txt += f" (+{len(rec.steps) - 5} más)"
        steps_lbl = QLabel(steps_txt or "Sin pasos configurados.", row_card)
        steps_lbl.setObjectName("MutedLabel")
        steps_lbl.setWordWrap(True)
        row_card.body_layout.addWidget(steps_lbl)

        return row_card

    def refresh_routines(self) -> None:
        self._clear_routines_layout()
        pc_mod = self._active_pc_module()
        active = pc_mod is not None
        self.capture_state_btn.setEnabled(active)
        self.new_routine_btn.setEnabled(active)

        if not active or pc_mod is None or pc_mod.routines is None:
            self._builder_panel.setVisible(False)
            self._routines_status_lbl.setText(
                "Activa el módulo 'Control Local del PC' en Módulos para capturar ventanas, "
                "volúmenes y ejecutar rutinas en 1 clic."
            )
            self._routines_status_lbl.setVisible(True)
            return

        self._routines_status_lbl.setVisible(False)
        routines = pc_mod.routines.list_routines(include_disabled=True)
        if not routines:
            empty = QLabel(
                "Aún no tienes rutinas guardadas. Usa 'Capturar estado actual' o 'Nueva rutina'.",
                self._routines_card,
            )
            empty.setObjectName("MutedLabel")
            empty.setWordWrap(True)
            self._routines_list_layout.addWidget(empty)
            return

        for rec in routines:
            self._routines_list_layout.addWidget(self._build_routine_row(rec))

    def closeEvent(self, event: QCloseEvent) -> None:
        self._tick_timer.stop()
        super().closeEvent(event)
