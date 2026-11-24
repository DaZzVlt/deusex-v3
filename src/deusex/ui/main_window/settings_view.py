from __future__ import annotations

import concurrent.futures
import re
from typing import Literal, cast

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.brain.personality import AVAILABLE_TONES, PersonalityProfile
from deusex.brain.providers import PROVIDER_METADATA, ModelInfo, detect_api_key_from_text
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.components import FluentCard, SettingExpander, SettingRow, ToggleSwitch

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class SettingsView(QWidget):
    """Ajustes integrados estilo Windows 11 con buscador, Smart Clipboard Catcher y Modo Desarrollador."""

    theme_changed = Signal()
    open_doctor_requested = Signal()
    _models_refreshed = Signal(str, object)
    _models_failed = Signal(str, str)

    def __init__(
        self,
        app: DeusExApplication,
        bridge: QtEventBridge,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._bridge = bridge
        self._syncing = False
        self._detected_clip: tuple[str, str, str] | None = None
        self._searchable_items: list[tuple[QWidget, str]] = []
        self._switches: list[ToggleSwitch] = []
        self._key_inputs: dict[str, QLineEdit] = {}
        self._key_status_pills: dict[str, QLabel] = {}

        self._build_ui()
        self._connect_signals()
        self.refresh_from_app()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)
        title = QLabel("Sistema y Ajustes", self)
        title.setObjectName("ViewTitle")
        title_col.addWidget(title)

        sub = QLabel(
            "Configuración unificada en settings.json · Claves aisladas en .env atómico.",
            self,
        )
        sub.setObjectName("ViewSubtitle")
        title_col.addWidget(sub)
        top.addLayout(title_col, 1)

        doctor_btn = QPushButton("Doctor y Logs →", self)
        doctor_btn.setProperty("variant", "pill")
        doctor_btn.setToolTip("Abrir Centro de Diagnóstico y visor de Latest Debug.txt")
        doctor_btn.clicked.connect(self.open_doctor_requested.emit)
        top.addWidget(doctor_btn)

        root.addLayout(top)

        # Buscador rápido estilo Windows 11 Settings
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(
            "Buscar un ajuste (ej. voz, orbe, gemini, tema, mica, privacidad, dry-run)..."
        )
        self.search_edit.setMinimumHeight(36)
        self.search_edit.textChanged.connect(self._apply_search_filter)
        root.addWidget(self.search_edit)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._body = QWidget(scroll)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 8, 12)
        self._body_layout.setSpacing(12)

        self._build_ai_section()
        self._build_appearance_profiles_section()
        self._build_voice_orb_section()
        self._build_privacy_cost_section()
        self._build_developer_section()

        self._body_layout.addStretch(1)
        scroll.setWidget(self._body)
        root.addWidget(scroll, 1)

    def _register_searchable(self, widget: QWidget, keywords: str) -> None:
        self._searchable_items.append((widget, keywords.lower()))

    def _make_switch(self, *, checked: bool = False) -> ToggleSwitch:
        accent = self._app.profiles.get(self._app.settings.ui.active_profile_id).accent_hex
        sw = ToggleSwitch(self._body, checked=checked, accent_hex=accent)
        self._switches.append(sw)
        return sw

    # ------------------------------------------------------------------
    # Sección 1: IA, Claves (Smart Clipboard Catcher) y Modelos Reales
    # ------------------------------------------------------------------
    def _build_ai_section(self) -> None:
        ai_card = FluentCard(
            title="Inteligencia Artificial y Modelos",
            subtitle="Gemini como motor principal con soporte para claves propias de OpenAI, Grok, Claude, Groq, DeepSeek y OpenRouter.",
            parent=self._body,
        )

        # Banner Smart Clipboard Catcher
        self.clip_banner = FluentCard(parent=ai_card)
        clip_row = QHBoxLayout()
        clip_row.setContentsMargins(0, 0, 0, 0)
        clip_row.setSpacing(10)
        self.clip_label = QLabel("", self.clip_banner)
        self.clip_label.setWordWrap(True)
        clip_row.addWidget(self.clip_label, 1)

        self.clip_save_btn = QPushButton("Guardar clave detectada", self.clip_banner)
        self.clip_save_btn.setProperty("variant", "primary")
        self.clip_save_btn.clicked.connect(self._save_detected_clipboard_key)
        clip_row.addWidget(self.clip_save_btn)

        self.clip_banner.body_layout.addLayout(clip_row)
        self.clip_banner.setVisible(False)
        ai_card.body_layout.addWidget(self.clip_banner)

        # Selector de proveedor principal + botón Refrescar modelos (Antídoto #1)
        prov_controls = QWidget(ai_card)
        prov_row = QHBoxLayout(prov_controls)
        prov_row.setContentsMargins(0, 0, 0, 0)
        prov_row.setSpacing(8)

        self.primary_provider_combo = QComboBox(prov_controls)
        for pid, meta in PROVIDER_METADATA.items():
            self.primary_provider_combo.addItem(meta.display_name, pid)
        self.primary_provider_combo.setMinimumWidth(180)
        self.primary_provider_combo.currentIndexChanged.connect(self._on_primary_provider_changed)
        prov_row.addWidget(self.primary_provider_combo)

        self.refresh_models_btn = QPushButton("Refrescar modelos", prov_controls)
        self.refresh_models_btn.setProperty("variant", "primary")
        self.refresh_models_btn.setToolTip("Consulta en vivo los modelos disponibles del proveedor activo")
        self.refresh_models_btn.clicked.connect(self.trigger_refresh_models)
        prov_row.addWidget(self.refresh_models_btn)

        detect_clip_btn = QPushButton("Detectar portapapeles", prov_controls)
        detect_clip_btn.setProperty("variant", "ghost")
        detect_clip_btn.setToolTip("Detecta automáticamente si copiaste una API key")
        detect_clip_btn.clicked.connect(self.check_clipboard_for_key)
        prov_row.addWidget(detect_clip_btn)

        ai_card.body_layout.addWidget(
            SettingRow(
                title="Proveedor principal y catálogo en vivo",
                description="Usa 'Automático (Recomendado)' para elegir los mejores modelos FAST y DEEP reales.",
                control=prov_controls,
                parent=ai_card,
            )
        )

        # Estado del catálogo + selector de modelo rápido
        model_controls = QWidget(ai_card)
        model_row = QHBoxLayout(model_controls)
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)

        self.models_combo = QComboBox(model_controls)
        self.models_combo.setMinimumWidth(260)
        self.models_combo.currentIndexChanged.connect(self._on_model_combo_changed)
        model_row.addWidget(self.models_combo)

        self.models_status_lbl = QLabel("Modo Automático", model_controls)
        self.models_status_lbl.setObjectName("StatusPill")
        model_row.addWidget(self.models_status_lbl)

        ai_card.body_layout.addWidget(
            SettingRow(
                title="Selección de modelo IA",
                description="Automático elige FAST para charla rápida y DEEP para análisis complejo.",
                control=model_controls,
                parent=ai_card,
            )
        )

        # Clave principal de Gemini en primera línea
        self._add_provider_key_row(ai_card, "gemini")

        # Expander para otros proveedores (OpenAI, Grok, Claude, Groq, DeepSeek, OpenRouter)
        other_exp = SettingExpander(
            title="Otras claves API (OpenAI, Grok, Claude, Groq, DeepSeek, OpenRouter)",
            description="Opcional: agrega claves propias como respaldo o proveedor principal.",
            expanded=False,
            parent=ai_card,
        )
        for pid in ("openai", "xai", "anthropic", "groq", "deepseek", "openrouter"):
            self._add_provider_key_row(other_exp.content_widget, pid, target_layout=other_exp.content_layout)
        ai_card.body_layout.addWidget(other_exp)

        self._body_layout.addWidget(ai_card)
        self._register_searchable(
            ai_card,
            "ia modelos gemini api key openai grok claude groq deepseek openrouter refrescar automatico portapapeles",
        )

    def _add_provider_key_row(
        self,
        parent_widget: QWidget,
        provider_id: str,
        *,
        target_layout: QVBoxLayout | None = None,
    ) -> None:
        meta = PROVIDER_METADATA[provider_id]
        controls = QWidget(parent_widget)
        row = QHBoxLayout(controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        edit = QLineEdit(controls)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(f"Pega tu {meta.env_key}...")
        edit.setMinimumWidth(220)
        self._key_inputs[provider_id] = edit
        row.addWidget(edit)

        save_btn = QPushButton("Guardar", controls)
        save_btn.setProperty("variant", "primary" if provider_id == "gemini" else "default")
        save_btn.clicked.connect(lambda _checked=False, pid=provider_id: self._on_save_provider_key(pid))
        row.addWidget(save_btn)

        pill = QLabel("Sin clave", controls)
        pill.setObjectName("StatusPill")
        self._key_status_pills[provider_id] = pill
        row.addWidget(pill)

        setting_row = SettingRow(
            title=f"Clave de {meta.display_name}",
            description=f"Variable segura en .env: {meta.env_key}",
            control=controls,
            parent=parent_widget,
        )
        if target_layout is not None:
            target_layout.addWidget(setting_row)
        elif isinstance(parent_widget, FluentCard):
            parent_widget.body_layout.addWidget(setting_row)

    # ------------------------------------------------------------------
    # Sección 2: Apariencia Windows 11 y Perfiles Editables
    # ------------------------------------------------------------------
    def _build_appearance_profiles_section(self) -> None:
        card = FluentCard(
            title="Apariencia y Personalidades",
            subtitle="Tema orgánico Windows 11 Fluent, material Mica opcional y edición de los 5 perfiles clásicos.",
            parent=self._body,
        )

        self.theme_combo = QComboBox(card)
        self.theme_combo.addItem("Automático (seguir a Windows 11)", "auto")
        self.theme_combo.addItem("Fluent Oscuro", "fluent_dark")
        self.theme_combo.addItem("Fluent Claro", "fluent_light")
        self.theme_combo.addItem("Cyber-HUD", "cyber_hud")
        self.theme_combo.setMinimumWidth(230)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        card.body_layout.addWidget(
            SettingRow(
                title="Tema visual",
                description="Cambia el aspecto global en caliente sin reiniciar.",
                control=self.theme_combo,
                parent=card,
            )
        )

        self.mica_switch = self._make_switch()
        self.mica_switch.toggled.connect(self._on_mica_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Efecto Mica nativo (Windows 11)",
                description="Translucidez sutil en el marco de ventana con respaldo sólido automático.",
                control=self.mica_switch,
                parent=card,
            )
        )

        self.win_accent_switch = self._make_switch()
        self.win_accent_switch.toggled.connect(self._on_win_accent_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Usar color de acento de Windows",
                description="Si está apagado, DeusEx inyecta el color propio de la personalidad activa.",
                control=self.win_accent_switch,
                parent=card,
            )
        )

        # Datos personales básicos para contexto (ciudad/país/nombre)
        user_box = QWidget(card)
        user_row = QHBoxLayout(user_box)
        user_row.setContentsMargins(0, 0, 0, 0)
        user_row.setSpacing(6)
        self.user_name_edit = QLineEdit(user_box)
        self.user_name_edit.setPlaceholderText("Tu nombre")
        self.city_edit = QLineEdit(user_box)
        self.city_edit.setPlaceholderText("Ciudad")
        self.country_edit = QLineEdit(user_box)
        self.country_edit.setPlaceholderText("País")
        save_user_btn = QPushButton("Guardar", user_box)
        save_user_btn.clicked.connect(self._on_save_user_identity)
        user_row.addWidget(self.user_name_edit)
        user_row.addWidget(self.city_edit)
        user_row.addWidget(self.country_edit)
        user_row.addWidget(save_user_btn)

        card.body_layout.addWidget(
            SettingRow(
                title="Tu nombre y ubicación",
                description="Usado por la personalidad, clima local y noticias adaptativas.",
                control=user_box,
                parent=card,
            )
        )

        # Expander editor de los 5 perfiles editables
        prof_exp = SettingExpander(
            title="Editor de perfiles de personalidad",
            description="Edita nombre, color de acento, tono por defecto e instrucciones del perfil seleccionado.",
            expanded=False,
            parent=card,
        )
        p_select_row = QHBoxLayout()
        self.edit_profile_combo = QComboBox(prof_exp.content_widget)
        self.edit_profile_combo.currentIndexChanged.connect(self._load_selected_profile_editor)
        p_select_row.addWidget(self.edit_profile_combo, 1)

        self.prof_accent_edit = QLineEdit(prof_exp.content_widget)
        self.prof_accent_edit.setPlaceholderText("#38BDF8")
        self.prof_accent_edit.setMaximumWidth(100)
        p_select_row.addWidget(self.prof_accent_edit)

        self.prof_tone_combo = QComboBox(prof_exp.content_widget)
        for tkey in AVAILABLE_TONES:
            self.prof_tone_combo.addItem(tkey.capitalize(), tkey)
        p_select_row.addWidget(self.prof_tone_combo)
        prof_exp.content_layout.addLayout(p_select_row)

        self.prof_tagline_edit = QLineEdit(prof_exp.content_widget)
        self.prof_tagline_edit.setPlaceholderText("Descripción breve del perfil")
        prof_exp.content_layout.addWidget(self.prof_tagline_edit)

        self.prof_prompt_edit = QPlainTextEdit(prof_exp.content_widget)
        self.prof_prompt_edit.setFixedHeight(84)
        self.prof_prompt_edit.setPlaceholderText("Prompt de personalidad...")
        prof_exp.content_layout.addWidget(self.prof_prompt_edit)

        save_prof_btn = QPushButton("Guardar cambios del perfil", prof_exp.content_widget)
        save_prof_btn.setProperty("variant", "primary")
        save_prof_btn.clicked.connect(self._on_save_profile_editor)
        prof_exp.content_layout.addWidget(save_prof_btn)

        card.body_layout.addWidget(prof_exp)

        self._body_layout.addWidget(card)
        self._register_searchable(
            card,
            "apariencia tema oscuro claro cyber hud mica acento perfil raphael jarvis friday magi glados nombre ciudad",
        )

    # ------------------------------------------------------------------
    # Sección 3: Voz, Orbe Flotante y Subtítulos
    # ------------------------------------------------------------------
    def _build_voice_orb_section(self) -> None:
        card = FluentCard(
            title="Voz y Orbe Flotante",
            subtitle="Escucha offline Vosk ES, biometría vocal, 5 estilos del Orbe reactivo y ducking selectivo.",
            parent=self._body,
        )

        # 1. Micrófono y medidor en vivo
        mic_widget = QWidget(card)
        mic_layout = QVBoxLayout(mic_widget)
        mic_layout.setContentsMargins(0, 0, 0, 0)
        mic_layout.setSpacing(6)

        mic_top = QHBoxLayout()
        mic_top.setContentsMargins(0, 0, 0, 0)
        mic_top.setSpacing(8)

        self.mic_combo = QComboBox(mic_widget)
        self.mic_combo.setMinimumWidth(260)
        mic_top.addWidget(self.mic_combo, 1)

        reload_mic_btn = QPushButton("Actualizar", mic_widget)
        reload_mic_btn.setProperty("variant", "default")
        reload_mic_btn.clicked.connect(self._reload_microphones)
        mic_top.addWidget(reload_mic_btn)
        mic_layout.addLayout(mic_top)

        meter_layout = QHBoxLayout()
        meter_layout.setContentsMargins(0, 0, 0, 0)
        meter_layout.setSpacing(8)

        self.mic_level_bar = QProgressBar(mic_widget)
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setValue(0)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setFixedHeight(8)
        meter_layout.addWidget(self.mic_level_bar, 1)

        self.mic_vad_pill = QLabel("En espera", mic_widget)
        self.mic_vad_pill.setObjectName("StatusPill")
        self.mic_vad_pill.setProperty("state", "idle")
        meter_layout.addWidget(self.mic_vad_pill)
        mic_layout.addLayout(meter_layout)

        card.body_layout.addWidget(
            SettingRow(
                title="Dispositivo de entrada (Micrófono)",
                description="Selecciona el micrófono de captura y monitoriza el nivel de entrada y VAD en vivo.",
                control=mic_widget,
                parent=card,
            )
        )

        # 2. Biometría vocal (26D)
        vp_widget = QWidget(card)
        vp_layout = QHBoxLayout(vp_widget)
        vp_layout.setContentsMargins(0, 0, 0, 0)
        vp_layout.setSpacing(8)

        self.voiceprint_switch = self._make_switch(checked=self._app.settings.voice.voiceprint_enabled)
        vp_layout.addWidget(self.voiceprint_switch)

        self.calibrate_voice_btn = QPushButton("Calibrar huella vocal (6 s)", vp_widget)
        self.calibrate_voice_btn.setProperty("variant", "default")
        vp_layout.addWidget(self.calibrate_voice_btn)

        self.voiceprint_status_lbl = QLabel("Sin calibrar", vp_widget)
        self.voiceprint_status_lbl.setObjectName("StatusPill")
        self.voiceprint_status_lbl.setProperty("state", "idle")
        vp_layout.addWidget(self.voiceprint_status_lbl)

        card.body_layout.addWidget(
            SettingRow(
                title="Biometría vocal (Huella 26D)",
                description="Reconoce únicamente la voz del propietario ignorando voces secundarias y ruido.",
                control=vp_widget,
                parent=card,
            )
        )

        # 3. Orbe Flotante (5 estilos + Vista previa 4 s)
        orb_widget = QWidget(card)
        orb_layout = QVBoxLayout(orb_widget)
        orb_layout.setContentsMargins(0, 0, 0, 0)
        orb_layout.setSpacing(6)

        orb_row1 = QHBoxLayout()
        orb_row1.setContentsMargins(0, 0, 0, 0)
        orb_row1.setSpacing(8)

        self.orb_style_combo = QComboBox(orb_widget)
        self.orb_style_combo.addItem("Reactor energético", "reactor")
        self.orb_style_combo.addItem("Ondas sinusoidales", "waves")
        self.orb_style_combo.addItem("Esfera luminosa 3D", "sphere")
        self.orb_style_combo.addItem("Ecualizador de barras", "bars")
        self.orb_style_combo.addItem("Avatar / GIF", "avatar")
        self.orb_style_combo.addItem("Geometría Sagrada (Tensura)", "sacred_geometry")
        self.orb_style_combo.addItem("Núcleo IA (J.A.R.V.I.S.)", "jarvis_core")
        self.orb_style_combo.setMinimumWidth(180)
        orb_row1.addWidget(self.orb_style_combo)

        self.orb_visibility_combo = QComboBox(orb_widget)
        self.orb_visibility_combo.addItem("Solo al hablar / activo", "speaking_only")
        self.orb_visibility_combo.addItem("Siempre visible (sutil)", "always_subtle")
        self.orb_visibility_combo.setMinimumWidth(180)
        orb_row1.addWidget(self.orb_visibility_combo)

        self.orb_corner_combo = QComboBox(orb_widget)
        self.orb_corner_combo.addItem("Inferior derecha", "bottom_right")
        self.orb_corner_combo.addItem("Inferior izquierda", "bottom_left")
        self.orb_corner_combo.addItem("Superior derecha", "top_right")
        self.orb_corner_combo.addItem("Superior izquierda", "top_left")
        self.orb_corner_combo.setMinimumWidth(160)
        orb_row1.addWidget(self.orb_corner_combo)

        self.orb_preview_btn = QPushButton("Vista previa 4 s", orb_widget)
        self.orb_preview_btn.setProperty("variant", "primary")
        orb_row1.addWidget(self.orb_preview_btn)
        orb_layout.addLayout(orb_row1)

        card.body_layout.addWidget(
            SettingRow(
                title="Orbe Flotante y Estilos Reactivos",
                description="Elige entre 7 renderers reactivos al RMS, posición en pantalla y modo de visibilidad.",
                control=orb_widget,
                parent=card,
            )
        )

        # 4. Respuesta hablada (TTS) y Ducking
        tts_widget = QWidget(card)
        tts_layout = QHBoxLayout(tts_widget)
        tts_layout.setContentsMargins(0, 0, 0, 0)
        tts_layout.setSpacing(10)

        tts_mute_lbl = QLabel("Silenciar habla:", tts_widget)
        tts_layout.addWidget(tts_mute_lbl)
        self.tts_mute_switch = self._make_switch(checked=self._app.settings.voice.tts_muted)
        tts_layout.addWidget(self.tts_mute_switch)

        rate_lbl = QLabel("Velocidad:", tts_widget)
        tts_layout.addWidget(rate_lbl)
        self.tts_rate_spin = QDoubleSpinBox(tts_widget)
        self.tts_rate_spin.setRange(0.5, 2.0)
        self.tts_rate_spin.setSingleStep(0.1)
        self.tts_rate_spin.setValue(self._app.settings.voice.tts_rate)
        self.tts_rate_spin.setSuffix("x")
        self.tts_rate_spin.setFixedWidth(75)
        tts_layout.addWidget(self.tts_rate_spin)

        duck_lbl = QLabel("Ducking multimedia:", tts_widget)
        tts_layout.addWidget(duck_lbl)
        self.ducking_spin = QSpinBox(tts_widget)
        self.ducking_spin.setRange(0, 100)
        self.ducking_spin.setValue(self._app.settings.voice.ducking_level_pct)
        self.ducking_spin.setSuffix("%")
        self.ducking_spin.setFixedWidth(75)
        tts_layout.addWidget(self.ducking_spin)

        card.body_layout.addWidget(
            SettingRow(
                title="Respuesta hablada (TTS) y Atenuación",
                description="Modo solo texto, velocidad de locución Edge-TTS y nivel de atenuación multimedia.",
                control=tts_widget,
                parent=card,
            )
        )

        # 5. Modo "En Llamada"
        self.in_call_switch = self._make_switch(checked=self._app.settings.voice.in_call_text_only)
        card.body_layout.addWidget(
            SettingRow(
                title="Modo \"En Llamada\"",
                description="Fuerza respuestas solo texto y desactiva el ducking y seguimiento continuo de 6 s.",
                control=self.in_call_switch,
                parent=card,
            )
        )

        # 6. Silenciar micrófono (pausar escucha)
        self.mic_mute_switch = self._make_switch(checked=self._app.settings.voice.mic_muted)
        card.body_layout.addWidget(
            SettingRow(
                title="Silenciar micrófono (pausar escucha)",
                description="Pausa de inmediato la captura de audio y reconocimiento de voz (0% uso de micrófono).",
                control=self.mic_mute_switch,
                parent=card,
            )
        )

        self._body_layout.addWidget(card)
        self._register_searchable(
            card,
            "voz orbe microfono nivel calibrar huella vocal biometria reactor waves sphere bars avatar vista previa tts velocidad silencio ducking llamada discord teams zoom",
        )

    # ------------------------------------------------------------------
    # Sección 4: Privacidad, Seguridad (SafetyGate) y Cost Guard
    # ------------------------------------------------------------------
    def _build_privacy_cost_section(self) -> None:
        card = FluentCard(
            title="Privacidad, Seguridad y Consumo (Cost Guard)",
            subtitle="Control físico SafetyGate, Modo Privacidad (Ctrl+Shift+P) y presupuesto opcional de tokens.",
            parent=self._body,
        )

        self.privacy_switch = self._make_switch()
        self.privacy_switch.toggled.connect(self._on_privacy_switch_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Modo Privacidad (Ctrl+Shift+P)",
                description="Congela la memoria adaptativa automática y bloquea visión/portapapeles.",
                control=self.privacy_switch,
                parent=card,
            )
        )

        self.auto_memory_switch = self._make_switch()
        self.auto_memory_switch.toggled.connect(self._on_auto_memory_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Memoria adaptativa automática",
                description="Aprende hechos e intereses útiles con píldora 'Deshacer' bajo cada respuesta.",
                control=self.auto_memory_switch,
                parent=card,
            )
        )

        self.trusted_msg_switch = self._make_switch()
        self.trusted_msg_switch.toggled.connect(self._on_trusted_msg_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Permitir envíos de mensajería sin confirmación",
                description="Opcional: las acciones destructivas del PC SIEMPRE pasan por SafetyGate.",
                control=self.trusted_msg_switch,
                parent=card,
            )
        )

        budget_box = QWidget(card)
        budget_row = QHBoxLayout(budget_box)
        budget_row.setContentsMargins(0, 0, 0, 0)
        budget_row.setSpacing(8)

        self.budget_spin = QSpinBox(budget_box)
        self.budget_spin.setRange(0, 5_000_000)
        self.budget_spin.setSingleStep(10_000)
        self.budget_spin.setSpecialValueText("Sin tope")
        self.budget_spin.setMinimumWidth(130)
        self.budget_spin.valueChanged.connect(self._on_budget_changed)
        budget_row.addWidget(self.budget_spin)

        self.cost_detail_lbl = QLabel("", budget_box)
        self.cost_detail_lbl.setObjectName("StatusPill")
        budget_row.addWidget(self.cost_detail_lbl)

        card.body_layout.addWidget(
            SettingRow(
                title="Tope diario de tokens (Cost Guard)",
                description="Si se alcanza, degrada automáticamente DEEP → FAST para proteger cuotas.",
                control=budget_box,
                parent=card,
            )
        )

        self._body_layout.addWidget(card)
        self._register_searchable(
            card,
            "privacidad seguridad safetygate cost guard tokens presupuesto memoria automatica",
        )

    # ------------------------------------------------------------------
    # Sección 4: Modo Desarrollador (Oculto por defecto)
    # ------------------------------------------------------------------
    def _build_developer_section(self) -> None:
        card = FluentCard(
            title="Modo Desarrollador",
            subtitle="Oculto por defecto para usuarios finales. Desbloquea Dry-Run, selector manual y tabla .env.",
            parent=self._body,
        )

        self.dev_mode_switch = self._make_switch()
        self.dev_mode_switch.toggled.connect(self._on_dev_mode_toggled)
        card.body_layout.addWidget(
            SettingRow(
                title="Activar Modo Desarrollador",
                description="Muestra controles avanzados de simulación, diagnóstico y entorno.",
                control=self.dev_mode_switch,
                parent=card,
            )
        )

        self.dev_panel = QWidget(card)
        dev_layout = QVBoxLayout(self.dev_panel)
        dev_layout.setContentsMargins(0, 6, 0, 0)
        dev_layout.setSpacing(10)

        self.dry_run_switch = self._make_switch()
        self.dry_run_switch.toggled.connect(self._on_dry_run_toggled)
        dev_layout.addWidget(
            SettingRow(
                title="Modo Simulación (Dry-Run)",
                description="Simula herramientas y Fast-Path sin ejecutar cambios reales en el PC.",
                control=self.dry_run_switch,
                parent=self.dev_panel,
            )
        )

        self.show_model_badge_switch = self._make_switch()
        self.show_model_badge_switch.toggled.connect(self._on_show_model_badge_toggled)
        dev_layout.addWidget(
            SettingRow(
                title="Mostrar insignia de modelo en cabecera de Chat",
                description="Muestra qué proveedor y modelo está activo durante la conversación.",
                control=self.show_model_badge_switch,
                parent=self.dev_panel,
            )
        )

        self.env_preview_lbl = QLabel("", self.dev_panel)
        self.env_preview_lbl.setObjectName("MutedLabel")
        self.env_preview_lbl.setWordWrap(True)
        dev_layout.addWidget(
            SettingRow(
                title="Variables almacenadas en .env (enmascaradas)",
                description="Los valores nunca se muestran completos ni se incluyen en backups.",
                control=self.env_preview_lbl,
                parent=self.dev_panel,
            )
        )

        card.body_layout.addWidget(self.dev_panel)
        self._body_layout.addWidget(card)
        self._register_searchable(
            card,
            "desarrollador avanzado dry run simulacion env variables puertos",
        )

    # ------------------------------------------------------------------
    # Señales y sincronización
    # ------------------------------------------------------------------
    def _connect_signals(self) -> None:
        self._models_refreshed.connect(self._on_models_refreshed_ui)
        self._models_failed.connect(self._on_models_failed_ui)
        self._bridge.privacy_mode_changed.connect(lambda _enabled: self.refresh_from_app())
        self._bridge.cost_usage_updated.connect(lambda _event: self._refresh_cost_summary())
        self._bridge.settings_updated.connect(lambda _event: self.refresh_from_app())
        self._bridge.voice_mute_changed.connect(lambda _muted: self.refresh_from_app())
        self._bridge.mic_level_changed.connect(self._on_mic_level_ui)
        self._bridge.voice_calibration_updated.connect(self._on_voice_calibration_ui)

        self.mic_combo.currentIndexChanged.connect(self._on_mic_changed)
        self.voiceprint_switch.toggled.connect(self._on_voiceprint_toggled)
        self.calibrate_voice_btn.clicked.connect(self._on_calibrate_voice_clicked)
        self.orb_style_combo.currentIndexChanged.connect(self._on_orb_style_changed)
        self.orb_visibility_combo.currentIndexChanged.connect(self._on_orb_visibility_changed)
        self.orb_corner_combo.currentIndexChanged.connect(self._on_orb_corner_changed)
        self.orb_preview_btn.clicked.connect(lambda: self._app.trigger_orb_preview(duration_sec=4.0))
        self.tts_mute_switch.toggled.connect(self._on_tts_mute_toggled)
        self.mic_mute_switch.toggled.connect(self._app.set_mic_muted)
        self._bridge.mic_mute_changed.connect(self.mic_mute_switch.setChecked)
        self.tts_rate_spin.valueChanged.connect(self._on_tts_rate_changed)
        self.ducking_spin.valueChanged.connect(self._on_ducking_changed)
        self.in_call_switch.toggled.connect(self._on_in_call_toggled)

    def set_accent_color(self, accent_hex: str) -> None:
        for sw in self._switches:
            sw.set_accent_color(accent_hex)

    def _apply_search_filter(self, query: str) -> None:
        needle = query.strip().lower()
        for widget, blob in self._searchable_items:
            widget.setVisible(not needle or needle in blob)

    def check_clipboard_for_key(self) -> bool:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        detected = detect_api_key_from_text(clipboard.text())
        if detected is None:
            self._detected_clip = None
            self.clip_banner.setVisible(False)
            return False

        provider_id, env_key, _token = detected
        if self._app.secrets.get(env_key) == detected[2]:
            self.clip_banner.setVisible(False)
            return False

        meta = PROVIDER_METADATA[provider_id]
        self._detected_clip = detected
        self.clip_label.setText(
            f"Smart Clipboard Catcher: detectamos una clave de {meta.display_name} ({env_key}) en tu portapapeles."
        )
        self.clip_banner.setVisible(True)
        return True

    def _save_detected_clipboard_key(self) -> None:
        if self._detected_clip is None:
            return
        provider_id, env_key, token = self._detected_clip
        self._app.save_api_key(env_key, token, provider_id=provider_id)
        self._detected_clip = None
        self.clip_banner.setVisible(False)
        self.refresh_from_app()
        if provider_id == self._app.settings.brain.primary_provider:
            self.trigger_refresh_models()

    def _on_save_provider_key(self, provider_id: str) -> None:
        meta = PROVIDER_METADATA[provider_id]
        edit = self._key_inputs[provider_id]
        raw_val = edit.text().strip()
        self._app.save_api_key(meta.env_key, raw_val, provider_id=provider_id)
        edit.clear()
        self.refresh_from_app()
        if raw_val and provider_id == self._app.settings.brain.primary_provider:
            self.trigger_refresh_models()

    def trigger_refresh_models(self) -> None:
        provider_id = self._app.settings.brain.primary_provider
        self.refresh_models_btn.setEnabled(False)
        self.models_status_lbl.setText("Consultando modelos...")

        fut = self._app.refresh_models(provider_id, force=True)

        def _done(done_fut: concurrent.futures.Future[list[ModelInfo]]) -> None:
            try:
                models = done_fut.result()
            except Exception as exc:
                self._models_failed.emit(provider_id, str(exc))
                return
            self._models_refreshed.emit(provider_id, models)

        fut.add_done_callback(_done)

    def _on_models_refreshed_ui(self, provider_id: str, models_obj: object) -> None:
        self.refresh_models_btn.setEnabled(True)
        models = models_obj if isinstance(models_obj, list) else []
        self._populate_models_combo(provider_id, models)
        if models:
            self.models_status_lbl.setText(f"{len(models)} modelos reales")
            self.models_status_lbl.setProperty("state", "active")
        else:
            self.models_status_lbl.setText("Sin clave configurada")
            self.models_status_lbl.setProperty("state", "warning")
        self.models_status_lbl.style().unpolish(self.models_status_lbl)
        self.models_status_lbl.style().polish(self.models_status_lbl)

    def _on_models_failed_ui(self, _provider_id: str, error_msg: str) -> None:
        self.refresh_models_btn.setEnabled(True)
        self.models_status_lbl.setText("Error al refrescar")
        self.models_status_lbl.setToolTip(error_msg)
        self.models_status_lbl.setProperty("state", "danger")
        self.models_status_lbl.style().unpolish(self.models_status_lbl)
        self.models_status_lbl.style().polish(self.models_status_lbl)

    def _populate_models_combo(self, provider_id: str, models: list[ModelInfo]) -> None:
        self._syncing = True
        try:
            self.models_combo.clear()
            self.models_combo.addItem("Automático (Recomendado)", "__auto__")
            for m in models:
                self.models_combo.addItem(f"{m.id}", m.id)

            if self._app.settings.brain.model_selection_mode == "manual":
                idx = self.models_combo.findData(self._app.settings.brain.manual_fast_model)
                self.models_combo.setCurrentIndex(idx if idx >= 0 else 0)
            else:
                self.models_combo.setCurrentIndex(0)
        finally:
            self._syncing = False

    def _on_primary_provider_changed(self, _index: int) -> None:
        if self._syncing:
            return
        pid = self.primary_provider_combo.currentData()
        if isinstance(pid, str) and pid in PROVIDER_METADATA:
            self._app.settings.brain.primary_provider = pid
            self._app.save_settings(section="brain")
            cached = self._app.catalog.get_cached_models(pid)
            self._populate_models_combo(pid, cached)

    def _on_model_combo_changed(self, _index: int) -> None:
        if self._syncing:
            return
        val = self.models_combo.currentData()
        if not isinstance(val, str):
            return
        if val == "__auto__":
            self._app.settings.brain.model_selection_mode = "auto"
            self._app.settings.brain.manual_fast_model = ""
            self._app.settings.brain.manual_deep_model = ""
        else:
            self._app.settings.brain.model_selection_mode = "manual"
            self._app.settings.brain.manual_fast_model = val
            self._app.settings.brain.manual_deep_model = val
        self._app.save_settings(section="brain")

    def _on_theme_changed(self, _index: int) -> None:
        if self._syncing:
            return
        mode = self.theme_combo.currentData()
        if mode in ("auto", "fluent_dark", "fluent_light", "cyber_hud"):
            self._app.settings.ui.theme = cast(
                Literal["auto", "fluent_dark", "fluent_light", "cyber_hud"],
                mode,
            )
            self._app.save_settings(section="ui")
            self.theme_changed.emit()

    def _on_mica_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.ui.use_mica = checked
        self._app.save_settings(section="ui")
        self.theme_changed.emit()

    def _on_win_accent_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.ui.use_windows_accent = checked
        self._app.save_settings(section="ui")
        self.theme_changed.emit()

    def _on_save_user_identity(self) -> None:
        self._app.settings.general.user_name = self.user_name_edit.text().strip()
        self._app.settings.general.city = self.city_edit.text().strip()
        self._app.settings.general.country = self.country_edit.text().strip()
        self._app.save_settings(section="general")

    def _load_selected_profile_editor(self, _index: int = 0) -> None:
        pid = self.edit_profile_combo.currentData()
        if not isinstance(pid, str) or not pid:
            return
        prof = self._app.profiles.get(pid)
        self.prof_accent_edit.setText(prof.accent_hex)
        t_idx = self.prof_tone_combo.findData(prof.default_tone)
        if t_idx >= 0:
            self.prof_tone_combo.setCurrentIndex(t_idx)
        self.prof_tagline_edit.setText(prof.tagline)
        self.prof_prompt_edit.setPlainText(prof.system_prompt)

    def _on_save_profile_editor(self) -> None:
        pid = self.edit_profile_combo.currentData()
        if not isinstance(pid, str) or not pid:
            return
        current = self._app.profiles.get(pid)
        accent_raw = self.prof_accent_edit.text().strip()
        accent_hex = accent_raw if _HEX_COLOR_RE.match(accent_raw) else current.accent_hex
        tone_raw = self.prof_tone_combo.currentData()
        tone = str(tone_raw) if isinstance(tone_raw, str) else current.default_tone

        updated = PersonalityProfile(
            id=current.id,
            name=current.name,
            tagline=self.prof_tagline_edit.text().strip() or current.tagline,
            accent_hex=accent_hex,
            default_tone=tone,
            default_voice=current.default_voice,
            wake_words=current.wake_words,
            system_prompt=self.prof_prompt_edit.toPlainText().strip() or current.system_prompt,
            is_builtin=current.is_builtin,
        )
        self._app.profiles.save_profile(updated)
        if self._app.settings.ui.active_profile_id == updated.id:
            self._app.set_active_profile(updated.id, tone)
            self.theme_changed.emit()

    def _on_privacy_switch_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.set_privacy_mode(checked)

    def _on_auto_memory_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.brain.memory_auto_extract = checked
        self._app.save_settings(section="brain")

    def _on_trusted_msg_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.security.allow_trusted_messaging_bypass = checked
        self._app.save_settings(section="security")

    def _on_budget_changed(self, value: int) -> None:
        if self._syncing:
            return
        self._app.settings.brain.daily_token_budget = value
        self._app.save_settings(section="brain")
        self._refresh_cost_summary()

    def _on_dev_mode_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.developer.enabled = checked
        if not checked:
            self._app.settings.developer.dry_run = False
        self._app.save_settings(section="developer")
        self.dev_panel.setVisible(checked)

    def _on_dry_run_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.developer.dry_run = checked
        self._app.save_settings(section="developer")

    def _on_show_model_badge_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.brain.show_model_selector_in_chat = checked
        self._app.save_settings(section="brain")

    # ------------------------------------------------------------------
    # Manejadores de sección: Voz y Orbe
    # ------------------------------------------------------------------
    def _reload_microphones(self) -> None:
        was_syncing = self._syncing
        self._syncing = True
        try:
            curr_data = self.mic_combo.currentData()
            self.mic_combo.clear()
            self.mic_combo.addItem("Predeterminado del sistema", None)
            mics = self._app.list_microphones()
            for m in mics:
                label = f"{m.name} ({m.channels} ch)" + (" [Default]" if m.is_default else "")
                self.mic_combo.addItem(label, m.index)
            target = curr_data if curr_data is not None else self._app.settings.voice.input_device_index
            idx = self.mic_combo.findData(target)
            if idx >= 0:
                self.mic_combo.setCurrentIndex(idx)
            else:
                self.mic_combo.setCurrentIndex(0)
        finally:
            self._syncing = was_syncing

    def _on_mic_changed(self, _index: int) -> None:
        if self._syncing:
            return
        data = self.mic_combo.currentData()
        dev_idx = int(data) if data is not None else None
        self._app.set_input_device(dev_idx)

    def _on_mic_level_ui(self, level: float, is_speech: bool, _device_index: object) -> None:
        pct = round(max(0.0, min(1.0, float(level))) * 100.0)
        self.mic_level_bar.setValue(pct)
        if is_speech:
            self.mic_vad_pill.setText("Voz detectada")
            self.mic_vad_pill.setProperty("state", "active")
        else:
            self.mic_vad_pill.setText("En espera")
            self.mic_vad_pill.setProperty("state", "idle")
        self.mic_vad_pill.style().unpolish(self.mic_vad_pill)
        self.mic_vad_pill.style().polish(self.mic_vad_pill)

    def _on_voiceprint_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.voice.voiceprint_enabled = bool(checked)
        self._app.save_settings(section="voice")
        self._update_voiceprint_status_ui()

    def _on_calibrate_voice_clicked(self) -> None:
        if self._app.settings.general.privacy_mode:
            self.voiceprint_status_lbl.setText("Calibración bloqueada en Modo Privacidad.")
            return
        started = self._app.start_voiceprint_calibration(duration_sec=6.0)
        if started:
            self.calibrate_voice_btn.setEnabled(False)
            self.voiceprint_status_lbl.setText("Habla con naturalidad durante 6 segundos...")
        else:
            self.voiceprint_status_lbl.setText("No se pudo iniciar (verifica micrófono y permisos).")

    def _on_voice_calibration_ui(self, event: object) -> None:
        from deusex.core.events import VoiceCalibrationEvent

        if not isinstance(event, VoiceCalibrationEvent):
            return
        if event.status in ("started", "progress"):
            self.calibrate_voice_btn.setEnabled(False)
            pct = round(event.progress * 100.0)
            self.voiceprint_status_lbl.setText(f"Calibrando huella vocal... {pct}%")
        else:
            self.calibrate_voice_btn.setEnabled(True)
            self._update_voiceprint_status_ui()
            if event.message:
                self.voiceprint_status_lbl.setText(event.message)

    def _update_voiceprint_status_ui(self) -> None:
        vp = self._app.voice_module.voiceprint
        is_enrolled = vp is not None and vp.is_enrolled
        if is_enrolled:
            self.voiceprint_status_lbl.setText("Huella vocal calibrada")
            self.voiceprint_status_lbl.setProperty("state", "active")
        else:
            self.voiceprint_status_lbl.setText("Sin calibrar")
            self.voiceprint_status_lbl.setProperty("state", "idle")
        self.voiceprint_status_lbl.style().unpolish(self.voiceprint_status_lbl)
        self.voiceprint_status_lbl.style().polish(self.voiceprint_status_lbl)

    def _on_orb_style_changed(self, _index: int) -> None:
        if self._syncing:
            return
        style_val = str(self.orb_style_combo.currentData() or "reactor")
        self._app.settings.ui.orb_style = cast(
            Literal[
                "reactor",
                "waves",
                "sphere",
                "bars",
                "avatar",
                "sacred_geometry",
                "jarvis_core",
            ],
            style_val,
        )
        self._app.save_settings(section="ui")

    def _on_orb_visibility_changed(self, _index: int) -> None:
        if self._syncing:
            return
        vis_val = str(self.orb_visibility_combo.currentData() or "speaking_only")
        self._app.settings.ui.orb_visibility = cast(
            Literal["speaking_only", "always_subtle"], vis_val
        )
        self._app.save_settings(section="ui")

    def _on_orb_corner_changed(self, _index: int) -> None:
        if self._syncing:
            return
        corner_val = str(self.orb_corner_combo.currentData() or "bottom_right")
        self._app.settings.ui.orb_corner = cast(
            Literal["bottom_right", "bottom_left", "top_right", "top_left"], corner_val
        )
        self._app.save_settings(section="ui")

    def _on_tts_mute_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.set_voice_muted(bool(checked))

    def _on_tts_rate_changed(self, value: float) -> None:
        if self._syncing:
            return
        self._app.settings.voice.tts_rate = round(float(value), 2)
        self._app.save_settings(section="voice")

    def _on_ducking_changed(self, value: int) -> None:
        if self._syncing:
            return
        self._app.settings.voice.ducking_level_pct = int(value)
        self._app.save_settings(section="voice")

    def _on_in_call_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._app.settings.voice.in_call_text_only = bool(checked)
        self._app.save_settings(section="voice")

    def _refresh_cost_summary(self) -> None:
        summary = self._app.cost_guard.today_summary()
        self.cost_detail_lbl.setText(
            f"Hoy: {summary.fast_path_hits} Fast-Path · {summary.cloud_calls} nube · {summary.total_tokens} tokens"
        )

    def refresh_from_app(self) -> None:
        self._syncing = True
        try:
            pid = self._app.settings.brain.primary_provider
            p_idx = self.primary_provider_combo.findData(pid)
            if p_idx >= 0:
                self.primary_provider_combo.setCurrentIndex(p_idx)

            cached_models = self._app.catalog.get_cached_models(pid)
            self._populate_models_combo(pid, cached_models)
            if cached_models:
                self.models_status_lbl.setText(f"{len(cached_models)} modelos en caché")
                self.models_status_lbl.setProperty("state", "active")
            else:
                self.models_status_lbl.setText("Modo Automático")
                self.models_status_lbl.setProperty("state", "idle")
            self.models_status_lbl.style().unpolish(self.models_status_lbl)
            self.models_status_lbl.style().polish(self.models_status_lbl)

            for prov_id, meta in PROVIDER_METADATA.items():
                pill = self._key_status_pills[prov_id]
                has_key = self._app.secrets.has(meta.env_key)
                pill.setText("Configurada" if has_key else "Sin clave")
                pill.setProperty("state", "active" if has_key else "idle")
                pill.style().unpolish(pill)
                pill.style().polish(pill)

            t_idx = self.theme_combo.findData(self._app.settings.ui.theme)
            if t_idx >= 0:
                self.theme_combo.setCurrentIndex(t_idx)
            self.mica_switch.setChecked(self._app.settings.ui.use_mica)
            self.win_accent_switch.setChecked(self._app.settings.ui.use_windows_accent)

            self.user_name_edit.setText(self._app.settings.general.user_name)
            self.city_edit.setText(self._app.settings.general.city)
            self.country_edit.setText(self._app.settings.general.country)

            prev_edit_pid = self.edit_profile_combo.currentData()
            self.edit_profile_combo.clear()
            for prof in self._app.profiles.list_profiles():
                self.edit_profile_combo.addItem(f"{prof.name} ({prof.id})", prof.id)
            target_pid = (
                prev_edit_pid
                if isinstance(prev_edit_pid, str) and prev_edit_pid
                else self._app.settings.ui.active_profile_id
            )
            e_idx = self.edit_profile_combo.findData(target_pid)
            if e_idx >= 0:
                self.edit_profile_combo.setCurrentIndex(e_idx)
            self._load_selected_profile_editor()

            # Voz y Orbe
            self._reload_microphones()
            self.voiceprint_switch.setChecked(self._app.settings.voice.voiceprint_enabled)
            self._update_voiceprint_status_ui()

            s_idx = self.orb_style_combo.findData(self._app.settings.ui.orb_style)
            if s_idx >= 0:
                self.orb_style_combo.setCurrentIndex(s_idx)

            v_idx = self.orb_visibility_combo.findData(self._app.settings.ui.orb_visibility)
            if v_idx >= 0:
                self.orb_visibility_combo.setCurrentIndex(v_idx)

            c_idx = self.orb_corner_combo.findData(self._app.settings.ui.orb_corner)
            if c_idx >= 0:
                self.orb_corner_combo.setCurrentIndex(c_idx)

            self.tts_mute_switch.setChecked(self._app.settings.voice.tts_muted)
            self.mic_mute_switch.setChecked(self._app.settings.voice.mic_muted)
            self.tts_rate_spin.setValue(self._app.settings.voice.tts_rate)
            self.ducking_spin.setValue(self._app.settings.voice.ducking_level_pct)
            self.in_call_switch.setChecked(self._app.settings.voice.in_call_text_only)

            self.privacy_switch.setChecked(self._app.settings.general.privacy_mode)
            self.auto_memory_switch.setChecked(self._app.settings.brain.memory_auto_extract)
            self.trusted_msg_switch.setChecked(
                self._app.settings.security.allow_trusted_messaging_bypass
            )
            self.budget_spin.setValue(self._app.settings.brain.daily_token_budget)
            self._refresh_cost_summary()

            dev_on = self._app.settings.developer.enabled
            self.dev_mode_switch.setChecked(dev_on)
            self.dev_panel.setVisible(dev_on)
            self.dry_run_switch.setChecked(self._app.settings.developer.dry_run)
            self.show_model_badge_switch.setChecked(
                self._app.settings.brain.show_model_selector_in_chat
            )

            masked = self._app.secrets.masked_items()
            if masked:
                preview = " · ".join(f"{k}={v}" for k, v in masked.items())
            else:
                preview = "Sin variables en .env local"
            self.env_preview_lbl.setText(preview)
        finally:
            self._syncing = False
