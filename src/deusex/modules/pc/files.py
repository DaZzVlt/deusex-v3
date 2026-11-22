from __future__ import annotations

from contextlib import suppress
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import Any
import uuid

from send2trash import send2trash

from deusex.core.db import Database
from deusex.modules.pc.apps import normalize_text

logger = logging.getLogger(__name__)

_SHERB_NOCONFIRMATION = 0x00000001
_SHERB_NOPROGRESSUI = 0x00000002
_SHERB_NOSOUND = 0x00000004

_KNOWN_USER_FOLDERS: dict[str, tuple[str, ...]] = {
    "descargas": ("Downloads", "Descargas"),
    "downloads": ("Downloads", "Descargas"),
    "documentos": ("Documents", "Documentos"),
    "documents": ("Documents", "Documentos"),
    "escritorio": ("Desktop", "Escritorio"),
    "desktop": ("Desktop", "Escritorio"),
    "imagenes": ("Pictures", "Imágenes", "Imagenes"),
    "pictures": ("Pictures", "Imágenes", "Imagenes"),
    "musica": ("Music", "Música", "Musica"),
    "music": ("Music", "Música", "Musica"),
    "videos": ("Videos", "Vídeos"),
}

_FILE_CATEGORIES: dict[str, frozenset[str]] = {
    "Documentos": frozenset(
        {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".odt", ".rtf"}
    ),
    "Imágenes": frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".heic", ".tiff"}
    ),
    "Audio": frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}),
    "Videos": frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".m4v"}),
    "Comprimidos": frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}),
    "Instaladores": frozenset({".exe", ".msi", ".msix", ".appx", ".iso", ".apk"}),
    "Código": frozenset(
        {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".html", ".css", ".sql", ".ps1", ".bat"}
    ),
}

_PROTECTED_SYSTEM_ROOTS: frozenset[str] = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "system32",
        "syswow64",
        "$recycle.bin",
    }
)


@dataclass(frozen=True, slots=True)
class FileSearchHit:
    path: str
    name: str
    size_bytes: int
    modified_iso: str


@dataclass(frozen=True, slots=True)
class OrganizeOutcome:
    ok: bool
    summary: str
    moved_count: int = 0
    audit_id: str | None = None
    dry_run: bool = False
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class UndoOutcome:
    ok: bool
    summary: str
    restored_count: int = 0
    already_undone: bool = False
    error_code: str | None = None


def resolve_user_folder(folder_hint: str | None = None, *, home_dir: Path | None = None) -> Path:
    """Resuelve alias amigables ('descargas', 'documentos', 'escritorio') o rutas explícitas."""
    home = (home_dir or Path.home()).resolve()
    if not folder_hint or not folder_hint.strip():
        return home

    raw = folder_hint.strip().strip('"').strip("'")
    norm = normalize_text(raw)
    if norm in _KNOWN_USER_FOLDERS:
        for candidate_name in _KNOWN_USER_FOLDERS[norm]:
            candidate = home / candidate_name
            if candidate.exists():
                return candidate.resolve()
        return (home / _KNOWN_USER_FOLDERS[norm][0]).resolve()

    expanded = Path(os.path.expandvars(raw)).expanduser()
    return expanded.resolve()


def is_protected_path(target: Path, *, home_dir: Path | None = None) -> bool:
    """Bloquea operaciones destructivas sobre raíces de disco, Windows o perfil completo."""
    resolved = target.resolve()
    if resolved == resolved.parent:
        return True
    home = (home_dir or Path.home()).resolve()
    if resolved == home:
        return True

    lowered_parts = [part.lower() for part in resolved.parts]
    return any(part in _PROTECTED_SYSTEM_ROOTS for part in lowered_parts[:3])


def classify_extension(path: Path) -> str:
    ext = path.suffix.lower()
    for category, extensions in _FILE_CATEGORIES.items():
        if ext in extensions:
            return category
    return "Otros"


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        alt = dest_dir / f"{stem} ({counter}){suffix}"
        if not alt.exists():
            return alt
        counter += 1


class FileManagerService:
    """Búsqueda segura, organización por tipo con Undo 1-clic en `action_audit` y papelera."""

    def __init__(self, db: Database, *, home_dir: Path | None = None) -> None:
        self._db = db
        self._home_dir = home_dir

    def search_files(
        self,
        query: str,
        *,
        root_hint: str | None = None,
        extension: str | None = None,
        max_results: int = 25,
        max_depth: int = 5,
    ) -> list[FileSearchHit]:
        needle = normalize_text(query)
        ext_filter = ""
        if extension and extension.strip():
            ext_filter = extension.strip().lower()
            if not ext_filter.startswith("."):
                ext_filter = f".{ext_filter}"

        roots: list[Path] = []
        if root_hint and root_hint.strip():
            roots.append(resolve_user_folder(root_hint, home_dir=self._home_dir))
        else:
            for key in ("descargas", "documentos", "escritorio"):
                cand = resolve_user_folder(key, home_dir=self._home_dir)
                if cand.is_dir() and cand not in roots:
                    roots.append(cand)

        hits: list[FileSearchHit] = []
        limit = max(1, min(100, int(max_results)))
        depth_cap = max(1, min(10, int(max_depth)))

        for root in roots:
            if not root.is_dir():
                continue
            root_depth = len(root.parts)
            for current_root, dirs, files in os.walk(root):
                cur_path = Path(current_root)
                rel_depth = len(cur_path.parts) - root_depth
                if rel_depth >= depth_cap:
                    dirs[:] = []
                else:
                    dirs[:] = [
                        d
                        for d in dirs
                        if not d.startswith(".")
                        and d.lower() not in ("node_modules", "__pycache__", ".git", ".venv")
                    ]

                for fname in files:
                    if ext_filter and not fname.lower().endswith(ext_filter):
                        continue
                    if needle and needle not in normalize_text(fname):
                        continue
                    fpath = cur_path / fname
                    with suppress(OSError):
                        st = fpath.stat()
                        hits.append(
                            FileSearchHit(
                                path=str(fpath),
                                name=fname,
                                size_bytes=int(st.st_size),
                                modified_iso=datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                            )
                        )
                        if len(hits) >= limit:
                            return hits
        return hits

    def organize_folder(
        self,
        folder_hint: str,
        *,
        dry_run: bool = False,
    ) -> OrganizeOutcome:
        folder = resolve_user_folder(folder_hint, home_dir=self._home_dir)
        if not folder.is_dir():
            return OrganizeOutcome(
                ok=False,
                summary=f"La carpeta '{folder_hint}' no existe.",
                error_code="FOLDER_NOT_FOUND",
            )
        if is_protected_path(folder, home_dir=self._home_dir):
            return OrganizeOutcome(
                ok=False,
                summary=f"Por seguridad no organizo carpetas críticas del sistema ({folder}).",
                error_code="PROTECTED_PATH",
            )

        files = [
            item
            for item in sorted(folder.iterdir(), key=lambda p: p.name.lower())
            if item.is_file() and not item.name.startswith(".") and item.name.lower() != "desktop.ini"
        ]
        if not files:
            return OrganizeOutcome(
                ok=True,
                summary=f"No hay archivos sueltos para organizar en '{folder.name}'.",
                moved_count=0,
                dry_run=dry_run,
            )

        if dry_run:
            preview_counts: dict[str, int] = {}
            for fpath in files:
                cat = classify_extension(fpath)
                preview_counts[cat] = preview_counts.get(cat, 0) + 1
            breakdown = ", ".join(f"{cat}: {cnt}" for cat, cnt in sorted(preview_counts.items()))
            return OrganizeOutcome(
                ok=True,
                summary=f"[Dry-Run] Organizaría {len(files)} archivo(s) en '{folder.name}' ({breakdown}).",
                moved_count=len(files),
                dry_run=True,
            )

        moves: list[dict[str, str]] = []
        created_dirs: list[str] = []

        for fpath in files:
            category = classify_extension(fpath)
            target_dir = folder / category
            if not target_dir.exists():
                target_dir.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(target_dir))
            dst = _unique_destination(target_dir, fpath.name)
            shutil.move(str(fpath), str(dst))
            moves.append({"src": str(fpath), "dst": str(dst)})

        audit_id = uuid.uuid4().hex
        undo_plan = {
            "kind": "organize_folder",
            "folder": str(folder),
            "moves": moves,
            "created_dirs": created_dirs,
        }
        now_iso = datetime.now(UTC).isoformat()

        self._db.execute(
            """
            INSERT INTO action_audit
                (id, tool_name, module_id, risk_level, status, args_json, undo_plan_json, undone_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                audit_id,
                "pc_organize_folder",
                "pc",
                "moderate",
                "completed",
                json.dumps({"folder": str(folder)}, ensure_ascii=False),
                json.dumps(undo_plan, ensure_ascii=False),
                now_iso,
            ),
        )

        return OrganizeOutcome(
            ok=True,
            summary=(
                f"Organicé {len(moves)} archivo(s) en '{folder.name}'. "
                f"Puedes revertirlo con el ID {audit_id[:8]}."
            ),
            moved_count=len(moves),
            audit_id=audit_id,
        )

    def undo_action(self, audit_id: str | None = None) -> UndoOutcome:
        """Revierte de forma idempotente la última organización o el `audit_id` indicado."""
        if audit_id and audit_id.strip():
            clean_id = audit_id.strip()
            row = self._db.fetchone(
                """
                SELECT id, undo_plan_json, undone_at
                FROM action_audit
                WHERE id = ? OR id LIKE ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (clean_id, f"{clean_id}%"),
            )
        else:
            row = self._db.fetchone(
                """
                SELECT id, undo_plan_json, undone_at
                FROM action_audit
                WHERE module_id = 'pc' AND undo_plan_json IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 1
                """
            )

        if row is None:
            return UndoOutcome(
                ok=False,
                summary="No encontré ninguna organización de archivos registrada para deshacer.",
                error_code="AUDIT_NOT_FOUND",
            )

        full_id = str(row["id"])
        if row["undone_at"] is not None:
            return UndoOutcome(
                ok=True,
                summary="Esa organización ya había sido revertida anteriormente.",
                restored_count=0,
                already_undone=True,
            )

        raw_plan = row["undo_plan_json"]
        if not raw_plan:
            return UndoOutcome(
                ok=False,
                summary="El registro seleccionado no contiene un plan de deshacer.",
                error_code="NO_UNDO_PLAN",
            )

        plan: dict[str, Any] = json.loads(str(raw_plan))
        moves = list(plan.get("moves") or [])
        created_dirs = list(plan.get("created_dirs") or [])

        restored = 0
        for move in reversed(moves):
            src = Path(str(move["src"]))
            dst = Path(str(move["dst"]))
            if not dst.exists():
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            final_target = src if not src.exists() else _unique_destination(src.parent, src.name)
            shutil.move(str(dst), str(final_target))
            restored += 1

        for raw_dir in reversed(created_dirs):
            dir_path = Path(str(raw_dir))
            if dir_path.is_dir():
                with suppress(OSError):
                    dir_path.rmdir()

        now_iso = datetime.now(UTC).isoformat()
        self._db.execute(
            "UPDATE action_audit SET status = 'undone', undone_at = ? WHERE id = ?",
            (now_iso, full_id),
        )

        return UndoOutcome(
            ok=True,
            summary=f"Deshice la organización: {restored} archivo(s) volvieron a su lugar original.",
            restored_count=restored,
            already_undone=False,
        )

    def send_to_trash(self, path_str: str, *, dry_run: bool = False) -> tuple[bool, str]:
        target = resolve_user_folder(path_str, home_dir=self._home_dir)
        if not target.exists():
            return False, f"No encontré '{path_str}'."
        if is_protected_path(target, home_dir=self._home_dir):
            return False, f"Bloqueado por seguridad: '{target}' es una ruta protegida."
        if dry_run:
            return True, f"[Dry-Run] Enviaría '{target.name}' a la papelera de reciclaje."

        send2trash(str(target))
        return True, f"Envié '{target.name}' a la papelera de reciclaje."

    @staticmethod
    def empty_recycle_bin(*, dry_run: bool = False) -> tuple[bool, str]:
        if dry_run:
            return True, "[Dry-Run] Vaciaría la papelera de reciclaje de Windows."
        if sys.platform != "win32":
            return False, "Vaciar la papelera solo está soportado en Windows."
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False, "Win32 no disponible."
        flags = _SHERB_NOCONFIRMATION | _SHERB_NOPROGRESSUI | _SHERB_NOSOUND
        hr = int(windll.shell32.SHEmptyRecycleBinW(None, None, flags))
        # 0 = S_OK; -2147418113 (0x8000FFFF) ocurre en Windows cuando la papelera ya está vacía
        if hr in (0, -2147418113, 0x8000FFFF):
            return True, "Papelera de reciclaje vaciada."
        return False, f"No pude vaciar la papelera (código HRESULT {hr})."
