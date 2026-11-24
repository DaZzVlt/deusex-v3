from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any
import uuid

from deusex.brain.providers import LLMMessage
from deusex.core.config import DeusExSettings
from deusex.core.db import Database
from deusex.core.events import EventBus, MemorySavedEvent

VALID_MEMORY_CATEGORIES: tuple[str, ...] = ("fact", "preference", "interest", "contact")

_EXTRACT_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "fact",
        "nombre",
        re.compile(r"\b(?:me llamo|mi nombre es)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ][A-ZÁÉÍÓÚÑa-záéíóúñ\s]{1,28})", re.IGNORECASE),
    ),
    (
        "fact",
        "ubicacion",
        re.compile(r"\b(?:vivo en|soy de|estoy en)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ][A-ZÁÉÍÓÚÑa-záéíóúñ,\s]{2,40})", re.IGNORECASE),
    ),
    (
        "fact",
        "ocupacion",
        re.compile(r"\b(?:trabajo como|soy|estudio)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ][A-ZÁÉÍÓÚÑa-záéíóúñ\s]{2,45})", re.IGNORECASE),
    ),
    (
        "interest",
        "interes",
        re.compile(r"\b(?:me gusta mucho|me encanta|me gusta|juego a|sigo)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ0-9][A-ZÁÉÍÓÚÑa-záéíóúñ0-9\s\-]{2,45})", re.IGNORECASE),
    ),
    (
        "preference",
        "preferencia",
        re.compile(r"\b(?:prefiero|me acomoda mas|siempre uso)\s+([A-ZÁÉÍÓÚÑa-záéíóúñ0-9][A-ZÁÉÍÓÚÑa-záéíóúñ0-9\s\-]{2,45})", re.IGNORECASE),
    ),
)

_TRAILING_CLAUSE_RE = re.compile(r"(?:\s+(?:pero|porque|aunque|y\s+hoy|y\s+quiero|y\s+necesito)\b|[.!?;,\n]).*$")
_BLOCKED_FACT_VALUES: frozenset[str] = frozenset(
    {"bien", "mal", "aqui", "acá", "nuevo", "nueva", "feliz", "triste", "cansado", "cansada"}
)


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    id: str
    title: str
    profile_id: str
    created_at: str
    updated_at: str
    message_count: int = 0


@dataclass(frozen=True, slots=True)
class ChatMessageRecord:
    id: str
    conversation_id: str
    role: str
    content: str
    model_used: str
    fast_path: bool
    sources: tuple[dict[str, str], ...]
    tokens_in: int
    tokens_out: int
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    category: str
    key: str
    value: str
    confidence: float
    pinned: bool
    source_message_id: str | None
    created_at: str
    updated_at: str

    def badge_label_es(self) -> str:
        cat_labels = {
            "fact": "Hecho",
            "preference": "Preferencia",
            "interest": "Interés",
            "contact": "Contacto",
        }
        return f"{cat_labels.get(self.category, self.category)} · {self.value}"


class MemoryManager:
    """Memoria Adaptativa en 2 Capas sobre SQLite WAL (congelada en Modo Privacidad)."""

    def __init__(self, db: Database, settings: DeusExSettings, events: EventBus) -> None:
        self._db = db
        self._settings = settings
        self._events = events

    # ------------------------------------------------------------------
    # Capa 1: Conversaciones e historial persistido
    # ------------------------------------------------------------------
    def create_conversation(
        self,
        *,
        title: str = "Nueva conversación",
        profile_id: str | None = None,
    ) -> ConversationSummary:
        conv_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        prof = profile_id or self._settings.ui.active_profile_id
        clean_title = title.strip()[:80] or "Nueva conversación"
        self._db.execute(
            """
            INSERT INTO conversations (id, title, profile_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conv_id, clean_title, prof, now, now),
        )
        return ConversationSummary(
            id=conv_id,
            title=clean_title,
            profile_id=prof,
            created_at=now,
            updated_at=now,
            message_count=0,
        )

    def get_or_create_active_conversation(self, profile_id: str | None = None) -> ConversationSummary:
        convs = self.list_conversations(limit=1)
        if convs:
            return convs[0]
        return self.create_conversation(profile_id=profile_id)

    def list_conversations(self, *, limit: int = 30) -> list[ConversationSummary]:
        rows = self._db.fetchall(
            """
            SELECT c.id, c.title, c.profile_id, c.created_at, c.updated_at,
                   COUNT(m.id) AS msg_count
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            ConversationSummary(
                id=str(r["id"]),
                title=str(r["title"]),
                profile_id=str(r["profile_id"]),
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
                message_count=int(r["msg_count"] or 0),
            )
            for r in rows
        ]

    def delete_conversation(self, conversation_id: str) -> bool:
        affected = self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return affected > 0

    def append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        model_used: str = "",
        fast_path: bool = False,
        sources: tuple[dict[str, str], ...] = (),
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> ChatMessageRecord:
        msg_id = uuid.uuid4().hex[:14]
        now = datetime.now(UTC).isoformat()
        sources_json = json.dumps(list(sources), ensure_ascii=False)

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (id, conversation_id, role, content, model_used, fast_path,
                     sources_json, tokens_in, tokens_out, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    conversation_id,
                    role,
                    content,
                    model_used,
                    1 if fast_path else 0,
                    sources_json,
                    max(0, int(tokens_in)),
                    max(0, int(tokens_out)),
                    now,
                ),
            )
            if role == "user":
                row = conn.execute(
                    "SELECT title FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if row is not None and str(row["title"]) == "Nueva conversación":
                    auto_title = content.strip().splitlines()[0][:56]
                    conn.execute(
                        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                        (auto_title or "Conversación", now, conversation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (now, conversation_id),
                    )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )

        return ChatMessageRecord(
            id=msg_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            model_used=model_used,
            fast_path=fast_path,
            sources=sources,
            tokens_in=max(0, int(tokens_in)),
            tokens_out=max(0, int(tokens_out)),
            created_at=now,
        )

    def get_messages(self, conversation_id: str, *, limit: int = 200) -> list[ChatMessageRecord]:
        rows = self._db.fetchall(
            """
            SELECT id, conversation_id, role, content, model_used, fast_path,
                   sources_json, tokens_in, tokens_out, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        )
        result: list[ChatMessageRecord] = []
        for row in rows:
            raw_sources = str(row["sources_json"] or "[]")
            parsed_sources: list[dict[str, str]] = []
            try:
                loaded = json.loads(raw_sources)
                if isinstance(loaded, list):
                    for item in loaded:
                        if isinstance(item, dict):
                            parsed_sources.append(
                                {
                                    "title": str(item.get("title") or item.get("url") or ""),
                                    "url": str(item.get("url") or ""),
                                }
                            )
            except ValueError:
                parsed_sources = []
            result.append(
                ChatMessageRecord(
                    id=str(row["id"]),
                    conversation_id=str(row["conversation_id"]),
                    role=str(row["role"]),
                    content=str(row["content"]),
                    model_used=str(row["model_used"] or ""),
                    fast_path=bool(row["fast_path"]),
                    sources=tuple(parsed_sources),
                    tokens_in=int(row["tokens_in"] or 0),
                    tokens_out=int(row["tokens_out"] or 0),
                    created_at=str(row["created_at"]),
                )
            )
        return result

    def recent_llm_messages(self, conversation_id: str, *, limit: int = 16) -> list[LLMMessage]:
        all_msgs = self.get_messages(conversation_id, limit=limit * 2)
        trimmed = all_msgs[-limit:]
        result: list[LLMMessage] = []
        for msg in trimmed:
            if msg.role == "user":
                result.append(LLMMessage(role="user", content=msg.content))
            elif msg.role == "assistant":
                result.append(LLMMessage(role="assistant", content=msg.content))
        return result

    # ------------------------------------------------------------------
    # Capa 2: Memoria Adaptativa Declarativa (editable, pinneable y con Deshacer)
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_memory(row: Any) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            category=str(row["category"]),
            key=str(row["key"]),
            value=str(row["value"]),
            confidence=float(row["confidence"]),
            pinned=bool(row["pinned"]),
            source_message_id=str(row["source_message_id"]) if row["source_message_id"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def save_memory(
        self,
        *,
        category: str,
        key: str,
        value: str,
        confidence: float = 1.0,
        pinned: bool = False,
        source_message_id: str | None = None,
        auto_extracted: bool = False,
    ) -> MemoryRecord | None:
        if auto_extracted and (
            self._settings.general.privacy_mode or not self._settings.brain.memory_auto_extract
        ):
            return None

        clean_cat = category.strip().lower()
        if clean_cat not in VALID_MEMORY_CATEGORIES:
            raise ValueError(f"Categoría de memoria inválida: {category!r}")
        clean_key = key.strip()[:60]
        clean_val = value.strip()[:240]
        if not clean_key or not clean_val:
            raise ValueError("La clave y el valor del recuerdo no pueden estar vacíos.")

        existing = self._db.fetchone(
            """
            SELECT id, category, key, value, confidence, pinned,
                   source_message_id, created_at, updated_at
            FROM memories
            WHERE category = ? AND LOWER(value) = LOWER(?)
            LIMIT 1
            """,
            (clean_cat, clean_val),
        )
        now = datetime.now(UTC).isoformat()
        if existing is not None:
            mem_id = str(existing["id"])
            self._db.execute(
                "UPDATE memories SET key = ?, value = ?, updated_at = ? WHERE id = ?",
                (clean_key, clean_val, now, mem_id),
            )
            updated = self.get_memory(mem_id)
            return updated

        mem_id = uuid.uuid4().hex[:12]
        self._db.execute(
            """
            INSERT INTO memories
                (id, category, key, value, confidence, pinned, source_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mem_id,
                clean_cat,
                clean_key,
                clean_val,
                float(confidence),
                1 if pinned else 0,
                source_message_id,
                now,
                now,
            ),
        )
        record = MemoryRecord(
            id=mem_id,
            category=clean_cat,
            key=clean_key,
            value=clean_val,
            confidence=float(confidence),
            pinned=bool(pinned),
            source_message_id=source_message_id,
            created_at=now,
            updated_at=now,
        )
        self._events.publish(
            MemorySavedEvent(
                memory_id=record.id,
                category=record.category,
                key=record.key,
                value=record.value,
                auto_extracted=auto_extracted,
            )
        )
        return record

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        row = self._db.fetchone(
            """
            SELECT id, category, key, value, confidence, pinned,
                   source_message_id, created_at, updated_at
            FROM memories
            WHERE id = ?
            """,
            (memory_id,),
        )
        return self._row_to_memory(row) if row is not None else None

    def list_memories(
        self,
        *,
        category: str | None = None,
        query: str = "",
        limit: int = 100,
    ) -> list[MemoryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if category and category != "all":
            clauses.append("category = ?")
            params.append(category.strip().lower())
        needle = query.strip()
        if needle:
            clauses.append("(LOWER(key) LIKE LOWER(?) OR LOWER(value) LIKE LOWER(?))")
            like = f"%{needle}%"
            params.extend([like, like])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._db.fetchall(
            f"""
            SELECT id, category, key, value, confidence, pinned,
                   source_message_id, created_at, updated_at
            FROM memories
            {where_sql}
            ORDER BY pinned DESC, updated_at DESC
            LIMIT ?
            """,
            params,
        )
        return [self._row_to_memory(r) for r in rows]

    def update_memory(
        self,
        memory_id: str,
        *,
        category: str | None = None,
        key: str | None = None,
        value: str | None = None,
        pinned: bool | None = None,
    ) -> MemoryRecord | None:
        current = self.get_memory(memory_id)
        if current is None:
            return None
        new_cat = category.strip().lower() if category is not None else current.category
        if new_cat not in VALID_MEMORY_CATEGORIES:
            raise ValueError(f"Categoría de memoria inválida: {new_cat!r}")
        new_key = key.strip()[:60] if key is not None else current.key
        new_val = value.strip()[:240] if value is not None else current.value
        new_pinned = bool(pinned) if pinned is not None else current.pinned
        now = datetime.now(UTC).isoformat()

        self._db.execute(
            """
            UPDATE memories
            SET category = ?, key = ?, value = ?, pinned = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_cat, new_key, new_val, 1 if new_pinned else 0, now, memory_id),
        )
        return self.get_memory(memory_id)

    def toggle_pin(self, memory_id: str) -> MemoryRecord | None:
        current = self.get_memory(memory_id)
        if current is None:
            return None
        return self.update_memory(memory_id, pinned=not current.pinned)

    def delete_memory(self, memory_id: str) -> bool:
        affected = self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return affected > 0

    def extract_from_user_message(
        self,
        text: str,
        *,
        source_message_id: str | None = None,
    ) -> list[MemoryRecord]:
        """Extrae hechos/gustos explícitos en español sin gastar tokens (congelado en Privacidad)."""
        if self._settings.general.privacy_mode or not self._settings.brain.memory_auto_extract:
            return []

        saved: list[MemoryRecord] = []
        for category, key, pattern in _EXTRACT_RULES:
            match = pattern.search(text)
            if match is None:
                continue
            raw_val = _TRAILING_CLAUSE_RE.sub("", match.group(1)).strip(" \t\r\n.,;:!?\"'")
            if len(raw_val) < 3 or raw_val.lower() in _BLOCKED_FACT_VALUES:
                continue
            rec = self.save_memory(
                category=category,
                key=key,
                value=raw_val,
                confidence=0.85,
                source_message_id=source_message_id,
                auto_extracted=True,
            )
            if rec is not None:
                saved.append(rec)
        return saved

    def format_prompt_context(self, *, max_items: int = 12) -> str:
        if self._settings.general.privacy_mode:
            return ""
        items = self.list_memories(limit=max_items)
        if not items:
            return ""
        lines = [f"- [{m.category}] {m.key}: {m.value}" for m in items]
        return "Contexto recordado del usuario:\n" + "\n".join(lines)
