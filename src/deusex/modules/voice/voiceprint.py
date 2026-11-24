from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import math
import os
from pathlib import Path
import threading

import numpy as np
import numpy.typing as npt

logger = logging.getLogger(__name__)

VOICEPRINT_DIMENSIONS = 26
CALIBRATION_DURATION_SEC = 6.0
DEFAULT_VOICEPRINT_THRESHOLD = 0.55
_VOICEPRINT_FILENAME = "owner_voiceprint.json"
_MIN_CALIBRATION_VOICED_SEC = 1.2


@dataclass(frozen=True, slots=True)
class VoiceprintVerification:
    """Resultado tipado de verificación biométrica del propietario."""

    accepted: bool
    similarity: float
    threshold: float
    enrolled: bool
    reason: str


def _hz_to_mel(hz: float) -> float:
    return 2595.0 * math.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: float) -> float:
    return float(700.0 * (10.0 ** (mel / 2595.0) - 1.0))


def _build_mel_filterbank(
    *,
    num_filters: int,
    n_fft: int,
    sample_rate: int,
    fmin_hz: float = 80.0,
    fmax_hz: float = 7_600.0,
) -> npt.NDArray[np.float64]:
    nyquist = sample_rate / 2.0
    upper_hz = min(fmax_hz, nyquist)
    mel_points = np.linspace(_hz_to_mel(fmin_hz), _hz_to_mel(upper_hz), num_filters + 2)
    hz_points = np.array([_mel_to_hz(float(m)) for m in mel_points], dtype=np.float64)
    bin_indices = np.floor((n_fft + 1) * hz_points / float(sample_rate)).astype(np.int64)

    n_bins = n_fft // 2 + 1
    fbank = np.zeros((num_filters, n_bins), dtype=np.float64)
    for m in range(1, num_filters + 1):
        left = int(max(0, min(n_bins - 1, bin_indices[m - 1])))
        center = int(max(left + 1, min(n_bins - 1, bin_indices[m])))
        right = int(max(center + 1, min(n_bins - 1, bin_indices[m + 1])))
        if center > left:
            for k in range(left, center):
                fbank[m - 1, k] = (k - left) / float(center - left)
        if right > center:
            for k in range(center, right):
                fbank[m - 1, k] = (right - k) / float(right - center)
    return fbank


def _build_dct_matrix(num_ceps: int, num_filters: int) -> npt.NDArray[np.float64]:
    """Matriz DCT-II (omitiendo c0 de energía para invarianza a volumen)."""
    n = np.arange(num_filters, dtype=np.float64)[None, :]
    k = np.arange(1, num_ceps + 1, dtype=np.float64)[:, None]
    scale = math.sqrt(2.0 / float(num_filters))
    return np.asarray(scale * np.cos(math.pi * k * (n + 0.5) / float(num_filters)), dtype=np.float64)


def _atomic_write_json(target: Path, payload: dict[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


class OwnerVoiceprintGuard:
    """
    Huella vocal biométrica de 26 dimensiones en NumPy puro (13 MFCC medios + 13 desviaciones),
    persistida localmente en `USER_DATA_DIR/owner_voiceprint.json`, con calibración guiada de 6 s.
    """

    def __init__(
        self,
        storage_dir: Path,
        *,
        sample_rate: int = 16_000,
        similarity_threshold: float = DEFAULT_VOICEPRINT_THRESHOLD,
        calibration_duration_sec: float = CALIBRATION_DURATION_SEC,
    ) -> None:
        self._storage_file = storage_dir / _VOICEPRINT_FILENAME
        self._sample_rate = max(8_000, int(sample_rate))
        self._threshold = max(0.40, min(0.98, float(similarity_threshold)))
        self._calibration_duration_sec = max(2.0, float(calibration_duration_sec))
        self._lock = threading.RLock()

        self._n_fft = 512
        self._frame_len = round(self._sample_rate * 0.025)
        self._hop_len = round(self._sample_rate * 0.010)
        self._num_filters = 26
        self._num_ceps = 13
        self._mel_bank = _build_mel_filterbank(
            num_filters=self._num_filters,
            n_fft=self._n_fft,
            sample_rate=self._sample_rate,
        )
        self._dct_mat = _build_dct_matrix(self._num_ceps, self._num_filters)

        self._owner_vector: npt.NDArray[np.float64] | None = None
        self._enrolled_at: str | None = None

        # Estado de calibración guiada
        self._calibrating = False
        self._cal_chunks: list[npt.NDArray[np.float64]] = []
        self._cal_samples_collected = 0
        self._cal_target_samples = round(self._calibration_duration_sec * self._sample_rate)

        self._load_from_disk()

    @property
    def storage_file(self) -> Path:
        return self._storage_file

    @property
    def is_enrolled(self) -> bool:
        with self._lock:
            return self._owner_vector is not None

    @property
    def enrolled_at(self) -> str | None:
        with self._lock:
            return self._enrolled_at

    @property
    def threshold(self) -> float:
        with self._lock:
            return self._threshold

    def set_threshold(self, threshold: float) -> None:
        with self._lock:
            self._threshold = max(0.40, min(0.98, float(threshold)))

    @property
    def is_calibrating(self) -> bool:
        with self._lock:
            return self._calibrating

    @property
    def calibration_progress(self) -> float:
        with self._lock:
            if not self._calibrating or self._cal_target_samples <= 0:
                return 0.0
            return max(0.0, min(1.0, self._cal_samples_collected / float(self._cal_target_samples)))

    def _load_from_disk(self) -> None:
        if not self._storage_file.is_file():
            return
        try:
            raw = json.loads(self._storage_file.read_text(encoding="utf-8"))
            version = int(raw.get("version", 1) or 1)
            if version < 3:
                logger.info(
                    "Huella vocal en disco obsoleta (versión %d < 3, generada con tanh). "
                    "Se ignora; por favor calibra tu huella vocal durante 6 segundos.",
                    version,
                )
                return
            vec_raw = raw.get("vector")
            if isinstance(vec_raw, list) and len(vec_raw) == VOICEPRINT_DIMENSIONS:
                vec = np.array([float(v) for v in vec_raw], dtype=np.float64)
                norm = float(np.linalg.norm(vec))
                if norm > 1e-8 and np.all(np.isfinite(vec)):
                    self._owner_vector = vec / norm
                    ts = raw.get("enrolled_at")
                    self._enrolled_at = str(ts) if isinstance(ts, str) else None
        except Exception as exc:
            logger.warning("No se pudo cargar huella vocal local (%s); se ignorará.", exc)

    def _save_to_disk(self, vector: npt.NDArray[np.float64]) -> None:
        enrolled_at = datetime.now(UTC).isoformat()
        payload: dict[str, object] = {
            "version": 3,
            "dimensions": VOICEPRINT_DIMENSIONS,
            "sample_rate": self._sample_rate,
            "enrolled_at": enrolled_at,
            "vector": [round(float(v), 6) for v in vector],
        }
        _atomic_write_json(self._storage_file, payload)
        self._enrolled_at = enrolled_at

    def clear_voiceprint(self) -> bool:
        with self._lock:
            had_one = self._owner_vector is not None or self._storage_file.is_file()
            self._owner_vector = None
            self._enrolled_at = None
            if self._storage_file.is_file():
                self._storage_file.unlink(missing_ok=True)
            return had_one

    def extract_embedding(
        self,
        samples: npt.NDArray[np.float32] | npt.NDArray[np.float64] | npt.NDArray[np.int16] | bytes,
    ) -> npt.NDArray[np.float64] | None:
        """Extrae un vector L2-normalizado de 26 dimensiones (13 MFCC medios + 13 std)."""
        if isinstance(samples, (bytes, bytearray, memoryview)):
            audio = np.frombuffer(samples, dtype=np.int16).astype(np.float64) / 32768.0
        else:
            arr = np.asarray(samples)
            if arr.ndim > 1:
                arr = arr.mean(axis=1) if arr.ndim == 2 else arr.reshape(-1)
            audio = (
                arr.astype(np.float64) / 32768.0
                if np.issubdtype(arr.dtype, np.integer)
                else arr.astype(np.float64)
            )

        if audio.size < self._frame_len:
            return None

        # Normalización a RMS unitario para invarianza total a distancia/volumen
        rms = float(np.sqrt(np.mean(audio * audio)))
        if rms < 1e-4:
            return None
        audio = audio / rms

        # Pre-énfasis clásico (0.97) para resaltar formantes vocales
        emphasized = np.empty_like(audio)
        emphasized[0] = audio[0]
        emphasized[1:] = audio[1:] - 0.97 * audio[:-1]

        num_frames = 1 + (emphasized.size - self._frame_len) // self._hop_len
        if num_frames <= 0:
            return None

        indices = (
            np.arange(self._frame_len, dtype=np.int64)[None, :]
            + (np.arange(num_frames, dtype=np.int64) * self._hop_len)[:, None]
        )
        frames = emphasized[indices]

        # Filtrar SOLO tramas vocálicas con tono (descartando silencios y fricativas sordas)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
        max_rms = float(np.max(frame_rms))
        if max_rms < 1e-4:
            return None

        zcr = np.mean(np.diff(np.signbit(frames), axis=1) != 0, axis=1)
        energy_mask = frame_rms >= max(0.15, max_rms * 0.22)
        zcr_mask = zcr <= 0.28
        voiced_mask = energy_mask & zcr_mask
        if not np.any(voiced_mask):
            voiced_mask = energy_mask
        voiced_frames = frames[voiced_mask] if np.any(voiced_mask) else frames

        window = np.hanning(self._frame_len)[None, :]
        spectrum = np.fft.rfft(voiced_frames * window, n=self._n_fft, axis=1)
        power = (np.abs(spectrum) ** 2) / float(self._n_fft)

        mel_energies = np.matmul(power, self._mel_bank.T)
        log_mel = np.log(np.maximum(mel_energies, 1e-10))
        mfcc = np.matmul(log_mel, self._dct_mat.T)  # (T, 13)

        mean_vec = np.mean(mfcc, axis=0)
        std_vec = np.std(mfcc, axis=0)
        embedding = np.concatenate([mean_vec, std_vec * 0.20], axis=0)
        if embedding.shape[0] != VOICEPRINT_DIMENSIONS:
            return None

        norm = float(np.linalg.norm(embedding))
        if norm <= 1e-8 or not np.all(np.isfinite(embedding)):
            return None
        return np.asarray(embedding / norm, dtype=np.float64)

    def enroll_from_audio(
        self,
        samples: npt.NDArray[np.float32] | npt.NDArray[np.float64] | npt.NDArray[np.int16] | bytes,
    ) -> bool:
        """Registra y persiste la huella vocal del propietario a partir de una grabación."""
        embedding = self.extract_embedding(samples)
        if embedding is None:
            return False
        with self._lock:
            self._owner_vector = embedding
            self._save_to_disk(embedding)
        return True

    def start_calibration(self, *, duration_sec: float | None = None) -> None:
        with self._lock:
            dur = (
                max(2.0, float(duration_sec))
                if duration_sec is not None
                else self._calibration_duration_sec
            )
            self._calibrating = True
            self._cal_chunks = []
            self._cal_samples_collected = 0
            self._cal_target_samples = round(dur * self._sample_rate)

    def cancel_calibration(self) -> None:
        with self._lock:
            self._calibrating = False
            self._cal_chunks.clear()
            self._cal_samples_collected = 0

    def feed_calibration_chunk(
        self,
        samples: npt.NDArray[np.float32] | npt.NDArray[np.float64] | npt.NDArray[np.int16] | bytes,
    ) -> tuple[float, bool]:
        """
        Alimenta una trama durante la calibración guiada de 6 s.
        Retorna `(progreso_0_a_1, listo_para_finalizar)`.
        """
        if isinstance(samples, (bytes, bytearray, memoryview)):
            arr = np.frombuffer(samples, dtype=np.int16).astype(np.float64) / 32768.0
        else:
            raw = np.asarray(samples)
            if raw.ndim > 1:
                raw = raw.mean(axis=1) if raw.ndim == 2 else raw.reshape(-1)
            arr = (
                raw.astype(np.float64) / 32768.0
                if np.issubdtype(raw.dtype, np.integer)
                else raw.astype(np.float64)
            )

        with self._lock:
            if not self._calibrating:
                return 0.0, False
            if arr.size > 0:
                self._cal_chunks.append(arr)
                self._cal_samples_collected += int(arr.size)
            progress = min(1.0, self._cal_samples_collected / float(max(1, self._cal_target_samples)))
            ready = self._cal_samples_collected >= self._cal_target_samples
            return progress, ready

    def finalize_calibration(self) -> bool:
        """Consolida las tramas capturadas durante los 6 s y guarda la huella vocal."""
        with self._lock:
            if not self._calibrating or not self._cal_chunks:
                self._calibrating = False
                return False
            combined = np.concatenate(self._cal_chunks, axis=0)
            self._calibrating = False
            self._cal_chunks.clear()
            self._cal_samples_collected = 0

        min_samples = round(_MIN_CALIBRATION_VOICED_SEC * self._sample_rate)
        if combined.size < min_samples:
            return False
        return self.enroll_from_audio(combined)

    def verify(
        self,
        samples: npt.NDArray[np.float32] | npt.NDArray[np.float64] | npt.NDArray[np.int16] | bytes,
        *,
        enabled: bool = True,
        threshold_override: float | None = None,
    ) -> VoiceprintVerification:
        """Verifica similitud coseno contra la huella del propietario."""
        with self._lock:
            owner = self._owner_vector
            threshold = (
                max(0.40, min(0.98, float(threshold_override)))
                if threshold_override is not None
                else self._threshold
            )

        if not enabled:
            return VoiceprintVerification(
                accepted=True,
                similarity=1.0,
                threshold=threshold,
                enrolled=owner is not None,
                reason="disabled",
            )

        if owner is None:
            return VoiceprintVerification(
                accepted=True,
                similarity=1.0,
                threshold=threshold,
                enrolled=False,
                reason="not_enrolled",
            )

        candidate = self.extract_embedding(samples)
        if candidate is None:
            return VoiceprintVerification(
                accepted=False,
                similarity=0.0,
                threshold=threshold,
                enrolled=True,
                reason="insufficient_audio",
            )

        similarity = float(np.dot(owner, candidate))
        similarity = max(-1.0, min(1.0, similarity))
        accepted = similarity >= threshold
        return VoiceprintVerification(
            accepted=accepted,
            similarity=similarity,
            threshold=threshold,
            enrolled=True,
            reason="matched" if accepted else "voiceprint_mismatch",
        )
