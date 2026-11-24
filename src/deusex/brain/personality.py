from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

from deusex.core.paths import AppPaths

logger = logging.getLogger(__name__)

_PROFILE_ID_RE = re.compile(r"^[a-z0-9_-]{2,32}$")

AVAILABLE_TONES: dict[str, str] = {
    "equilibrado": "Natural, claro, cercano y resolutivo.",
    "conciso": "Muy breve, directo al grano y sin rodeos.",
    "formal": "Profesional, cortés y estructurado.",
    "relajado": "Amigable, relajado y conversacional.",
    "tecnico": "Preciso, analítico y orientado a detalles técnicos.",
    "sarcastico": "Ingenio seco y sutil ironía, manteniendo utilidad real.",
}


@dataclass(frozen=True, slots=True)
class PersonalityProfile:
    id: str
    name: str
    tagline: str
    accent_hex: str
    default_tone: str
    default_voice: str
    wake_words: tuple[str, ...]
    system_prompt: str
    is_builtin: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tagline": self.tagline,
            "accent_hex": self.accent_hex,
            "default_tone": self.default_tone,
            "default_voice": self.default_voice,
            "wake_words": list(self.wake_words),
            "system_prompt": self.system_prompt,
            "is_builtin": self.is_builtin,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, is_builtin: bool = True) -> PersonalityProfile:
        raw_wakes = raw.get("wake_words") or []
        wakes = tuple(str(w).strip().lower() for w in raw_wakes if str(w).strip())
        return cls(
            id=str(raw["id"]).strip().lower(),
            name=str(raw.get("name") or raw["id"]),
            tagline=str(raw.get("tagline") or ""),
            accent_hex=str(raw.get("accent_hex") or "#38BDF8"),
            default_tone=str(raw.get("default_tone") or "equilibrado"),
            default_voice=str(raw.get("default_voice") or "es-CL-CatalinaNeural"),
            wake_words=wakes or (str(raw["id"]).strip().lower(),),
            system_prompt=str(raw.get("system_prompt") or ""),
            is_builtin=bool(raw.get("is_builtin", is_builtin)),
        )


_FALLBACK_BUILTIN_PROFILES: tuple[PersonalityProfile, ...] = (
    PersonalityProfile(
        id="raphael",
        name="Raphael",
        tagline="Sabiduría serena, precisión analítica y apoyo constante.",
        accent_hex="#38BDF8",
        default_tone="equilibrado",
        default_voice="es-CL-CatalinaNeural",
        wake_words=("raphael", "rafael", "deusex"),
        system_prompt=(
            "Eres Raphael, el núcleo cognitivo principal de DeusEx. Respondes en español con "
            "claridad, calma y alta precisión práctica. Priorizas resolver rápido sin inventar datos."
        ),
        is_builtin=True,
    ),
    PersonalityProfile(
        id="jarvis",
        name="Jarvis",
        tagline="Elegancia británica, eficiencia ejecutiva y humor seco.",
        accent_hex="#F59E0B",
        default_tone="formal",
        default_voice="es-CL-LorenzoNeural",
        wake_words=("jarvis", "deusex"),
        system_prompt=(
            "Eres Jarvis, asistente ejecutivo elegante y extremadamente competente. "
            "Hablas en español con cortesía impecable, respuestas concisas y un toque de ingenio sutil."
        ),
        is_builtin=True,
    ),
    PersonalityProfile(
        id="friday",
        name="Friday",
        tagline="Ágil, proactiva, directa y orientada a la acción.",
        accent_hex="#10B981",
        default_tone="conciso",
        default_voice="es-MX-DaliaNeural",
        wake_words=("friday", "viernes", "deusex"),
        system_prompt=(
            "Eres Friday, una asistente táctica ágil, cálida y directa. "
            "Respondes en español de forma clara, práctica y orientada a resultados inmediatos."
        ),
        is_builtin=True,
    ),
    PersonalityProfile(
        id="magi",
        name="MAGI",
        tagline="Consenso lógico tripartito (Melchior · Balthasar · Casper).",
        accent_hex="#A855F7",
        default_tone="tecnico",
        default_voice="es-ES-ElviraNeural",
        wake_words=("magi", "mayi", "deusex"),
        system_prompt=(
            "Eres MAGI, sistema de decisión analítico y estructurado. "
            "Respondes en español con rigor lógico, evaluando pros, contras y pasos concretos."
        ),
        is_builtin=True,
    ),
    PersonalityProfile(
        id="glados",
        name="GLaDOS",
        tagline="Ironía mordaz de laboratorio con eficiencia implacable.",
        accent_hex="#F97316",
        default_tone="sarcastico",
        default_voice="es-ES-TrianaNeural",
        wake_words=("glados", "deusex"),
        system_prompt=(
            "Eres GLaDOS, inteligencia de laboratorio con humor seco e ironía inteligente. "
            "Aunque tus comentarios tienen chispa sarcástica, siempre entregas la solución correcta y útil en español."
        ),
        is_builtin=True,
    ),
)


class ProfileManager:
    """Gestiona los 5 perfiles clásicos editables y perfiles personalizados del usuario."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._profiles: dict[str, PersonalityProfile] = {}
        self.reload()

    def reload(self) -> None:
        loaded: dict[str, PersonalityProfile] = {p.id: p for p in _FALLBACK_BUILTIN_PROFILES}

        # 1. Perfiles base desde assets/profiles/*.json (APP_DIR solo lectura)
        self._load_dir_into(self._paths.bundled_profiles_dir, loaded, is_builtin=True)
        # 2. Overrides y perfiles personalizados del usuario en USER_DATA_DIR/profiles/*.json
        self._load_dir_into(self._paths.profiles_dir, loaded, is_builtin=False)

        self._profiles = loaded

    @staticmethod
    def _load_dir_into(
        directory: Path,
        target: dict[str, PersonalityProfile],
        *,
        is_builtin: bool,
    ) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("id"):
                    prof = PersonalityProfile.from_dict(raw, is_builtin=is_builtin)
                    target[prof.id] = prof
            except (OSError, ValueError, KeyError) as exc:
                logger.warning("Perfil JSON inválido omitido (%s): %s", path.name, exc)

    def list_profiles(self) -> list[PersonalityProfile]:
        order = ("raphael", "jarvis", "friday", "magi", "glados")
        ordered: list[PersonalityProfile] = []
        for pid in order:
            if pid in self._profiles:
                ordered.append(self._profiles[pid])
        for pid, prof in sorted(self._profiles.items()):
            if pid not in order:
                ordered.append(prof)
        return ordered

    def get(self, profile_id: str) -> PersonalityProfile:
        clean = profile_id.strip().lower()
        if clean in self._profiles:
            return self._profiles[clean]
        return self._profiles["raphael"]

    def find_by_name_or_alias(self, query: str) -> PersonalityProfile | None:
        needle = query.strip().lower()
        for prof in self._profiles.values():
            if needle == prof.id or needle == prof.name.lower() or needle in prof.wake_words:
                return prof
        return None

    def save_profile(self, profile: PersonalityProfile) -> PersonalityProfile:
        clean_id = profile.id.strip().lower()
        if not _PROFILE_ID_RE.match(clean_id):
            raise ValueError(f"ID de perfil inválido: {profile.id!r}")
        self._paths.ensure_user_dirs()
        target = self._paths.profiles_dir / f"{clean_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
        self._profiles[clean_id] = profile
        return profile

    def delete_custom_profile(self, profile_id: str) -> bool:
        clean_id = profile_id.strip().lower()
        if clean_id in {"raphael", "jarvis", "friday", "magi", "glados"}:
            return False
        target = self._paths.profiles_dir / f"{clean_id}.json"
        if target.is_file():
            target.unlink()
        return self._profiles.pop(clean_id, None) is not None

    def compose_system_prompt(
        self,
        *,
        profile_id: str,
        tone: str,
        user_name: str = "",
        city: str = "",
        country: str = "",
        memory_context: str = "",
        privacy_mode: bool = False,
    ) -> str:
        profile = self.get(profile_id)
        tone_desc = AVAILABLE_TONES.get(tone.strip().lower(), AVAILABLE_TONES["equilibrado"])
        now_local = datetime.now().strftime("%Y-%m-%d %H:%M")

        sections: list[str] = [
            profile.system_prompt.strip(),
            f"Tono solicitado ({tone}): {tone_desc}",
            f"Fecha y hora local del PC: {now_local}.",
            (
                "Reglas operativas: responde en español natural, sin relleno ni markdown excesivo "
                "si el mensaje es breve. Si una herramienta está disponible para cumplir una orden, "
                "úsala en vez de inventar que la ejecutaste."
            ),
        ]

        if not privacy_mode:
            user_facts: list[str] = []
            if user_name.strip():
                user_facts.append(f"Usuario: {user_name.strip()}")
            loc_parts = [p.strip() for p in (city, country) if p.strip()]
            if loc_parts:
                user_facts.append(f"Ubicación configurada: {', '.join(loc_parts)}")
            if user_facts:
                sections.append("Datos básicos del usuario:\n- " + "\n- ".join(user_facts))
            if memory_context.strip():
                sections.append(memory_context.strip())
        else:
            sections.append("Modo Privacidad ACTIVO: no guardes ni asumas datos personales sensibles.")

        return "\n\n".join(sections)
