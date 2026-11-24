from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import threading
import time

import numpy as np
import numpy.typing as npt
import pytest

from deusex.app import DeusExApplication
from deusex.core.config import DeusExSettings, SecretsManager
from deusex.core.contracts import ModuleState
from deusex.core.events import EventBus, MicLevelEvent
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import AudioInputThread, ComStaThread
from deusex.modules.voice.context_gate import CONTEXTUAL_SILENCE_TOKEN, ContextGate
from deusex.modules.voice.dsp import DEFAULT_SAMPLE_RATE, FarFieldAudioConditioner
from deusex.modules.voice.ducking import AudioSessionInfo, SelectiveAudioDucker
from deusex.modules.voice.stt import (
    VoiceInputEngine,
    VoiceUtterance,
    extract_wake_word,
    parse_hotkey_vks,
)
from deusex.modules.voice.tts import (
    SynthesizedSpeech,
    VoiceTTSEngine,
    WordTiming,
    format_edge_rate,
    sanitize_text_for_speech,
)
from deusex.modules.voice.voiceprint import VOICEPRINT_DIMENSIONS, OwnerVoiceprintGuard


def _make_sine(
    freq_hz: float,
    *,
    duration_sec: float = 0.25,
    amplitude: float = 0.25,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> npt.NDArray[np.float32]:
    t = np.arange(round(duration_sec * sample_rate), dtype=np.float64) / float(sample_rate)
    wave = amplitude * np.sin(2.0 * np.pi * freq_hz * t)
    return np.asarray(wave, dtype=np.float32)


def _make_harmonic_voice(
    f0_hz: float,
    *,
    duration_sec: float = 2.0,
    amplitude: float = 0.22,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> npt.NDArray[np.float32]:
    t = np.arange(round(duration_sec * sample_rate), dtype=np.float64) / float(sample_rate)
    mod = 0.75 + 0.25 * np.sin(2.0 * np.pi * 3.5 * t)
    sig = (
        0.55 * np.sin(2.0 * np.pi * f0_hz * t)
        + 0.30 * np.sin(2.0 * np.pi * (f0_hz * 2.0) * t + 0.4)
        + 0.15 * np.sin(2.0 * np.pi * (f0_hz * 3.0) * t + 0.9)
    )
    return np.asarray(amplitude * mod * sig, dtype=np.float32)


class _FakeAudioSessionBackend:
    def __init__(self, sessions: list[AudioSessionInfo]) -> None:
        self._sessions = list(sessions)
        self.volume_history: list[tuple[str, float]] = []

    def list_sessions(self) -> list[AudioSessionInfo]:
        return list(self._sessions)

    def set_session_volume(self, session_key: str, volume: float) -> bool:
        self.volume_history.append((session_key, volume))
        for idx, sess in enumerate(self._sessions):
            if sess.session_key == session_key:
                self._sessions[idx] = AudioSessionInfo(
                    session_key=sess.session_key,
                    process_name=sess.process_name,
                    display_name=sess.display_name,
                    pid=sess.pid,
                    volume=volume,
                    muted=sess.muted,
                    is_active=sess.is_active,
                    peak=sess.peak,
                )
                return True
        return False

    def get_volume(self, session_key: str) -> float:
        for sess in self._sessions:
            if sess.session_key == session_key:
                return sess.volume
        raise KeyError(session_key)


class _FakeRecognizer:
    def __init__(self, partials: list[str], finals: list[str] | None = None) -> None:
        self._partials = list(partials)
        self._finals = list(finals) if finals is not None else []
        self._last_result = ""
        self._last_partial = ""
        self.reset_calls = 0

    def accept_waveform(self, _pcm16: bytes) -> bool:
        if self._finals:
            text = self._finals.pop(0)
            if text:
                self._last_result = text
                return True
        if self._partials:
            self._last_partial = self._partials.pop(0)
        else:
            self._last_partial = ""
        return False

    def result_text(self) -> str:
        val = self._last_result
        self._last_result = ""
        return val

    def partial_text(self) -> str:
        return self._last_partial

    def final_result_text(self) -> str:
        return self._last_partial

    def reset(self) -> None:
        self.reset_calls += 1
        self._last_result = ""
        self._last_partial = ""


class _FakeKeyPoller:
    def __init__(self) -> None:
        self.pressed = False

    def is_combo_pressed(self, _combo: str) -> bool:
        return self.pressed


class _FakeEdgeSynthesizer:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, str]] = []

    async def synthesize(self, text: str, *, voice: str, rate_str: str) -> SynthesizedSpeech:
        self.calls.append((text, voice, rate_str))
        if self.should_fail:
            raise RuntimeError("Simulated Edge-TTS network error")
        words = text.split()
        timings = tuple(
            WordTiming(offset_ms=float(idx * 15), duration_ms=25.0, text=w)
            for idx, w in enumerate(words)
        )
        return SynthesizedSpeech(audio_bytes=b"ID3FAKEAUDIO", word_timings=timings, voice=voice)


class _FakePlaybackBackend:
    def __init__(self, *, delay_sec: float = 0.04, succeed: bool = True) -> None:
        self._delay_sec = delay_sec
        self._succeed = succeed
        self._timer: threading.Timer | None = None
        self.played_files: list[Path] = []
        self.stop_calls = 0

    def play_file(self, file_path: Path, *, on_finished: Callable[[bool], None]) -> bool:
        self.played_files.append(file_path)
        self._timer = threading.Timer(self._delay_sec, lambda: on_finished(self._succeed))
        self._timer.daemon = True
        self._timer.start()
        return True

    def stop(self) -> None:
        self.stop_calls += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class _FakeSapiBackend:
    def __init__(self) -> None:
        self.spoken_texts: list[str] = []
        self.purge_calls = 0
        self._remaining_polls = 0

    def speak_async_start(self, text: str, *, rate: float) -> bool:
        self.spoken_texts.append(f"{text}@{rate:.1f}")
        self._remaining_polls = 1
        return True

    def is_speaking(self) -> bool:
        if self._remaining_polls > 0:
            self._remaining_polls -= 1
            return True
        return False

    def purge(self) -> None:
        self.purge_calls += 1
        self._remaining_polls = 0


def test_dsp_highpass_agc_and_vocal_band_vad() -> None:
    cond = FarFieldAudioConditioner(sample_rate=DEFAULT_SAMPLE_RATE)

    # 1. Zumbido sub-85 Hz (50 Hz) debe atenuarse y no disparar VAD vocal
    hum_50hz = _make_sine(50.0, duration_sec=0.35, amplitude=0.30)
    hum_frame = cond.process(hum_50hz)
    assert hum_frame.vocal_band_ratio < 0.25
    assert hum_frame.is_speech is False

    # 2. Señal en banda vocal (650 Hz + 1300 Hz) debe disparar VAD y respetar límites AGC np.tanh
    cond.reset()
    speech_sig = _make_harmonic_voice(420.0, duration_sec=0.25, amplitude=0.22)
    speech_frame = cond.process(speech_sig)
    assert speech_frame.vocal_band_ratio > 0.70
    assert speech_frame.is_speech is True
    assert 0.0 < speech_frame.normalized_level <= 1.0
    assert float(np.max(np.abs(speech_frame.samples_f32))) <= 1.0
    assert len(speech_frame.pcm16_bytes) == speech_sig.size * 2

    # 3. Sensibilidad elevada cuando hay multimedia activa exige señal más fuerte
    quiet_voice = _make_harmonic_voice(420.0, duration_sec=0.25, amplitude=0.045)
    cond.reset()
    normal_frame = cond.process(quiet_voice, sensitivity_multiplier=1.0)
    cond.reset()
    media_frame = cond.process(quiet_voice, sensitivity_multiplier=2.2)
    assert normal_frame.is_speech is True
    assert media_frame.is_speech is False


def test_voiceprint_26d_calibration_persistence_and_verification(tmp_path: Path) -> None:
    guard = OwnerVoiceprintGuard(
        tmp_path,
        sample_rate=DEFAULT_SAMPLE_RATE,
        similarity_threshold=0.80,
        calibration_duration_sec=2.0,
    )
    assert guard.is_enrolled is False

    owner_audio = _make_harmonic_voice(185.0, duration_sec=2.2, amplitude=0.24)
    emb = guard.extract_embedding(owner_audio)
    assert emb is not None
    assert emb.shape == (VOICEPRINT_DIMENSIONS,)
    assert abs(float(np.linalg.norm(emb)) - 1.0) < 1e-5

    # Flujo guiado de calibración
    guard.start_calibration(duration_sec=2.0)
    assert guard.is_calibrating is True
    chunk_size = 3_200
    ready = False
    for start in range(0, owner_audio.size, chunk_size):
        _prog, ready = guard.feed_calibration_chunk(owner_audio[start : start + chunk_size])
    assert ready is True
    assert guard.finalize_calibration() is True
    assert guard.is_enrolled is True
    assert guard.storage_file.is_file()

    # Persistencia en disco al recrear instancia
    reloaded = OwnerVoiceprintGuard(
        tmp_path,
        sample_rate=DEFAULT_SAMPLE_RATE,
        similarity_threshold=0.80,
    )
    assert reloaded.is_enrolled is True

    # Misma firma vocal -> aceptada
    same_owner = _make_harmonic_voice(185.0, duration_sec=1.5, amplitude=0.14)
    ok_res = reloaded.verify(same_owner, enabled=True)
    assert ok_res.accepted is True
    assert ok_res.similarity >= 0.80

    # Voz claramente distinta (alta frecuencia) -> rechazada
    other_voice = _make_harmonic_voice(980.0, duration_sec=1.5, amplitude=0.20)
    reject_res = reloaded.verify(other_voice, enabled=True)
    assert reject_res.accepted is False
    assert reject_res.reason == "voiceprint_mismatch"


async def test_selective_audio_ducker_excludes_calls_and_restores_cleanly() -> None:
    backend = _FakeAudioSessionBackend(
        [
            AudioSessionInfo("100:spotify.exe:0", "Spotify.exe", "Spotify", 100, 0.80, False, True, 0.25),
            AudioSessionInfo("200:discord.exe:1", "Discord.exe", "Discord", 200, 0.90, False, True, 0.18),
            AudioSessionInfo("300:teams.exe:2", "ms-teams.exe", "Microsoft Teams", 300, 0.85, False, True, 0.0),
            AudioSessionInfo("999:deusex.exe:3", "python.exe", "DeusEx", 999, 1.00, False, True, 0.30),
        ]
    )
    com_sta = ComStaThread(name="TestComStaDucker")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(
            com_sta,
            default_level_pct=25,
            backend=backend,
            own_pid=999,
            smooth_ramp=False,
        )

        snap = await ducker.inspect_sessions()
        assert snap.is_multimedia_active is True
        assert snap.is_call_active is True
        assert "Spotify.exe" in snap.active_media_apps
        assert "Discord.exe" in snap.active_call_apps

        ducked_count = await ducker.duck(level_pct=25)
        assert ducked_count == 1
        assert ducker.is_ducked is True
        assert abs(backend.get_volume("100:spotify.exe:0") - 0.20) < 1e-4
        # Llamadas y proceso propio nunca se atenúan
        assert abs(backend.get_volume("200:discord.exe:1") - 0.90) < 1e-4
        assert abs(backend.get_volume("300:teams.exe:2") - 0.85) < 1e-4
        assert abs(backend.get_volume("999:deusex.exe:3") - 1.00) < 1e-4

        # Restauración garantizada en stop()
        ducker.stop()
        assert ducker.is_ducked is False
        assert abs(backend.get_volume("100:spotify.exe:0") - 0.80) < 1e-4
    finally:
        com_sta.stop()


def test_context_gate_multimedia_in_call_privacy_and_contextual_silence(tmp_path: Path) -> None:
    settings = DeusExSettings()
    backend = _FakeAudioSessionBackend(
        [
            AudioSessionInfo("100:spotify.exe:0", "Spotify.exe", "Spotify", 100, 0.70, False, True, 0.30),
        ]
    )
    com_sta = ComStaThread(name="TestComStaGate")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=backend, own_pid=999, smooth_ramp=False)
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)

        assert gate.is_multimedia_active(force_refresh=True) is True
        assert gate.dynamic_vad_multiplier() > 1.0
        assert gate.is_contextual_silence_response(f"ok {CONTEXTUAL_SILENCE_TOKEN}") is True

        # Ventana de seguimiento continuo y filtro de muletillas/tercera persona
        assert gate.open_follow_up_window(duration_sec=6.0) is True
        assert gate.is_follow_up_active() is True
        filler_dec = gate.evaluate_utterance("eh", triggered_by="follow_up", vocal_band_ratio=0.8)
        assert filler_dec.allow is False
        assert filler_dec.reason == "noise_or_third_person"

        third_person_dec = gate.evaluate_utterance(
            "mira lo que te digo que paso ayer",
            triggered_by="follow_up",
            vocal_band_ratio=0.8,
        )
        assert third_person_dec.allow is False

        valid_follow = gate.evaluate_utterance(
            "pon un temporizador de cinco minutos",
            triggered_by="follow_up",
            vocal_band_ratio=0.85,
        )
        assert valid_follow.allow is True
        assert valid_follow.text_only is False

        # Modo En Llamada: bloquea seguimiento continuo y fuerza solo texto
        settings.voice.in_call_text_only = True
        assert gate.open_follow_up_window(duration_sec=6.0) is False
        call_follow = gate.evaluate_utterance(
            "abre el navegador",
            triggered_by="follow_up",
            vocal_band_ratio=0.85,
        )
        assert call_follow.allow is False
        assert call_follow.reason == "follow_up_blocked_in_call"

        call_wake = gate.evaluate_utterance(
            "abre el navegador",
            triggered_by="wake_word",
            vocal_band_ratio=0.85,
        )
        assert call_wake.allow is True
        assert call_wake.text_only is True

        # Modo Privacidad bloquea completamente la entrada de voz
        settings.general.privacy_mode = True
        priv_dec = gate.evaluate_utterance(
            "qué hora es",
            triggered_by="wake_word",
            vocal_band_ratio=0.9,
        )
        assert priv_dec.allow is False
        assert priv_dec.reason == "privacy_mode"
    finally:
        com_sta.stop()


def test_stt_wake_word_ptt_and_barge_in_pipeline(tmp_path: Path) -> None:
    wake, rem = extract_wake_word("Oye Rafael pon música tranquila", ("raphael", "rafael"))
    assert wake == "rafael"
    assert rem == "pon musica tranquila"
    assert parse_hotkey_vks("Ctrl+Alt+V") == (0x11, 0x12, 0x56)

    paths = resolve_app_paths(user_data_override=tmp_path)
    settings = DeusExSettings()
    com_sta = ComStaThread(name="TestComStaSTT")
    audio_thread = AudioInputThread(name="TestAudioSTT")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=_FakeAudioSessionBackend([]), smooth_ramp=False)
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)
        cond = FarFieldAudioConditioner()

        utterances: list[VoiceUtterance] = []
        wake_hits: list[tuple[str, str]] = []
        barge_ins: list[bool] = []
        key_poller = _FakeKeyPoller()

        stt = VoiceInputEngine(
            paths=paths,
            settings=settings,
            audio_thread=audio_thread,
            conditioner=cond,
            voiceprint=vp,
            context_gate=gate,
            on_utterance=utterances.append,
            on_wake_word=lambda w, trig: wake_hits.append((w, trig)),
            on_barge_in=lambda: barge_ins.append(True),
            is_tts_speaking=lambda: True,
            key_poller=key_poller,
        )
        stt.set_wake_words(("raphael",))

        speech = _make_harmonic_voice(380.0, duration_sec=0.1, amplitude=0.22)
        silence = np.zeros(1_600, dtype=np.float32)

        # 1. Wake-word + barge-in + commit por silencio
        rec = _FakeRecognizer(partials=["raphael dime la hora", ""], finals=[])
        stt.process_pcm_frame(speech, recognizer=rec, now=10.0)
        assert wake_hits == [("raphael", "wake_word")]
        assert barge_ins == [True]

        stt.process_pcm_frame(silence, recognizer=rec, now=11.5)
        assert len(utterances) == 1
        assert utterances[0].triggered_by == "wake_word"
        assert utterances[0].transcript == "dime la hora"

        # 2. Push-to-Talk vía GetAsyncKeyState poller
        rec_ptt = _FakeRecognizer(partials=["pon volumen al 40"], finals=[])
        key_poller.pressed = True
        stt.process_pcm_frame(speech, recognizer=rec_ptt, now=20.0)
        key_poller.pressed = False
        stt.process_pcm_frame(silence, recognizer=rec_ptt, now=20.4)
        assert len(utterances) == 2
        assert utterances[1].triggered_by == "ptt"
        assert utterances[1].transcript == "pon volumen al 40"
    finally:
        com_sta.stop()


async def test_tts_edge_word_boundary_sapi_fallback_and_mute(tmp_path: Path) -> None:
    assert sanitize_text_for_speech("**Hola** visita https://example.com ahora") == "Hola visita enlace web ahora"
    assert format_edge_rate(1.25) == "+25%"
    assert format_edge_rate(0.8) == "-20%"

    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    settings = DeusExSettings()
    backend = _FakeAudioSessionBackend(
        [AudioSessionInfo("100:spotify.exe:0", "Spotify.exe", "Spotify", 100, 0.80, False, True, 0.2)]
    )
    com_sta = ComStaThread(name="TestComStaTTS")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, default_level_pct=25, backend=backend, smooth_ramp=False)
        subtitles: list[tuple[str, str, bool, bool]] = []
        levels: list[float] = []

        # 1. Edge-TTS principal con WordBoundary y ducking restaurado
        edge = _FakeEdgeSynthesizer(should_fail=False)
        player = _FakePlaybackBackend(delay_sec=0.05, succeed=True)
        sapi = _FakeSapiBackend()

        tts = VoiceTTSEngine(
            paths=paths,
            settings=settings,
            com_sta=com_sta,
            ducker=ducker,
            on_subtitle_word=lambda t, w, f, c: subtitles.append((t, w, f, c)),
            on_tts_level=levels.append,
            edge_synthesizer=edge,
            playback_backend=player,
            sapi_backend=sapi,
        )

        res = await tts.speak("Hola mundo listo", profile_id="raphael")
        assert res.ok is True
        assert res.engine == "edge-tts"
        assert ducker.is_ducked is False
        assert abs(backend.get_volume("100:spotify.exe:0") - 0.80) < 1e-4
        assert any(word == "Hola" for _txt, word, _fin, _clr in subtitles)
        assert levels[-1] == 0.0

        # 2. Fallback automático a SAPI en ComStaThread cuando Edge-TTS falla
        edge.should_fail = True
        res_sapi = await tts.speak("Respuesta sin red", profile_id="raphael")
        assert res_sapi.ok is True
        assert res_sapi.engine == "sapi"
        assert len(sapi.spoken_texts) == 1
        assert ducker.is_ducked is False

        # 3. Silencio 1-clic omite síntesis
        settings.voice.tts_muted = True
        res_muted = await tts.speak("No debería sonar")
        assert res_muted.ok is True
        assert res_muted.engine == "muted"
    finally:
        com_sta.stop()


def test_voice_module_antidote2_reconciliation_permissions_and_fast_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Crear marcador mínimo de modelo Vosk ES en USER_DATA_DIR aislado y evitar hardware real de mic
    vosk_am = tmp_path / "models" / "vosk-es" / "am"
    vosk_am.mkdir(parents=True, exist_ok=True)
    (vosk_am / "final.mdl").write_bytes(b"fake-vosk-model")
    monkeypatch.setattr(VoiceInputEngine, "start", lambda self: True)

    # Arranque 1: Zero-Base (voice desactivado por defecto)
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled_1 = app1.start()
    assert "voice" not in reconciled_1
    assert bool(app1.voice_module.state == ModuleState.DISABLED)

    # Activar voice en configuración persistida y cerrar limpiamente
    assert app1.set_module_enabled("voice", True).result(timeout=5.0) is True
    assert app1.voice_module.state == ModuleState.ACTIVE
    elapsed_1 = app1.shutdown()
    assert elapsed_1 < 3.0

    # Arranque 2 (Antídoto #2): voice y sus FastPathRules deben reconciliarse automáticamente
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    reconciled_2 = app2.start()
    try:
        assert "voice" in reconciled_2
        assert app2.voice_module.state == ModuleState.ACTIVE

        # Verificar Antídoto #3: permisos tipados exclusivos (cero strings mágicos con punto)
        tool_perms = {t.permission for t in app2.voice_module.get_tools()}
        assert tool_perms <= {Permission.VOICE_LISTEN, Permission.VOICE_SPEAK}
        rule_perms = {r.permission for r in app2.voice_module.get_fast_path_rules()}
        assert rule_perms <= {Permission.VOICE_LISTEN, Permission.VOICE_SPEAK}

        # Verificar Fast-Path locales (<50 ms, 0 tokens)
        t0 = time.perf_counter()
        turn_mute = app2.submit_user_message("silencia la voz").result(timeout=5.0)
        assert (time.perf_counter() - t0) < 1.0
        assert app2.settings.voice.tts_muted is True
        assert "silenciada" in turn_mute.reply_text.lower()

        turn_call = app2.submit_user_message("activa el modo en llamada").result(timeout=5.0)
        assert app2.settings.voice.in_call_text_only is True
        assert "llamada" in turn_call.reply_text.lower()

        # Verificar diagnósticos del Doctor
        diags = app2.voice_module.diagnose()
        diag_codes = {d.code for d in diags}
        assert {"voice.vosk_model", "voice.microphone", "voice.edge_tts", "voice.sapi_fallback"} <= diag_codes

        # Verificar Antídoto #5: publicar desde un hilo secundario no lanza RuntimeError
        received_async: list[float] = []
        done_evt = threading.Event()

        async def _async_listener(ev: MicLevelEvent) -> None:
            received_async.append(ev.level)
            done_evt.set()

        app2.events.subscribe(MicLevelEvent, _async_listener)
        worker = threading.Thread(
            target=lambda: app2.events.publish(MicLevelEvent(level=0.64, is_speech=True))
        )
        worker.start()
        worker.join(timeout=2.0)
        assert done_evt.wait(timeout=2.0) is True
        assert received_async == [0.64]
    finally:
        elapsed_2 = app2.shutdown()
        assert elapsed_2 < 3.0


def test_event_bus_publish_from_non_loop_thread_is_safe() -> None:
    bus = EventBus()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    got_event = threading.Event()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        bus.bind_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    assert ready.wait(timeout=2.0)
    try:

        async def _handler(_ev: MicLevelEvent) -> None:
            got_event.set()

        bus.subscribe(MicLevelEvent, _handler)
        bus.publish(MicLevelEvent(level=0.5, is_speech=False))
        assert got_event.wait(timeout=2.0) is True
    finally:
        bus.unbind_loop()
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2.0)
        loop.close()


def test_stt_hotfix_tv_commands_silence_cancellation_and_filler_cleanup(tmp_path: Path) -> None:
    # (d) "rafa el sube el volumen de la tele" se limpia a "sube el volumen de la tele"
    wake, rem = extract_wake_word("rafa el sube el volumen de la tele", ("raphael",))
    assert wake == "rafa el"
    assert rem == "sube el volumen de la tele"

    wake2, _ = extract_wake_word("el cómo está el clima mañana", ("raphael",))
    assert wake2 is None

    paths = resolve_app_paths(user_data_override=tmp_path)
    settings = DeusExSettings()
    com_sta = ComStaThread(name="HotfixComSta")
    audio_thread = AudioInputThread(name="HotfixAudio")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=_FakeAudioSessionBackend([]), smooth_ramp=False)
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)
        cond = FarFieldAudioConditioner()

        utterances: list[VoiceUtterance] = []
        cancelled: list[bool] = []

        stt = VoiceInputEngine(
            paths=paths,
            settings=settings,
            audio_thread=audio_thread,
            conditioner=cond,
            voiceprint=vp,
            context_gate=gate,
            on_utterance=utterances.append,
            on_capture_cancelled=lambda: cancelled.append(True),
        )
        stt.set_wake_words(("raphael",))

        # (a) y (b): tramas de silencio finales no bajan avg_vocal_ratio y partial repetido en silencio permite commit tras 1.15 s
        speech = _make_harmonic_voice(400.0, duration_sec=0.1, amplitude=0.25)
        silence = np.zeros(1_600, dtype=np.float32)

        rec = _FakeRecognizer(
            partials=[
                "rafael sube el volumen de la tele",
                "sube el volumen de la tele",
                "sube el volumen de la tele",
                "sube el volumen de la tele",
            ],
            finals=[],
        )
        stt.process_pcm_frame(speech, recognizer=rec, now=10.0)
        stt.process_pcm_frame(silence, recognizer=rec, now=10.2)
        stt.process_pcm_frame(silence, recognizer=rec, now=10.5)
        assert len(utterances) == 0

        # Pasa 1.15 s de silencio (11.45 - 10.2 = 1.25 s) con el mismo partial -> commit
        stt.process_pcm_frame(silence, recognizer=rec, now=11.45)
        assert len(utterances) == 1
        assert utterances[0].transcript == "sube el volumen de la tele"
        assert utterances[0].avg_vocal_ratio >= 0.70

        # (c) Captura vacía (solo wake-word sin comando) tras 2.6 s invoca on_capture_cancelled
        utterances.clear()
        cancelled.clear()
        rec_empty = _FakeRecognizer(partials=["raphael", "", ""], finals=[])
        stt.process_pcm_frame(speech, recognizer=rec_empty, now=20.0)

        stt.process_pcm_frame(silence, recognizer=rec_empty, now=21.0)
        assert len(cancelled) == 0
        stt.process_pcm_frame(silence, recognizer=rec_empty, now=22.8)
        assert len(cancelled) == 1
        assert len(utterances) == 0
    finally:
        com_sta.stop()


def test_context_gate_rejects_single_syllable_and_filler_noise(tmp_path: Path) -> None:
    """Regresión Hotfix #2 (a): ContextGate rechaza 'sí s', 'o a' y filler noise."""
    settings = DeusExSettings()
    com_sta = ComStaThread()
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=_FakeAudioSessionBackend([]))
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)

        assert gate.is_noise_or_third_person("sí s", is_follow_up=False) is True
        assert gate.is_noise_or_third_person("o a", is_follow_up=False) is True
        assert gate.is_noise_or_third_person("eh m", is_follow_up=False) is True

        dec1 = gate.evaluate_utterance("sí s", triggered_by="wake_word")
        assert dec1.allow is False
        assert dec1.reason == "noise_or_third_person"

        dec2 = gate.evaluate_utterance("o a", triggered_by="wake_word")
        assert dec2.allow is False
        assert dec2.reason == "noise_or_third_person"

        # En follow_up exige sum(len(w) for w in real_words) >= 4
        assert gate.is_noise_or_third_person("si", is_follow_up=True) is True
        assert gate.is_noise_or_third_person("gracias", is_follow_up=True) is False
        assert gate.is_noise_or_third_person("enciende la tele", is_follow_up=True) is False
    finally:
        com_sta.stop()


@pytest.mark.asyncio
async def test_transcribe_wav_cascade_fallbacks() -> None:
    """Regresión Hotfix #2 (b): Cascada Groq -> Gemini -> OpenAI -> None."""
    import httpx

    from deusex.modules.voice.stt import transcribe_wav_cascade

    wav_sample = b"RIFF....WAVEfmt ...." + b"\x00" * 3200

    async def _failing_google(_data: bytes) -> str | None:
        return None

    # 1. Groq responde 200
    class _MockGroqTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "api.groq.com" in str(request.url):
                return httpx.Response(200, json={"text": "reproduce mi música"})
            return httpx.Response(500)

    client1 = httpx.AsyncClient(transport=_MockGroqTransport())
    try:
        res1 = await transcribe_wav_cascade(
            wav_sample,
            groq_api_key="gsk-test",
            google_transcriber=_failing_google,
            client=client1,
        )
        assert res1 == ("reproduce mi música", "groq:whisper-large-v3")
    finally:
        await client1.aclose()

    # 2. Groq falla (503), cae a Gemini
    class _MockGroqFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "api.groq.com" in str(request.url):
                return httpx.Response(503, json={"error": "service unavailable"})
            return httpx.Response(404)

    async def _mock_gemini(data: bytes) -> str | None:
        return "pausa la música"

    client2 = httpx.AsyncClient(transport=_MockGroqFailTransport())
    try:
        res2 = await transcribe_wav_cascade(
            wav_sample,
            groq_api_key="gsk-test",
            google_transcriber=_failing_google,
            gemini_transcriber=_mock_gemini,
            client=client2,
        )
        assert res2 == ("pausa la música", "gemini:audio")
    finally:
        await client2.aclose()

    # 3. Groq y Gemini fallan, cae a OpenAI
    class _MockOpenAITransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if "api.openai.com" in str(request.url):
                return httpx.Response(200, json={"text": "sube el volumen"})
            return httpx.Response(500)

    async def _failing_gemini(data: bytes) -> str | None:
        return None

    client3 = httpx.AsyncClient(transport=_MockOpenAITransport())
    try:
        res3 = await transcribe_wav_cascade(
            wav_sample,
            groq_api_key="gsk-test",
            google_transcriber=_failing_google,
            gemini_transcriber=_failing_gemini,
            openai_api_key="sk-test",
            client=client3,
        )
        assert res3 == ("sube el volumen", "openai:whisper-1")
    finally:
        await client3.aclose()

    # 4. Todos fallan -> retorna None (fallback Vosk local)
    class _MockAllFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

    client4 = httpx.AsyncClient(transport=_MockAllFailTransport())
    try:
        res4 = await transcribe_wav_cascade(
            wav_sample,
            groq_api_key="gsk-test",
            google_transcriber=_failing_google,
            gemini_transcriber=_failing_gemini,
            openai_api_key="sk-test",
            client=client4,
        )
        assert res4 is None
    finally:
        await client4.aclose()


@pytest.mark.asyncio
async def test_voice_module_fast_path_and_hybrid_transcriber(tmp_path: Path) -> None:
    """Regresión Hotfix #2 (c): VoiceModule omite transcriber en FastPath y lo llama cuando no calza."""
    from deusex.core.contracts import ModuleContext
    from deusex.core.runtime import RuntimeServices
    from deusex.modules.voice.module import ProfileVoiceInfo, VoiceCommandReply, VoiceModule

    settings = DeusExSettings()
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    events = EventBus()
    runtime = RuntimeServices(events)
    runtime.start()

    try:
        transcriber_calls: list[bytes] = []
        command_calls: list[tuple[str, bool]] = []

        async def _mock_transcriber(audio: bytes) -> tuple[str, str] | None:
            transcriber_calls.append(audio)
            return "Raphael, reproduce rock en spotify", "groq:whisper-large-v3-turbo"

        def _fast_path_matcher(text: str) -> bool:
            return "enciende la tele" in text.lower()

        async def _on_cmd(text: str, is_follow_up: bool) -> VoiceCommandReply:
            command_calls.append((text, is_follow_up))
            return VoiceCommandReply(text="Ok", should_speak=False)

        module = VoiceModule(
            on_voice_command=_on_cmd,
            profile_info_provider=lambda: ProfileVoiceInfo(
                profile_id="raphael",
                wake_words=("raphael", "deusex"),
            ),
            audio_transcriber=_mock_transcriber,
            fast_path_matcher=_fast_path_matcher,
            auto_start_capture=False,
            edge_synthesizer=_FakeEdgeSynthesizer(),
            playback_backend=_FakePlaybackBackend(),
            sapi_backend=_FakeSapiBackend(),
            audio_session_backend=_FakeAudioSessionBackend([]),
        )

        ctx = ModuleContext(
            paths=paths,
            settings=settings,
            secrets=secrets,
            events=events,
            runtime=runtime,
            db=None,  # type: ignore[arg-type]
        )
        await module.start(ctx)

        dummy_wav = b"RIFF" + b"\x00" * 4500
        dummy_samples = np.zeros(1600, dtype=np.float32)

        # 1. Vosk coincide con FastPath -> 0 tokens (NO llama a transcriber)
        utt_fast = VoiceUtterance(
            transcript="enciende la tele",
            triggered_by="wake_word",
            wake_word="raphael",
            audio_samples=dummy_samples,
            avg_vocal_ratio=0.85,
            wav_bytes=dummy_wav,
        )
        dec_fast = await module.handle_utterance(utt_fast)
        assert dec_fast.allow is True
        assert len(transcriber_calls) == 0
        assert len(command_calls) == 1
        assert command_calls[0][0] == "enciende la tele"

        # 2. Vosk NO coincide -> llama audio_transcriber y limpia wake-word
        command_calls.clear()
        utt_slow = VoiceUtterance(
            transcript="reproduce rock",
            triggered_by="wake_word",
            wake_word="raphael",
            audio_samples=dummy_samples,
            avg_vocal_ratio=0.85,
            wav_bytes=dummy_wav,
        )
        dec_slow = await module.handle_utterance(utt_slow)
        assert dec_slow.allow is True
        assert len(transcriber_calls) == 1
        assert len(command_calls) == 1
        # Se limpió "Raphael, " y se pasó "reproduce rock en spotify"
        assert command_calls[0][0] == "reproduce rock en spotify"

        await module.stop()
    finally:
        runtime.shutdown()


def test_voiceprint_short_command_acceptance_and_rejection(tmp_path: Path) -> None:
    """Regresión Hotfix #2 (d): OwnerVoiceprintGuard acepta comando corto (1.2 s) de la misma voz y rechaza otra."""
    vp = OwnerVoiceprintGuard(tmp_path)

    # 1. Calibración de 6.0 s con voz base f0=180 Hz
    cal_audio = _make_harmonic_voice(180.0, duration_sec=6.0, amplitude=0.25)
    ok_cal = vp.enroll_from_audio(cal_audio)
    assert ok_cal is True
    assert vp.is_enrolled is True

    # 2. Comando corto (1.2 s) con la MISMA voz (180 Hz) -> debe ser ACEPTADO
    short_same_voice = _make_harmonic_voice(180.0, duration_sec=1.2, amplitude=0.22)
    match_same = vp.verify(short_same_voice)
    assert match_same.accepted is True
    assert match_same.similarity >= vp.threshold

    # 3. Voz ajena / diferente frecuencia fundamental (f0=500 Hz) -> debe ser RECHAZADA
    other_voice = _make_harmonic_voice(500.0, duration_sec=1.5, amplitude=0.25)
    match_other = vp.verify(other_voice)
    assert match_other.accepted is False
    assert match_other.similarity < vp.threshold


def test_prepare_wav_for_cloud_stt_linear_norm_and_trim() -> None:
    """Hotfix #5: prepare_wav_for_cloud_stt no corta inicio, limita cola y aplica ganancia suave si peak < 0.15."""
    import io
    import wave

    from deusex.modules.voice.stt import prepare_wav_for_cloud_stt

    sr = 16000
    silence = np.zeros(16000, dtype=np.float32)
    t = np.arange(8000, dtype=np.float32) / float(sr)
    sine = (0.10 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    full_audio = np.concatenate([silence, sine, silence], axis=0)
    pcm16 = (full_audio * 32767.0).astype(np.int16).tobytes()

    wav_bytes, peak_before, dur = prepare_wav_for_cloud_stt(pcm16, sample_rate=sr)

    # 1. Pico original detectado cercano a 0.10
    assert abs(peak_before - 0.10) < 0.005

    # 2. Silencio inicial preservado para plosivas, cola recortada con margen de 320 ms -> dur ~1.82 s
    assert 1.75 <= dur <= 1.90

    # 3. Leer WAV resultante: pico normalizado hacia 0.40-0.45 (sin tanh, ganancia suave 4.0x)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        audio_norm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        peak_after = float(np.max(np.abs(audio_norm)))
        assert 0.38 <= peak_after <= 0.46


def test_clean_brand_articles() -> None:
    """Regresión Hotfix #3 (b): Limpieza selectiva de artículos delante de marcas sin tocar lenguaje natural."""
    from deusex.modules.voice.phonetics import clean_brand_articles

    # Marcas y servicios conocidos
    assert clean_brand_articles("abre el spotify") == "abre spotify"
    assert clean_brand_articles("cierra la discord") == "cierra discord"
    assert clean_brand_articles("pon el youtube") == "pon youtube"
    assert clean_brand_articles("ejecuta el chrome") == "ejecuta chrome"
    assert clean_brand_articles("inicia el whatsapp") == "inicia whatsapp"

    # Frases válidas del lenguaje natural preservadas intactas
    assert clean_brand_articles("abre el navegador") == "abre el navegador"
    assert clean_brand_articles("enciende la tele") == "enciende la tele"
    assert clean_brand_articles("sube el volumen") == "sube el volumen"
    assert clean_brand_articles("cierra la ventana") == "cierra la ventana"


def test_app_has_fast_path_match_selective_wildcard(tmp_path: Path) -> None:
    """Regresión Hotfix #3 (c): _has_fast_path_match exige app real para comodines y acepta cerradas."""
    from deusex.app import DeusExApplication

    app = DeusExApplication(user_data_dir=tmp_path, enable_hotkeys=False, enable_earcons=False)
    try:
        app.start()

        # Comandos cerrados / deterministas -> True (0 tokens)
        assert app._has_fast_path_match("silencia la voz") is True
        assert app._has_fast_path_match("pon un temporizador de 5 segundos") is True

        # Comodín abierto con texto desconocido ('es lo dificil') -> False (fuerza Groq Whisper)
        assert app._has_fast_path_match("abre es lo dificil") is False
        assert app._has_fast_path_match("cierra no existe esta app jamas") is False
    finally:
        app.shutdown()


def test_wake_phonetic_matcher_exact_fuzzy_and_command_extraction() -> None:
    """Port Raphael: Detección exacta, variantes fonéticas camaleónicas y descarte de falsos positivos."""
    from deusex.modules.voice.phonetics import WakePhoneticMatcher

    matcher = WakePhoneticMatcher(("raphael", "deusex"))

    # 1. Detección exacta en Vosk
    det, fuzzy_only = matcher.detect_in_vosk("raphael abre spotify")
    assert det is True
    assert fuzzy_only is False

    det, fuzzy_only = matcher.detect_in_vosk("oye rafael como estas")
    assert det is True
    assert fuzzy_only is False

    # Apócope y par contiguo ("rafa el")
    det, fuzzy_only = matcher.detect_in_vosk("rafa el pon musica")
    assert det is True
    assert fuzzy_only is False

    # 2. Detección difusa / variante fonética
    det, fuzzy_only = matcher.detect_in_vosk("rafel abre la puerta")
    assert det is True
    assert fuzzy_only is True

    # 3. No coincidencia
    det, fuzzy_only = matcher.detect_in_vosk("el perro ladra en la calle")
    assert det is False
    assert fuzzy_only is False

    # 4. Descarte de falsa alarma difusa cuando STT nube no contiene wake word ni energía suficiente
    ok_wake, cmd = matcher.extract_command_post_wake(
        "la television de la sala esta encendida",
        fuzzy_only=True,
        rms_int16=110.0,
        noise_floor=120.0,
    )
    assert ok_wake is False
    assert cmd == ""

    # 5. Confirmación con wake word presente en STT nube aunque Vosk haya sido difuso
    ok_wake, cmd = matcher.extract_command_post_wake(
        "Raphael abre Spotify por favor",
        fuzzy_only=True,
        rms_int16=450.0,
        noise_floor=120.0,
    )
    assert ok_wake is True
    assert cmd.lower() == "abre spotify"

    # 6. Gramática cerrada de barge-in incluye palabras clave y wake words
    grammar_json = matcher.build_bargein_grammar_json()
    assert "silencio" in grammar_json
    assert "para" in grammar_json
    assert "raphael" in grammar_json
    assert "rafael" in grammar_json


def test_tts_humanizer_units_temperatures_and_formatting() -> None:
    """Port Raphael: Humanizador de unidades de disco, temperatura, viñetas y emojis para TTS natural."""
    from deusex.modules.voice.tts import sanitize_text_for_speech

    # Temperaturas
    assert sanitize_text_for_speech("La temperatura es de 22.4 °C hoy") == "La temperatura es de 22 grados hoy"
    assert sanitize_text_for_speech("Está a 18°C afuera") == "Está a 18 grados afuera"

    # Almacenamiento y memoria (decimales y enteros)
    assert sanitize_text_for_speech("Quedan 13.7 GB libres en el disco C:") == "Quedan 13 coma 7 gigas libres en el disco C:"
    assert sanitize_text_for_speech("Tienes 16 GB de RAM instalados") == "Tienes 16 gigas de RAM instalados"
    assert sanitize_text_for_speech("El archivo pesa 450.5 MB en total") == "El archivo pesa 450 coma 5 megas en total"
    assert sanitize_text_for_speech("Descargando 200 MB") == "Descargando 200 megas"

    # Comillas ornamentales y viñetas
    assert sanitize_text_for_speech("Dice: «Listo para operar»") == "Dice: Listo para operar"
    assert sanitize_text_for_speech('Estado "conectado"') == "Estado conectado"

    # Emojis eliminados
    assert sanitize_text_for_speech("Todo excelente 🚀🔥 en el sistema 💻") == "Todo excelente en el sistema"


def test_stt_ring_buffer_3s_pre_roll_and_vad_hysteresis(tmp_path: Path) -> None:
    """Port Raphael: Ring buffer de 3.0 s y rebobinado al cruzar umbral de corte sin cortar la primera sílaba."""
    paths = resolve_app_paths(user_data_override=tmp_path)
    settings = DeusExSettings()
    com_sta = ComStaThread(name="TestComStaRingSTT")
    audio_thread = AudioInputThread(name="TestAudioRingSTT")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=_FakeAudioSessionBackend([]), smooth_ramp=False)
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)
        cond = FarFieldAudioConditioner()

        utterances: list[VoiceUtterance] = []
        stt = VoiceInputEngine(
            paths=paths,
            settings=settings,
            audio_thread=audio_thread,
            conditioner=cond,
            voiceprint=vp,
            context_gate=gate,
            on_utterance=utterances.append,
        )
        stt.set_wake_words(("raphael",))

        # 1. Alimentar ring buffer con varios bloques de audio simulado
        # 1024 muestras por bloque
        noise_chunk = (_make_sine(120.0, duration_sec=0.064, amplitude=0.01) * 32767.0).astype(np.int16).tobytes()
        speech_chunk = (_make_harmonic_voice(320.0, duration_sec=0.064, amplitude=0.25) * 32767.0).astype(np.int16).tobytes()

        # Inyectar 10 bloques de ruido
        rec = _FakeRecognizer(partials=["", "raphael enciende la tele", ""], finals=[])
        for i in range(10):
            stt.process_pcm_frame(noise_chunk, recognizer=rec, now=1.0 + i * 0.064)

        # Inyectar 5 bloques de voz (despierta wake word)
        for i in range(5):
            stt.process_pcm_frame(speech_chunk, recognizer=rec, now=2.0 + i * 0.064)

        # El buffer de captura debe contener los bloques pre-roll rebobinados
        assert stt._capturing is True
        assert len(stt._capture_pcm16) >= 5

        # Silencio para comitear
        silence_chunk = np.zeros(1024, dtype=np.int16).tobytes()
        stt.process_pcm_frame(silence_chunk, recognizer=rec, now=4.0)

        assert len(utterances) == 1
        assert utterances[0].transcript == "enciende la tele"
        assert utterances[0].rms_int16 > 0.0
        assert utterances[0].noise_floor_int16 > 0.0
        assert utterances[0].fuzzy_wake_only is False
    finally:
        com_sta.stop()


def test_voiceprint_v3_amplitude_invariance_and_filter(tmp_path: Path) -> None:
    """Hotfix #4: Voiceprint v3 con normalización unit-RMS es invariante a variaciones de amplitud."""
    vp = OwnerVoiceprintGuard(tmp_path)
    low_audio = _make_harmonic_voice(220.0, duration_sec=2.0, amplitude=0.06)
    high_audio = _make_harmonic_voice(220.0, duration_sec=2.0, amplitude=0.55)

    assert vp.enroll_from_audio(low_audio) is True
    assert vp.is_enrolled is True
    res_high = vp.verify(high_audio)
    assert res_high.similarity >= 0.85

    # Ruido/fricativa sin formantes estables cae por debajo de 0.50
    rng = np.random.default_rng(42)
    noise_audio = rng.uniform(-0.3, 0.3, size=len(low_audio)).astype(np.float32)
    res_noise = vp.verify(noise_audio)
    assert res_noise.similarity < 0.50


def test_stt_follow_up_promoted_to_wake_word_and_window_extended(tmp_path: Path) -> None:
    """Hotfix #4: Follow-up con wake-word se promueve a 'wake_word' y extiende la ventana al hablar."""
    paths = resolve_app_paths(user_data_override=tmp_path)
    settings = DeusExSettings()
    com_sta = ComStaThread(name="TestComStaFollowUp")
    audio_thread = AudioInputThread(name="TestAudioFollowUp")
    com_sta.start()
    try:
        ducker = SelectiveAudioDucker(com_sta, backend=_FakeAudioSessionBackend([]), smooth_ramp=False)
        vp = OwnerVoiceprintGuard(tmp_path)
        gate = ContextGate(settings, ducker, vp)
        cond = FarFieldAudioConditioner()

        utterances: list[VoiceUtterance] = []
        stt = VoiceInputEngine(
            paths=paths,
            settings=settings,
            audio_thread=audio_thread,
            conditioner=cond,
            voiceprint=vp,
            context_gate=gate,
            on_utterance=utterances.append,
        )
        stt.set_wake_words(("raphael",))

        # Abrir ventana de follow-up
        gate.open_follow_up_window(duration_sec=2.0)
        assert gate.is_follow_up_active() is True
        initial_deadline = gate._follow_up_deadline

        # Inyectar voz durante follow-up
        speech_chunk = (_make_harmonic_voice(320.0, duration_sec=0.064, amplitude=0.25) * 32767.0).astype(np.int16).tobytes()
        rec = _FakeRecognizer(partials=["", "rafael enciende la tv", ""], finals=[])

        stt.process_pcm_frame(speech_chunk, recognizer=rec, now=1.0)
        assert gate._follow_up_deadline > initial_deadline

        # Segunda trama con el texto parcial
        stt.process_pcm_frame(speech_chunk, recognizer=rec, now=1.064)

        # Silencio para comitear
        silence_chunk = np.zeros(1024, dtype=np.int16).tobytes()
        stt.process_pcm_frame(silence_chunk, recognizer=rec, now=2.5)

        assert len(utterances) == 1
        assert utterances[0].triggered_by == "wake_word"
        assert utterances[0].transcript == "enciende la tv"
    finally:
        com_sta.stop()


def test_phonetics_polite_tails_and_whisper_corrections() -> None:
    """Hotfix #4: Normalización de coletillas de cortesía y deslizamiento de Whisper 'con' -> 'pon'."""
    from deusex.modules.voice.phonetics import WakePhoneticMatcher

    matcher = WakePhoneticMatcher()
    assert matcher.normalize_stt_text("cierra Spotify, por favor.") == "cierra Spotify"
    assert matcher.normalize_stt_text("abre Discord muchas gracias!") == "abre Discord"
    assert matcher.normalize_stt_text("por favor enciende la tele") == "enciende la tele"
    assert matcher.normalize_stt_text("con YouTube en la TV") == "pon YouTube en la TV"
    assert matcher.normalize_stt_text("con Spotify en la tele") == "pon Spotify en la tele"


@pytest.mark.asyncio
async def test_transcribe_wav_cascade_extra_vocabulary() -> None:
    """Hotfix #4: transcribe_wav_cascade incluye extra_vocabulary dinámico en el prompt."""
    import httpx

    from deusex.modules.voice.stt import transcribe_wav_cascade

    captured_prompt = ""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_prompt
        content = request.content.decode("utf-8", errors="replace")
        if "name=\"prompt\"" in content:
            idx = content.find("name=\"prompt\"")
            captured_prompt = content[idx : idx + 300]
        return httpx.Response(200, json={"text": "abre hearthstone"})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await transcribe_wav_cascade(
            b"RIFFdummywav",
            groq_api_key="gsk-test",
            client=client,
            extra_vocabulary=("Hearthstone", "Discord", "Spotify"),
        )
        assert res is not None
        assert res[0] == "abre hearthstone"
        assert "Hearthstone" in captured_prompt


def test_app_scanner_hearthstone_phonetic_matching() -> None:
    """Hotfix #4: AppScanner resuelve 'Herbstón' / 'Heardstone' a Hearthstone con tolerancia 0.72."""
    from deusex.modules.pc.apps import AppEntry, AppScanner, generate_aliases

    entry = AppEntry(
        name="Hearthstone",
        launch_target="Hearthstone.exe",
        source="appsfolder",
        aliases=generate_aliases("Hearthstone"),
        executable_name="hearthstone.exe",
    )
    scanner = AppScanner(entries_override=[entry])
    assert scanner.find_app("Hearthstone") is not None
    assert scanner.find_app("herbston") is not None
    assert scanner.find_app("herston") is not None
    assert scanner.find_app("heardstone") is not None


@pytest.mark.asyncio
async def test_stt_parallel_arbiter_deterministic_scoring() -> None:
    """Hotfix #5: Árbitro determinista en paralelo elige el mejor candidato según Fast-Path, apps y penalizaciones."""
    import httpx

    from deusex.modules.voice.stt import transcribe_wav_cascade

    # Caso 1: Google acierta 'pon youtube en la tele' vs Groq con alucinación Title-Case 'Ponyo Tud, En La Tele'
    async def mock_google_1(_b: bytes) -> str | None:
        return "pon youtube en la tele"

    class _MockGroqTransport1(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"text": "Ponyo Tud, En La Tele"})

    client1 = httpx.AsyncClient(transport=_MockGroqTransport1())
    try:
        res1 = await transcribe_wav_cascade(
            b"RIFFdummy",
            groq_api_key="gsk-test",
            client=client1,
            google_transcriber=mock_google_1,
            extra_vocabulary=("YouTube", "Spotify", "Hearthstone"),
        )
        assert res1 is not None
        text1, engine1 = res1
        assert text1 == "pon youtube en la tele"
        assert engine1 == "google:es-CL"
    finally:
        await client1.aclose()

    # Caso 2: Groq acierta app instalada 'abre Hearthstone' vs Google deformado 'abre geston'
    async def mock_google_2(_b: bytes) -> str | None:
        return "abre geston"

    class _MockGroqTransport2(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"text": "abre Hearthstone"})

    client2 = httpx.AsyncClient(transport=_MockGroqTransport2())
    try:
        res2 = await transcribe_wav_cascade(
            b"RIFFdummy",
            groq_api_key="gsk-test",
            client=client2,
            google_transcriber=mock_google_2,
            extra_vocabulary=("Hearthstone", "Spotify"),
        )
        assert res2 is not None
        text2, engine2 = res2
        assert text2 == "abre Hearthstone"
        assert engine2 == "groq:whisper-large-v3"
    finally:
        await client2.aclose()


@pytest.mark.asyncio
async def test_groq_uses_whisper_large_v3_and_fallback_cascade() -> None:
    """Hotfix #5: Groq envía whisper-large-v3 (NO turbo) y ante fallo cae a Gemini -> OpenAI -> None."""
    import httpx

    from deusex.modules.voice.stt import transcribe_wav_cascade

    captured_model = ""

    class _MockGroqTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal captured_model
            body = await request.aread()
            content = body.decode("utf-8", errors="replace")
            if 'name="model"' in content:
                idx = content.find('name="model"')
                captured_model = content[idx : idx + 100]
            return httpx.Response(200, json={"text": "enciende la tele"})

    async def _mock_g_fail(_b: bytes) -> str | None:
        return None

    client = httpx.AsyncClient(transport=_MockGroqTransport())
    try:
        res = await transcribe_wav_cascade(
            b"RIFFdummy",
            groq_api_key="gsk-test",
            client=client,
            google_transcriber=_mock_g_fail,
        )
        assert res is not None
        assert res[1] == "groq:whisper-large-v3"
        assert "whisper-large-v3" in captured_model
        assert "turbo" not in captured_model
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_voice_module_safety_gate_confirmation_by_voice(tmp_path: Path) -> None:
    """Hotfix #5: SafetyGate 100% operable por voz ('sí/confirmar' aprueba, 'no/cancelar' rechaza en 0 tokens)."""
    from deusex.core.contracts import ModuleContext
    from deusex.core.events import (
        SafetyConfirmationRequestedEvent,
        SafetyConfirmationResolvedEvent,
    )
    from deusex.core.runtime import RuntimeServices
    from deusex.modules.voice.module import ProfileVoiceInfo, VoiceModule

    settings = DeusExSettings()
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    events = EventBus()
    runtime = RuntimeServices(events)
    runtime.start()

    resolved_calls: list[tuple[str, bool, str]] = []

    def mock_safety_resolver(conf_id: str, approved: bool, reason: str) -> bool:
        resolved_calls.append((conf_id, approved, reason))
        return True

    ctx = ModuleContext(
        settings=settings,
        paths=paths,
        secrets=secrets,
        events=events,
        runtime=runtime,
        db=None,  # type: ignore[arg-type]
    )

    module = VoiceModule(
        profile_info_provider=lambda: ProfileVoiceInfo(wake_words=("raphael",)),
        on_resolve_safety=mock_safety_resolver,
        auto_start_capture=False,
        edge_synthesizer=_FakeEdgeSynthesizer(),
        playback_backend=_FakePlaybackBackend(),
        sapi_backend=_FakeSapiBackend(),
        audio_session_backend=_FakeAudioSessionBackend([]),
    )
    await module.start(ctx)

    try:
        # 1. Simular evento de confirmación de seguridad
        event_req = SafetyConfirmationRequestedEvent(
            confirmation_id="conf-123",
            tool_name="pc_close_app",
            summary_es="Cerrar Visual Studio Code",
            timeout_sec=15,
        )
        ctx.events.publish(event_req)
        await asyncio.sleep(0.05)
        assert module._pending_confirmation is not None
        assert module._pending_confirmation.confirmation_id == "conf-123"

        # 2. Voz dice 'sí' -> se aprueba en 0 tokens
        utt_yes = VoiceUtterance(
            transcript="sí",
            triggered_by="follow_up",
            wake_word="",
            audio_samples=np.zeros(1600, dtype=np.float32),
            avg_vocal_ratio=0.85,
        )
        dec_yes = await module.handle_utterance(utt_yes)
        assert dec_yes.allow is True
        assert len(resolved_calls) == 1
        assert resolved_calls[0] == ("conf-123", True, "voice")
        assert module._pending_confirmation is None

        # 3. Nueva confirmación -> voz dice 'cancelar' -> se rechaza en 0 tokens
        event_req2 = SafetyConfirmationRequestedEvent(
            confirmation_id="conf-456",
            tool_name="pc_close_app",
            summary_es="Cerrar Cursor",
            timeout_sec=15,
        )
        ctx.events.publish(event_req2)
        await asyncio.sleep(0.05)
        assert module._pending_confirmation is not None

        utt_no = VoiceUtterance(
            transcript="cancelar",
            triggered_by="follow_up",
            wake_word="",
            audio_samples=np.zeros(1600, dtype=np.float32),
            avg_vocal_ratio=0.85,
        )
        dec_no = await module.handle_utterance(utt_no)
        assert dec_no.allow is True
        assert len(resolved_calls) == 2
        assert resolved_calls[1] == ("conf-456", False, "voice")
        assert module._pending_confirmation is None

        # 4. Evento SafetyConfirmationResolvedEvent externo limpia confirmación pendiente
        event_req3 = SafetyConfirmationRequestedEvent(
            confirmation_id="conf-789",
            tool_name="pc_close_app",
            summary_es="Cerrar App",
            timeout_sec=15,
        )
        ctx.events.publish(event_req3)
        await asyncio.sleep(0.05)
        assert module._pending_confirmation is not None

        ctx.events.publish(SafetyConfirmationResolvedEvent(confirmation_id="conf-789", approved=True, reason="gui"))
        await asyncio.sleep(0.05)
        assert module._pending_confirmation is None
    finally:
        await module.stop()
        runtime.shutdown(total_budget_sec=2.0)


@pytest.mark.asyncio
async def test_voice_safety_gate_no_deadlock_concurrent_confirmation(tmp_path: Path) -> None:
    """Hotfix #6: Cero deadlock en SafetyGate por voz.
    Simula _on_voice_command esperando SafetyGate mientras handle_utterance('confirmo la acción')
    resuelve concurrentemente en <1 s sin bloquearse en self._utterance_lock.
    """
    from deusex.core.contracts import ModuleContext
    from deusex.core.events import SafetyConfirmationRequestedEvent
    from deusex.core.runtime import RuntimeServices
    from deusex.modules.voice.module import ProfileVoiceInfo, VoiceCommandReply, VoiceModule

    settings = DeusExSettings()
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    events = EventBus()
    runtime = RuntimeServices(events)
    runtime.start()

    resolved_event = asyncio.Event()
    resolved_calls: list[tuple[str, bool, str]] = []

    def mock_safety_resolver(conf_id: str, approved: bool, reason: str) -> bool:
        resolved_calls.append((conf_id, approved, reason))
        if approved:
            resolved_event.set()
        return True

    async def mock_on_voice_command(cmd: str, is_follow_up: bool) -> VoiceCommandReply:
        if "bloc de notas" in cmd:
            events.publish(
                SafetyConfirmationRequestedEvent(
                    confirmation_id="deadlock-test-1",
                    tool_name="pc_close_app",
                    summary_es="Cerrar Bloc de Notas",
                    timeout_sec=25,
                )
            )
            # Esperar a que la confirmación por voz resuelva la compuerta
            await asyncio.wait_for(resolved_event.wait(), timeout=3.0)
            return VoiceCommandReply(text="Bloc de notas cerrado.", should_speak=False)
        return VoiceCommandReply(text="Ok", should_speak=False)

    ctx = ModuleContext(
        settings=settings,
        paths=paths,
        secrets=secrets,
        events=events,
        runtime=runtime,
        db=None,  # type: ignore[arg-type]
    )

    module = VoiceModule(
        profile_info_provider=lambda: ProfileVoiceInfo(wake_words=("raphael",)),
        on_resolve_safety=mock_safety_resolver,
        on_voice_command=mock_on_voice_command,
        auto_start_capture=False,
        edge_synthesizer=_FakeEdgeSynthesizer(),
        playback_backend=_FakePlaybackBackend(),
        sapi_backend=_FakeSapiBackend(),
        audio_session_backend=_FakeAudioSessionBackend([]),
    )
    await module.start(ctx)

    try:
        # 1. Turno 1 entra y queda esperando la confirmación (adquiere _utterance_lock)
        task_cmd = asyncio.create_task(
            module.handle_utterance(
                VoiceUtterance(
                    transcript="cierra el bloc de notas",
                    triggered_by="wake_word",
                    wake_word="raphael",
                    audio_samples=np.zeros(1600, dtype=np.float32),
                    avg_vocal_ratio=0.85,
                )
            )
        )
        await asyncio.sleep(0.05)
        assert module._pending_confirmation is not None

        # 2. Turno 2 ("Rafael, confirmo la acción") entra concurrentemente
        # Debe resolver en <1.0 s sin esperar a que task_cmd libere _utterance_lock
        t0 = time.monotonic()
        dec_confirm = await asyncio.wait_for(
            module.handle_utterance(
                VoiceUtterance(
                    transcript="Rafael confirmo la acción",
                    triggered_by="wake_word",
                    wake_word="raphael",
                    audio_samples=np.zeros(1600, dtype=np.float32),
                    avg_vocal_ratio=0.85,
                )
            ),
            timeout=1.0,
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert dec_confirm.allow is True
        assert dec_confirm.reason == "safety_resolved"
        assert resolved_event.is_set()
        assert len(resolved_calls) == 1
        assert resolved_calls[0] == ("deadlock-test-1", True, "voice")

        # 3. Turno 1 se desbloquea exitosamente sin timeout
        dec_cmd = await asyncio.wait_for(task_cmd, timeout=1.0)
        assert dec_cmd.allow is True
    finally:
        await module.stop()
        runtime.shutdown(total_budget_sec=2.0)


def test_courtesy_prefix_and_repeated_wake_words_normalization() -> None:
    """Hotfix #6: Normalización de prefijos de cortesía y múltiples wake-words consecutivas."""
    from deusex.modules.voice.phonetics import WakePhoneticMatcher

    matcher = WakePhoneticMatcher(("raphael",))

    # a) Cortesía convierte infinitivo a imperativo y entra en Fast-Path
    assert matcher.normalize_stt_text("puedes apagar la tele") == "apaga la tele"
    assert matcher.normalize_stt_text("podrías abrir Spotify") == "abre Spotify"
    assert matcher.normalize_stt_text("por favor me puedes abrir spotify") == "abre spotify"
    assert matcher.normalize_stt_text("podrías poner YouTube en la tele") == "pon YouTube en la tele"
    assert matcher.normalize_stt_text("puedes abrir la aplicación de Xbox").lower() == "abre xbox"

    # b) Múltiples wake-words consecutivas se limpian completamente
    ok1, cmd1 = matcher.extract_command_post_wake("Rafael Rafael Rafael confirmo")
    assert ok1 is True
    assert cmd1 == "confirmo"

    ok2, cmd2 = matcher.extract_command_post_wake("Rafael Rafael abre Spotify")
    assert ok2 is True
    assert cmd2 == "abre Spotify"


def test_network_module_fast_path_tv_launch_app() -> None:
    """Hotfix #6: Fast-Path directo para lanzar apps en TV (0 tokens)."""
    from deusex.modules.network.module import NetworkModule

    net_module = NetworkModule()
    rules = net_module.get_fast_path_rules()
    launch_rules = [r for r in rules if r.rule_id == "network.launch_app"]
    assert len(launch_rules) == 1
    rule = launch_rules[0]

    # Verifica matches de patrones naturales
    test_cases = [
        "pon YouTube en la tele",
        "pon youtube en la tv",
        "abre youtube en la tele",
        "poner netflix en la tv",
        "abre spotify en la tv",
        "pon prime video en la tele",
        "ejecuta twitch en la tele",
    ]
    for tc in test_cases:
        m = rule.pattern.search(tc)
        assert m is not None, f"Patrón falló en: {tc}"
        assert m.group("app")
        assert m.group("target")


def test_third_person_wake_mention_shield() -> None:
    """Hotfix #7: Escudo de mención en 3ª persona para no activar wake-word al hablar sobre el asistente."""
    from deusex.modules.voice.phonetics import is_third_person_wake_mention

    wake_variants = frozenset({"raphael", "rafael", "rafa"})

    # Casos que DEBEN ser descartados (hablando SOBRE el asistente o precedido de preposiciones indirectas)
    assert is_third_person_wake_mention("No, estoy modificando a Rafael mientras juego Hearthstone contigo", wake_variants) is True
    assert is_third_person_wake_mention("Estaba hablando de Rafael hace un rato", wake_variants) is True
    assert is_third_person_wake_mention("Le dije a Rafael que cerrara eso", wake_variants) is True
    assert is_third_person_wake_mention("Eso lo hice con Rafael ayer", wake_variants) is True
    assert is_third_person_wake_mention("Tengo una pregunta para Rafael", wake_variants) is True
    assert is_third_person_wake_mention("no se si con rafael se puede", wake_variants) is True
    assert is_third_person_wake_mention("creo que el rafael se apago", wake_variants) is True

    # Casos legítimos que NO deben descartarse (vocativos directos, órdenes directas)
    assert is_third_person_wake_mention("Rafael, abre Spotify", wake_variants) is False
    assert is_third_person_wake_mention("hola Rafael", wake_variants) is False
    assert is_third_person_wake_mention("oye Rafael enciende la tele", wake_variants) is False
    assert is_third_person_wake_mention("por favor Rafael abre el navegador", wake_variants) is False
    assert is_third_person_wake_mention("apaga la tele Rafael", wake_variants) is False
    assert is_third_person_wake_mention("cierra Discord por favor Rafael", wake_variants) is False


def test_safety_spoken_prompt_format() -> None:
    """Hotfix #7: Formateo conciso y natural de confirmaciones de seguridad habladas."""
    from deusex.core.events import SafetyConfirmationRequestedEvent
    from deusex.modules.voice.module import _format_safety_spoken_prompt

    ev1 = SafetyConfirmationRequestedEvent(
        confirmation_id="conf-1",
        tool_name="pc_kill_process",
        module_id="pc",
        summary_es="Cerrar el bloc de notas (PID 1234).",
    )
    assert _format_safety_spoken_prompt(ev1) == "¿Confirmo cerrar el bloc de notas? Di sí o no."

    ev2 = SafetyConfirmationRequestedEvent(
        confirmation_id="conf-2",
        tool_name="file_delete",
        module_id="pc",
        summary_es="Eliminar C:\\Users\\Diego\\archivo.txt definitivamente.",
    )
    assert _format_safety_spoken_prompt(ev2) == "¿Confirmo eliminar archivo.txt definitivamente? Di sí o no."


def test_selective_audio_ducker_detects_call_active_on_capture_session() -> None:
    """Hotfix #7: SelectiveAudioDucker detecta llamada activa si hay captura de micrófono en Discord."""
    from deusex.modules.voice.ducking import AudioSessionInfo, SelectiveAudioDucker

    class FakeBackend:
        def list_sessions(self) -> list[AudioSessionInfo]:
            return [
                AudioSessionInfo(
                    session_key="discord_cap",
                    process_name="Discord.exe",
                    display_name="Discord Voice",
                    pid=1001,
                    volume=1.0,
                    muted=False,
                    is_active=True,
                    peak=0.0,
                    is_capture=True,
                )
            ]

        def get_master_volume(self) -> float:
            return 1.0

        def set_master_volume(self, volume: float) -> None:
            pass

        def set_session_volume(self, session_key: str, volume: float) -> bool:
            return True

    ducker = SelectiveAudioDucker(com_sta=None, backend=FakeBackend())
    snap = ducker._inspect_sessions_impl()
    assert snap.is_call_active is True
    assert "Discord.exe" in snap.active_call_apps


def test_app_mic_muted_toggle_and_event(tmp_path: Path) -> None:
    """Hotfix #7: app.set_mic_muted y toggle_mic_muted actualizan configuración y suspenden STT."""
    app = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    app.start()
    assert app.set_module_enabled("voice", True).result(timeout=5.0) is True
    try:
        assert app.settings.voice.mic_muted is False
        assert app.voice_module.stt is not None
        assert app.voice_module.stt.is_suspended is False

        # Silenciar micrófono
        app.set_mic_muted(True)
        assert app.settings.voice.mic_muted is True
        assert app.voice_module.stt.is_suspended is True

        # Toggle reactiva micrófono
        res = app.toggle_mic_muted()
        assert res is False
        assert app.settings.voice.mic_muted is False
        assert app.voice_module.stt.is_suspended is False
    finally:
        app.shutdown()



