from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from deusex.app import DeusExApplication
from deusex.core.contracts import ModuleState
from deusex.core.events import OrbStateChangedEvent, SubtitleUpdatedEvent
from deusex.core.paths import resolve_app_paths
from deusex.core.runtime import ComStaThread
from deusex.modules.voice.ducking import SelectiveAudioDucker
from deusex.modules.voice.stt import is_vosk_model_present, list_input_devices
from deusex.ui.bridge_qt import QtEventBridge
from deusex.ui.orb import FloatingOrbWindow, SubtitleHolobox


@pytest.mark.live
def test_slice4_live_vosk_model_microphones_and_pycaw_sta() -> None:
    """Verifica runtimes reales de Windows 11: Vosk ES en disco, microfono y pycaw en ComStaThread."""
    real_paths = resolve_app_paths()
    assert is_vosk_model_present(real_paths.vosk_es_dir), (
        "Modelo Vosk ES ausente en USER_DATA_DIR/models/vosk-es. "
        "Ejecuta `uv run python scripts/setup_runtimes.py`."
    )

    import vosk

    vosk.SetLogLevel(-1)
    model = vosk.Model(str(real_paths.vosk_es_dir))
    rec = vosk.KaldiRecognizer(model, 16_000.0)
    silent_pcm = np.zeros(3_200, dtype=np.int16).tobytes()
    rec.AcceptWaveform(silent_pcm)
    parsed = json.loads(str(rec.FinalResult()))
    assert isinstance(parsed, dict)
    assert "text" in parsed

    mics = list_input_devices()
    assert len(mics) >= 1, "No se detectaron microfonos de entrada en Windows 11."

    com_sta = ComStaThread(name="LiveComStaSlice4")
    com_sta.start()
    try:
        assert com_sta.com_initialized is True
        ducker = SelectiveAudioDucker(com_sta, default_level_pct=25)
        snap = ducker.inspect_sessions_sync()
        assert isinstance(snap.is_multimedia_active, bool)
        assert isinstance(snap.is_call_active, bool)
        assert ducker.restore_sync() >= 0
    finally:
        com_sta.stop()


@pytest.mark.live
def test_slice4_live_reconciliation_edge_tts_orb_subtitles_and_shutdown(
    qapp: Any,
    qtbot: Any,
    tmp_path: Path,
) -> None:
    """
    Smoke test EN VIVO de 4 hilos en Windows 11:
    1. Activa `voice`, reinicia y confirma reconciliacion automatica (Antidoto #2).
    2. Captura real en AudioInputThread + sintesis real Edge-TTS (o SAPI fallback) + ducking.
    3. Sincroniza FloatingOrbWindow y SubtitleHolobox via EventBus -> QtEventBridge.
    4. Confirma restauracion de ducking y cierre determinista en <3 s.
    """
    # Arranque 1: activar modulo voice y persistir en settings.json aislado
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=True,
    )
    app1.start()
    try:
        assert app1.set_module_enabled("voice", True).result(timeout=5.0) is True
        assert app1.voice_module.state in (ModuleState.ACTIVE, ModuleState.DEGRADED)
    finally:
        elapsed_1 = app1.shutdown()
        assert elapsed_1 < 3.0

    # Arranque 2: reconciliacion automatica + prueba real de TTS, Orbe y Subtitulos
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=True,
    )
    reconciled = app2.start()
    bridge = QtEventBridge(app2.events)
    orb = FloatingOrbWindow(
        style=app2.settings.ui.orb_style,
        accent_hex="#38BDF8",
        visibility_mode="speaking_only",
        corner="bottom_right",
    )
    holobox = SubtitleHolobox(accent_hex="#38BDF8")

    orb_states: list[str] = []
    subtitle_texts: list[str] = []

    def _on_orb_state(ev: object) -> None:
        if isinstance(ev, OrbStateChangedEvent):
            orb_states.append(ev.state)
            orb.apply_orb_state(
                ev.state,
                visible=ev.visible,
                accent_hex=ev.accent_hex,
                preview_sec=ev.preview_sec,
            )

    def _on_subtitle(ev: object) -> None:
        if isinstance(ev, SubtitleUpdatedEvent):
            if ev.text:
                subtitle_texts.append(ev.text)
            holobox.update_subtitle(
                ev.text,
                word=ev.word,
                is_final=ev.is_final,
                clear=ev.clear,
            )

    bridge.orb_state_changed.connect(_on_orb_state)
    bridge.audio_level_changed.connect(lambda _src, lvl: orb.set_audio_level(lvl))
    bridge.subtitle_updated.connect(_on_subtitle)

    try:
        assert "voice" in reconciled
        assert app2.voice_module.state in (ModuleState.ACTIVE, ModuleState.DEGRADED)

        # Lanzar sintesis real en AsyncCoreThread mientras QtMainThread bombea QtMultimedia
        speak_fut = app2.runtime.submit_async(
            app2.voice_module.speak_text("Prueba en vivo de voz y orbe lista.")
        )
        qtbot.waitUntil(speak_fut.done, timeout=20_000)
        tts_res = speak_fut.result(timeout=1.0)
        assert tts_res.ok is True
        assert tts_res.engine in ("edge-tts", "sapi")
        assert "speaking" in orb_states
        assert len(subtitle_texts) >= 1

        # Verificar que el ducking se haya restaurado por completo al terminar
        assert app2.voice_module.ducker is not None
        assert app2.voice_module.ducker.is_ducked is False

        # Verificar que el Orbe vuelve a ocultarse al finalizar en modo speaking_only
        qtbot.waitUntil(lambda: not orb.isVisible(), timeout=3_000)
    finally:
        orb.prepare_for_exit()
        orb.close()
        orb.deleteLater()
        holobox.prepare_for_exit()
        holobox.close()
        holobox.deleteLater()
        bridge.detach()
        qapp.processEvents()
        elapsed_2 = app2.shutdown()
        assert elapsed_2 < 3.0
