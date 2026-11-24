from __future__ import annotations

import re
from typing import Final

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from deusex.ui.orb.renderers import parse_accent_color

MAX_SUBTITLE_WORDS: Final[int] = 14
MAX_SUBTITLE_CHARS: Final[int] = 85
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def chunk_subtitle_blocks(
    text: str,
    *,
    max_words: int = MAX_SUBTITLE_WORDS,
    max_chars: int = MAX_SUBTITLE_CHARS,
) -> list[str]:
    """
    Segmenta el texto hablado en bloques secuenciales de máximo `max_words` palabras
    y máximo `max_chars` caracteres para lectura instantánea en `SubtitleHolobox`.
    """
    normalized = _SPACE_RE.sub(" ", text.strip())
    if not normalized:
        return []

    word_limit = max(1, int(max_words))
    char_limit = max(8, int(max_chars))

    blocks: list[str] = []
    current_words: list[str] = []
    current_len = 0

    for raw_word in normalized.split(" "):
        word = raw_word
        if len(word) > char_limit:
            word = word[: char_limit - 3] + "..."

        projected_len = len(word) if not current_words else current_len + 1 + len(word)
        if current_words and (len(current_words) >= word_limit or projected_len > char_limit):
            blocks.append(" ".join(current_words))
            current_words = [word]
            current_len = len(word)
        else:
            current_words.append(word)
            current_len = projected_len

    if current_words:
        blocks.append(" ".join(current_words))
    return blocks


def active_subtitle_block(
    text: str,
    *,
    max_words: int = MAX_SUBTITLE_WORDS,
    max_chars: int = MAX_SUBTITLE_CHARS,
) -> str:
    """Devuelve el bloque visible actual (el último bloque en curso) cumpliendo los topes."""
    blocks = chunk_subtitle_blocks(text, max_words=max_words, max_chars=max_chars)
    return blocks[-1] if blocks else ""


class SubtitleHolobox(QWidget):
    """
    Superficie flotante click-through para subtítulos palabra por palabra sincronizados con TTS.
    - Máximo 14 palabras u 85 caracteres visibles simultáneamente.
    - Oculto en reposo y estrictamente oculto en Modo Privacidad.
    """

    def __init__(
        self,
        *,
        accent_hex: str = "#38BDF8",
        auto_hide_ms: int = 1_800,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._accent = parse_accent_color(accent_hex)
        self._auto_hide_ms = max(400, int(auto_hide_ms))
        self._privacy_mode = False
        self._display_text = ""

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.clear_subtitle)

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._font = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
        self.setFixedSize(640, 62)
        self.hide()

    @property
    def current_text(self) -> str:
        return self._display_text

    def set_accent_color(self, accent_hex: str) -> None:
        self._accent = parse_accent_color(accent_hex)
        if self.isVisible():
            self.update()

    def set_privacy_mode(self, enabled: bool) -> None:
        self._privacy_mode = bool(enabled)
        if self._privacy_mode:
            self.clear_subtitle()

    def reposition(self) -> None:
        """Ubica el Holobox centrado en la parte inferior del monitor principal."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        box_w = min(680, max(360, avail.width() - 220))
        self.setFixedSize(box_w, 64)
        x = avail.x() + (avail.width() - self.width()) // 2
        y = avail.y() + avail.height() - self.height() - 34
        self.move(x, y)

    def update_subtitle(
        self,
        text: str,
        *,
        word: str = "",
        is_final: bool = False,
        clear: bool = False,
    ) -> None:
        """Actualiza el texto visible respetando el tope de 14 palabras / 85 caracteres."""
        if clear or self._privacy_mode:
            self.clear_subtitle()
            return

        source_text = text.strip() if text.strip() else word.strip()
        block = active_subtitle_block(
            source_text,
            max_words=MAX_SUBTITLE_WORDS,
            max_chars=MAX_SUBTITLE_CHARS,
        )
        if not block:
            self.clear_subtitle()
            return

        self._display_text = block
        self._hide_timer.stop()
        self.reposition()
        if not self.isVisible():
            self.show()
        self.update()

        if is_final:
            self._hide_timer.start(self._auto_hide_ms)

    def clear_subtitle(self) -> None:
        self._hide_timer.stop()
        self._display_text = ""
        if self.isVisible():
            self.hide()

    def prepare_for_exit(self) -> None:
        self._hide_timer.stop()
        self._display_text = ""
        self.hide()

    def paintEvent(self, _event: QPaintEvent) -> None:
        if not self._display_text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(4.0, 4.0, float(self.width() - 8), float(self.height() - 8))
        bg = QColor("#0B1220")
        bg.setAlpha(224)
        border = QColor(self._accent)
        border.setAlpha(185)

        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(border, 1.5))
        painter.drawRoundedRect(rect, 18.0, 18.0)

        painter.setFont(self._font)
        painter.setPen(QColor("#F8FAFC"))
        text_rect = rect.adjusted(18.0, 6.0, -18.0, -6.0)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            self._display_text,
        )
        painter.end()
