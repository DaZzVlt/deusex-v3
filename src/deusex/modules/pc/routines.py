from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal
import uuid

from deusex.core.db import Database
from deusex.modules.pc.apps import AppScanner, close_application, normalize_text
from deusex.modules.pc.audio_ctrl import AudioBrightnessController
from deusex.modules.pc.windows import WindowManager

logger = logging.getLogger(__name__)

RoutineStepKind = Literal[
    "open_app",
    "close_app",
    "set_volume",
    "set_mute",
    "set_app_volume",
    "set_brightness",
    "move_window",
    "maximize_window",
    "minimize_window",
    "delay_ms",
]

_EXCLUDED_CAPTURE_PROCESSES: frozenset[str] = frozenset(
    {
        "deusex.exe",
        "python.exe",
        "pythonw.exe",
        "applicationframehost.exe",
        "textinputhost.exe",
        "shellexperiencehost.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
    }
)


@dataclass(frozen=True, slots=True)
class RoutineStep:
    kind: RoutineStepKind
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": dict(self.params)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RoutineStep:
        kind = str(raw.get("kind") or "").strip()
        params = raw.get("params")
        if not isinstance(params, dict):
            params = {}
        return cls(kind=kind, params=dict(params))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RoutineRecord:
    id: str
    name: str
    trigger_phrase: str
    icon: str
    steps: tuple[RoutineStep, ...]
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RoutineExecutionResult:
    ok: bool
    routine_name: str
    summary: str
    executed_steps: int
    total_steps: int
    step_logs: tuple[str, ...]
    dry_run: bool = False


def describe_step_es(step: RoutineStep) -> str:
    """Devuelve una descripción legible en español de un paso de rutina."""
    p = step.params
    if step.kind == "open_app":
        return f"Abrir {p.get('app', 'app')}"
    if step.kind == "close_app":
        return f"Cerrar {p.get('app', 'app')}"
    if step.kind == "set_volume":
        return f"Volumen maestro al {p.get('level_pct', 50)}%"
    if step.kind == "set_mute":
        return "Silenciar audio" if p.get("muted", True) else "Activar audio"
    if step.kind == "set_app_volume":
        return f"Volumen de {p.get('app', 'app')} al {p.get('level_pct', 50)}%"
    if step.kind == "set_brightness":
        return f"Brillo al {p.get('level_pct', 70)}%"
    if step.kind == "move_window":
        mon = int(p.get("monitor_index", 0)) + 1
        return f"Mover '{p.get('app', 'ventana')}' al monitor {mon}"
    if step.kind == "maximize_window":
        return f"Maximizar '{p.get('app', 'ventana')}'"
    if step.kind == "minimize_window":
        return f"Minimizar '{p.get('app', 'ventana')}'"
    if step.kind == "delay_ms":
        return f"Esperar {p.get('ms', 300)} ms"
    return step.kind


class RoutineManager:
    """Gestor de Modos y Rutinas persistido en SQLite WAL (`routines`) con captura de estado."""

    def __init__(
        self,
        db: Database,
        apps: AppScanner,
        audio: AudioBrightnessController,
        windows: WindowManager,
    ) -> None:
        self._db = db
        self._apps = apps
        self._audio = audio
        self._windows = windows

    @staticmethod
    def _row_to_record(row: Any) -> RoutineRecord:
        raw_steps = json.loads(str(row["steps_json"] or "[]"))
        steps = tuple(
            RoutineStep.from_dict(item)
            for item in raw_steps
            if isinstance(item, dict) and item.get("kind")
        )
        return RoutineRecord(
            id=str(row["id"]),
            name=str(row["name"]),
            trigger_phrase=str(row["trigger_phrase"] or ""),
            icon=str(row["icon"] or "⚡"),
            steps=steps,
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_routines(self, *, include_disabled: bool = True) -> list[RoutineRecord]:
        sql = (
            "SELECT id, name, trigger_phrase, icon, steps_json, enabled, created_at, updated_at "
            "FROM routines "
        )
        if not include_disabled:
            sql += "WHERE enabled = 1 "
        sql += "ORDER BY updated_at DESC, name ASC"
        return [self._row_to_record(row) for row in self._db.fetchall(sql)]

    def get_routine(self, routine_id_or_name: str) -> RoutineRecord | None:
        key = routine_id_or_name.strip()
        if not key:
            return None
        row = self._db.fetchone(
            """
            SELECT id, name, trigger_phrase, icon, steps_json, enabled, created_at, updated_at
            FROM routines
            WHERE id = ? OR lower(name) = lower(?)
            LIMIT 1
            """,
            (key, key),
        )
        if row is not None:
            return self._row_to_record(row)

        norm_key = normalize_text(key)
        for rec in self.list_routines(include_disabled=True):
            if normalize_text(rec.name) == norm_key:
                return rec
            if rec.trigger_phrase and normalize_text(rec.trigger_phrase) == norm_key:
                return rec
        return None

    def save_routine(
        self,
        *,
        name: str,
        steps: list[RoutineStep],
        trigger_phrase: str = "",
        icon: str = "⚡",
        enabled: bool = True,
        routine_id: str | None = None,
    ) -> RoutineRecord:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("El nombre de la rutina no puede estar vacío.")
        now_iso = datetime.now(UTC).isoformat()
        steps_json = json.dumps([s.to_dict() for s in steps], ensure_ascii=False)
        clean_trigger = trigger_phrase.strip() or clean_name.lower()
        clean_icon = icon.strip() or "⚡"

        existing = self.get_routine(routine_id or clean_name)
        if existing is not None:
            rid = existing.id
            created_at = existing.created_at
            self._db.execute(
                """
                UPDATE routines
                SET name = ?, trigger_phrase = ?, icon = ?, steps_json = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    clean_name,
                    clean_trigger,
                    clean_icon,
                    steps_json,
                    1 if enabled else 0,
                    now_iso,
                    rid,
                ),
            )
        else:
            rid = routine_id or uuid.uuid4().hex
            created_at = now_iso
            self._db.execute(
                """
                INSERT INTO routines
                    (id, name, trigger_phrase, icon, steps_json, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    clean_name,
                    clean_trigger,
                    clean_icon,
                    steps_json,
                    1 if enabled else 0,
                    created_at,
                    now_iso,
                ),
            )

        return RoutineRecord(
            id=rid,
            name=clean_name,
            trigger_phrase=clean_trigger,
            icon=clean_icon,
            steps=tuple(steps),
            enabled=enabled,
            created_at=created_at,
            updated_at=now_iso,
        )

    def delete_routine(self, routine_id: str) -> bool:
        deleted = self._db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))
        return deleted > 0

    def set_enabled(self, routine_id: str, enabled: bool) -> bool:
        now_iso = datetime.now(UTC).isoformat()
        updated = self._db.execute(
            "UPDATE routines SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now_iso, routine_id),
        )
        return updated > 0

    async def capture_current_state(
        self,
        *,
        name: str,
        trigger_phrase: str = "",
        icon: str = "📸",
        save: bool = True,
    ) -> RoutineRecord:
        """Captura volumen maestro/por app y distribución de ventanas/monitores actuales."""
        steps: list[RoutineStep] = []

        # 1. Audio (maestro + por app)
        with suppress(Exception):
            audio_snap = await self._audio.capture_audio_snapshot()
            master_pct = int(audio_snap.get("master_volume_pct", 50))
            steps.append(RoutineStep(kind="set_volume", params={"level_pct": master_pct}))
            if bool(audio_snap.get("master_muted", False)):
                steps.append(RoutineStep(kind="set_mute", params={"muted": True}))

            app_vols = audio_snap.get("app_volumes")
            if isinstance(app_vols, dict):
                for proc_name, vol_pct in list(app_vols.items())[:6]:
                    low = str(proc_name).lower()
                    if low in _EXCLUDED_CAPTURE_PROCESSES:
                        continue
                    stem = Path(low).stem
                    steps.append(
                        RoutineStep(
                            kind="set_app_volume",
                            params={"app": stem, "level_pct": int(vol_pct)},
                        )
                    )

        # 2. Ventanas abiertas + monitores
        current_pid = os.getpid()
        seen_apps: set[str] = set()
        for win in self._windows.list_windows(include_minimized=False):
            if win.pid == current_pid or not win.process_name:
                continue
            low = win.process_name.lower()
            if low in _EXCLUDED_CAPTURE_PROCESSES:
                continue
            stem = Path(low).stem
            if stem in seen_apps:
                continue
            seen_apps.add(stem)
            steps.append(RoutineStep(kind="open_app", params={"app": stem}))
            steps.append(
                RoutineStep(
                    kind="move_window",
                    params={"app": stem, "monitor_index": int(win.monitor_index)},
                )
            )
            if win.is_maximized:
                steps.append(RoutineStep(kind="maximize_window", params={"app": stem}))
            if len(seen_apps) >= 6:
                break

        if not steps:
            steps.append(RoutineStep(kind="set_volume", params={"level_pct": 40}))

        if save:
            return self.save_routine(
                name=name,
                steps=steps,
                trigger_phrase=trigger_phrase or name.lower(),
                icon=icon,
                enabled=True,
            )

        now_iso = datetime.now(UTC).isoformat()
        return RoutineRecord(
            id=uuid.uuid4().hex,
            name=name.strip() or "Estado capturado",
            trigger_phrase=(trigger_phrase or name).strip().lower(),
            icon=icon,
            steps=tuple(steps),
            enabled=True,
            created_at=now_iso,
            updated_at=now_iso,
        )

    async def execute_routine(
        self,
        routine_id_or_name: str,
        *,
        dry_run: bool = False,
    ) -> RoutineExecutionResult:
        record = self.get_routine(routine_id_or_name)
        if record is None:
            return RoutineExecutionResult(
                ok=False,
                routine_name=routine_id_or_name,
                summary=f"No encontré ninguna rutina llamada '{routine_id_or_name}'.",
                executed_steps=0,
                total_steps=0,
                step_logs=(),
                dry_run=dry_run,
            )

        if not record.enabled:
            return RoutineExecutionResult(
                ok=False,
                routine_name=record.name,
                summary=f"La rutina '{record.name}' está desactivada.",
                executed_steps=0,
                total_steps=len(record.steps),
                step_logs=(),
                dry_run=dry_run,
            )

        logs: list[str] = []
        ok_count = 0

        for step in record.steps:
            step_ok, msg = await self._execute_step(step, dry_run=dry_run)
            logs.append(msg)
            if step_ok:
                ok_count += 1

        prefix = "[Dry-Run] " if dry_run else ""
        summary = (
            f"{prefix}Rutina '{record.name}' ejecutada "
            f"({ok_count}/{len(record.steps)} pasos completados)."
        )
        return RoutineExecutionResult(
            ok=ok_count > 0 or len(record.steps) == 0,
            routine_name=record.name,
            summary=summary,
            executed_steps=ok_count,
            total_steps=len(record.steps),
            step_logs=tuple(logs),
            dry_run=dry_run,
        )

    async def _execute_step(self, step: RoutineStep, *, dry_run: bool) -> tuple[bool, str]:
        p = step.params
        try:
            if step.kind == "open_app":
                app_name = str(p.get("app") or "").strip()
                res = await asyncio.to_thread(self._apps.launch_app, app_name, dry_run=dry_run)
                return res.ok, res.summary

            if step.kind == "close_app":
                app_name = str(p.get("app") or "").strip()
                ok, msg, _ = await asyncio.to_thread(
                    close_application,
                    app_name,
                    allow_productivity=False,
                    dry_run=dry_run,
                    app_scanner=self._apps,
                )
                return ok, msg

            if step.kind == "set_volume":
                level = int(p.get("level_pct", 50))
                ok, msg, _ = await self._audio.set_master_volume(level, dry_run=dry_run)
                return ok, msg

            if step.kind == "set_mute":
                muted = bool(p.get("muted", True))
                return await self._audio.set_master_mute(muted, dry_run=dry_run)

            if step.kind == "set_app_volume":
                app_name = str(p.get("app") or "").strip()
                level = int(p.get("level_pct", 50))
                ok, msg, _ = await self._audio.set_app_volume(app_name, level, dry_run=dry_run)
                return ok, msg

            if step.kind == "set_brightness":
                level = int(p.get("level_pct", 70))
                b_res = await asyncio.to_thread(self._audio.set_brightness, level, dry_run=dry_run)
                return b_res.ok, b_res.summary

            if step.kind == "move_window":
                win_query: str | None = str(p.get("app") or "").strip() or None
                mon_idx = p.get("monitor_index")
                target_idx = int(mon_idx) if mon_idx is not None else None
                return await asyncio.to_thread(
                    self._windows.move_window_to_monitor,
                    query=win_query,
                    target_monitor_index=target_idx,
                    dry_run=dry_run,
                )

            if step.kind == "maximize_window":
                win_query = str(p.get("app") or "").strip() or None
                return await asyncio.to_thread(
                    self._windows.maximize_window,
                    win_query,
                    dry_run=dry_run,
                )

            if step.kind == "minimize_window":
                win_query = str(p.get("app") or "").strip() or None
                return await asyncio.to_thread(
                    self._windows.minimize_window,
                    win_query,
                    dry_run=dry_run,
                )

            if step.kind == "delay_ms":
                ms = max(0, min(10_000, int(p.get("ms", 300))))
                if not dry_run:
                    await asyncio.sleep(ms / 1000.0)
                return True, f"Pausa de {ms} ms."

            return False, f"Paso de rutina desconocido: {step.kind}."
        except Exception as exc:
            logger.warning("Error ejecutando paso de rutina '%s': %s", step.kind, exc)
            return False, f"Error en '{step.kind}': {exc}"
