from __future__ import annotations

import concurrent.futures

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
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
from deusex.brain.personality import AVAILABLE_TONES
from deusex.core.contracts import ModuleState
from deusex.core.events import (
    ChatMessageEvent,
    LiveGuideStateChangedEvent,
    MemorySavedEvent,
    ProfileChangedEvent,
    SafetyConfirmationRequestedEvent,
    SafetyConfirmationResolvedEvent,
)
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import ChatBubble, MemorySavedPill, SafetyCard


class ChatView(QWidget):
    """Vista principal de conversación con historial persistente, SafetyGate y píldoras de memoria."""

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
        self._syncing_controls = False
        self._rendered_message_ids: set[str] = set()
        self._safety_cards: dict[str, SafetyCard] = {}

        active_conv = self._app.memory.get_or_create_active_conversation()
        self._conversation_id: str = active_conv.id

        self._build_ui()
        self._connect_signals()
        self.sync_header_state()
        self.reload_conversation(self._conversation_id)

    @property
    def conversation_id(self) -> str:
        return self._conversation_id

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        # Cabecera minimalista
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)
        title = QLabel("Conversación", self)
        title.setObjectName("ViewTitle")
        title_col.addWidget(title)

        self._subtitle_lbl = QLabel("Historial completo · Comandos locales en <50 ms", self)
        self._subtitle_lbl.setObjectName("ViewSubtitle")
        title_col.addWidget(self._subtitle_lbl)
        header.addLayout(title_col, 1)

        self.profile_combo = QComboBox(self)
        self.profile_combo.setToolTip("Cambiar personalidad activa")
        self.profile_combo.setMinimumWidth(120)
        header.addWidget(self.profile_combo)

        self.tone_combo = QComboBox(self)
        self.tone_combo.setToolTip("Cambiar tono de respuesta")
        self.tone_combo.setMinimumWidth(120)
        for tone_key in AVAILABLE_TONES:
            self.tone_combo.addItem(tone_key.capitalize(), tone_key)
        header.addWidget(self.tone_combo)

        self.model_badge = QLabel("IA: Automático", self)
        self.model_badge.setObjectName("StatusPill")
        self.model_badge.setToolTip("Modelo activo (configurable en Ajustes → IA y Claves)")
        header.addWidget(self.model_badge)

        self.dry_run_pill = QLabel("Dry-Run", self)
        self.dry_run_pill.setObjectName("StatusPill")
        self.dry_run_pill.setProperty("state", "warning")
        self.dry_run_pill.setToolTip("Modo Simulación activo (Modo Desarrollador)")
        header.addWidget(self.dry_run_pill)

        self.in_call_pill = QLabel("En llamada", self)
        self.in_call_pill.setObjectName("StatusPill")
        self.in_call_pill.setProperty("state", "info")
        self.in_call_pill.setToolTip("Llamada activa detectada: respuestas solo por texto")
        self.in_call_pill.setVisible(False)
        header.addWidget(self.in_call_pill)

        self.live_guide_pill = QLabel("Guía Live", self)
        self.live_guide_pill.setObjectName("StatusPill")
        self.live_guide_pill.setProperty("state", "info")
        self.live_guide_pill.setToolTip(
            "Modo Guía Live activo: capturas bajo demanda en cada turno"
        )
        self.live_guide_pill.setVisible(False)
        header.addWidget(self.live_guide_pill)

        self.privacy_btn = QPushButton("Privacidad", self)
        self.privacy_btn.setProperty("variant", "pill")
        self.privacy_btn.setToolTip("Alternar Modo Privacidad (Ctrl+Shift+P)")
        self.privacy_btn.clicked.connect(self._on_toggle_privacy)
        header.addWidget(self.privacy_btn)

        self.mute_btn = QPushButton("Voz activa", self)
        self.mute_btn.setProperty("variant", "pill")
        self.mute_btn.setToolTip("Silenciar salida de voz y responder solo por texto")
        self.mute_btn.clicked.connect(self._on_toggle_mute)
        header.addWidget(self.mute_btn)

        self.mic_mute_btn = QPushButton("Mic activo", self)
        self.mic_mute_btn.setProperty("variant", "pill")
        self.mic_mute_btn.setToolTip("Silenciar micrófono (pausar escucha)")
        self.mic_mute_btn.clicked.connect(self._on_toggle_mic_mute)
        header.addWidget(self.mic_mute_btn)

        self.new_chat_btn = QPushButton("Nuevo chat", self)
        self.new_chat_btn.setProperty("variant", "ghost")
        self.new_chat_btn.setToolTip("Iniciar una conversación limpia")
        self.new_chat_btn.clicked.connect(self.start_new_conversation)
        header.addWidget(self.new_chat_btn)

        root.addLayout(header)

        # Área desplazable del hilo de conversación
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._messages_container = QWidget(self._scroll)
        self._messages_layout = QVBoxLayout(self._messages_container)
        self._messages_layout.setContentsMargins(0, 4, 8, 8)
        self._messages_layout.setSpacing(10)
        self._messages_layout.addStretch(1)

        self._scroll.setWidget(self._messages_container)
        root.addWidget(self._scroll, 1)

        # Indicador discreto de estado
        self._status_lbl = QLabel("", self)
        self._status_lbl.setObjectName("MutedLabel")
        self._status_lbl.setVisible(False)
        root.addWidget(self._status_lbl)

        # Barra de entrada
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(10)

        icon_btn_style = "QPushButton { padding: 0px; font-size: 17px; }"

        self.capture_btn = QPushButton("📷", self)
        self.capture_btn.setProperty("variant", "ghost")
        self.capture_btn.setFixedSize(40, 40)
        self.capture_btn.setStyleSheet(icon_btn_style)
        self.capture_btn.setToolTip("Captura rápida de pantalla y análisis multimodal")
        self.capture_btn.clicked.connect(self._on_quick_capture)
        input_row.addWidget(self.capture_btn)

        self.live_guide_btn = QPushButton("👁️ Guía", self)
        self.live_guide_btn.setProperty("variant", "ghost")
        self.live_guide_btn.setMinimumHeight(40)
        self.live_guide_btn.setToolTip("Iniciar Modo Guía Live (Ctrl+G)")
        self.live_guide_btn.clicked.connect(self._on_toggle_live_guide)
        input_row.addWidget(self.live_guide_btn)

        self.live_guide_stop_btn = QPushButton("■", self)
        self.live_guide_stop_btn.setProperty("variant", "ghost")
        self.live_guide_stop_btn.setFixedSize(40, 40)
        self.live_guide_stop_btn.setStyleSheet(icon_btn_style)
        self.live_guide_stop_btn.setToolTip("Detener Modo Guía Live")
        self.live_guide_stop_btn.setVisible(False)
        self.live_guide_stop_btn.clicked.connect(self._on_stop_live_guide)
        input_row.addWidget(self.live_guide_stop_btn)

        self.input_edit = QLineEdit(self)
        self.input_edit.setPlaceholderText(
            "Escribe una orden o pregunta (ej. 'pon un temporizador de 5 segundos' o chatea con Gemini)..."
        )
        self.input_edit.setMinimumHeight(40)
        self.input_edit.returnPressed.connect(self.send_current_input)
        input_row.addWidget(self.input_edit, 1)

        self.send_btn = QPushButton("Enviar", self)
        self.send_btn.setProperty("variant", "primary")
        self.send_btn.setMinimumHeight(40)
        self.send_btn.setMinimumWidth(96)
        self.send_btn.setToolTip("Enviar mensaje (Enter)")
        self.send_btn.clicked.connect(self.send_current_input)
        input_row.addWidget(self.send_btn)

        root.addLayout(input_row)

    def _connect_signals(self) -> None:
        self.profile_combo.currentIndexChanged.connect(self._on_profile_combo_changed)
        self.tone_combo.currentIndexChanged.connect(self._on_tone_combo_changed)

        self._turn_finished.connect(self._on_turn_finished)
        self._turn_failed.connect(self._on_turn_failed)

        self._bridge.chat_message_received.connect(self._on_chat_message_event)
        self._bridge.memory_saved.connect(self._on_memory_saved_event)
        self._bridge.safety_confirmation_requested.connect(self._on_safety_requested)
        self._bridge.safety_confirmation_resolved.connect(self._on_safety_resolved)
        self._bridge.privacy_mode_changed.connect(lambda _enabled: self.sync_header_state())
        self._bridge.voice_mute_changed.connect(lambda _muted: self.sync_header_state())
        self._bridge.mic_mute_changed.connect(lambda _muted: self.sync_header_state())
        self._bridge.in_call_state_changed.connect(self._on_in_call_state_changed)
        self._bridge.profile_changed.connect(self._on_profile_changed_event)
        self._bridge.settings_updated.connect(lambda _e: self.sync_header_state())
        self._bridge.module_state_changed.connect(lambda _e: self.sync_header_state())
        self._bridge.live_guide_state_changed.connect(self._on_live_guide_state_changed)

    def sync_header_state(self) -> None:
        self._syncing_controls = True
        try:
            profiles = self._app.profiles.list_profiles()
            self.profile_combo.clear()
            for prof in profiles:
                self.profile_combo.addItem(prof.name, prof.id)
            p_idx = self.profile_combo.findData(self._app.settings.ui.active_profile_id)
            if p_idx >= 0:
                self.profile_combo.setCurrentIndex(p_idx)

            t_idx = self.tone_combo.findData(self._app.settings.ui.active_tone)
            if t_idx >= 0:
                self.tone_combo.setCurrentIndex(t_idx)

            privacy_on = self._app.settings.general.privacy_mode
            self.privacy_btn.setText("Privacidad: ON" if privacy_on else "Privacidad")
            self.privacy_btn.setProperty("selected", privacy_on)
            self._repolish(self.privacy_btn)

            muted = self._app.settings.voice.tts_muted
            self.mute_btn.setText("Voz silenciada" if muted else "Voz activa")
            self.mute_btn.setProperty("selected", muted)
            self._repolish(self.mute_btn)

            mic_muted = self._app.settings.voice.mic_muted
            self.mic_mute_btn.setText("Mic silenciado" if mic_muted else "Mic activo")
            self.mic_mute_btn.setProperty("selected", mic_muted)
            self._repolish(self.mic_mute_btn)

            in_call = getattr(self, "_in_call_active", False) or self._app.settings.voice.in_call_text_only
            self.in_call_pill.setVisible(in_call)

            self.dry_run_pill.setVisible(
                self._app.settings.developer.enabled and self._app.settings.developer.dry_run
            )

            show_model = (
                self._app.settings.brain.show_model_selector_in_chat
                or self._app.settings.developer.enabled
            )
            self.model_badge.setVisible(show_model)
            if show_model:
                if self._app.settings.brain.model_selection_mode == "manual":
                    chosen = self._app.settings.brain.manual_fast_model or "Manual"
                    self.model_badge.setText(f"IA: {chosen}")
                else:
                    self.model_badge.setText(
                        f"IA: {self._app.settings.brain.primary_provider.upper()} (Auto)"
                    )

            vision_active = (
                hasattr(self._app, "vision_module")
                and self._app.vision_module.state == ModuleState.ACTIVE
            )
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
                self.live_guide_pill.setVisible(True)
                self.live_guide_pill.setText("Guía Live: Activa")
                self.live_guide_pill.setProperty("state", "success")
                self.live_guide_btn.setText("⏸ Pausar")
                self.live_guide_btn.setEnabled(True)
                self.live_guide_btn.setToolTip("Pausar Guía Live (0 capturas / 0 tokens)")
            elif lg_state == "paused":
                self.live_guide_pill.setVisible(True)
                self.live_guide_pill.setText("Guía Live: Pausada")
                self.live_guide_pill.setProperty("state", "warning")
                self.live_guide_btn.setText("▶ Reanudar")
                self.live_guide_btn.setEnabled(not privacy_on)
                self.live_guide_btn.setToolTip(
                    "Bloqueado en Modo Privacidad"
                    if privacy_on
                    else "Reanudar Guía Live por turnos"
                )
            else:
                self.live_guide_pill.setVisible(False)
                self.live_guide_btn.setText("👁️ Guía")
                self.live_guide_btn.setEnabled(not privacy_on)
                self.live_guide_btn.setToolTip(
                    "Bloqueado en Modo Privacidad"
                    if privacy_on
                    else "Iniciar Modo Guía Live (Ctrl+G)"
                )

            self._repolish(self.live_guide_pill)
            self._repolish(self.live_guide_btn)
            self._repolish(self.live_guide_stop_btn)
        finally:
            self._syncing_controls = False

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _clear_messages_widgets(self) -> None:
        while self._messages_layout.count() > 1:
            item = self._messages_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.close()
                widget.deleteLater()
        self._rendered_message_ids.clear()
        self._safety_cards.clear()

    def reload_conversation(self, conversation_id: str) -> None:
        self._conversation_id = conversation_id
        self._clear_messages_widgets()
        messages = self._app.memory.get_messages(conversation_id)
        if not messages:
            active_prof = self._app.profiles.get(self._app.settings.ui.active_profile_id)
            self._append_widget(
                ChatBubble(
                    role="system",
                    sender_label=active_prof.name,
                    content=(
                        f"Hola. Soy {active_prof.name}. Puedes probar comandos instantáneos locales "
                        "(como 'pon un temporizador de 5 segundos' o 'qué hora es') o conversar con tu modelo IA."
                    ),
                    parent=self._messages_container,
                )
            )
            return

        for msg in messages:
            self._rendered_message_ids.add(msg.id)
            self._append_message_bubble(
                role=msg.role,
                content=msg.content,
                model_used=msg.model_used,
                fast_path=msg.fast_path,
                sources=msg.sources,
            )

    def start_new_conversation(self) -> None:
        conv = self._app.memory.create_conversation(
            profile_id=self._app.settings.ui.active_profile_id
        )
        self.reload_conversation(conv.id)

    def _append_widget(self, widget: QWidget) -> None:
        insert_pos = max(0, self._messages_layout.count() - 1)
        self._messages_layout.insertWidget(insert_pos, widget)
        QTimer.singleShot(20, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        vbar = self._scroll.verticalScrollBar()
        if vbar is not None:
            vbar.setValue(vbar.maximum())

    def _append_message_bubble(
        self,
        *,
        role: str,
        content: str,
        model_used: str = "",
        fast_path: bool = False,
        sources: tuple[dict[str, str], ...] = (),
    ) -> None:
        if role == "user":
            sender = self._app.settings.general.user_name.strip() or "Tú"
        else:
            sender = self._app.profiles.get(self._app.settings.ui.active_profile_id).name
        bubble = ChatBubble(
            role=role,
            sender_label=sender,
            content=content,
            model_used=model_used,
            fast_path=fast_path,
            sources=sources,
            parent=self._messages_container,
        )
        self._append_widget(bubble)

    def send_current_input(self) -> None:
        text = self.input_edit.text().strip()
        if not text or self._busy:
            return
        self.input_edit.clear()
        self.submit_text(text)

    def submit_text(self, text: str) -> None:
        clean = text.strip()
        if not clean or self._busy:
            return
        self._set_busy(True, "Procesando...")
        fut = self._app.submit_user_message(clean, conversation_id=self._conversation_id)
        fut.add_done_callback(self._handle_future_from_worker)

    def _handle_future_from_worker(self, fut: concurrent.futures.Future[TurnResult]) -> None:
        try:
            result = fut.result()
        except Exception as exc:
            self._turn_failed.emit(str(exc))
            return
        self._turn_finished.emit(result)

    def _set_busy(self, busy: bool, status_text: str = "") -> None:
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.input_edit.setEnabled(not busy)
        privacy_on = self._app.settings.general.privacy_mode
        vision_active = (
            hasattr(self._app, "vision_module")
            and self._app.vision_module.state == ModuleState.ACTIVE
        )
        self.capture_btn.setEnabled(not busy and vision_active and not privacy_on)
        self._status_lbl.setText(status_text)
        self._status_lbl.setVisible(bool(status_text))

    def _on_turn_finished(self, _result: object) -> None:
        self._set_busy(False, "")
        self.sync_header_state()
        self.input_edit.setFocus()

    def _on_turn_failed(self, err_text: str) -> None:
        self._set_busy(False, "")
        self.sync_header_state()
        self._append_widget(
            ChatBubble(
                role="system",
                sender_label="Sistema",
                content=f"No se pudo completar la solicitud: {err_text}",
                parent=self._messages_container,
            )
        )
        self.input_edit.setFocus()

    def _on_chat_message_event(self, event: object) -> None:
        if not isinstance(event, ChatMessageEvent):
            return
        if event.conversation_id != self._conversation_id:
            self._conversation_id = event.conversation_id
        if event.message_id in self._rendered_message_ids:
            return
        self._rendered_message_ids.add(event.message_id)
        self._append_message_bubble(
            role=event.role,
            content=event.content,
            model_used=event.model_used,
            fast_path=event.fast_path,
            sources=event.sources,
        )

    def _on_memory_saved_event(self, event: object) -> None:
        if not isinstance(event, MemorySavedEvent) or not event.auto_extracted:
            return
        cat_labels = {
            "fact": "Hecho",
            "preference": "Preferencia",
            "interest": "Interés",
            "contact": "Contacto",
        }
        summary = f"{cat_labels.get(event.category, event.category)} · {event.value}"
        pill = MemorySavedPill(
            memory_id=event.memory_id,
            summary_es=summary,
            parent=self._messages_container,
        )
        pill.undo_requested.connect(self._app.memory.delete_memory)
        self._append_widget(pill)

    def _on_safety_requested(self, event: object) -> None:
        if not isinstance(event, SafetyConfirmationRequestedEvent):
            return
        if event.confirmation_id in self._safety_cards:
            return
        card = SafetyCard(event, parent=self._messages_container)
        card.decision_made.connect(
            lambda cid, approved: self._app.resolve_safety_confirmation(
                cid, approved=approved, reason="main_window"
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

    def _on_profile_changed_event(self, _event: object) -> None:
        if isinstance(_event, ProfileChangedEvent):
            self.sync_header_state()

    def _on_profile_combo_changed(self, _index: int) -> None:
        if self._syncing_controls:
            return
        prof_id = self.profile_combo.currentData()
        if isinstance(prof_id, str) and prof_id:
            self._app.set_active_profile(prof_id)

    def _on_tone_combo_changed(self, _index: int) -> None:
        if self._syncing_controls:
            return
        tone = self.tone_combo.currentData()
        if isinstance(tone, str) and tone:
            self._app.set_active_profile(self._app.settings.ui.active_profile_id, tone)

    def _on_toggle_privacy(self) -> None:
        self._app.toggle_privacy_mode()

    def _on_toggle_mute(self) -> None:
        self._app.set_voice_muted(not self._app.settings.voice.tts_muted)

    def _on_toggle_mic_mute(self) -> None:
        self._app.toggle_mic_muted()
        self.sync_header_state()

    def _on_in_call_state_changed(self, active: bool, apps: object) -> None:
        self._in_call_active = active
        self.in_call_pill.setVisible(active or self._app.settings.voice.in_call_text_only)
        if active and isinstance(apps, (tuple, list)) and apps:
            app_str = ", ".join(str(a) for a in apps)
            self.in_call_pill.setToolTip(f"Llamada detectada ({app_str}): respuestas solo por texto")

    def _on_live_guide_state_changed(self, event: object) -> None:
        if isinstance(event, LiveGuideStateChangedEvent):
            self.sync_header_state()

    def _on_quick_capture(self) -> None:
        if self._busy or self._app.settings.general.privacy_mode:
            return
        prompt = self.input_edit.text().strip()
        if prompt:
            self.input_edit.clear()

        self._set_busy(True, "Capturando y analizando pantalla...")
        # ChatView ya recibe el ChatMessageEvent de handle_user_input; evitamos doble burbuja.
        effective_prompt = prompt or "📷 Analizar pantalla"
        fut = self._app.capture_screen_and_analyze(
            prompt=effective_prompt,
            conversation_id=self._conversation_id,
        )
        fut.add_done_callback(self._handle_future_from_worker)

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
                    parent=self._messages_container,
                )
            )
        self.sync_header_state()

    def _on_stop_live_guide(self) -> None:
        self._app.stop_live_guide()
        self.sync_header_state()
