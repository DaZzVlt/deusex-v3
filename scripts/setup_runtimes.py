from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import urllib.request
import zipfile

# Permite ejecutar el script directo o vía `uv run python scripts/setup_runtimes.py`
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from deusex.core.paths import AppPaths, resolve_app_paths  # noqa: E402

USER_AGENT = "DeusEx-Setup/3.0 (+https://github.com/dazzvlt)"
VOSK_MODEL_NAME = "vosk-model-small-es-0.42"
VOSK_MODEL_URL = f"https://alphacephei.com/vosk/models/{VOSK_MODEL_NAME}.zip"
FFMPEG_RELEASE_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_TARGET_MAJOR = 22
BAILEYS_PACKAGE_SPEC = "@whiskeysockets/baileys"
PINO_PACKAGE_SPEC = "pino"


def _download_file(url: str, destination: Path, label: str) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"URL insegura rechazada: {url}")

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    print(f"[setup] Descargando {label}...")
    with urllib.request.urlopen(req, timeout=180) as response:
        total_header = response.headers.get("Content-Length")
        total_bytes = int(total_header) if total_header and total_header.isdigit() else 0
        downloaded = 0
        last_pct = -1

        with destination.open("wb") as out_file:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if total_bytes > 0:
                    pct = int((downloaded * 100) / total_bytes)
                    if pct >= last_pct + 10 or pct == 100:
                        print(
                            f"  -> {label}: {pct}% ({downloaded / (1024 * 1024):.1f} MB)",
                            flush=True,
                        )
                        last_pct = pct
    print(f"[setup] Descarga completada: {label} ({downloaded / (1024 * 1024):.1f} MB)")


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    """Extrae un archivo ZIP validando protección anti Zip-Slip."""
    resolved_target = target_dir.resolve()
    resolved_target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_dest = (resolved_target / member.filename).resolve()
            if resolved_target != member_dest and resolved_target not in member_dest.parents:
                raise RuntimeError(f"Entrada ZIP fuera del directorio objetivo: {member.filename}")
        zf.extractall(resolved_target)


def _is_vosk_ready(paths: AppPaths) -> bool:
    return (
        (paths.vosk_es_dir / "am" / "final.mdl").is_file()
        and (paths.vosk_es_dir / "conf" / "model.conf").is_file()
    )


def _setup_vosk(paths: AppPaths, *, force: bool) -> dict[str, Any]:
    if not force and _is_vosk_ready(paths):
        print(f"[ok] Vosk ES ya presente en {paths.vosk_es_dir}")
        return {"ready": True, "model": VOSK_MODEL_NAME, "path": str(paths.vosk_es_dir)}

    with tempfile.TemporaryDirectory(prefix="deusex_vosk_") as tmp_str:
        tmp_dir = Path(tmp_str)
        archive = tmp_dir / f"{VOSK_MODEL_NAME}.zip"
        extract_dir = tmp_dir / "extracted"
        _download_file(VOSK_MODEL_URL, archive, f"Vosk ES ({VOSK_MODEL_NAME})")
        _safe_extract_zip(archive, extract_dir)

        source_model = extract_dir / VOSK_MODEL_NAME
        if not source_model.is_dir():
            subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not subdirs:
                raise RuntimeError("El archivo ZIP de Vosk no contiene una carpeta de modelo.")
            source_model = subdirs[0]

        if paths.vosk_es_dir.exists():
            shutil.rmtree(paths.vosk_es_dir)
        paths.vosk_es_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_model), str(paths.vosk_es_dir))

    if not _is_vosk_ready(paths):
        raise RuntimeError("La verificación posterior de Vosk ES falló (faltan archivos del modelo).")

    print(f"[ok] Vosk ES preparado en {paths.vosk_es_dir}")
    return {"ready": True, "model": VOSK_MODEL_NAME, "path": str(paths.vosk_es_dir)}


def _verify_ffmpeg(ffmpeg_exe: Path) -> str | None:
    if not ffmpeg_exe.is_file():
        return None
    proc = subprocess.run(
        [str(ffmpeg_exe), "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    first_line = proc.stdout.splitlines()[0].strip() if proc.stdout else "ffmpeg ok"
    return first_line


def _setup_ffmpeg(paths: AppPaths, *, force: bool) -> dict[str, Any]:
    existing_ver = _verify_ffmpeg(paths.ffmpeg_exe)
    if not force and existing_ver is not None:
        print(f"[ok] FFmpeg portable ya presente: {existing_ver}")
        return {"ready": True, "version": existing_ver, "exe": str(paths.ffmpeg_exe)}

    if sys.platform != "win32":
        raise RuntimeError("setup_runtimes FFmpeg portable está diseñado para Windows x64.")

    with tempfile.TemporaryDirectory(prefix="deusex_ffmpeg_") as tmp_str:
        tmp_dir = Path(tmp_str)
        archive = tmp_dir / "ffmpeg-essentials.zip"
        extract_dir = tmp_dir / "extracted"
        _download_file(FFMPEG_RELEASE_URL, archive, "FFmpeg Portable (Windows x64)")
        _safe_extract_zip(archive, extract_dir)

        ffmpeg_candidates = list(extract_dir.rglob("ffmpeg.exe"))
        if not ffmpeg_candidates:
            raise RuntimeError("No se encontró ffmpeg.exe dentro del archivo descargado.")
        src_bin_dir = ffmpeg_candidates[0].parent

        target_bin = paths.ffmpeg_dir / "bin"
        if paths.ffmpeg_dir.exists():
            shutil.rmtree(paths.ffmpeg_dir)
        target_bin.mkdir(parents=True, exist_ok=True)

        for binary_name in ("ffmpeg.exe", "ffprobe.exe"):
            src_binary = src_bin_dir / binary_name
            if src_binary.is_file():
                shutil.copy2(src_binary, target_bin / binary_name)

    verified = _verify_ffmpeg(paths.ffmpeg_exe)
    if verified is None:
        raise RuntimeError("FFmpeg portable no respondió correctamente a -version.")

    print(f"[ok] FFmpeg portable preparado: {verified}")
    return {"ready": True, "version": verified, "exe": str(paths.ffmpeg_exe)}


def _resolve_latest_node_lts_win_x64() -> tuple[str, str]:
    req = urllib.request.Request(NODE_INDEX_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        releases: list[dict[str, Any]] = json.loads(response.read().decode("utf-8"))

    prefix = f"v{NODE_TARGET_MAJOR}."
    for entry in releases:
        version = str(entry.get("version", ""))
        files = entry.get("files", [])
        if version.startswith(prefix) and isinstance(files, list) and "win-x64-zip" in files:
            url = f"https://nodejs.org/dist/{version}/node-{version}-win-x64.zip"
            return version, url

    raise RuntimeError(f"No se encontró release win-x64-zip de Node.js {NODE_TARGET_MAJOR}.x.")


def _node_paths(paths: AppPaths) -> tuple[Path, Path]:
    node_dir = paths.whatsapp_runtime_dir / "node"
    return node_dir / "node.exe", node_dir / "npm.cmd"


def _verify_whatsapp_runtime(paths: AppPaths) -> str | None:
    node_exe, _ = _node_paths(paths)
    if not node_exe.is_file():
        return None
    baileys_pkg = paths.whatsapp_runtime_dir / "node_modules" / "@whiskeysockets" / "baileys"
    if not baileys_pkg.is_dir():
        return None

    proc = subprocess.run(
        [
            str(node_exe),
            "-e",
            "import('@whiskeysockets/baileys').then(() => console.log(process.version))",
        ],
        cwd=str(paths.whatsapp_runtime_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or "node ok"


def _setup_whatsapp(paths: AppPaths, *, force: bool) -> dict[str, Any]:
    existing_node = _verify_whatsapp_runtime(paths)
    if not force and existing_node is not None:
        print(f"[ok] Runtime WhatsApp (Node {existing_node} + Baileys) ya presente")
        return {
            "ready": True,
            "node_version": existing_node,
            "path": str(paths.whatsapp_runtime_dir),
        }

    if sys.platform != "win32":
        raise RuntimeError("setup_runtimes WhatsApp portable está diseñado para Windows x64.")

    wa_dir = paths.whatsapp_runtime_dir
    node_dir = wa_dir / "node"
    node_exe, npm_cmd = _node_paths(paths)

    if force and wa_dir.exists():
        shutil.rmtree(wa_dir)
    wa_dir.mkdir(parents=True, exist_ok=True)

    if not node_exe.is_file():
        node_version, node_url = _resolve_latest_node_lts_win_x64()
        with tempfile.TemporaryDirectory(prefix="deusex_node_") as tmp_str:
            tmp_dir = Path(tmp_str)
            archive = tmp_dir / f"node-{node_version}-win-x64.zip"
            extract_dir = tmp_dir / "extracted"
            _download_file(node_url, archive, f"Node.js Portable ({node_version} win-x64)")
            _safe_extract_zip(archive, extract_dir)

            subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not subdirs:
                raise RuntimeError("El archivo ZIP de Node.js no contiene carpeta raíz.")
            if node_dir.exists():
                shutil.rmtree(node_dir)
            shutil.move(str(subdirs[0]), str(node_dir))

    pkg_json_path = wa_dir / "package.json"
    pkg_payload = {
        "name": "deusex-whatsapp-runtime",
        "version": "3.0.0",
        "private": True,
        "type": "module",
        "dependencies": {
            BAILEYS_PACKAGE_SPEC: "latest",
            PINO_PACKAGE_SPEC: "^9.0.0",
        },
    }
    pkg_json_path.write_text(json.dumps(pkg_payload, indent=2) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = f"{node_dir}{os.pathsep}{env.get('PATH', '')}"

    print("[setup] Instalando @whiskeysockets/baileys en runtime aislado...")
    install_proc = subprocess.run(
        [
            "cmd.exe",
            "/c",
            str(npm_cmd),
            "install",
            "--omit=dev",
            "--no-audit",
            "--no-fund",
        ],
        cwd=str(wa_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if install_proc.returncode != 0:
        raise RuntimeError(
            f"Fallo al instalar Baileys (código {install_proc.returncode}):\n"
            f"{install_proc.stderr[-1000:]}"
        )

    verified = _verify_whatsapp_runtime(paths)
    if verified is None:
        raise RuntimeError("Node.js portable no pudo importar @whiskeysockets/baileys.")

    print(f"[ok] Runtime WhatsApp preparado (Node {verified} + Baileys) en {wa_dir}")
    return {"ready": True, "node_version": verified, "path": str(wa_dir)}


def _write_manifest(paths: AppPaths, summary: dict[str, Any]) -> None:
    payload = {
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "runtimes": summary,
    }
    tmp_path = paths.runtimes_manifest_file.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, paths.runtimes_manifest_file)


def _check_all(paths: AppPaths) -> int:
    vosk_ok = _is_vosk_ready(paths)
    ffmpeg_ver = _verify_ffmpeg(paths.ffmpeg_exe)
    wa_ver = _verify_whatsapp_runtime(paths)

    print(f"USER_DATA_DIR: {paths.user_data_dir}")
    print(f"  - Vosk ES   : {'OK' if vosk_ok else 'FALTA'} ({paths.vosk_es_dir})")
    print(f"  - FFmpeg    : {'OK (' + ffmpeg_ver + ')' if ffmpeg_ver else 'FALTA'}")
    print(f"  - WhatsApp  : {'OK (Node ' + wa_ver + ' + Baileys)' if wa_ver else 'FALTA'}")
    return 0 if (vosk_ok and ffmpeg_ver and wa_ver) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepara los runtimes offline de DeusEx v3 en USER_DATA_DIR."
    )
    parser.add_argument(
        "--only",
        choices=("all", "vosk", "ffmpeg", "whatsapp"),
        default="all",
        help="Subconjunto de runtimes a preparar (por defecto: all).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-descarga y reinstala aunque el runtime ya esté verificado.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verifica si los runtimes están listos sin descargar nada.",
    )
    parser.add_argument(
        "--user-data-dir",
        type=Path,
        default=None,
        help="Sobrescribe USER_DATA_DIR (opcional).",
    )
    args = parser.parse_args(argv)

    paths = resolve_app_paths(user_data_override=args.user_data_dir)
    paths.ensure_user_dirs()

    if args.check_only:
        return _check_all(paths)

    summary: dict[str, Any] = {}
    if args.only in ("all", "vosk"):
        summary["vosk_es"] = _setup_vosk(paths, force=args.force)
    if args.only in ("all", "ffmpeg"):
        summary["ffmpeg"] = _setup_ffmpeg(paths, force=args.force)
    if args.only in ("all", "whatsapp"):
        summary["whatsapp"] = _setup_whatsapp(paths, force=args.force)

    _write_manifest(paths, summary)
    print(f"[listo] Manifiesto guardado en {paths.runtimes_manifest_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
