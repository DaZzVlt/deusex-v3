from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Final

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QLinearGradient,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)

ORB_STYLES: Final[tuple[str, ...]] = (
    "reactor",
    "waves",
    "sphere",
    "bars",
    "avatar",
    "sacred_geometry",
    "jarvis_core",
)
DEFAULT_ACCENT_HEX: Final[str] = "#38BDF8"


@dataclass(frozen=True, slots=True)
class OrbRenderContext:
    """Estado visual inmutable pasado a cada renderer en cada frame."""

    rect: QRectF
    phase: float
    level: float  # Nivel RMS suavizado en [0.0, 1.0]
    state: str  # idle | listening | thinking | speaking | calibrating | preview | privacy
    accent: QColor
    subtle: bool = False


def parse_accent_color(accent_hex: str) -> QColor:
    """Convierte un color hexadecimal a QColor válido con respaldo por defecto."""
    color = QColor(accent_hex.strip()) if accent_hex else QColor()
    if color.isValid():
        return color
    return QColor(DEFAULT_ACCENT_HEX)


def _with_alpha(color: QColor, alpha: int) -> QColor:
    copy = QColor(color)
    copy.setAlpha(max(0, min(255, alpha)))
    return copy


def _state_energy(ctx: OrbRenderContext) -> float:
    base = 0.14 if ctx.subtle else 0.25
    if ctx.state == "thinking":
        return max(0.42, ctx.level)
    if ctx.state in ("listening", "speaking", "calibrating", "preview"):
        return max(0.30, min(1.0, ctx.level + 0.18))
    return max(base, ctx.level * 0.5)


class OrbRenderer(ABC):
    """Contrato común para los 5 estilos intercambiables del Orbe Flotante."""

    style_id: str

    @abstractmethod
    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        """Dibuja un frame del Orbe en el lienzo transparente."""

    def set_avatar_path(self, path: str) -> None:  # noqa: B027
        """Solo relevante para el renderer `avatar`."""

    def set_active(self, active: bool) -> None:  # noqa: B027
        """Notifica visibilidad para pausar animaciones auxiliares (ej. QMovie)."""

    def cleanup(self) -> None:  # noqa: B027
        """Libera recursos gráficos antes de destruir la ventana."""


class ReactorOrbRenderer(OrbRenderer):
    """Estilo 1: Núcleo reactor concéntrico con arcos rotatorios y pulso energético."""

    style_id = "reactor"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.44
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # Halo radial externo reactivo al RMS
        halo_r = radius * (0.95 + 0.18 * energy)
        halo = QRadialGradient(center, halo_r)
        halo.setColorAt(0.0, _with_alpha(ctx.accent, round(135 * alpha_scale)))
        halo.setColorAt(0.55, _with_alpha(ctx.accent, round(55 * alpha_scale)))
        halo.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, halo_r, halo_r)

        # Anillo exterior segmentado en rotación
        outer_r = radius * (0.80 + 0.06 * energy)
        outer_rect = QRectF(
            center.x() - outer_r,
            center.y() - outer_r,
            outer_r * 2.0,
            outer_r * 2.0,
        )
        pen_outer = QPen(_with_alpha(ctx.accent, round(220 * alpha_scale)), 2.6)
        pen_outer.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_outer)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        base_deg = (ctx.phase * (95.0 if ctx.state == "thinking" else 55.0)) % 360.0
        for idx in range(3):
            start_deg = base_deg + idx * 120.0
            painter.drawArc(outer_rect, round(start_deg * 16.0), round(74.0 * 16.0))

        # Anillo interno contra-rotatorio
        mid_r = radius * (0.58 + 0.08 * energy)
        mid_rect = QRectF(
            center.x() - mid_r,
            center.y() - mid_r,
            mid_r * 2.0,
            mid_r * 2.0,
        )
        lighter = ctx.accent.lighter(135)
        pen_mid = QPen(_with_alpha(lighter, round(200 * alpha_scale)), 2.0)
        pen_mid.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_mid)
        rev_deg = (-ctx.phase * 72.0) % 360.0
        for idx in range(4):
            painter.drawArc(mid_rect, round((rev_deg + idx * 90.0) * 16.0), round(42.0 * 16.0))

        # Núcleo central pulsante
        core_r = radius * (0.28 + 0.14 * energy)
        core_grad = QRadialGradient(center, core_r)
        core_grad.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(240 * alpha_scale)))
        core_grad.setColorAt(0.45, _with_alpha(lighter, round(225 * alpha_scale)))
        core_grad.setColorAt(1.0, _with_alpha(ctx.accent, round(170 * alpha_scale)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(core_grad))
        painter.drawEllipse(center, core_r, core_r)


class WavesOrbRenderer(OrbRenderer):
    """Estilo 2: Ondas sinusoidales concéntricas moduladas en vivo por el nivel RMS."""

    style_id = "waves"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.44
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # Fondo circular translúcido oscuro
        bg_r = radius * 0.90
        bg_grad = QRadialGradient(center, bg_r)
        bg_grad.setColorAt(0.0, _with_alpha(QColor("#0F172A"), round(205 * alpha_scale)))
        bg_grad.setColorAt(0.82, _with_alpha(QColor("#0B1120"), round(180 * alpha_scale)))
        bg_grad.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg_grad))
        painter.drawEllipse(center, bg_r, bg_r)

        # Tres ondas sinusoidales desplazadas en fase
        span_x = radius * 1.44
        start_x = center.x() - span_x * 0.5
        samples = 48
        wave_specs = (
            (1.0, 0.0, 2.6, 235),
            (0.72, 1.3, 2.0, 170),
            (0.48, 2.6, 1.5, 115),
        )

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for amp_mult, phase_off, width, alpha in wave_specs:
            path = QPainterPath()
            for i in range(samples + 1):
                t = i / float(samples)
                x = start_x + span_x * t
                envelope = math.sin(math.pi * t) ** 1.6
                angle = (t * 4.5 * math.pi) - (ctx.phase * 5.2) + phase_off
                y_off = math.sin(angle) * radius * 0.38 * energy * amp_mult * envelope
                pt = QPointF(x, center.y() + y_off)
                if i == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)

            color = ctx.accent.lighter(120) if amp_mult > 0.9 else ctx.accent
            pen = QPen(_with_alpha(color, round(alpha * alpha_scale)), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPath(path)

        # Borde exterior suave
        ring_pen = QPen(_with_alpha(ctx.accent, round(160 * alpha_scale)), 1.8)
        painter.setPen(ring_pen)
        painter.drawEllipse(center, bg_r * 0.96, bg_r * 0.96)


class SphereOrbRenderer(OrbRenderer):
    """Estilo 3: Esfera luminosa 3D con brillo especular y aurora periférica."""

    style_id = "sphere"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.42
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # Aurora exterior
        glow_r = radius * (1.04 + 0.16 * energy)
        glow = QRadialGradient(center, glow_r)
        glow.setColorAt(0.4, _with_alpha(ctx.accent, round(120 * alpha_scale)))
        glow.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(center, glow_r, glow_r)

        # Cuerpo esférico con foco desplazado por fase
        body_r = radius * (0.78 + 0.10 * energy)
        focal = QPointF(
            center.x() - body_r * 0.30 + math.cos(ctx.phase * 1.4) * body_r * 0.12,
            center.y() - body_r * 0.30 + math.sin(ctx.phase * 1.4) * body_r * 0.12,
        )
        sphere = QRadialGradient(center, body_r, focal)
        sphere.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(245 * alpha_scale)))
        sphere.setColorAt(0.32, _with_alpha(ctx.accent.lighter(130), round(230 * alpha_scale)))
        sphere.setColorAt(0.76, _with_alpha(ctx.accent, round(215 * alpha_scale)))
        sphere.setColorAt(1.0, _with_alpha(ctx.accent.darker(210), round(220 * alpha_scale)))
        painter.setBrush(QBrush(sphere))
        painter.drawEllipse(center, body_r, body_r)

        # Anillo cónico sutil alrededor de la esfera
        conical = QConicalGradient(center, (ctx.phase * 85.0) % 360.0)
        conical.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(180 * alpha_scale)))
        conical.setColorAt(0.35, _with_alpha(ctx.accent, round(45 * alpha_scale)))
        conical.setColorAt(0.75, _with_alpha(ctx.accent.lighter(140), round(140 * alpha_scale)))
        conical.setColorAt(1.0, _with_alpha(QColor("#FFFFFF"), round(180 * alpha_scale)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QBrush(conical), 2.2))
        painter.drawEllipse(center, body_r * 1.06, body_r * 1.06)


class BarsOrbRenderer(OrbRenderer):
    """Estilo 4: Ecualizador de barras verticales simétricas reactivo al RMS."""

    style_id = "bars"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.44
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # Base circular oscura limpia
        bg_r = radius * 0.92
        painter.setPen(QPen(_with_alpha(ctx.accent, round(155 * alpha_scale)), 1.8))
        painter.setBrush(QBrush(_with_alpha(QColor("#0F172A"), round(210 * alpha_scale))))
        painter.drawEllipse(center, bg_r, bg_r)

        num_bars = 9
        total_w = radius * 1.24
        step_x = total_w / float(num_bars)
        bar_w = max(3.5, step_x * 0.56)
        max_h = radius * 1.06
        start_x = center.x() - total_w * 0.5 + (step_x - bar_w) * 0.5

        grad = QLinearGradient(
            QPointF(center.x(), center.y() - max_h * 0.5),
            QPointF(center.x(), center.y() + max_h * 0.5),
        )
        grad.setColorAt(0.0, _with_alpha(ctx.accent.lighter(140), round(240 * alpha_scale)))
        grad.setColorAt(0.5, _with_alpha(ctx.accent, round(235 * alpha_scale)))
        grad.setColorAt(1.0, _with_alpha(ctx.accent.darker(135), round(220 * alpha_scale)))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        mid_idx = (num_bars - 1) * 0.5
        for idx in range(num_bars):
            dist = abs(idx - mid_idx) / max(1.0, mid_idx)
            center_weight = 1.0 - 0.52 * dist
            osc = 0.55 + 0.45 * abs(math.sin(ctx.phase * 5.0 + idx * 0.72))
            bar_h = max(bar_w, max_h * energy * center_weight * osc)
            x = start_x + idx * step_x
            y = center.y() - bar_h * 0.5
            painter.drawRoundedRect(QRectF(x, y, bar_w, bar_h), bar_w * 0.5, bar_w * 0.5)


class AvatarOrbRenderer(OrbRenderer):
    """Estilo 5: Avatar circular (imagen estática o GIF animado) con aro reactivo al RMS."""

    style_id = "avatar"

    def __init__(self, avatar_path: str = "") -> None:
        self._avatar_path = ""
        self._pixmap: QPixmap | None = None
        self._movie: QMovie | None = None
        self._active = False
        if avatar_path:
            self.set_avatar_path(avatar_path)

    def _release_media(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        self._pixmap = None

    def set_avatar_path(self, path: str) -> None:
        clean = path.strip()
        if clean == self._avatar_path:
            return
        self._release_media()
        self._avatar_path = clean
        if not clean:
            return
        file_path = Path(clean)
        if not file_path.is_file():
            return

        if file_path.suffix.lower() == ".gif":
            movie = QMovie(str(file_path))
            if movie.isValid():
                self._movie = movie
                if self._active:
                    self._movie.start()
                else:
                    self._movie.jumpToFrame(0)
            else:
                movie.deleteLater()
        else:
            pix = QPixmap(str(file_path))
            if not pix.isNull():
                self._pixmap = pix

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._movie is not None:
            if self._active:
                self._movie.start()
            else:
                self._movie.stop()

    def cleanup(self) -> None:
        self._release_media()

    def _current_pixmap(self) -> QPixmap | None:
        if self._movie is not None:
            pix = self._movie.currentPixmap()
            if not pix.isNull():
                return pix
        return self._pixmap

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.43
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # Halo exterior reactivo
        halo_r = radius * (0.98 + 0.16 * energy)
        halo = QRadialGradient(center, halo_r)
        halo.setColorAt(0.55, _with_alpha(ctx.accent, round(130 * alpha_scale)))
        halo.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, halo_r, halo_r)

        avatar_r = radius * 0.80
        avatar_rect = QRectF(
            center.x() - avatar_r,
            center.y() - avatar_r,
            avatar_r * 2.0,
            avatar_r * 2.0,
        )

        pix = self._current_pixmap()
        if pix is not None:
            painter.save()
            clip = QPainterPath()
            clip.addEllipse(avatar_rect)
            painter.setClipPath(clip)
            if ctx.subtle:
                painter.setOpacity(0.65)
            scaled = pix.scaled(
                round(avatar_rect.width()),
                round(avatar_rect.height()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            sx = (scaled.width() - avatar_rect.width()) * 0.5
            sy = (scaled.height() - avatar_rect.height()) * 0.5
            painter.drawPixmap(
                avatar_rect,
                scaled,
                QRectF(sx, sy, avatar_rect.width(), avatar_rect.height()),
            )
            painter.restore()
        else:
            # Avatar vectorial estilizado de respaldo cuando no se configuró imagen/GIF
            painter.setBrush(QBrush(_with_alpha(QColor("#111827"), round(225 * alpha_scale))))
            painter.drawEllipse(avatar_rect)
            visor_h = avatar_r * (0.28 + 0.18 * energy)
            visor_w = avatar_r * 1.10
            visor_rect = QRectF(
                center.x() - visor_w * 0.5,
                center.y() - visor_h * 0.5,
                visor_w,
                visor_h,
            )
            painter.setBrush(QBrush(_with_alpha(ctx.accent.lighter(125), round(235 * alpha_scale))))
            painter.drawRoundedRect(visor_rect, visor_h * 0.5, visor_h * 0.5)

        # Aro de borde reactivo
        ring_width = 2.4 + 2.4 * energy
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_with_alpha(ctx.accent, round(235 * alpha_scale)), ring_width))
        painter.drawEllipse(avatar_rect)


class SacredGeometryOrbRenderer(OrbRenderer):
    """Estilo 6: Círculo mágico y geometría sagrada (Raphael · Tensura)."""

    style_id = "sacred_geometry"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.45
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # 1. Halo áureo exterior reactivo al RMS
        halo_r = radius * (1.02 + 0.16 * energy)
        halo = QRadialGradient(center, halo_r)
        gold = QColor("#F59E0B")
        halo.setColorAt(0.0, _with_alpha(gold, round(120 * alpha_scale)))
        halo.setColorAt(0.50, _with_alpha(ctx.accent, round(65 * alpha_scale)))
        halo.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, halo_r, halo_r)

        # 2. 24 rayos radiales reactivos al RMS
        ray_inner_r = radius * 0.42
        ray_outer_r = radius * (0.82 + 0.28 * energy)
        ray_pen = QPen(_with_alpha(ctx.accent.lighter(130), round(140 * alpha_scale)), 1.2)
        painter.setPen(ray_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        base_angle = ctx.phase * 0.25
        for i in range(24):
            ang = base_angle + (i * (math.pi / 12.0))
            cos_a = math.cos(ang)
            sin_a = math.sin(ang)
            mod_r = ray_outer_r if (i % 2 == 0) else (ray_inner_r + (ray_outer_r - ray_inner_r) * 0.65)
            p1 = QPointF(center.x() + ray_inner_r * cos_a, center.y() + ray_inner_r * sin_a)
            p2 = QPointF(center.x() + mod_r * cos_a, center.y() + mod_r * sin_a)
            painter.drawLine(p1, p2)

        # 3. Anillos concéntricos y runas procedurales
        outer_circle_r = radius * 0.90
        painter.setPen(QPen(_with_alpha(ctx.accent, round(210 * alpha_scale)), 1.8))
        painter.drawEllipse(center, outer_circle_r, outer_circle_r)
        inner_circle_r = radius * 0.76
        painter.setPen(QPen(_with_alpha(gold, round(180 * alpha_scale)), 1.2))
        painter.drawEllipse(center, inner_circle_r, inner_circle_r)

        # Runas procedurales entre anillos (16 glifos rotatorios)
        rune_pen = QPen(_with_alpha(ctx.accent.lighter(140), round(200 * alpha_scale)), 1.4)
        painter.setPen(rune_pen)
        rune_mid_r = (outer_circle_r + inner_circle_r) * 0.5
        rune_rot = -ctx.phase * 0.35
        for k in range(16):
            ang = rune_rot + k * (2.0 * math.pi / 16.0)
            rx = center.x() + rune_mid_r * math.cos(ang)
            ry = center.y() + rune_mid_r * math.sin(ang)
            gl_size = 3.2 + 1.2 * energy
            gl_type = k % 4
            if gl_type == 0:
                poly = QPolygonF([
                    QPointF(rx, ry - gl_size),
                    QPointF(rx + gl_size, ry),
                    QPointF(rx, ry + gl_size),
                    QPointF(rx - gl_size, ry),
                ])
                painter.drawPolygon(poly)
            elif gl_type == 1:
                painter.drawLine(QPointF(rx - gl_size, ry), QPointF(rx + gl_size, ry))
                painter.drawLine(QPointF(rx, ry - gl_size), QPointF(rx, ry + gl_size))
            elif gl_type == 2:
                c_cos = math.cos(ang)
                c_sin = math.sin(ang)
                tip = QPointF(rx + gl_size * c_cos, ry + gl_size * c_sin)
                base1 = QPointF(rx - gl_size * c_sin, ry + gl_size * c_cos)
                base2 = QPointF(rx + gl_size * c_sin, ry - gl_size * c_cos)
                painter.drawLine(base1, tip)
                painter.drawLine(base2, tip)
            else:
                poly = QPolygonF([
                    QPointF(rx, ry - gl_size),
                    QPointF(rx + gl_size * 0.86, ry + gl_size * 0.5),
                    QPointF(rx - gl_size * 0.86, ry + gl_size * 0.5),
                ])
                painter.drawPolygon(poly)

        # 4. Doble estrella geométrica contra-rotatoria
        star1_r = radius * (0.64 + 0.08 * energy)
        star1_ang = ctx.phase * 0.45
        path_star1 = QPainterPath()
        points_12: list[QPointF] = []
        for i in range(12):
            ang = star1_ang + i * (math.pi / 6.0)
            cur_r = star1_r if (i % 2 == 0) else (star1_r * 0.55)
            points_12.append(QPointF(center.x() + cur_r * math.cos(ang), center.y() + cur_r * math.sin(ang)))
        path_star1.moveTo(points_12[0])
        for pt in points_12[1:]:
            path_star1.lineTo(pt)
        path_star1.closeSubpath()
        painter.setPen(QPen(_with_alpha(gold, round(190 * alpha_scale)), 1.5))
        painter.drawPath(path_star1)

        star2_r = radius * (0.50 + 0.06 * energy)
        star2_ang = -ctx.phase * 0.38
        points_hex: list[QPointF] = []
        for i in range(6):
            ang = star2_ang + i * (math.pi / 3.0)
            points_hex.append(QPointF(center.x() + star2_r * math.cos(ang), center.y() + star2_r * math.sin(ang)))
        for i in range(6):
            p_a = points_hex[i]
            p_b = points_hex[(i + 2) % 6]
            painter.drawLine(p_a, p_b)

        # 5. Dodecágono central luminoso
        dodec_r = radius * (0.34 + 0.05 * energy)
        dodec_ang = ctx.phase * 0.20
        dodec_poly = QPolygonF([
            QPointF(
                center.x() + dodec_r * math.cos(dodec_ang + i * (math.pi / 6.0)),
                center.y() + dodec_r * math.sin(dodec_ang + i * (math.pi / 6.0)),
            )
            for i in range(12)
        ])
        painter.setPen(QPen(_with_alpha(ctx.accent.lighter(150), round(230 * alpha_scale)), 1.8))
        painter.drawPolygon(dodec_poly)

        # 6. Núcleo central radiante
        core_r = radius * (0.20 + 0.12 * energy)
        core_grad = QRadialGradient(center, core_r)
        core_grad.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(255 * alpha_scale)))
        core_grad.setColorAt(0.40, _with_alpha(gold.lighter(130), round(230 * alpha_scale)))
        core_grad.setColorAt(1.0, _with_alpha(ctx.accent, round(140 * alpha_scale)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(core_grad))
        painter.drawEllipse(center, core_r, core_r)


class JarvisCoreOrbRenderer(OrbRenderer):
    """Estilo 7: Núcleo holográfico IA 3D con filamentos neuronales (J.A.R.V.I.S.)."""

    style_id = "jarvis_core"

    def paint(self, painter: QPainter, ctx: OrbRenderContext) -> None:
        center = ctx.rect.center()
        radius = min(ctx.rect.width(), ctx.rect.height()) * 0.45
        energy = _state_energy(ctx)
        alpha_scale = 0.55 if ctx.subtle else 1.0

        # 1. Halo exterior holográfico cian / acento
        halo_r = radius * (1.02 + 0.18 * energy)
        halo = QRadialGradient(center, halo_r)
        cyan = QColor("#06B6D4")
        halo.setColorAt(0.0, _with_alpha(ctx.accent, round(110 * alpha_scale)))
        halo.setColorAt(0.55, _with_alpha(cyan, round(50 * alpha_scale)))
        halo.setColorAt(1.0, _with_alpha(cyan, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(center, halo_r, halo_r)

        # 2. Arcos HUD segmentados exteriores
        hud_r = radius * 0.88
        hud_rect = QRectF(center.x() - hud_r, center.y() - hud_r, hud_r * 2.0, hud_r * 2.0)
        hud_pen = QPen(_with_alpha(cyan, round(180 * alpha_scale)), 1.8)
        hud_pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(hud_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        hud_rot = (ctx.phase * 40.0) % 360.0
        for s in range(4):
            start = hud_rot + s * 90.0
            painter.drawArc(hud_rect, round(start * 16.0), round(60.0 * 16.0))

        # 3. Anillos orbitales elípticos inclinados en 3 ejes (proyección 3D)
        ring_specs = (
            (35.0, 0.45 + 0.25 * math.sin(ctx.phase * 0.8), ctx.phase * 0.9, ctx.accent.lighter(130), 2.2),
            (-45.0, 0.40 + 0.28 * math.cos(ctx.phase * 0.7), -ctx.phase * 0.75, cyan.lighter(125), 1.8),
            (15.0, 0.50 + 0.20 * math.sin(ctx.phase * 0.9 + 1.2), ctx.phase * 0.6, ctx.accent, 1.5),
        )

        nodes: list[QPointF] = []
        for tilt_deg, squish, rot_phase, color, stroke in ring_specs:
            painter.save()
            painter.translate(center)
            painter.rotate(tilt_deg)

            orb_rx = radius * (0.75 + 0.10 * energy)
            orb_ry = orb_rx * max(0.20, min(0.95, squish))
            ellipse_rect = QRectF(-orb_rx, -orb_ry, orb_rx * 2.0, orb_ry * 2.0)

            pen = QPen(_with_alpha(color, round(200 * alpha_scale)), stroke)
            painter.setPen(pen)
            painter.drawEllipse(ellipse_rect)

            node_ang = rot_phase
            nx = orb_rx * math.cos(node_ang)
            ny = orb_ry * math.sin(node_ang)
            rad = math.radians(tilt_deg)
            gx = center.x() + (nx * math.cos(rad) - ny * math.sin(rad))
            gy = center.y() + (nx * math.sin(rad) + ny * math.cos(rad))
            nodes.append(QPointF(gx, gy))

            painter.restore()

        # 4. Nodos orbitales y filamentos neuronales hacia el centro
        for idx, node in enumerate(nodes):
            filament_pen = QPen(
                _with_alpha(cyan.lighter(130), round((100 + 120 * energy) * alpha_scale)),
                1.0 + 0.8 * energy,
            )
            painter.setPen(filament_pen)
            painter.drawLine(node, center)

            next_node = nodes[(idx + 1) % len(nodes)]
            syn_pen = QPen(_with_alpha(ctx.accent, round(80 * alpha_scale)), 0.8)
            painter.setPen(syn_pen)
            painter.drawLine(node, next_node)

            node_r = 3.5 + 2.0 * energy
            node_grad = QRadialGradient(node, node_r)
            node_grad.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(255 * alpha_scale)))
            node_grad.setColorAt(0.5, _with_alpha(cyan.lighter(140), round(220 * alpha_scale)))
            node_grad.setColorAt(1.0, _with_alpha(cyan, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(node_grad))
            painter.drawEllipse(node, node_r, node_r)

        # 5. Núcleo central de plasma J.A.R.V.I.S.
        plasma_r = radius * (0.26 + 0.14 * energy)
        plasma = QRadialGradient(center, plasma_r)
        plasma.setColorAt(0.0, _with_alpha(QColor("#FFFFFF"), round(255 * alpha_scale)))
        plasma.setColorAt(0.35, _with_alpha(cyan.lighter(135), round(235 * alpha_scale)))
        plasma.setColorAt(0.70, _with_alpha(ctx.accent, round(180 * alpha_scale)))
        plasma.setColorAt(1.0, _with_alpha(ctx.accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(plasma))
        painter.drawEllipse(center, plasma_r, plasma_r)

        core_ring_r = plasma_r * 0.75
        painter.setPen(QPen(_with_alpha(QColor("#FFFFFF"), round(210 * alpha_scale)), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, core_ring_r, core_ring_r)


def create_orb_renderer(style: str, *, avatar_path: str = "") -> OrbRenderer:
    """Factoría tipada de los 7 estilos del Orbe Flotante."""
    key = style.strip().lower()
    if key == "waves":
        return WavesOrbRenderer()
    if key == "sphere":
        return SphereOrbRenderer()
    if key == "bars":
        return BarsOrbRenderer()
    if key == "avatar":
        return AvatarOrbRenderer(avatar_path=avatar_path)
    if key == "sacred_geometry":
        return SacredGeometryOrbRenderer()
    if key == "jarvis_core":
        return JarvisCoreOrbRenderer()
    return ReactorOrbRenderer()
