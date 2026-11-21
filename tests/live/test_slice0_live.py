from __future__ import annotations

import asyncio
import ctypes
import importlib
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from deusex.core.db import Database
from deusex.core.events import EventBus, PrivacyModeChangedEvent
from deusex.core.paths import resolve_app_paths
from deusex.core.runtime import RuntimeServices

_COINIT_APARTMENTTHREADED = 0x2
_S_FALSE = 1

_CRITICAL_PRODUCTION_MODULES: tuple[str, ...] = (
    "PySide6.QtWidgets",
    "pydantic",
    "google.genai",
    "httpx",
    "websockets",
    "numpy",
    "vosk",
    "sounddevice",
    "edge_tts",
    "psutil",
    "pynvml",
    "send2trash",
    "mss",
    "PIL.Image",
    "trafilatura",
    "pypdf",
    "youtube_transcript_api",
    "zeroconf",
    "pychromecast",
    "mutagen",
    "qrcode",
    "discord",
    "davey",
    "nacl",
    "audioop",
    "yt_dlp",
)

_WIN32_PRODUCTION_MODULES: tuple[str, ...] = (
    "pycaw.pycaw",
    "comtypes",
    "uiautomation",
    "winrt.windows.media.control",
)


@pytest.mark.live
def test_slice0_live_production_imports() -> None:
    """Verifica en vivo que todo el stack Python 3.14 de producción importa sin errores."""
    modules = list(_CRITICAL_PRODUCTION_MODULES)
    if sys.platform == "win32":
        modules.extend(_WIN32_PRODUCTION_MODULES)

    for module_name in modules:
        imported = importlib.import_module(module_name)
        assert imported is not None


@pytest.mark.live
def test_slice0_live_local_runtimes_ready() -> None:
    """Verifica que Vosk ES, FFmpeg portable y Node.js + Baileys estén listos en USER_DATA_DIR."""
    paths = resolve_app_paths()

    assert (paths.vosk_es_dir / "am" / "final.mdl").is_file()
    assert (paths.vosk_es_dir / "conf" / "model.conf").is_file()

    assert paths.ffmpeg_exe.is_file()
    ffmpeg_proc = subprocess.run(
        [str(paths.ffmpeg_exe), "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert ffmpeg_proc.returncode == 0

    node_exe = paths.whatsapp_runtime_dir / "node" / "node.exe"
    assert node_exe.is_file()
    wa_proc = subprocess.run(
        [
            str(node_exe),
            "-e",
            "import('@whiskeysockets/baileys').then(() => console.log('BAILEYS_OK'))",
        ],
        cwd=str(paths.whatsapp_runtime_dir),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert wa_proc.returncode == 0
    assert "BAILEYS_OK" in wa_proc.stdout


@pytest.mark.live
def test_slice0_live_four_threads_com_sta_wal_and_fast_shutdown(tmp_path: Path) -> None:
    """Prueba real en Windows 11: 4 hilos + COM STA + SQLite WAL + EventBus + cierre <3 s."""
    bus = EventBus()
    runtime = RuntimeServices(bus)
    db = Database(tmp_path / "slice0_live.db")

    received = threading.Event()
    async_tid: list[int] = []

    async def _on_privacy(_event: PrivacyModeChangedEvent) -> None:
        await asyncio.sleep(0.01)
        async_tid.append(threading.get_ident())
        received.set()

    bus.subscribe(PrivacyModeChangedEvent, _on_privacy)

    db.open()
    runtime.start()
    try:
        assert db.journal_mode() == "wal"
        assert db.integrity_ok() is True

        if sys.platform == "win32":
            assert runtime.com_sta.com_initialized is True

            def _verify_sta_apartment() -> int:
                windll = ctypes.windll
                hr = int(windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED))
                if hr in (0, _S_FALSE):
                    windll.ole32.CoUninitialize()
                return hr

            # S_FALSE (1) demuestra que ComStaThread ya está inicializado en modo STA real.
            assert runtime.com_sta.run_sync(_verify_sta_apartment) == _S_FALSE

        bus.publish(PrivacyModeChangedEvent(enabled=True))
        assert received.wait(timeout=2.0) is True
        assert async_tid == [runtime.async_core.thread_id]
    finally:
        t0 = time.monotonic()
        shutdown_sec = runtime.shutdown()
        db.close()
        total_close_sec = time.monotonic() - t0

    assert shutdown_sec < 3.0
    assert total_close_sec < 3.0
    assert db.is_open is False
