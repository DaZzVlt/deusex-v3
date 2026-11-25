from __future__ import annotations

import concurrent.futures
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
from deusex.modules.workspace import GoogleAuthSessionInfo
from deusex.ui.components import FluentCard, SettingRow


class OnboardingDialog(QDialog):
    """Asistente inicial de 4 pasos para nuevos usuarios (con botón 'Saltar todo' en el paso 1)."""

    theme_preview_requested = Signal()
    _oauth_finished = Signal(bool, str)
    _setup_sync_finished = Signal(bool, str)

    def __init__(
        self,
        app: DeusExApplication,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._step = 0
        self._closing = False
        self._google_busy = False
        self._detected_clip: tuple[str, str, str] | None = None

        self.setObjectName("OnboardingRoot")
        self.setWindowTitle("Bienvenido a DeusEx v3")
        self.resize(620, 440)
        self.setModal(True)

        self._oauth_finished.connect(self._on_oauth_finished)
        self._setup_sync_finished.connect(self._on_setup_sync_finished)

        self._build_ui()
        self._update_step_ui()
        self._refresh_oauth_status()
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

        sync_row = QHBoxLayout()
        self.setup_sync_btn = QPushButton("Abrir guía y Setup Sync (127.0.0.1)", card)
        self.setup_sync_btn.setObjectName("OnboardingSetupSyncBtn")
        self.setup_sync_btn.clicked.connect(self._on_setup_sync_clicked)
        sync_row.addWidget(self.setup_sync_btn)
        sync_row.addStretch(1)
        card.body_layout.addLayout(sync_row)

        self.sync_status_lbl = QLabel("", card)
        self.sync_status_lbl.setObjectName("ViewSubtitle")
        self.sync_status_lbl.setWordWrap(True)
        card.body_layout.addWidget(self.sync_status_lbl)

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

        oauth_row = QHBoxLayout()
        self.google_status_lbl = QLabel("Google: Desconectado", card)
        self.google_status_lbl.setObjectName("OnboardingGoogleStatus")
        oauth_row.addWidget(self.google_status_lbl)
        oauth_row.addStretch(1)

        self.portal_btn = QPushButton("Abrir Portal Web", card)
        self.portal_btn.setObjectName("OnboardingPortalBtn")
        self.portal_btn.clicked.connect(self._on_open_portal_clicked)
        oauth_row.addWidget(self.portal_btn)

        self.google_oauth_btn = QPushButton("Iniciar sesión con Google", card)
        self.google_oauth_btn.setObjectName("OnboardingGoogleBtn")
        self.google_oauth_btn.setProperty("variant", "primary")
        self.google_oauth_btn.clicked.connect(self._on_google_oauth_clicked)
        oauth_row.addWidget(self.google_oauth_btn)
        card.body_layout.addLayout(oauth_row)

        self.oauth_feedback_lbl = QLabel("", card)
        self.oauth_feedback_lbl.setObjectName("ViewSubtitle")
        self.oauth_feedback_lbl.setWordWrap(True)
        card.body_layout.addWidget(self.oauth_feedback_lbl)

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
            elif self._step == 2:
                self._refresh_oauth_status()
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

    def _refresh_oauth_status(self) -> None:
        info = self._app.get_google_auth_status()
        if info.connected:
            label = f"Google: Conectado ({info.email})" if info.email else "Google: Conectado"
            self.google_status_lbl.setText(label)
            self.google_oauth_btn.setText("Reconectar Google")
        elif info.client_configured:
            self.google_status_lbl.setText("Google: Desconectado")
            self.google_oauth_btn.setText("Iniciar sesión con Google")
        else:
            self.google_status_lbl.setText("Google: Sin Client ID")
            self.google_oauth_btn.setText("Iniciar sesión con Google")
        self.google_oauth_btn.setEnabled(not self._google_busy)

    def _on_setup_sync_clicked(self) -> None:
        if self._closing:
            return
        self.setup_sync_btn.setEnabled(False)
        self.sync_status_lbl.setText("Abriendo guías y activando Setup Sync en 127.0.0.1...")

        def _done_cb(fut: concurrent.futures.Future[str]) -> None:
            if self._closing:
                return
            try:
                fut.result()
                port = self._app.oauth_server.port
                self._setup_sync_finished.emit(
                    True,
                    f"Setup Sync listo en 127.0.0.1:{port}. Envía tu clave desde el navegador.",
                )
            except Exception as exc:
                self._setup_sync_finished.emit(False, str(exc))

        try:
            fut = self._app.open_setup_sync_portal(page="guides.html")
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._setup_sync_finished.emit(False, str(exc))

    def _on_setup_sync_finished(self, _ok: bool, message: str) -> None:
        self.setup_sync_btn.setEnabled(True)
        self.sync_status_lbl.setText(message)

    def _on_open_portal_clicked(self) -> None:
        url = self._app.open_web_portal(page="index.html")
        self.oauth_feedback_lbl.setText(f"Portal abierto en el navegador: {url}")

    def _on_google_oauth_clicked(self) -> None:
        if self._google_busy or self._closing:
            return
        if not self._app.google_auth.is_client_configured():
            self.oauth_feedback_lbl.setText(
                "Configura GOOGLE_OAUTH_CLIENT_ID en Setup Sync o .env para conectar Google."
            )
            return
        self._google_busy = True
        self.google_oauth_btn.setEnabled(False)
        self.google_oauth_btn.setText("Esperando navegador...")
        self.oauth_feedback_lbl.setText("Abriendo navegador para iniciar sesión con Google...")

        def _done_cb(fut: concurrent.futures.Future[GoogleAuthSessionInfo]) -> None:
            if self._closing:
                return
            try:
                info = fut.result()
                account = info.email or info.name or "Google"
                self._oauth_finished.emit(True, f"Cuenta conectada: {account}.")
            except Exception as exc:
                self._oauth_finished.emit(False, str(exc))

        try:
            fut = self._app.start_google_oauth_flow()
            fut.add_done_callback(_done_cb)
        except RuntimeError as exc:
            self._oauth_finished.emit(False, str(exc))

    def _on_oauth_finished(self, _ok: bool, message: str) -> None:
        self._google_busy = False
        self.oauth_feedback_lbl.setText(message)
        self._refresh_oauth_status()

    def done(self, r: int) -> None:
        self._closing = True
        super().done(r)
