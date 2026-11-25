from __future__ import annotations

import concurrent.futures

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.brain.orchestrator import TurnResult
from deusex.core.contracts import ModuleState
from deusex.core.events import (
    LiveGuideStateChangedEvent,
    SafetyConfirmationRequestedEvent,
    SafetyConfirmationResolvedEvent,
    ScreenCapturedEvent,
)
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import ChatBubble, SafetyCard


class MiniChatWindow(QWidget):
    """Ventana compacta (450x520) en esquina inferior derecha enfocada en comandos rápidos (Alt+Espacio)."""

    open_full_history_requested = Signal()
    _turn_finished = Signal(object)
    _turn_failed = Signal(str)

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._busy = False
        self._syncing = False
        self._pinned = True
        self._safety_cards: dict[str, SafetyCard] = {}

        self.setObjectName("MiniChatRoot")
        self.setWindowTitle("DeusEx · Comando Rápido")
        self.resize(450, 520)
        self.setMinimumSize(420, 400)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        self._build_ui()
        self._connect_signals()
        self.sync_controls()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # Barra superior de una sola línea
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)

        self.profile_combo = QComboBox(self)
        self.profile_combo.setToolTip("Cambiar perfil activo")
        self.profile_combo.setMaximumWidth(105)
        top.addWidget(self.profile_combo)

        icon_btn_style = "QPushButton { padding: 0px; font-size: 16px; }"

        self.mute_btn = QPushButton("🔊", self)
        self.mute_btn.setProperty("variant", "ghost")
        self.mute_btn.setFixedSize(36, 34)
        self.mute_btn.setStyleSheet(icon_btn_style)
        self.mute_btn.setToolTip("Silenciar o reactivar respuesta por voz")
        self.mute_btn.clicked.connect(self._on_toggle_mute)
        top.addWidget(self.mute_btn)

        self.pin_btn = QPushButton("📌", self)
        self.pin_btn.setProperty("variant", "pill")
        self.pin_btn.setProperty("selected", True)
        self.pin_btn.setFixedSize(36, 34)
        self.pin_btn.setStyleSheet(
            'QPushButton, QPushButton[variant="pill"] { padding: 0px; font-size: 15px; }'
        )
        self.pin_btn.setToolTip("Mantener siempre encima")
        self.pin_btn.clicked.connect(self.toggle_pin)
        top.addWidget(self.pin_btn)

        # Botones de Visión (ocultos hasta que el módulo vision esté activo en Rebanada 5)
        self.capture_btn = QPushButton("📷", self)
        self.capture_btn.setProperty("variant", "ghost")
        self.capture_btn.setFixedSize(36, 34)
        self.capture_btn.setStyleSheet(icon_btn_style)
        self.capture_btn.setToolTip("Captura puntual de pantalla")
        self.capture_btn.setVisible(False)
        self.capture_btn.clicked.connect(self._on_quick_capture)
        top.addWidget(self.capture_btn)

        self.live_guide_btn = QPushButton("Guía", self)
        self.live_guide_btn.setProperty("variant", "pill")
        self.live_guide_btn.setStyleSheet(
            'QPushButton[variant="pill"] { padding: 5px 7px; }'
        )
        self.live_guide_btn.setToolTip("Iniciar Modo Guía Live por turnos")
        self.live_guide_btn.setVisible(False)
        self.live_guide_btn.clicked.connect(self._on_toggle_live_guide)
        top.addWidget(self.live_guide_btn)

        self.live_guide_stop_btn = QPushButton("■", self)
        self.live_guide_stop_btn.setProperty("variant", "ghost")
        self.live_guide_stop_btn.setFixedSize(36, 34)
        self.live_guide_stop_btn.setStyleSheet(icon_btn_style)
        self.live_guide_stop_btn.setToolTip("Detener Modo Guía Live")
        self.live_guide_stop_btn.setVisible(False)
        self.live_guide_stop_btn.clicked.connect(self._on_stop_live_guide)
        top.addWidget(self.live_guide_stop_btn)

        top.addStretch(1)

        self.full_history_btn = QPushButton("Historial →", self)
        self.full_history_btn.setProperty("variant", "ghost")
        self.full_history_btn.setStyleSheet("QPushButton { padding: 5px 6px; }")
        self.full_history_btn.setToolTip("Abrir la Ventana Principal con el historial completo")
        self.full_history_btn.clicked.connect(self.open_full_history_requested.emit)
        top.addWidget(self.full_history_btn)

        root.addLayout(top)

        # Respuesta inmediata + tarjetas SafetyGate
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._feed = QWidget(self._scroll)
        self._feed_layout = QVBoxLayout(self._feed)
        self._feed_layout.setContentsMargins(0, 2, 4, 4)
        self._feed_layout.setSpacing(8)
        self._feed_layout.addStretch(1)
        self._scroll.setWidget(self._feed)
        root.addWidget(self._scroll, 1)

        self._hint_lbl = QLabel("Alt+Espacio para abrir/ocultar · Esc para ocultar", self)
        self._hint_lbl.setObjectName("MutedLabel")
        root.addWidget(self._hint_lbl)

        # Campo de comando rápido
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)

        self.input_edit = QLineEdit(self)
        self.input_edit.setPlaceholderText("Escribe una orden rápida para DeusEx...")
        self.input_edit.setMinimumHeight(38)
        self.input_edit.returnPressed.connect(self.send_command)
        input_row.addWidget(self.input_edit, 1)

        self.send_btn = QPushButton("Enviar", self)
        self.send_btn.setProperty("variant", "primary")
        self.send_btn.setMinimumHeight(38)
        self.send_btn.clicked.connect(self.send_command)
        input_row.addWidget(self.send_btn)

        root.addLayout(input_row)

    def _connect_signals(self) -> None:
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._turn_finished.connect(self._on_turn_finished)
        self._turn_failed.connect(self._on_turn_failed)
        self._bridge.safety_confirmation_requested.connect(self._on_safety_requested)
        self._bridge.safety_confirmation_resolved.connect(self._on_safety_resolved)
        self._bridge.profile_changed.connect(lambda _e: self.sync_controls())
        self._bridge.voice_mute_changed.connect(lambda _m: self.sync_controls())
        self._bridge.module_state_changed.connect(lambda _e: self.sync_controls())
        self._bridge.privacy_mode_changed.connect(lambda _p: self.sync_controls())
        self._bridge.live_guide_state_changed.connect(self._on_live_guide_state_changed)
        self._bridge.screen_captured.connect(self._on_screen_captured)

    def sync_controls(self) -> None:
        self._syncing = True
        try:
            self.profile_combo.clear()
            for prof in self._app.profiles.list_profiles():
                self.profile_combo.addItem(prof.name, prof.id)
            idx = self.profile_combo.findData(self._app.settings.ui.active_profile_id)
            if idx >= 0:
                self.profile_combo.setCurrentIndex(idx)

            muted = self._app.settings.voice.tts_muted
            self.mute_btn.setText("🔇" if muted else "🔊")

            vision_active = self._app.vision_module.state == ModuleState.ACTIVE
            privacy_on = self._app.settings.general.privacy_mode
            lg_state = self._app.get_live_guide_state() if vision_active else "off"

            self.capture_btn.setVisible(vision_active)
            self.capture_btn.setEnabled(vision_active and not privacy_on and not self._busy)
            self.capture_btn.setToolTip(
                "Bloqueado en Modo Privacidad"
                if privacy_on
                else "Captura rápida de pantalla y análisis multimodal"
            )

            self.live_guide_btn.setVisible(vision_active)
            self.live_guide_stop_btn.setVisible(vision_active and lg_state in ("active", "paused"))

            if lg_state == "active":
                self.live_guide_btn.setText("Guía: ON")
                self.live_guide_btn.setProperty("selected", True)
                self.live_guide_btn.setEnabled(True)
                self.live_guide_btn.setToolTip("Guía Live activa por turnos (clic para pausar)")
            elif lg_state == "paused":
                self.live_guide_btn.setText("Guía: Pausa")
                self.live_guide_btn.setProperty("selected", False)
                self.live_guide_btn.setEnabled(not privacy_on)
                self.live_guide_btn.setToolTip(
                    "Bloqueado en Modo Privacidad"
                    if privacy_on
                    else "Guía Live en pausa · 0 capturas / 0 tokens (clic para reanudar)"
                )
            else:
                self.live_guide_btn.setText("Guía")
                self.live_guide_btn.setProperty("selected", False)
                self.live_guide_btn.setEnabled(not privacy_on)
                self.live_guide_btn.setToolTip(
                    "Bloqueado en Modo Privacidad"
                    if privacy_on
                    else "Iniciar Modo Guía Live por turnos"
                )

            self.live_guide_btn.style().unpolish(self.live_guide_btn)
            self.live_guide_btn.style().polish(self.live_guide_btn)

            if not self._busy:
                if lg_state == "active":
                    self._hint_lbl.setText(
                        "Guía Live ACTIVA · Captura puntual en cada turno · Esc para ocultar"
                    )
                elif lg_state == "paused":
                    self._hint_lbl.setText(
                        "Guía Live en PAUSA · 0 capturas y 0 tokens · Esc para ocultar"
                    )
                else:
                    self._hint_lbl.setText("Alt+Espacio para abrir/ocultar · Esc para ocultar")
        finally:
            self._syncing = False

    def _on_profile_changed(self, _index: int) -> None:
        if self._syncing:
            return
        pid = self.profile_combo.currentData()
        if isinstance(pid, str) and pid:
            self._app.set_active_profile(pid)

    def _on_toggle_mute(self) -> None:
        self._app.set_voice_muted(not self._app.settings.voice.tts_muted)

    def toggle_pin(self) -> None:
        self._pinned = not self._pinned
        self.pin_btn.setProperty("selected", self._pinned)
        self.pin_btn.style().unpolish(self.pin_btn)
        self.pin_btn.style().polish(self.pin_btn)
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._pinned)
        if was_visible:
            self.show()

    def position_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        margin = 18
        x = avail.right() - self.width() - margin
        y = avail.bottom() - self.height() - margin
        self.move(max(avail.left(), x), max(avail.top(), y))

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
            return
        self.sync_controls()
        self.position_bottom_right()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_edit.setFocus()

    def _append_widget(self, widget: QWidget) -> None:
        pos = max(0, self._feed_layout.count() - 1)
        self._feed_layout.insertWidget(pos, widget)
        QTimer.singleShot(20, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        vbar = self._scroll.verticalScrollBar()
        if vbar is not None:
            vbar.setValue(vbar.maximum())

    def send_command(self) -> None:
        text = self.input_edit.text().strip()
        if not text or self._busy:
            return
        self.input_edit.clear()
        self._busy = True
        self.send_btn.setEnabled(False)
        self._hint_lbl.setText("Ejecutando...")

        user_name = self._app.settings.general.user_name.strip() or "Tú"
        self._append_widget(
            ChatBubble(role="user", sender_label=user_name, content=text, parent=self._feed)
        )

        fut = self._app.submit_user_message(text)

        def _done(done_fut: concurrent.futures.Future[TurnResult]) -> None:
            try:
                res = done_fut.result()
            except Exception as exc:
                self._turn_failed.emit(str(exc))
                return
            self._turn_finished.emit(res)

        fut.add_done_callback(_done)

    def _on_turn_finished(self, result_obj: object) -> None:
        self._busy = False
        self.send_btn.setEnabled(True)
        self._hint_lbl.setText("Alt+Espacio para abrir/ocultar · Esc para ocultar")
        if isinstance(result_obj, TurnResult):
            sender = self._app.profiles.get(self._app.settings.ui.active_profile_id).name
            self._append_widget(
                ChatBubble(
                    role="assistant",
                    sender_label=sender,
                    content=result_obj.reply_text,
                    model_used=result_obj.model_used,
                    fast_path=result_obj.fast_path,
                    sources=result_obj.sources,
                    parent=self._feed,
                )
            )
        self.sync_controls()
        self.input_edit.setFocus()

    def _on_turn_failed(self, err_text: str) -> None:
        self._busy = False
        self.send_btn.setEnabled(True)
        self._hint_lbl.setText("Alt+Espacio para abrir/ocultar · Esc para ocultar")
        self._append_widget(
            ChatBubble(
                role="system",
                sender_label="Sistema",
                content=f"Error: {err_text}",
                parent=self._feed,
            )
        )
        self.sync_controls()

    def _on_live_guide_state_changed(self, event: object) -> None:
        if isinstance(event, LiveGuideStateChangedEvent):
            self.sync_controls()

    def _on_screen_captured(self, event: object) -> None:
        if isinstance(event, ScreenCapturedEvent) and not self._busy:
            self._hint_lbl.setText(
                f"Captura lista ({event.width}x{event.height}) · Esc para ocultar"
            )

    def _on_quick_capture(self) -> None:
        if self._busy or self._app.settings.general.privacy_mode:
            return
        prompt = self.input_edit.text().strip()
        if prompt:
            self.input_edit.clear()

        self._busy = True
        self.send_btn.setEnabled(False)
        self.capture_btn.setEnabled(False)
        self._hint_lbl.setText("Capturando y analizando pantalla...")

        user_name = self._app.settings.general.user_name.strip() or "Tú"
        bubble_text = f"📷 {prompt}" if prompt else "📷 Analizar pantalla"
        self._append_widget(
            ChatBubble(role="user", sender_label=user_name, content=bubble_text, parent=self._feed)
        )

        fut = self._app.capture_screen_and_analyze(prompt=prompt)

        def _done(done_fut: concurrent.futures.Future[TurnResult]) -> None:
            try:
                res = done_fut.result()
            except Exception as exc:
                self._turn_failed.emit(str(exc))
                return
            self._turn_finished.emit(res)

        fut.add_done_callback(_done)

    def _on_toggle_live_guide(self) -> None:
        state = self._app.get_live_guide_state()
        try:
            if state == "off":
                self._app.start_live_guide()
            elif state == "active":
                self._app.pause_live_guide()
            else:
                self._app.resume_live_guide()
        except PermissionError as exc:
            self._append_widget(
                ChatBubble(
                    role="system",
                    sender_label="Visión",
                    content=str(exc),
                    parent=self._feed,
                )
            )
        self.sync_controls()

    def _on_stop_live_guide(self) -> None:
        self._app.stop_live_guide()
        self.sync_controls()

    def _on_safety_requested(self, event: object) -> None:
        if not isinstance(event, SafetyConfirmationRequestedEvent):
            return
        if event.confirmation_id in self._safety_cards:
            return
        card = SafetyCard(event, parent=self._feed)
        card.decision_made.connect(
            lambda cid, approved: self._app.resolve_safety_confirmation(
                cid, approved=approved, reason="minichat"
            )
        )
        self._safety_cards[event.confirmation_id] = card
        self._append_widget(card)

    def _on_safety_resolved(self, event: object) -> None:
        if not isinstance(event, SafetyConfirmationResolvedEvent):
            return
        card = self._safety_cards.get(event.confirmation_id)
        if card is not None and not card.is_resolved:
            card.mark_resolved(approved=event.approved, reason=event.reason)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()
