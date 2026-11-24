from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
import re
from typing import Final
import unicodedata

# ----------------------------------------------------------------------
# Variantes fonéticas curadas y probadas en producción
# ----------------------------------------------------------------------
CURATED_WAKE_VARIANTS: Final[dict[str, tuple[str, ...]]] = {
    "raphael": (
        "raphael",
        "rafael",
        "rafa",
        "rafel",
        "raquel",
        "rapael",
        "jafael",
        "afael",
        "rafaelo",
        "farfael",
        "grafael",
        "rafal",
        "rafai",
        "rafita",
        "rafaele",
        "rafae",
    ),
    "rafael": (
        "raphael",
        "rafael",
        "rafa",
        "rafel",
        "raquel",
        "rapael",
        "jafael",
        "afael",
        "rafaelo",
        "farfael",
        "grafael",
        "rafal",
        "rafai",
        "rafita",
        "rafaele",
        "rafae",
    ),
    "jarvis": (
        "jarvis",
        "davis",
        "yarvis",
        "harvis",
        "charvis",
        "garvis",
        "darvis",
        "llarvis",
        "yerbis",
        "jervis",
        "service",
        "chavis",
        "javi",
        "yavi",
        "travis",
        "harvey",
        "jarvi",
        "yarvi",
    ),
    "friday": ("friday", "viernes", "fraidei", "fridei"),
    "magi": ("magi", "mayi", "maji"),
    "glados": ("glados", "gladys", "clados"),
}

_COMMON_BARGEIN_WORDS: Final[tuple[str, ...]] = (
    "para",
    "espera",
    "detente",
    "silencio",
    "basta",
    "callate",
    "cállate",
    "alto",
    "cancela",
    "[unk]",
)

_LEADING_FILLER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?:oye|hey|eh|hola|por\s+favor|disculpa|ok|porfa)\s+)+",
    re.IGNORECASE,
)

_KNOWN_BRANDS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(abre|cierra|pon|ejecuta|inicia)\s+"
    r"(?:(?:el|la|los|las)\s+|"
    r"(?:la\s+aplicaci[oó]n\s+de|la\s+app\s+de|el\s+programa\s+de)\s+)"
    r"(spotify|youtube|discord|whatsapp|steam|chrome|opera|edge|xbox|chromecast|netflix|tidal|obs)\b",
    re.IGNORECASE,
)

_COURTESY_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:por\s+favor\s+)?(?:me\s+)?(?:puedes|podr[ií]as|quiero\s+que|necesito\s+que)\s+"
    r"(apagar|encender|prender|abrir|cerrar|subir|bajar|poner|silenciar|activar|desactivar)\b",
    re.IGNORECASE,
)

_VERB_INFINITIVE_TO_IMPERATIVE: Final[dict[str, str]] = {
    "apagar": "apaga",
    "encender": "enciende",
    "prender": "enciende",
    "abrir": "abre",
    "cerrar": "cierra",
    "subir": "sube",
    "bajar": "baja",
    "poner": "pon",
    "silenciar": "silencia",
    "activar": "activa",
    "desactivar": "desactiva",
}

_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
_POLITE_TAIL_RE: Final[re.Pattern[str]] = re.compile(
    r",?\s*(?:por\s+favor|gracias|muchas\s+gracias)\s*[\.?!,;:]*$",
    re.IGNORECASE,
)
_TRAILING_PUNCT_RE: Final[re.Pattern[str]] = re.compile(r"[\s\.,;:!?¿¡]+$")
_WHISPER_CON_PON_RE: Final[re.Pattern[str]] = re.compile(
    r"^con\s+(youtube|netflix|spotify|disney|prime|twitch)\b",
    re.IGNORECASE,
)

_VOCATIVE_INTERJECTIONS: Final[frozenset[str]] = frozenset(
    {
        "hola", "oye", "hey", "ey", "buenas", "buenos", "dias", "días",
        "tardes", "noches", "ok", "okay", "bueno", "eh", "mmm", "disculpa",
        "perdon", "perdón", "por", "favor", "che", "mira", "escucha",
    }
)

_INDIRECT_PREPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        "a", "de", "del", "sobre", "con", "para", "por", "el", "al", "ese", "este", "mi", "tu", "un",
    }
)


def is_third_person_wake_mention(transcript: str, wake_variants: frozenset[str]) -> bool:
    """
    Analiza la frase completa ANTES de recortar la wake-word para detectar si el usuario
    estaba hablando SOBRE el asistente en tercera persona en lugar de dirigirse a él.
    Retorna True si debe ser descartado (falso positivo).
    """
    clean = strip_accents(transcript.lower().strip())
    if not clean:
        return False

    words = re.findall(r"\b[a-z0-9]+\b", clean)
    if not words:
        return False

    norm_variants = {strip_accents(v.lower().strip()) for v in wake_variants if v.strip()}
    if not norm_variants:
        norm_variants = {"raphael", "rafael", "rafa"}

    wake_indices: list[int] = [i for i, w in enumerate(words) if w in norm_variants]
    if not wake_indices:
        for i in range(len(words) - 1):
            pair = f"{words[i]} {words[i+1]}"
            if pair in norm_variants:
                wake_indices.append(i)
                break
        if not wake_indices:
            return False

    wake_idx = wake_indices[0]

    # 1. Precedida por preposición / artículo de referencia indirecta
    # (ej: "modificando a Rafael", "hablando de Rafael", "con Rafael", "para Rafael")
    if wake_idx > 0 and words[wake_idx - 1] in _INDIRECT_PREPOSITIONS:
        return True

    # 2. Wake-word al final absoluto como vocativo de una orden corta
    # (ej: "apaga la tele rafael")
    is_terminal_vocative = (wake_idx >= len(words) - 2) and len(words) <= 7
    if is_terminal_vocative:
        return False

    # 3. Más de 2 palabras previas que NO son saludos ni interjecciones vocativas
    # (ej: "No, estoy modificando a Rafael...")
    words_before = words[:wake_idx]
    non_vocative_before = [w for w in words_before if w not in _VOCATIVE_INTERJECTIONS]
    return len(non_vocative_before) >= 2


def strip_accents(text: str) -> str:
    """Elimina tildes del español pero preserva la 'ñ' y 'Ñ'."""
    t = text.replace("ñ", "\x00").replace("Ñ", "\x01")
    decomposed = unicodedata.normalize("NFD", t)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.replace("\x00", "ñ").replace("\x01", "Ñ")


def clean_brand_articles(text: str) -> str:
    """
    Elimina artículos directos (el/la/los/las) colados antes de nombres de marcas/apps conocidas:
    'abre el spotify' -> 'abre spotify'
    'cierra la discord' -> 'cierra discord'
    Preserva frases legítimas como 'abre el navegador', 'enciende la tele', 'sube el volumen'.
    """
    return _KNOWN_BRANDS_RE.sub(r"\1 \2", text)


def generate_phonetic_variants(names: Iterable[str]) -> frozenset[str]:
    """
    Genera un conjunto robusto de variantes fonéticas en español para los nombres dados,
    combinando tablas curadas con reglas fonológicas (b/v, c/s/z, j/g/y/ll, f/p/j, ael->el, etc.).
    """
    variants: set[str] = set()
    for raw in names:
        base = strip_accents(raw.strip().lower())
        if not base:
            continue
        variants.add(base)

        # 1. Variantes curadas si el nombre coincide con un perfil conocido
        for key, curated in CURATED_WAKE_VARIANTS.items():
            if base == key or base in curated:
                variants.update(curated)

        # 2. Reglas fonológicas en español
        f_cand = base.replace("ph", "f")
        variants.add(f_cand)

        for w in list(variants):
            # b / v interchange
            if "v" in w:
                variants.add(w.replace("v", "b"))
            if "b" in w:
                variants.add(w.replace("b", "v"))

            # z / c / s interchange
            if "z" in w:
                variants.add(w.replace("z", "s"))
                variants.add(w.replace("z", "c"))
            if "c" in w:
                variants.add(w.replace("c", "s"))

            # j / g / y / ll
            if "j" in w:
                variants.add(w.replace("j", "g"))
                variants.add(w.replace("j", "y"))
            if "y" in w:
                variants.add(w.replace("y", "ll"))
                variants.add(w.replace("y", "j"))

            # f / p
            if "f" in w:
                variants.add(w.replace("f", "p"))

            # Silenciosa 'h' inicial
            if w.startswith("h"):
                variants.add(w[1:])
            else:
                variants.add("h" + w)

            # Terminaciones en 'ael' -> 'el', 'al', 'ai', 'ae', 'a'
            if w.endswith("ael") and len(w) > 3:
                stem = w[:-3]
                variants.add(stem + "el")
                variants.add(stem + "al")
                variants.add(stem + "ai")
                variants.add(stem + "ae")
                variants.add(stem + "a")
                variants.add(stem + " el")

            # Apócope
            if len(w) >= 5 and w.endswith("el"):
                variants.add(w[:-2])  # rafael -> rafa
            if len(w) >= 6 and w.endswith("is"):
                variants.add(w[:-2])  # jarvis -> jarv -> javi

    # Limpieza final: solo palabras no vacías
    return frozenset(v.strip() for v in variants if v.strip())


@dataclass(slots=True)
class WakePhoneticMatcher:
    """
    Motor fonético camaleónico portado de Raphael:
    - Sets curados para perfiles Raphael, Jarvis, Friday, Magi, GLaDOS.
    - Detección en Vosk con pares de palabras contiguas ("rafa el", "hollar vis").
    - Distinción entre detección exacta y difusa.
    - Extracción de orden post-wake sobre transcripción limpia.
    - Construcción de gramática cerrada para barge-in Kaldi.
    """

    primary_wake_words: tuple[str, ...] = ("raphael", "deusex")
    _exact_set: set[str] = field(init=False, default_factory=set)
    _all_variants: frozenset[str] = field(init=False, default_factory=frozenset)
    _re_exact: re.Pattern[str] = field(init=False)
    _re_fuzzy: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        exacts: set[str] = set()
        for w in self.primary_wake_words:
            nw = strip_accents(w.strip().lower())
            if nw:
                exacts.add(nw)
                exacts.add(nw.replace("ph", "f"))
                if nw == "raphael" or nw == "rafael":
                    exacts.add("rafa")
                    exacts.add("rafa el")
                elif nw == "deusex":
                    exacts.add("deus ex")

        self._exact_set = exacts
        self._all_variants = generate_phonetic_variants(self.primary_wake_words)

        exact_alts = sorted(self._exact_set, key=len, reverse=True)
        fuzzy_alts = sorted(self._all_variants, key=len, reverse=True)

        self._re_exact = re.compile(
            rf"\b(?:{'|'.join(re.escape(a) for a in exact_alts)})\b",
            re.IGNORECASE,
        )
        self._re_fuzzy = re.compile(
            rf"\b(?:{'|'.join(re.escape(a) for a in fuzzy_alts)})\b",
            re.IGNORECASE,
        )

    @property
    def variants(self) -> frozenset[str]:
        return self._all_variants

    def detect_in_vosk(self, text: str) -> tuple[bool, bool]:
        """
        Analiza texto parcial o final de Vosk.
        Retorna `(detectado, solo_difuso)`:
        - `(True, False)` si coincide con la wake-word exacta/primaria.
        - `(True, True)` si solo coincide con una variante difusa/aproximada.
        - `(False, False)` si no hay coincidencia.
        """
        clean = strip_accents(text.lower().strip())
        if not clean:
            return False, False

        # 1. Comprobación exacta con regex
        if self._re_exact.search(clean) is not None:
            return True, False

        # 2. Comprobación difusa directa con regex
        if self._re_fuzzy.search(clean) is not None:
            return True, True

        # 3. Comprobación de pares de palabras contiguas (ej: "rafa el", "hollar vis")
        words = clean.split()
        if len(words) >= 2:
            for i in range(len(words) - 1):
                pair_spaced = f"{words[i]} {words[i + 1]}"
                pair_joined = f"{words[i]}{words[i + 1]}"

                if pair_spaced in self._exact_set or pair_joined in self._exact_set:
                    return True, False
                if pair_spaced in self._all_variants or pair_joined in self._all_variants:
                    return True, True

        return False, False

    def extract_command_post_wake(
        self,
        transcript: str,
        *,
        fuzzy_only: bool = False,
        rms_int16: float = 0.0,
        noise_floor: float = 120.0,
    ) -> tuple[bool, str]:
        """
        Extrae la orden recortando el nombre del asistente de la transcripción limpia.
        Si `fuzzy_only` es True y el STT de alta precisión confirma que NO se llamó
        al asistente, retorna `(False, "")` para descartar el falso positivo.
        """
        normalized = self.normalize_stt_text(transcript)
        if not normalized:
            return False, ""

        norm_lower = strip_accents(normalized.lower())

        # Si fue una activación solo difusa en Vosk y el STT de calidad no contiene
        # ninguna variante de wake word, descartar falsa alarma (ej. TV/música de fondo)
        has_exact = self._re_exact.search(norm_lower) is not None
        has_fuzzy = self._re_fuzzy.search(norm_lower) is not None
        if fuzzy_only and not (has_exact or has_fuzzy):
            return False, ""

        # Recortar todas las repeticiones consecutivas de la wake-word al inicio
        # (ej: "Rafael Rafael Rafael confirmo" -> "confirmo")
        curr_norm = normalized
        while True:
            sub = curr_norm.strip(" ,:.-_¿?¡!")
            sub_lower = strip_accents(sub.lower())
            m = self._re_exact.match(sub_lower)
            if m is None:
                m = self._re_fuzzy.match(sub_lower)
            if m is not None:
                curr_norm = sub[m.end():]
            else:
                break

        remainder = curr_norm.strip(" ,:.-_¿?¡!")

        # Si no coincidió al inicio con match(), pero contenía la wake-word en otra posición
        if remainder == normalized.strip(" ,:.-_¿?¡!") and (has_exact or has_fuzzy):
            match = self._re_exact.search(norm_lower) or self._re_fuzzy.search(norm_lower)
            if match is not None:
                remainder = normalized[match.end():].strip(" ,:.-_¿?¡!")

        # Limpiar puntuación inicial, saludos y muletillas
        remainder = _LEADING_FILLER_RE.sub("", remainder).strip(" ,:.-_¿?¡!")
        remainder = self.normalize_stt_text(remainder)

        return True, remainder

    def build_bargein_grammar_json(self) -> str:
        """
        Genera gramática JSON cerrada para KaldiRecognizer en Vosk durante TTS.
        Incluye palabras de interrupción y nombres del asistente.
        """
        grammar_list: list[str] = list(_COMMON_BARGEIN_WORDS)
        for w in self.primary_wake_words:
            nw = strip_accents(w.strip().lower())
            if nw and nw not in grammar_list:
                grammar_list.append(nw)
            base = nw.replace("ph", "f")
            if base and base not in grammar_list:
                grammar_list.append(base)
            if base.endswith("el") and len(base) > 2:
                short = base[:-2]
                if short and short not in grammar_list:
                    grammar_list.append(short)

        return json.dumps(grammar_list, ensure_ascii=False)

    def normalize_stt_text(self, text: str) -> str:
        """Limpia repeticiones, espacios múltiples, cortesías finales y artículos colados."""
        cleaned = _WHITESPACE_RE.sub(" ", text.strip())
        if not cleaned:
            return ""

        # Quitar cortesías y muletillas iniciales (ej: 'por favor enciende la tele' -> 'enciende la tele')
        cleaned = _LEADING_FILLER_RE.sub("", cleaned).strip()

        # Normalizar prefijos de cortesía (ej: 'puedes apagar la tele' -> 'apaga la tele')
        def _replace_courtesy(m: re.Match[str]) -> str:
            verb = m.group(1).lower()
            return _VERB_INFINITIVE_TO_IMPERATIVE.get(verb, verb)

        cleaned = _COURTESY_PREFIX_RE.sub(_replace_courtesy, cleaned)

        # Quitar cortesías finales opcionales (ej: 'cierra Spotify, por favor.' -> 'cierra Spotify')
        cleaned = _POLITE_TAIL_RE.sub("", cleaned).strip()
        # Quitar puntuación final suelta
        cleaned = _TRAILING_PUNCT_RE.sub("", cleaned).strip()

        # Corregir desliz común de Whisper: '^con (youtube|...)' -> 'pon \1' cuando menciona la tele/tv
        lower_txt = cleaned.lower()
        if "tele" in lower_txt or "tv" in lower_txt:
            cleaned = _WHISPER_CON_PON_RE.sub(r"pon \1", cleaned)

        # Eliminar repeticiones consecutivas de palabras (ej. "abre abre spotify" -> "abre spotify")
        words = cleaned.split()
        deduped: list[str] = []
        for w in words:
            if not deduped or deduped[-1].lower() != w.lower():
                deduped.append(w)

        reconstructed = " ".join(deduped)
        cleaned = clean_brand_articles(reconstructed)
        return _TRAILING_PUNCT_RE.sub("", cleaned).strip()
