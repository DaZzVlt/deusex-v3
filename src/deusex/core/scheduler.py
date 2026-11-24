from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import re
from typing import Any
import uuid

from deusex.core.contracts import FastPathRule, RiskLevel, ToolResult, ToolSpec
from deusex.core.db import Database
from deusex.core.events import EventBus, ScheduledJobFiredEvent, ScheduledJobsUpdatedEvent

logger = logging.getLogger(__name__)

_SET_TIMER_RE = re.compile(
    r"^(?:pon|ponme|crea|programa|configura|inicia)\s+(?:un\s+)?"
    r"(?:temporizador|timer|cuenta regresiva)\s+(?:de\s+)?"
    r"(\d+)\s*(segundos?|seg|s|minutos?|min|m|horas?|h)"
    r"(?:\s+(?:para|llamado|de)\s+(.+))?$"
)
_REMIND_IN_RE = re.compile(
    r"^(?:avisame|recuerdame)\s+en\s+(\d+)\s*(segundos?|seg|s|minutos?|min|m|horas?|h)"
    r"(?:\s+(?:para|que|de)\s+(.+))?$"
)
_LIST_TIMERS_RE = re.compile(
    r"^(?:que|cuales|lista|muestra|ver)\s+(?:son\s+)?"
    r"(?:mis\s+|los\s+)?(?:temporizadores|timers|alarmas|recordatorios)(?:\s+activos?)?$"
)
_CANCEL_TIMER_RE = re.compile(
    r"^(?:cancela|elimina|borra|deten)\s+(?:el\s+|la\s+|los\s+|las\s+|todos los\s+)?"
    r"(?:temporizador(?:es)?|timer(?:s)?|alarma(?:s)?|recordatorio(?:s)?)(?:\s+(.+))?$"
)


def parse_duration_seconds(amount: int, unit_raw: str) -> int:
    unit = unit_raw.strip().lower()
    if unit in ("s", "seg", "segundo", "segundos"):
        return amount
    if unit in ("m", "min", "minuto", "minutos"):
        return amount * 60
    if unit in ("h", "hora", "horas"):
        return amount * 3600
    raise ValueError(f"Unidad de tiempo no soportada: {unit_raw!r}")


def format_duration_es(total_seconds: int) -> str:
    secs = max(0, int(total_seconds))
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} s")
    return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    id: str
    kind: str
    label: str
    fire_at: datetime
    status: str
    created_at: datetime
    recurrence: str | None = None
    payload: dict[str, Any] | None = None

    def remaining_seconds(self, now: datetime | None = None) -> float:
        ref = now if now is not None else datetime.now(UTC)
        return max(0.0, (self.fire_at - ref).total_seconds())


class LocalScheduler:
    """Planificador 100% offline sobre SQLite WAL (siempre activo incluso en Zero-Base)."""

    module_id = "core.scheduler"

    def __init__(
        self,
        db: Database,
        events: EventBus,
        *,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self._db = db
        self._events = events
        self._poll_interval_sec = poll_interval_sec
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(), name="LocalSchedulerPoll")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._stop_event = None

    async def _poll_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self.check_due_jobs()
            except Exception:
                logger.exception("Error comprobando trabajos vencidos en LocalScheduler")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_sec,
                )

    def _row_to_job(self, row: Any) -> ScheduledJob:
        raw_payload = str(row["payload_json"] or "{}")
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            payload = {}
        return ScheduledJob(
            id=str(row["id"]),
            kind=str(row["kind"]),
            label=str(row["label"]),
            fire_at=datetime.fromisoformat(str(row["fire_at"])),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            recurrence=str(row["recurrence"]) if row["recurrence"] is not None else None,
            payload=payload if isinstance(payload, dict) else {},
        )

    def _notify_updated(self) -> None:
        count = int(
            self._db.scalar("SELECT COUNT(*) FROM scheduled_jobs WHERE status = 'pending'") or 0
        )
        self._events.publish(ScheduledJobsUpdatedEvent(pending_count=count))

    def schedule_timer(self, seconds: int, label: str = "Temporizador") -> ScheduledJob:
        if seconds <= 0:
            raise ValueError("La duración del temporizador debe ser mayor a 0 segundos.")
        now = datetime.now(UTC)
        fire_at = now + timedelta(seconds=seconds)
        job_id = uuid.uuid4().hex[:12]
        clean_label = label.strip() or "Temporizador"
        payload = {"duration_seconds": seconds}

        self._db.execute(
            """
            INSERT INTO scheduled_jobs
                (id, kind, label, fire_at, recurrence, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "timer",
                clean_label,
                fire_at.isoformat(),
                None,
                json.dumps(payload, ensure_ascii=False),
                "pending",
                now.isoformat(),
            ),
        )
        self._notify_updated()
        return ScheduledJob(
            id=job_id,
            kind="timer",
            label=clean_label,
            fire_at=fire_at,
            status="pending",
            created_at=now,
            payload=payload,
        )

    def schedule_at(
        self,
        fire_at: datetime,
        label: str,
        *,
        kind: str = "reminder",
    ) -> ScheduledJob:
        now = datetime.now(UTC)
        target_utc = fire_at.astimezone(UTC) if fire_at.tzinfo else fire_at.replace(tzinfo=UTC)
        job_id = uuid.uuid4().hex[:12]
        clean_label = label.strip() or ("Alarma" if kind == "alarm" else "Recordatorio")

        self._db.execute(
            """
            INSERT INTO scheduled_jobs
                (id, kind, label, fire_at, recurrence, payload_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                kind,
                clean_label,
                target_utc.isoformat(),
                None,
                "{}",
                "pending",
                now.isoformat(),
            ),
        )
        self._notify_updated()
        return ScheduledJob(
            id=job_id,
            kind=kind,
            label=clean_label,
            fire_at=target_utc,
            status="pending",
            created_at=now,
            payload={},
        )

    def list_pending(self) -> list[ScheduledJob]:
        rows = self._db.fetchall(
            """
            SELECT id, kind, label, fire_at, recurrence, payload_json, status, created_at
            FROM scheduled_jobs
            WHERE status = 'pending'
            ORDER BY fire_at ASC
            """
        )
        return [self._row_to_job(r) for r in rows]

    def cancel_job(self, job_id: str) -> bool:
        affected = self._db.execute(
            "UPDATE scheduled_jobs SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (job_id,),
        )
        if affected > 0:
            self._notify_updated()
            return True
        return False

    def cancel_all_pending(self) -> int:
        affected = self._db.execute(
            "UPDATE scheduled_jobs SET status = 'cancelled' WHERE status = 'pending'"
        )
        if affected > 0:
            self._notify_updated()
        return affected

    def check_due_jobs(self, *, now: datetime | None = None) -> list[ScheduledJob]:
        ref = now if now is not None else datetime.now(UTC)
        ref_iso = ref.isoformat()
        fired: list[ScheduledJob] = []

        with self._db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, kind, label, fire_at, recurrence, payload_json, status, created_at
                FROM scheduled_jobs
                WHERE status = 'pending' AND fire_at <= ?
                ORDER BY fire_at ASC
                """,
                (ref_iso,),
            ).fetchall()
            for row in rows:
                job = self._row_to_job(row)
                conn.execute(
                    "UPDATE scheduled_jobs SET status = 'fired' WHERE id = ?",
                    (job.id,),
                )
                fired.append(
                    ScheduledJob(
                        id=job.id,
                        kind=job.kind,
                        label=job.label,
                        fire_at=job.fire_at,
                        status="fired",
                        created_at=job.created_at,
                        recurrence=job.recurrence,
                        payload=job.payload,
                    )
                )

        if fired:
            for job in fired:
                self._events.publish(
                    ScheduledJobFiredEvent(job_id=job.id, kind=job.kind, label=job.label)
                )
            self._notify_updated()

        return fired

    def get_fast_path_rules(self) -> list[FastPathRule]:
        async def _fp_set_timer(match: re.Match[str], _raw: str) -> ToolResult:
            amount = int(match.group(1))
            unit = match.group(2)
            label = (match.group(3) or "Temporizador").strip()
            seconds = parse_duration_seconds(amount, unit)
            job = self.schedule_timer(seconds=seconds, label=label)
            pretty = format_duration_es(seconds)
            msg = f"Temporizador '{job.label}' programado para {pretty}."
            return ToolResult(
                ok=True,
                summary=msg,
                spoken_text=msg,
                data={"job_id": job.id, "seconds": seconds, "label": job.label},
            )

        async def _fp_list_timers(_match: re.Match[str], _raw: str) -> ToolResult:
            pending = self.list_pending()
            if not pending:
                msg = "No tienes temporizadores ni alarmas activas."
                return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"jobs": []})
            lines = [
                f"- {job.label}: faltan {format_duration_es(int(job.remaining_seconds()))}"
                for job in pending
            ]
            msg = "Temporizadores activos:\n" + "\n".join(lines)
            return ToolResult(
                ok=True,
                summary=msg,
                spoken_text=f"Tienes {len(pending)} temporizadores activos.",
                data={"jobs": [job.id for job in pending]},
            )

        async def _fp_cancel_timer(match: re.Match[str], _raw: str) -> ToolResult:
            target = (match.group(1) or "").strip().lower()
            pending = self.list_pending()
            if not pending:
                msg = "No hay temporizadores activos para cancelar."
                return ToolResult(ok=True, summary=msg, spoken_text=msg)
            if not target or target in ("todos", "todo", "all"):
                count = self.cancel_all_pending()
                msg = f"Se cancelaron {count} temporizador(es)."
                return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"cancelled": count})
            for job in pending:
                if target in job.label.lower():
                    self.cancel_job(job.id)
                    msg = f"Temporizador '{job.label}' cancelado."
                    return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"job_id": job.id})
            job = pending[0]
            self.cancel_job(job.id)
            msg = f"Temporizador '{job.label}' cancelado."
            return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"job_id": job.id})

        return [
            FastPathRule(
                rule_id="core.scheduler.set_timer",
                module_id=self.module_id,
                priority=15,
                pattern=_SET_TIMER_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_set_timer,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.scheduler.remind_in",
                module_id=self.module_id,
                priority=16,
                pattern=_REMIND_IN_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_set_timer,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.scheduler.list_timers",
                module_id=self.module_id,
                priority=17,
                pattern=_LIST_TIMERS_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_list_timers,
                earcon="confirm",
            ),
            FastPathRule(
                rule_id="core.scheduler.cancel_timer",
                module_id=self.module_id,
                priority=18,
                pattern=_CANCEL_TIMER_RE,
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_fp_cancel_timer,
                earcon="confirm",
            ),
        ]

    def get_tools(self) -> list[ToolSpec]:
        async def _tool_set_timer(args: dict[str, Any]) -> ToolResult:
            seconds = int(args.get("seconds", 0))
            label = str(args.get("label", "Temporizador"))
            job = self.schedule_timer(seconds=seconds, label=label)
            msg = f"Temporizador '{job.label}' programado para {format_duration_es(seconds)}."
            return ToolResult(ok=True, summary=msg, spoken_text=msg, data={"job_id": job.id})

        async def _tool_cancel_timer(args: dict[str, Any]) -> ToolResult:
            job_id = str(args.get("job_id", "")).strip()
            if job_id:
                ok = self.cancel_job(job_id)
                return ToolResult(
                    ok=ok,
                    summary="Temporizador cancelado." if ok else "No se encontró ese temporizador.",
                )
            count = self.cancel_all_pending()
            return ToolResult(ok=True, summary=f"Se cancelaron {count} temporizador(es).")

        return [
            ToolSpec(
                name="scheduler.set_timer",
                module_id=self.module_id,
                description="Programa un temporizador local offline en segundos.",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "seconds": {"type": "integer", "minimum": 1},
                        "label": {"type": "string"},
                    },
                    "required": ["seconds"],
                },
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_tool_set_timer,
            ),
            ToolSpec(
                name="scheduler.cancel_timer",
                module_id=self.module_id,
                description="Cancela un temporizador local pendiente por ID o todos.",
                parameters_schema={
                    "type": "object",
                    "properties": {"job_id": {"type": "string"}},
                },
                permission=None,
                risk_level=RiskLevel.SAFE,
                handler=_tool_cancel_timer,
            ),
        ]
