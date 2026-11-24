from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from deusex.brain.providers import ModelTier
from deusex.core.config import DeusExSettings
from deusex.core.db import Database
from deusex.core.events import CostUsageUpdatedEvent, EventBus


@dataclass(frozen=True, slots=True)
class DailyUsageSummary:
    day_utc: str
    fast_path_hits: int
    cloud_calls: int
    tokens_in: int
    tokens_out: int
    daily_token_budget: int

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def budget_exceeded(self) -> bool:
        return self.daily_token_budget > 0 and self.total_tokens >= self.daily_token_budget

    @property
    def sidebar_label_es(self) -> str:
        if self.fast_path_hits == 1:
            return "1 orden instantánea hoy · 0 tokens"
        return f"{self.fast_path_hits} órdenes instantáneas hoy · 0 tokens"


class CostGuard:
    """Contabiliza ahorro Fast-Path vs. consumo en nube sobre SQLite WAL y degrada DEEP -> FAST."""

    def __init__(self, db: Database, settings: DeusExSettings, events: EventBus) -> None:
        self._db = db
        self._settings = settings
        self._events = events

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _publish_update(self, summary: DailyUsageSummary) -> None:
        self._events.publish(
            CostUsageUpdatedEvent(
                fast_path_hits_today=summary.fast_path_hits,
                cloud_calls_today=summary.cloud_calls,
                tokens_today=summary.total_tokens,
            )
        )

    def record_fast_path_hit(self, rule_id: str = "fast_path") -> DailyUsageSummary:
        day = self._today_utc()
        self._db.execute(
            """
            INSERT INTO usage_ledger
                (day_utc, provider_id, model_id, fast_path_hits, cloud_calls, tokens_in, tokens_out)
            VALUES (?, 'local', ?, 1, 0, 0, 0)
            ON CONFLICT(day_utc, provider_id, model_id) DO UPDATE SET
                fast_path_hits = fast_path_hits + 1
            """,
            (day, rule_id or "fast_path"),
        )
        summary = self.today_summary()
        self._publish_update(summary)
        return summary

    def record_cloud_usage(
        self,
        *,
        provider_id: str,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
    ) -> DailyUsageSummary:
        day = self._today_utc()
        clean_in = max(0, int(tokens_in))
        clean_out = max(0, int(tokens_out))
        self._db.execute(
            """
            INSERT INTO usage_ledger
                (day_utc, provider_id, model_id, fast_path_hits, cloud_calls, tokens_in, tokens_out)
            VALUES (?, ?, ?, 0, 1, ?, ?)
            ON CONFLICT(day_utc, provider_id, model_id) DO UPDATE SET
                cloud_calls = cloud_calls + 1,
                tokens_in = tokens_in + excluded.tokens_in,
                tokens_out = tokens_out + excluded.tokens_out
            """,
            (day, provider_id, model_id, clean_in, clean_out),
        )
        summary = self.today_summary()
        self._publish_update(summary)
        return summary

    def today_summary(self) -> DailyUsageSummary:
        day = self._today_utc()
        row = self._db.fetchone(
            """
            SELECT
                COALESCE(SUM(fast_path_hits), 0) AS fp_hits,
                COALESCE(SUM(cloud_calls), 0) AS cloud_calls,
                COALESCE(SUM(tokens_in), 0) AS t_in,
                COALESCE(SUM(tokens_out), 0) AS t_out
            FROM usage_ledger
            WHERE day_utc = ?
            """,
            (day,),
        )
        return DailyUsageSummary(
            day_utc=day,
            fast_path_hits=int(row["fp_hits"]) if row is not None else 0,
            cloud_calls=int(row["cloud_calls"]) if row is not None else 0,
            tokens_in=int(row["t_in"]) if row is not None else 0,
            tokens_out=int(row["t_out"]) if row is not None else 0,
            daily_token_budget=self._settings.brain.daily_token_budget,
        )

    def resolve_effective_tier(self, requested_tier: ModelTier) -> ModelTier:
        if requested_tier == ModelTier.DEEP and self.today_summary().budget_exceeded:
            return ModelTier.FAST
        return requested_tier
