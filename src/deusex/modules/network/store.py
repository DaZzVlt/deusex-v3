from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import logging
import re
import sqlite3
from typing import Any
import unicodedata

from deusex.core.config import SecretsManager
from deusex.core.db import Database
from deusex.modules.network.adapters import DeviceType, NetworkDeviceRecord, normalize_mac

logger = logging.getLogger(__name__)

_ENV_SANITIZE_RE = re.compile(r"[^A-Za-z0-9]+")
_SENSITIVE_META_FRAGMENTS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "auth_key",
    "api_key",
    "cookie",
    "credential",
)

_SYNONYMS: dict[str, str] = {
    "tele": "tv",
    "televisor": "tv",
}

_STOP_WORDS: frozenset[str] = frozenset(
    {"la", "el", "los", "las", "un", "una", "de", "del", "mi", "al", "en"}
)


def _normalize_search_text(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.strip().lower())
    without_accents = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_accents).strip()


def _sanitize_metadata_for_sqlite(metadata: dict[str, Any]) -> dict[str, Any]:
    """Elimina cualquier campo con nombre sensible antes de persistir en SQLite."""
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        key_lower = str(key).lower()
        if any(frag in key_lower for frag in _SENSITIVE_META_FRAGMENTS):
            continue
        if isinstance(value, dict):
            clean[str(key)] = _sanitize_metadata_for_sqlite(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            clean[str(key)] = value
        elif isinstance(value, (list, tuple)):
            clean[str(key)] = [
                item
                for item in value
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
    return clean


def _parse_device_type(raw: str) -> DeviceType:
    try:
        return DeviceType(raw)
    except ValueError:
        return DeviceType.GENERIC


class DeviceAliasStore:
    """
    Persistencia de dispositivos LAN en SQLite WAL (`device_aliases`) con aislamiento estricto
    de tokens de emparejamiento en `.env` vía `SecretsManager` (cero tokens en SQLite).
    """

    def __init__(self, db: Database, secrets: SecretsManager) -> None:
        self._db = db
        self._secrets = secrets

    @staticmethod
    def build_token_env_key(device_uid: str, *, kind: str = "auth") -> str:
        """Genera una clave `.env` válida y determinista para tokens de un dispositivo."""
        clean_kind = _ENV_SANITIZE_RE.sub("_", kind.strip()).strip("_").upper() or "AUTH"
        clean_uid = _ENV_SANITIZE_RE.sub("_", device_uid.strip()).strip("_").upper()
        digest = hashlib.sha256(device_uid.encode("utf-8")).hexdigest()[:8].upper()
        if not clean_uid:
            clean_uid = f"DEV_{digest}"
        elif len(clean_uid) > 36:
            clean_uid = f"{clean_uid[:27]}_{digest}"
        return f"NETWORK_{clean_kind}_{clean_uid}"

    def _row_to_record(self, row: sqlite3.Row) -> NetworkDeviceRecord:
        raw_meta = str(row["metadata_json"] or "{}")
        try:
            parsed_meta = json.loads(raw_meta)
            metadata = parsed_meta if isinstance(parsed_meta, dict) else {}
        except ValueError:
            metadata = {}

        return NetworkDeviceRecord(
            device_uid=str(row["device_uid"]),
            alias=str(row["alias"]),
            device_type=_parse_device_type(str(row["device_type"])),
            last_ip=str(row["last_ip"] or ""),
            mac_address=normalize_mac(str(row["mac_address"] or "")),
            port=int(row["port"] or 0),
            metadata=metadata,
            updated_at=str(row["updated_at"] or ""),
            online=False,
        )

    def list_devices(self, *, device_type: DeviceType | None = None) -> list[NetworkDeviceRecord]:
        if device_type is not None:
            rows = self._db.fetchall(
                """
                SELECT device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
                FROM device_aliases
                WHERE device_type = ?
                ORDER BY LOWER(alias) ASC, updated_at DESC
                """,
                (device_type.value,),
            )
        else:
            rows = self._db.fetchall(
                """
                SELECT device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
                FROM device_aliases
                ORDER BY LOWER(alias) ASC, updated_at DESC
                """
            )
        records = [self._row_to_record(row) for row in rows]
        records.sort(key=lambda dev: (not dev.is_favorite, dev.alias.lower()))
        return records

    def get_device(self, device_uid: str) -> NetworkDeviceRecord | None:
        row = self._db.fetchone(
            """
            SELECT device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
            FROM device_aliases
            WHERE device_uid = ?
            """,
            (device_uid.strip(),),
        )
        return self._row_to_record(row) if row is not None else None

    def find_by_mac(self, mac_address: str) -> NetworkDeviceRecord | None:
        clean_mac = normalize_mac(mac_address)
        if not clean_mac:
            return None
        row = self._db.fetchone(
            """
            SELECT device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
            FROM device_aliases
            WHERE mac_address = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (clean_mac,),
        )
        return self._row_to_record(row) if row is not None else None

    def find_by_ip(self, ip_address: str) -> NetworkDeviceRecord | None:
        clean_ip = ip_address.strip()
        if not clean_ip:
            return None
        row = self._db.fetchone(
            """
            SELECT device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
            FROM device_aliases
            WHERE last_ip = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (clean_ip,),
        )
        return self._row_to_record(row) if row is not None else None

    def set_favorite(self, device_uid: str, favorite: bool = True) -> NetworkDeviceRecord | None:
        """
        Marca un dispositivo como favorito exclusivo (si `favorite=True`) o lo desmarca.
        Persiste en `metadata_json` de SQLite WAL.
        """
        clean_uid = device_uid.strip()
        target = self.get_device(clean_uid)
        if target is None:
            return None

        now_iso = datetime.now(UTC).isoformat()
        if favorite:
            # Desmarcar cualquier otro favorito existente
            all_devices = self.list_devices()
            for other in all_devices:
                if other.device_uid != clean_uid and other.is_favorite:
                    meta = dict(other.metadata)
                    meta["favorite"] = False
                    clean_meta = _sanitize_metadata_for_sqlite(meta)
                    self._db.execute(
                        "UPDATE device_aliases SET metadata_json = ?, updated_at = ? WHERE device_uid = ?",
                        (json.dumps(clean_meta, ensure_ascii=False, sort_keys=True), now_iso, other.device_uid),
                    )

        target_meta = dict(target.metadata)
        target_meta["favorite"] = bool(favorite)
        clean_target_meta = _sanitize_metadata_for_sqlite(target_meta)
        self._db.execute(
            "UPDATE device_aliases SET metadata_json = ?, updated_at = ? WHERE device_uid = ?",
            (json.dumps(clean_target_meta, ensure_ascii=False, sort_keys=True), now_iso, clean_uid),
        )
        return self.get_device(clean_uid)

    def find_by_query(
        self,
        query: str = "",
        *,
        device_types: tuple[DeviceType, ...] | None = None,
    ) -> NetworkDeviceRecord | None:
        """Resuelve un dispositivo por UID exacto, alias amigable, tipo o coincidencia por tokens."""
        devices = self.list_devices()
        if device_types is not None:
            allowed = set(device_types)
            devices = [dev for dev in devices if dev.device_type in allowed]

        if not devices:
            return None

        clean_query = _normalize_search_text(query)
        if not clean_query:
            # Favorito primero (list_devices ya los sitúa al frente)
            return devices[0]

        def _pick_best(candidates: list[NetworkDeviceRecord]) -> NetworkDeviceRecord | None:
            if not candidates:
                return None
            for cand in candidates:
                if cand.is_favorite:
                    return cand
            return candidates[0]

        # 1. Coincidencia exacta por UID o alias normalizado
        level1 = [
            dev
            for dev in devices
            if _normalize_search_text(dev.device_uid) == clean_query
            or _normalize_search_text(dev.alias) == clean_query
        ]
        if level1:
            return _pick_best(level1)

        # 2. Alias empieza con el término o lo contiene
        level2 = [
            dev
            for dev in devices
            if _normalize_search_text(dev.alias).startswith(clean_query)
            or clean_query in _normalize_search_text(dev.alias)
        ]
        if level2:
            return _pick_best(level2)

        # 3. Coincidencia continua contra nombre del tipo, modelo o fabricante en metadata
        level3: list[NetworkDeviceRecord] = []
        for dev in devices:
            haystack = _normalize_search_text(
                " ".join(
                    [
                        dev.alias,
                        dev.device_type.value.replace("_", " "),
                        str(dev.metadata.get("friendly_name", "")),
                        str(dev.metadata.get("model", "")),
                        str(dev.metadata.get("manufacturer", "")),
                    ]
                )
            )
            if clean_query in haystack:
                level3.append(dev)
        if level3:
            return _pick_best(level3)

        # 4. Coincidencia por tokens en cualquier orden (normalizando sinónimos tele/televisor -> tv)
        raw_tokens = clean_query.split()
        normalized_tokens = [
            _SYNONYMS.get(t, t)
            for t in raw_tokens
            if t not in _STOP_WORDS and len(t) > 0
        ]
        if normalized_tokens:
            level4: list[NetworkDeviceRecord] = []
            for dev in devices:
                haystack_raw = " ".join(
                    [
                        dev.alias,
                        dev.device_type.value.replace("_", " "),
                        str(dev.metadata.get("friendly_name", "")),
                        str(dev.metadata.get("model", "")),
                        str(dev.metadata.get("manufacturer", "")),
                    ]
                )
                norm_words = [
                    _SYNONYMS.get(w, w)
                    for w in _normalize_search_text(haystack_raw).split()
                ]
                haystack_normalized = " ".join(norm_words)
                if all(t in haystack_normalized for t in normalized_tokens):
                    level4.append(dev)
            if level4:
                return _pick_best(level4)

        return None

    def upsert_device(
        self,
        record: NetworkDeviceRecord,
        *,
        preserve_alias: bool = True,
    ) -> NetworkDeviceRecord:
        """
        Inserta o actualiza un dispositivo LAN.
        Si `preserve_alias=True` y el dispositivo ya tenía alias propio, lo conserva.
        """
        clean_uid = record.device_uid.strip()
        if not clean_uid:
            raise ValueError("device_uid no puede estar vacío.")

        existing = self.get_device(clean_uid)
        if existing is None and record.mac_address:
            existing = self.find_by_mac(record.mac_address)
            if existing is not None and not clean_uid.startswith("mac:"):
                clean_uid = existing.device_uid
        if existing is None and record.last_ip.strip():
            existing = self.find_by_ip(record.last_ip.strip())
            if existing is not None and not clean_uid.startswith("mac:"):
                clean_uid = existing.device_uid

        merged_alias = record.alias.strip() or clean_uid
        if preserve_alias and existing is not None and existing.alias.strip():
            merged_alias = existing.alias.strip()

        merged_type = record.device_type
        if existing is not None and merged_type == DeviceType.GENERIC and existing.device_type != DeviceType.GENERIC:
            merged_type = existing.device_type

        merged_ip = record.last_ip.strip() or (existing.last_ip if existing is not None else "")
        merged_mac = normalize_mac(record.mac_address) or (
            existing.mac_address if existing is not None else ""
        )
        merged_port = int(record.port) if int(record.port) > 0 else (
            existing.port if existing is not None else 0
        )

        merged_meta: dict[str, Any] = dict(existing.metadata) if existing is not None else {}
        merged_meta.update(_sanitize_metadata_for_sqlite(record.metadata))
        alt_set = set(merged_meta.get("alt_macs", []))
        if existing is not None:
            alt_set.update(existing.metadata.get("alt_macs", []))
            if existing.mac_address:
                alt_set.add(existing.mac_address)
        if record.mac_address:
            alt_set.add(normalize_mac(record.mac_address))
        if merged_mac in alt_set:
            alt_set.remove(merged_mac)
        alt_set.discard("")
        if alt_set:
            merged_meta["alt_macs"] = sorted(alt_set)
        merged_meta = _sanitize_metadata_for_sqlite(merged_meta)

        now_iso = datetime.now(UTC).isoformat()
        self._db.execute(
            """
            INSERT INTO device_aliases (
                device_uid, alias, device_type, last_ip, mac_address, port, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_uid) DO UPDATE SET
                alias = excluded.alias,
                device_type = excluded.device_type,
                last_ip = excluded.last_ip,
                mac_address = excluded.mac_address,
                port = excluded.port,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                clean_uid,
                merged_alias,
                merged_type.value,
                merged_ip,
                merged_mac,
                merged_port,
                json.dumps(merged_meta, ensure_ascii=False, sort_keys=True),
                now_iso,
            ),
        )

        if existing is not None and existing.device_uid != clean_uid:
            for kind in ("auth", "tizen", "lounge", "xbox"):
                if not self.has_token(clean_uid, kind=kind):
                    old_token = self.get_token(existing.device_uid, kind=kind)
                    if old_token:
                        self.set_token(clean_uid, old_token, kind=kind)
                self.delete_token(existing.device_uid, kind=kind)
            self._db.execute("DELETE FROM device_aliases WHERE device_uid = ?", (existing.device_uid,))

        if merged_ip:
            dup_rows = self._db.fetchall(
                "SELECT device_uid FROM device_aliases WHERE last_ip = ? AND device_uid != ?",
                (merged_ip, clean_uid),
            )
            for dup in dup_rows:
                dup_uid = str(dup["device_uid"])
                for kind in ("auth", "tizen", "lounge", "xbox"):
                    if not self.has_token(clean_uid, kind=kind):
                        dup_token = self.get_token(dup_uid, kind=kind)
                        if dup_token:
                            self.set_token(clean_uid, dup_token, kind=kind)
                    self.delete_token(dup_uid, kind=kind)
                self._db.execute("DELETE FROM device_aliases WHERE device_uid = ?", (dup_uid,))

        return NetworkDeviceRecord(
            device_uid=clean_uid,
            alias=merged_alias,
            device_type=merged_type,
            last_ip=merged_ip,
            mac_address=merged_mac,
            port=merged_port,
            metadata=merged_meta,
            updated_at=now_iso,
            online=record.online,
        )

    def rename_alias(self, device_uid: str, new_alias: str) -> NetworkDeviceRecord | None:
        clean_alias = new_alias.strip()
        if not clean_alias:
            raise ValueError("El alias del dispositivo no puede estar vacío.")
        existing = self.get_device(device_uid)
        if existing is None:
            return None
        updated = existing.with_updates(alias=clean_alias)
        return self.upsert_device(updated, preserve_alias=False)

    def delete_device(self, device_uid: str, *, purge_tokens: bool = True) -> bool:
        clean_uid = device_uid.strip()
        deleted = self._db.execute("DELETE FROM device_aliases WHERE device_uid = ?", (clean_uid,)) > 0
        if purge_tokens:
            for kind in ("auth", "tizen", "lounge", "xbox"):
                self.delete_token(clean_uid, kind=kind)
        return deleted

    # ------------------------------------------------------------------
    # Tokens aislados en `.env` (prohibido guardar secretos en SQLite)
    # ------------------------------------------------------------------
    def get_token(self, device_uid: str, *, kind: str = "auth") -> str | None:
        env_key = self.build_token_env_key(device_uid, kind=kind)
        return self._secrets.get(env_key)

    def set_token(self, device_uid: str, token: str, *, kind: str = "auth") -> str:
        clean_token = token.strip()
        if not clean_token:
            raise ValueError("El token del dispositivo no puede estar vacío.")
        env_key = self.build_token_env_key(device_uid, kind=kind)
        self._secrets.set(env_key, clean_token)
        return env_key

    def delete_token(self, device_uid: str, *, kind: str = "auth") -> bool:
        env_key = self.build_token_env_key(device_uid, kind=kind)
        return self._secrets.delete(env_key)

    def has_token(self, device_uid: str, *, kind: str = "auth") -> bool:
        return self.get_token(device_uid, kind=kind) is not None
