from __future__ import annotations

from pathlib import Path
import time

import pytest

from deusex.app import DeusExApplication
from deusex.core.config import SecretsManager
from deusex.core.paths import resolve_app_paths


def _resolve_real_gemini_key() -> str | None:
    real_paths = resolve_app_paths()
    secrets = SecretsManager(real_paths, fallback_to_os_env=True)
    return secrets.get("GEMINI_API_KEY")


@pytest.mark.live
def test_slice1_live_gemini_models_chat_fast_path_and_shutdown(tmp_path: Path) -> None:
    """
    Compuerta 4 (Rebanada 1):
    - Antídoto #1 en vivo: `refresh_models("gemini")` lista los modelos reales de google-genai.
    - Chat real en vivo con Gemini guardado en SQLite WAL.
    - Temporizador por Fast-Path (<50 ms, 0 tokens) + cierre determinista <3 s.
    """
    api_key = _resolve_real_gemini_key()
    if not api_key:
        pytest.skip(
            "Configura GEMINI_API_KEY en %LOCALAPPDATA%\\DeusEx\\.env o variable de entorno "
            "para ejecutar el smoke test en vivo de Gemini."
        )

    app = DeusExApplication(
        user_data_dir=tmp_path / "slice1_live_data",
        enable_hotkeys=False,
        enable_earcons=True,
        fallback_to_os_env=False,
    )
    app.save_api_key("GEMINI_API_KEY", api_key, provider_id="gemini")
    app.start()
    try:
        # 1. Antídoto #1 en vivo contra google-genai
        models = app.refresh_models("gemini", force=True).result(timeout=20.0)
        assert len(models) >= 5
        assert any("gemini" in m.id.lower() for m in models)

        # 2. Fast-Path local (temporizador de 5 s)
        fp_turn = app.submit_user_message("pon un temporizador de 5 segundos").result(timeout=5.0)
        assert fp_turn.fast_path is True
        assert "5 s" in fp_turn.reply_text
        assert len(app.scheduler.list_pending()) == 1

        # 3. Chat real con Gemini guardado en SQLite WAL
        live_turn = app.submit_user_message(
            "Responde únicamente con la palabra: OPERATIVO"
        ).result(timeout=25.0)
        assert live_turn.fast_path is False
        assert live_turn.error_code is None
        assert len(live_turn.reply_text.strip()) > 0
        assert app.cost_guard.today_summary().cloud_calls >= 1

        stored = app.memory.get_messages(live_turn.conversation_id)
        assert len(stored) >= 4
    finally:
        t0 = time.monotonic()
        elapsed = app.shutdown()
        wall = time.monotonic() - t0

    assert elapsed < 3.0
    assert wall < 3.0
