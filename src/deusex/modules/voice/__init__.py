from __future__ import annotations

from deusex.modules.voice.context_gate import ContextDecision, ContextGate
from deusex.modules.voice.dsp import ConditionedAudioFrame, FarFieldAudioConditioner
from deusex.modules.voice.ducking import (
    AudioSessionBackend,
    AudioSessionInfo,
    AudioSessionSnapshot,
    SelectiveAudioDucker,
)
from deusex.modules.voice.module import ProfileVoiceInfo, VoiceCommandReply, VoiceModule
from deusex.modules.voice.phonetics import (
    WakePhoneticMatcher,
    clean_brand_articles,
    generate_phonetic_variants,
    strip_accents,
)
from deusex.modules.voice.stt import (
    AudioInputDevice,
    KeyStatePoller,
    SpeechRecognizer,
    VoiceInputEngine,
    VoiceUtterance,
    Win32AsyncKeyPoller,
    extract_wake_word,
    is_vosk_model_present,
    list_input_devices,
    parse_hotkey_vks,
    pcm16_to_wav_bytes,
    prepare_wav_for_cloud_stt,
    transcribe_wav_cascade,
)
from deusex.modules.voice.tts import (
    AudioPlaybackBackend,
    EdgeTTSSynthesizer,
    SapiBackend,
    SynthesizedSpeech,
    TTSResult,
    VoiceTTSEngine,
    WordTiming,
    format_edge_rate,
    sanitize_text_for_speech,
)
from deusex.modules.voice.voiceprint import (
    CALIBRATION_DURATION_SEC,
    VOICEPRINT_DIMENSIONS,
    OwnerVoiceprintGuard,
    VoiceprintVerification,
)

__all__ = [
    "CALIBRATION_DURATION_SEC",
    "VOICEPRINT_DIMENSIONS",
    "AudioInputDevice",
    "AudioPlaybackBackend",
    "AudioSessionBackend",
    "AudioSessionInfo",
    "AudioSessionSnapshot",
    "ConditionedAudioFrame",
    "ContextDecision",
    "ContextGate",
    "EdgeTTSSynthesizer",
    "FarFieldAudioConditioner",
    "KeyStatePoller",
    "OwnerVoiceprintGuard",
    "ProfileVoiceInfo",
    "SapiBackend",
    "SelectiveAudioDucker",
    "SpeechRecognizer",
    "SynthesizedSpeech",
    "TTSResult",
    "VoiceCommandReply",
    "VoiceInputEngine",
    "VoiceModule",
    "VoiceTTSEngine",
    "VoiceUtterance",
    "VoiceprintVerification",
    "WakePhoneticMatcher",
    "Win32AsyncKeyPoller",
    "WordTiming",
    "clean_brand_articles",
    "extract_wake_word",
    "format_edge_rate",
    "generate_phonetic_variants",
    "is_vosk_model_present",
    "list_input_devices",
    "parse_hotkey_vks",
    "pcm16_to_wav_bytes",
    "prepare_wav_for_cloud_stt",
    "sanitize_text_for_speech",
    "strip_accents",
    "transcribe_wav_cascade",
]
