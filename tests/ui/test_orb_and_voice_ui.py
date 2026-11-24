from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
import pytest

from deusex.app import DeusExApplication
from deusex.core.events import MicLevelEvent, OrbStateChangedEvent, VoiceCalibrationEvent
from deusex.modules.voice import stt as stt_mod
from deusex.modules.voice.stt import AudioInputDevice
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.main_window.settings_view import SettingsView
from deusex.ui.orb import (
    MAX_SUBTITLE_CHARS,
    MAX_SUBTITLE_WORDS,
    ORB_STYLES,
    FloatingOrbWindow,
    SubtitleHolobox,
    active_subtitle_block,
    chunk_subtitle_blocks,
)


@pytest.mark.ui
def test_floating_orb_click_through_5_styles_preview_and_privacy(qapp: Any, qtbot: Any) -> None:
    orb = FloatingOrbWindow(
        style="reactor",
        accent_hex="#38BDF8",
        visibility_mode="speaking_only",
        corner="bottom_right",
    )
    try:
        # Click-through y ventana siempre encima
        flags = orb.windowFlags()
        assert bool(flags & Qt.WindowType.FramelessWindowHint)
        assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
        assert bool(flags & Qt.WindowType.WindowTransparentForInput)

        # Oculto por defecto en IDLE (speaking_only)
        assert orb.isVisible() is False

        # Vista previa fija activa el Orbe y permite renderizar los 5 estilos
        orb.show_preview(duration_sec=1.0)
        qapp.processEvents()
        assert orb.isVisible() is True
        assert orb.is_preview_active is True

        assert len(ORB_STYLES) == 7
        assert "sacred_geometry" in ORB_STYLES
        assert "jarvis_core" in ORB_STYLES

        for style in ORB_STYLES:
            orb.set_style(style)
            orb.set_audio_level(0.75)
            pix = orb.grab()
            assert not pix.isNull()
            assert orb.style_id == style

        # Modo Privacidad oculta el Orbe inmediatamente y bloquea nuevas vistas previas
        orb.set_privacy_mode(True)
        qapp.processEvents()
        assert orb.isVisible() is False
        assert orb.current_state == "privacy"

        orb.show_preview(duration_sec=2.0)
        qapp.processEvents()
        assert orb.isVisible() is False

        # Al salir de privacidad y pasar a listening, el Orbe vuelve a mostrarse
        orb.set_privacy_mode(False)
        orb.apply_orb_state("listening", visible=True, accent_hex="#22C55E")
        qtbot.waitUntil(orb.isVisible, timeout=1_500)
        assert orb.current_state == "listening"
    finally:
        orb.prepare_for_exit()
        orb.close()
        orb.deleteLater()
        qapp.processEvents()


@pytest.mark.ui
def test_subtitle_holobox_word_and_char_limits_and_privacy(qapp: Any, qtbot: Any) -> None:
    long_text = (
        "Esta es una frase deliberadamente extensa para comprobar que el holobox de subtitulos "
        "divide el contenido en bloques compactos sincronizados palabra por palabra sin desbordarse nunca"
    )
    blocks = chunk_subtitle_blocks(long_text)
    assert len(blocks) >= 2
    for block in blocks:
        assert len(block.split()) <= MAX_SUBTITLE_WORDS
        assert len(block) <= MAX_SUBTITLE_CHARS

    active = active_subtitle_block(long_text)
    assert active == blocks[-1]

    holobox = SubtitleHolobox(accent_hex="#38BDF8", auto_hide_ms=1_500)
    try:
        assert holobox.isVisible() is False
        holobox.update_subtitle(long_text, word="nunca", is_final=False)
        qtbot.waitUntil(holobox.isVisible, timeout=1_500)
        assert len(holobox.current_text.split()) <= MAX_SUBTITLE_WORDS
        assert len(holobox.current_text) <= MAX_SUBTITLE_CHARS

        pix = holobox.grab()
        assert not pix.isNull()

        # Privacidad limpia y oculta de inmediato
        holobox.set_privacy_mode(True)
        qapp.processEvents()
        assert holobox.isVisible() is False
        assert holobox.current_text == ""
    finally:
        holobox.prepare_for_exit()
        holobox.close()
        holobox.deleteLater()
        qapp.processEvents()


@pytest.mark.ui
def test_settings_view_voice_orb_controls_and_live_signals(
    qapp: Any,
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mics = [
        AudioInputDevice(index=2, name="Micrófono USB Prueba", channels=1, default_sample_rate=16_000.0, is_default=True),
    ]
    monkeypatch.setattr(stt_mod, "list_input_devices", lambda: list(fake_mics))
    monkeypatch.setattr("deusex.modules.voice.module.list_input_devices", lambda: list(fake_mics))

    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()
    bridge = QtEventBridge(app.events)
    view = SettingsView(app, bridge)
    preview_events: list[OrbStateChangedEvent] = []
    bridge.orb_state_changed.connect(
        lambda ev: preview_events.append(ev) if isinstance(ev, OrbStateChangedEvent) else None
    )
    try:
        # 1. Cambios de estilo de Orbe, TTS, Ducking y Modo En Llamada se persisten
        waves_idx = view.orb_style_combo.findData("waves")
        assert waves_idx >= 0
        view.orb_style_combo.setCurrentIndex(waves_idx)
        assert app.settings.ui.orb_style == "waves"

        view.tts_mute_switch.setChecked(True)
        assert app.settings.voice.tts_muted is True

        view.tts_rate_spin.setValue(1.3)
        assert abs(app.settings.voice.tts_rate - 1.3) < 0.05

        view.ducking_spin.setValue(30)
        assert app.settings.voice.ducking_level_pct == 30

        view.in_call_switch.setChecked(True)
        assert app.settings.voice.in_call_text_only is True

        view.mic_mute_switch.setChecked(True)
        assert app.settings.voice.mic_muted is True
        app.set_mic_muted(False)
        qtbot.waitUntil(lambda: not view.mic_mute_switch.isChecked(), timeout=1_500)

        # 2. Botón "Vista previa 4 s" emite OrbStateChangedEvent(state="preview", preview_sec=4.0)
        view.orb_preview_btn.click()
        qtbot.waitUntil(lambda: len(preview_events) >= 1, timeout=1_500)
        assert preview_events[-1].state == "preview"
        assert preview_events[-1].visible is True
        assert abs(preview_events[-1].preview_sec - 4.0) < 0.01

        # 3. Señales en vivo del medidor de micrófono y progreso de calibración vocal
        app.events.publish(MicLevelEvent(level=0.72, is_speech=True, device_index=2))
        qtbot.waitUntil(lambda: view.mic_level_bar.value() == 72, timeout=1_500)
        assert "voz" in view.mic_vad_pill.text().lower()

        app.events.publish(
            VoiceCalibrationEvent(status="progress", progress=0.5, message="Calibrando 50%")
        )
        qtbot.waitUntil(lambda: "50%" in view.voiceprint_status_lbl.text(), timeout=1_500)
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        qapp.processEvents()
        elapsed = app.shutdown()
        assert elapsed < 3.0


@pytest.mark.ui
def test_chat_view_mic_mute_and_in_call_pill(
    qapp: Any,
    qtbot: Any,
    tmp_path: Path,
) -> None:
    from deusex.ui.main_window.chat_view import ChatView

    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()
    bridge = QtEventBridge(app.events)
    view = ChatView(app, bridge)
    view.show()
    qapp.processEvents()
    try:
        # 1. 1-Click Mic Mute en el header de ChatView
        assert "activo" in view.mic_mute_btn.text().lower()
        view.mic_mute_btn.click()
        qtbot.waitUntil(lambda: "silenciado" in view.mic_mute_btn.text().lower(), timeout=1_500)
        assert app.settings.voice.mic_muted is True

        view.mic_mute_btn.click()
        qtbot.waitUntil(lambda: "activo" in view.mic_mute_btn.text().lower(), timeout=1_500)
        assert app.settings.voice.mic_muted is False

        # 2. Status pill "En llamada" visible/oculto ante InCallStateChangedEvent
        assert not view.in_call_pill.isVisible()
        bridge.in_call_state_changed.emit(True, ("Discord.exe",))
        qtbot.waitUntil(lambda: view.in_call_pill.isVisible(), timeout=1_500)
        assert "Discord.exe" in view.in_call_pill.toolTip()

        bridge.in_call_state_changed.emit(False, ())
        qtbot.waitUntil(lambda: not view.in_call_pill.isVisible(), timeout=1_500)
    finally:
        view.close()
        view.deleteLater()
        bridge.detach()
        qapp.processEvents()
        app.shutdown()

