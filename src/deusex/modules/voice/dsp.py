from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import numpy.typing as npt

DEFAULT_SAMPLE_RATE = 16_000
HIGHPASS_CUTOFF_HZ = 85.0
VOCAL_BAND_LOW_HZ = 300.0
VOCAL_BAND_HIGH_HZ = 3_400.0


@dataclass(frozen=True, slots=True)
class ConditionedAudioFrame:
    """Resultado por trama del acondicionamiento de audio far-field."""

    pcm16_bytes: bytes
    samples_f32: npt.NDArray[np.float32]
    raw_rms: float
    conditioned_rms: float
    normalized_level: float
    vocal_band_ratio: float
    is_speech: bool
    gain_applied: float
    raw_hp_pcm16: bytes = b""


class _FirstOrderHighPass:
    """Filtro pasa-altos IIR exacto de primer orden en NumPy puro con continuidad entre tramas."""

    def __init__(self, *, cutoff_hz: float, sample_rate: int) -> None:
        rc = 1.0 / (2.0 * math.pi * max(1.0, cutoff_hz))
        dt = 1.0 / float(max(1, sample_rate))
        self._alpha: float = rc / (rc + dt)
        self._prev_x: float = 0.0
        self._prev_y: float = 0.0
        # Longitud suficiente para que alpha**k < 1e-7 en float64
        decay_steps = math.ceil(-16.0 / math.log(max(1e-6, min(0.9999, self._alpha))))
        self._max_kernel_len = max(64, min(2048, decay_steps))

    def reset(self) -> None:
        self._prev_x = 0.0
        self._prev_y = 0.0

    def process(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        n = int(x.shape[0])
        if n == 0:
            return x.copy()

        alpha = self._alpha
        # u[k] = alpha * (x[k] - x[k-1])
        dx = np.empty(n, dtype=np.float64)
        dx[0] = x[0] - self._prev_x
        if n > 1:
            dx[1:] = np.diff(x)
        u = alpha * dx

        # Solución exacta de y[k] = alpha * y[k-1] + u[k] mediante convolución lineal por FFT
        k_len = min(n, self._max_kernel_len)
        powers = alpha ** np.arange(k_len, dtype=np.float64)
        n_fft = 1 << (n + k_len - 1).bit_length()
        conv_full = np.fft.irfft(
            np.fft.rfft(u, n=n_fft) * np.fft.rfft(powers, n=n_fft),
            n=n_fft,
        )
        y = np.asarray(conv_full[:n], dtype=np.float64)

        if self._prev_y != 0.0:
            init_len = min(n, self._max_kernel_len)
            init_powers = alpha ** np.arange(1, init_len + 1, dtype=np.float64)
            y[:init_len] += self._prev_y * init_powers

        self._prev_x = float(x[-1])
        self._prev_y = float(y[-1])
        return y


class FarFieldAudioConditioner:
    """
    Acondicionador de audio far-field en NumPy puro:
    - Filtro pasa-altos en cascada (>85 Hz) para eliminar zumbido eléctrico y golpes graves.
    - AGC adaptativo con compresión suave basada en `np.tanh`.
    - VAD centrado en banda vocal humana (300-3400 Hz) y nivel RMS normalizado (0.0-1.0).
    """

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        highpass_hz: float = HIGHPASS_CUTOFF_HZ,
        vocal_low_hz: float = VOCAL_BAND_LOW_HZ,
        vocal_high_hz: float = VOCAL_BAND_HIGH_HZ,
        target_rms: float = 0.14,
        min_gain: float = 0.6,
        max_gain: float = 2.8,
        noise_floor_rms: float = 0.004,
        base_vad_rms: float = 0.015,
        min_vocal_ratio: float = 0.42,
        tanh_drive: float = 1.0,
    ) -> None:
        self._sample_rate = max(8_000, int(sample_rate))
        self._vocal_low_hz = float(vocal_low_hz)
        self._vocal_high_hz = float(vocal_high_hz)
        self._target_rms = float(target_rms)
        self._min_gain = float(min_gain)
        self._max_gain = float(max_gain)
        self._noise_floor_rms = float(noise_floor_rms)
        self._base_vad_rms = float(base_vad_rms)
        self._min_vocal_ratio = float(min_vocal_ratio)
        self._tanh_drive = max(0.5, float(tanh_drive))
        self._tanh_norm = math.tanh(self._tanh_drive)

        # Dos etapas en cascada (~12 dB/oct) para atenuar frecuencias sub-85 Hz
        self._hp_stage1 = _FirstOrderHighPass(cutoff_hz=highpass_hz, sample_rate=self._sample_rate)
        self._hp_stage2 = _FirstOrderHighPass(cutoff_hz=highpass_hz, sample_rate=self._sample_rate)
        self._agc_envelope: float = self._target_rms

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def reset(self) -> None:
        self._hp_stage1.reset()
        self._hp_stage2.reset()
        self._agc_envelope = self._target_rms

    @staticmethod
    def _to_float64_mono(
        chunk: npt.NDArray[np.float32] | npt.NDArray[np.int16] | bytes,
    ) -> npt.NDArray[np.float64]:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            arr_i16 = np.frombuffer(chunk, dtype=np.int16)
            return arr_i16.astype(np.float64) / 32768.0

        arr = np.asarray(chunk)
        if arr.ndim == 2:
            arr = arr.mean(axis=1)
        elif arr.ndim > 2:
            arr = arr.reshape(-1)

        if np.issubdtype(arr.dtype, np.integer):
            return arr.astype(np.float64) / 32768.0
        return np.clip(arr.astype(np.float64), -1.0, 1.0)

    def _compute_vocal_band_ratio(self, samples: npt.NDArray[np.float64]) -> float:
        n = int(samples.shape[0])
        if n < 16:
            return 0.0

        window = np.hanning(n)
        spectrum = np.fft.rfft(samples * window)
        power = np.abs(spectrum) ** 2
        freqs = np.fft.rfftfreq(n, d=1.0 / float(self._sample_rate))

        total_mask = freqs >= HIGHPASS_CUTOFF_HZ
        total_energy = float(np.sum(power[total_mask]))
        if total_energy <= 1e-12:
            return 0.0

        vocal_mask = (freqs >= self._vocal_low_hz) & (freqs <= self._vocal_high_hz)
        vocal_energy = float(np.sum(power[vocal_mask]))
        return max(0.0, min(1.0, vocal_energy / total_energy))

    def process(
        self,
        chunk: npt.NDArray[np.float32] | npt.NDArray[np.int16] | bytes,
        *,
        sensitivity_multiplier: float = 1.0,
    ) -> ConditionedAudioFrame:
        """Procesa una trama PCM y devuelve audio acondicionado, métricas VAD y nivel visual."""
        raw = self._to_float64_mono(chunk)
        if raw.size == 0:
            empty = np.zeros(0, dtype=np.float32)
            return ConditionedAudioFrame(
                pcm16_bytes=b"",
                samples_f32=empty,
                raw_rms=0.0,
                conditioned_rms=0.0,
                normalized_level=0.0,
                vocal_band_ratio=0.0,
                is_speech=False,
                gain_applied=1.0,
            )

        raw_rms = float(np.sqrt(np.mean(raw * raw)))

        # 1. Filtro pasa-altos >85 Hz
        hp = self._hp_stage2.process(self._hp_stage1.process(raw))
        hp_rms = float(np.sqrt(np.mean(hp * hp)))

        # 2. Relación de energía vocal (300-3400 Hz) sobre la señal ya filtrada
        vocal_ratio = self._compute_vocal_band_ratio(hp)

        # 3. AGC adaptativo suave con compresión np.tanh
        if hp_rms < self._noise_floor_rms:
            # No amplificar ruido de fondo en silencio
            gain = 1.0
            self._agc_envelope = 0.92 * self._agc_envelope + 0.08 * self._target_rms
        else:
            alpha_env = 0.35 if hp_rms > self._agc_envelope else 0.08
            self._agc_envelope = (1.0 - alpha_env) * self._agc_envelope + alpha_env * hp_rms
            desired_gain = self._target_rms / max(self._agc_envelope, self._noise_floor_rms)
            gain = max(self._min_gain, min(self._max_gain, desired_gain))

        amplified = hp * gain
        conditioned_f64 = np.tanh(amplified * self._tanh_drive) / self._tanh_norm
        conditioned_f64 = np.clip(conditioned_f64, -1.0, 1.0)
        conditioned_f32 = conditioned_f64.astype(np.float32)
        conditioned_rms = float(np.sqrt(np.mean(conditioned_f64 * conditioned_f64)))

        # 4. VAD en banda vocal con sensibilidad dinámica (ej. elevada cuando hay multimedia)
        mult = max(0.5, float(sensitivity_multiplier))
        effective_rms_threshold = self._base_vad_rms * mult
        effective_vocal_ratio = min(0.75, self._min_vocal_ratio + (0.08 if mult > 1.25 else 0.0))
        is_speech = (hp_rms >= effective_rms_threshold) and (vocal_ratio >= effective_vocal_ratio)

        # 5. Nivel visual normalizado (0.0-1.0) con curva perceptual para Orbe y medidor UI
        normalized_level = max(0.0, min(1.0, math.sqrt(min(1.0, hp_rms / 0.22))))

        pcm16 = np.clip(np.round(conditioned_f64 * 32767.0), -32768.0, 32767.0).astype(np.int16)
        raw_hp = np.clip(np.round(hp * 32767.0), -32768.0, 32767.0).astype(np.int16)
        return ConditionedAudioFrame(
            pcm16_bytes=pcm16.tobytes(),
            samples_f32=conditioned_f32,
            raw_rms=raw_rms,
            conditioned_rms=conditioned_rms,
            normalized_level=normalized_level,
            vocal_band_ratio=vocal_ratio,
            is_speech=is_speech,
            gain_applied=gain,
            raw_hp_pcm16=raw_hp.tobytes(),
        )
