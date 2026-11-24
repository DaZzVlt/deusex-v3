from __future__ import annotations

from typing import Literal, cast

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deusex.app import DeusExApplication
from deusex.brain.providers import PROVIDER_METADATA, detect_api_key_from_text
from deusex.ui.components import FluentCard, SettingRow


class OnboardingDialog(QDialog):
    """Asistente inicial de 4 pasos para nuevos usuarios (con botón 'Saltar todo' en el paso 1)."""

    theme_preview_requested = Signal()

    def __init__(
        self,
        app: DeusExApplication,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._step = 0
        self._detected_clip: tuple[str, str, str] | None = None

        self.setObjectName("OnboardingRoot")
        self.setWindowTitle("Bienvenido a DeusEx v3")
        self.resize(620, 440)
        self.setModal(True)

        self._build_ui()
        self._update_step_ui()
        self.check_clipboard_for_key()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        self._step_title = QLabel("Paso 1 de 4 · Tu asistente", self)
        self._step_title.setObjectName("ViewTitle")
        header.addWidget(self._step_title, 1)

        self.skip_btn = QPushButton("Saltar todo", self)
        self.skip_btn.setProperty("variant", "ghost")
        self.skip_btn.clicked.connect(self.finish_onboarding)
        header.addWidget(self.skip_btn)
        root.addLayout(header)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_step_1())
        self._stack.addWidget(self._build_step_2())
        self._stack.addWidget(self._build_step_3())
        self._stack.addWidget(self._build_step_4())
        root.addWidget(self._stack, 1)

        footer = QHBoxLayout()
        self.back_btn = QPushButton("Atrás", self)
        self.back_btn.clicked.connect(self._go_back)
        footer.addWidget(self.back_btn)

        footer.addStretch(1)

        self.next_btn = QPushButton("Siguiente", self)
        self.next_btn.setProperty("variant", "primary")
        self.next_btn.clicked.connect(self._go_next)
        footer.addWidget(self.next_btn)
        root.addLayout(footer)

    def _build_step_1(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        self.name_edit = QLineEdit(page)
        self.name_edit.setPlaceholderText("Tu nombre (opcional)")
        self.name_edit.setText(self._app.settings.general.user_name)
        layout.addWidget(
            SettingRow(
                title="¿Cómo te llamamos?",
                description="DeusEx usará tu nombre para saludarte y personalizar respuestas.",
                control=self.name_edit,
                parent=page,
            )
        )

        self.theme_combo = QComboBox(page)
        self.theme_combo.addItem("Automático (seguir a Windows 11)", "auto")
        self.theme_combo.addItem("Fluent Oscuro", "fluent_dark")
        self.theme_combo.addItem("Fluent Claro", "fluent_light")
        self.theme_combo.addItem("Cyber-HUD", "cyber_hud")
        t_idx = self.theme_combo.findData(self._app.settings.ui.theme)
        if t_idx >= 0:
            self.theme_combo.setCurrentIndex(t_idx)
        self.theme_combo.currentIndexChanged.connect(self._apply_step1_preview)
        layout.addWidget(
            SettingRow(
                title="Tema visual",
                description="Puedes cambiarlo en cualquier momento desde el pie del sidebar.",
                control=self.theme_combo,
                parent=page,
            )
        )

        self.profile_combo = QComboBox(page)
        for prof in self._app.profiles.list_profiles():
            self.profile_combo.addItem(f"{prof.name} - {prof.tagline[:42]}", prof.id)
        p_idx = self.profile_combo.findData(self._app.settings.ui.active_profile_id)
        if p_idx >= 0:
            self.profile_combo.setCurrentIndex(p_idx)
        self.profile_combo.currentIndexChanged.connect(self._apply_step1_preview)
        layout.addWidget(
            SettingRow(
                title="Personalidad inicial",
                description="Define el tono, voz y color de acento de tu interfaz.",
                control=self.profile_combo,
                parent=page,
            )
        )
        layout.addStretch(1)
        return page

    def _build_step_2(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        card = FluentCard(
            title="Conectar Cerebro IA (Opcional)",
            subtitle=(
                "Pega tu GEMINI_API_KEY gratuita o continúa en Modo Local (Zero-Base) "
                "usando temporizadores y comandos rápidos sin cuenta."
            ),
            parent=page,
        )

        self.clip_lbl = QLabel("", card)
        self.clip_lbl.setObjectName("StatusPill")
        self.clip_lbl.setProperty("state", "active")
        self.clip_lbl.setVisible(False)
        card.body_layout.addWidget(self.clip_lbl)

        key_row = QHBoxLayout()
        self.gemini_key_edit = QLineEdit(card)
        self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key_edit.setPlaceholderText("Pega tu GEMINI_API_KEY aquí (opcional)...")
        key_row.addWidget(self.gemini_key_edit, 1)

        detect_btn = QPushButton("Detectar portapapeles", card)
        detect_btn.clicked.connect(self.check_clipboard_for_key)
        key_row.addWidget(detect_btn)
        card.body_layout.addLayout(key_row)

        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_step_3(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        card = FluentCard(
            title="Cuentas y Módulos Zero-Base",
            subtitle=(
                "Todos los módulos enchufables inician apagados para ahorrar RAM y CPU. "
                "Podrás encender PC Local, Red, Voz, Google, Spotify, Discord y WhatsApp desde el Sidebar cuando quieras."
            ),
            parent=page,
        )
        info = QLabel(
            "Atajos globales listos:\n"
            "• Alt + Espacio: abrir o cerrar el Mini-Chat de comandos rápidos.\n"
            "• Ctrl + Shift + P: activar o desactivar el Modo Privacidad al instante.",
            card,
        )
        info.setWordWrap(True)
        card.body_layout.addWidget(info)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _build_step_4(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(10)

        card = FluentCard(
            title="Todo listo para comenzar",
            subtitle=(
                "Tu configuración quedó guardada localmente en %LOCALAPPDATA%\\DeusEx\\. "
                "Ninguna clave ni dato sale de tu equipo sin tu permiso."
            ),
            parent=page,
        )
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def check_clipboard_for_key(self) -> bool:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        detected = detect_api_key_from_text(clipboard.text())
        if detected is None:
            self.clip_lbl.setVisible(False)
            return False
        provider_id, env_key, token = detected
        self._detected_clip = detected
        meta = PROVIDER_METADATA[provider_id]
        self.clip_lbl.setText(f"Smart Clipboard Catcher: clave detectada de {meta.display_name} ({env_key})")
        self.clip_lbl.setVisible(True)
        if provider_id == "gemini" and not self.gemini_key_edit.text().strip():
            self.gemini_key_edit.setText(token)
        return True

    def _apply_step1_preview(self) -> None:
        theme = self.theme_combo.currentData()
        if theme in ("auto", "fluent_dark", "fluent_light", "cyber_hud"):
            self._app.settings.ui.theme = cast(
                Literal["auto", "fluent_dark", "fluent_light", "cyber_hud"],
                theme,
            )
        pid = self.profile_combo.currentData()
        if isinstance(pid, str) and pid:
            self._app.settings.ui.active_profile_id = pid
        self.theme_preview_requested.emit()

    def _update_step_ui(self) -> None:
        titles = (
            "Paso 1 de 4 · Tu asistente",
            "Paso 2 de 4 · Cerebro IA",
            "Paso 3 de 4 · Atajos y Zero-Base",
            "Paso 4 de 4 · ¡Listo!",
        )
        self._step_title.setText(titles[self._step])
        self._stack.setCurrentIndex(self._step)
        self.back_btn.setEnabled(self._step > 0)
        self.next_btn.setText("Empezar a usar DeusEx" if self._step == 3 else "Siguiente")

    def _go_back(self) -> None:
        if self._step > 0:
            self._step -= 1
            self._update_step_ui()

    def _go_next(self) -> None:
        if self._step < 3:
            self._step += 1
            self._update_step_ui()
            if self._step == 1:
                self.check_clipboard_for_key()
            return
        self.finish_onboarding()

    def finish_onboarding(self) -> None:
        self._app.settings.general.user_name = self.name_edit.text().strip()
        self._apply_step1_preview()

        raw_key = self.gemini_key_edit.text().strip()
        if raw_key:
            self._app.save_api_key("GEMINI_API_KEY", raw_key, provider_id="gemini")
        elif self._detected_clip is not None:
            pid, env_key, token = self._detected_clip
            self._app.save_api_key(env_key, token, provider_id=pid)

        self._app.settings.general.onboarding_completed = True
        self._app.save_settings(section="all")
        self.accept()
