# FILE: tests/unit/test_network_adapters_module.py
from __future__ import annotations

import json
from pathlib import Path
import struct
import threading
from typing import Any

import httpx
import pytest

from deusex.app import DeusExApplication
from deusex.core.config import SecretsManager
from deusex.core.contracts import ModuleState
from deusex.core.db import Database
from deusex.core.paths import resolve_app_paths
from deusex.core.permissions import Permission
from deusex.core.runtime import ComStaThread
from deusex.modules.network.adapters import AdapterActionResult, DeviceType, NetworkDeviceRecord
from deusex.modules.network.cast import ChromecastAdapter, guess_cast_mime_type
from deusex.modules.network.miracast import MiracastAdapter, resolve_display_switch_flag
from deusex.modules.network.samsung import (
    SamsungTizenAdapter,
    build_tizen_key_payload,
    build_tizen_ws_url,
    resolve_tizen_app_id,
    resolve_tizen_key,
)
from deusex.modules.network.store import DeviceAliasStore
from deusex.modules.network.xbox import (
    XboxSmartGlassAdapter,
    build_xbox_discovery_packet,
    build_xbox_power_on_packet,
    parse_xbox_live_id_from_response,
)
from deusex.modules.network.youtube_tv import (
    YouTubeTVAdapter,
    extract_youtube_video_id,
)


class _FakeWebSocket:
    def __init__(self, handshake: dict[str, Any]) -> None:
        self._handshake = handshake
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        _ = (exc_type, exc, tb)

    async def recv(self) -> str:
        return json.dumps(self._handshake)

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


async def test_samsung_tizen_adapter_saves_token_in_env_and_sends_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    db = Database(paths.db_file)
    db.open()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    store = DeviceAliasStore(db, secrets)
    try:
        device = store.upsert_device(
            NetworkDeviceRecord(
                device_uid="mac:00005e0053aa",
                alias="TV Samsung",
                device_type=DeviceType.SAMSUNG_TV,
                last_ip="198.51.100.12",
                mac_address="00:00:5E:00:53:AA",
                port=8002,
            ),
            preserve_alias=False,
        )

        url, use_tls = build_tizen_ws_url("198.51.100.12", port=8002, token="abc123")
        assert use_tls is True
        assert url.startswith("wss://198.51.100.12:8002/")
        assert "token=abc123" in url
        assert resolve_tizen_key("vol_up") == "KEY_VOLUP"
        assert resolve_tizen_app_id("youtube") == "111299001912"
        assert build_tizen_key_payload("KEY_MUTE")["params"]["DataOfCmd"] == "KEY_MUTE"

        fake_ws = _FakeWebSocket({"event": "ms.channel.connect", "data": {"token": "new_tv_tok_42"}})
        monkeypatch.setattr(
            "deusex.modules.network.samsung.websockets.connect",
            lambda *args, **kwargs: fake_ws,
        )

        adapter = SamsungTizenAdapter(store)
        res = await adapter.set_volume(device, delta=2)
        assert res.ok is True
        assert len(fake_ws.sent) == 2
        assert fake_ws.sent[0]["params"]["DataOfCmd"] == "KEY_VOLUP"
        assert store.get_token(device.device_uid, kind="tizen") == "new_tv_tok_42"

        wol_calls: list[tuple[str, str]] = []

        def _fake_wol(mac: str, *, target_ip: str = "") -> bool:
            wol_calls.append((mac, target_ip))
            return True

        monkeypatch.setattr(
            "deusex.modules.network.samsung.send_wake_on_lan",
            _fake_wol,
        )
        on_res = await adapter.power_on(device)
        assert on_res.ok is True
        assert wol_calls == [
            ("00:00:5E:00:53:AA", "198.51.100.12"),
            ("00:00:5E:00:53:AA", "198.51.100.12"),
        ]
    finally:
        db.close()


async def test_cast_miracast_youtube_and_xbox_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    db = Database(paths.db_file)
    db.open()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    store = DeviceAliasStore(db, secrets)
    com_sta = ComStaThread(name="TestComStaNetwork")
    com_sta.start()
    try:
        # 1. Chromecast helpers y manejo de IP faltante
        assert guess_cast_mime_type("https://example.test/video.m3u8") == "application/x-mpegURL"
        assert guess_cast_mime_type("https://example.test/clip.mp4") == "video/mp4"
        cast_adapter = ChromecastAdapter()
        no_ip_dev = NetworkDeviceRecord(
            device_uid="cast:1",
            alias="Chromecast",
            device_type=DeviceType.CHROMECAST,
        )
        cast_res = await cast_adapter.media_control(no_ip_dev, "pause")
        assert cast_res.ok is False
        assert cast_res.error_code == "missing_ip"
        await cast_adapter.close()

        # 2. Miracast ejecuta el worker UIA estrictamente en ComStaThread
        assert resolve_display_switch_flag("duplicar") == "/clone"
        assert resolve_display_switch_flag("extender") == "/extend"
        miracast = MiracastAdapter(com_sta, wol_wait_sec=0.0, ui_wait_sec=0.5)
        called_threads: list[int | None] = []

        def _fake_win_k(queries: list[str], wait_sec: float) -> AdapterActionResult:
            _ = (queries, wait_sec)
            called_threads.append(threading.get_ident())
            return AdapterActionResult(ok=True, message="Proyección simulada")

        monkeypatch.setattr(miracast, "_connect_win_k_sta", _fake_win_k)
        proj_res = await miracast.project_to_device(
            NetworkDeviceRecord(
                device_uid="miracast:tv",
                alias="TV Sala",
                device_type=DeviceType.MIRACAST,
                online=True,
            ),
            display_mode="",
        )
        assert proj_res.ok is True
        assert called_threads == [com_sta.thread_id]

        # 3. YouTube TV extractor + Lounge API con MockTransport
        assert extract_youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert extract_youtube_video_id("https://youtu.be/dQw4w9WgXcQ?t=10") == "dQw4w9WgXcQ"
        assert extract_youtube_video_id("https://youtube.com/shorts/abcdefghijk") == "abcdefghijk"
        assert extract_youtube_video_id("https://example.com/no-video") is None

        tv_dev = store.upsert_device(
            NetworkDeviceRecord(
                device_uid="mac:00005e0053bb",
                alias="TV Lounge",
                device_type=DeviceType.YOUTUBE_TV,
                last_ip="198.51.100.44",
                mac_address="00:00:5E:00:53:BB",
            ),
            preserve_alias=False,
        )

        def _lounge_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/pairing/get_screen"):
                return httpx.Response(
                    200,
                    json={"screen": {"screenId": "scr_001", "loungeToken": "lounge_tok_xyz"}},
                )
            if request.url.path.endswith("/bc/bind"):
                return httpx.Response(200, text="ok")
            return httpx.Response(404)

        yt_adapter = YouTubeTVAdapter(store)
        yt_adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(_lounge_handler))
        pair_res = await yt_adapter.pair_with_code(tv_dev, "123-456-789")
        assert pair_res.ok is True
        assert store.get_token(tv_dev.device_uid, kind="lounge") == "lounge_tok_xyz"

        refreshed_tv = store.get_device(tv_dev.device_uid)
        assert refreshed_tv is not None
        send_res = await yt_adapter.send_video(
            refreshed_tv,
            url_or_id="https://youtu.be/dQw4w9WgXcQ",
        )
        assert send_res.ok is True
        assert send_res.data["video_id"] == "dQw4w9WgXcQ"
        await yt_adapter.close()

        # 4. Xbox SmartGlass paquetes UDP y power_on mockeado
        disc_pkt = build_xbox_discovery_packet()
        assert disc_pkt[:2] == b"\xdd\x00"
        pwr_pkt = build_xbox_power_on_packet("FD00AABBCCDDEEFF")
        assert pwr_pkt[:2] == b"\xdd\x02"
        assert b"FD00AABBCCDDEEFF" in pwr_pkt
        fake_resp = struct.pack("!HHH", 0xDD01, 20, 0x0002) + b"hdrFD00AABBCCDDEEFF\x00"
        assert parse_xbox_live_id_from_response(fake_resp) == "FD00AABBCCDDEEFF"

        xbox_dev = store.upsert_device(
            NetworkDeviceRecord(
                device_uid="mac:00005e0053cc",
                alias="Xbox Sala",
                device_type=DeviceType.XBOX,
                last_ip="198.51.100.55",
                mac_address="00:00:5E:00:53:CC",
                port=5050,
                metadata={"live_id": "FD00AABBCCDDEEFF"},
            ),
            preserve_alias=False,
        )
        xbox_adapter = XboxSmartGlassAdapter(store)
        monkeypatch.setattr(
            "deusex.modules.network.xbox._send_xbox_power_on_sync",
            lambda live_id, *, target_ip="", bursts=4: live_id == "FD00AABBCCDDEEFF",
        )
        monkeypatch.setattr(
            "deusex.modules.network.xbox.send_wake_on_lan",
            lambda mac, *, target_ip="": True,
        )
        xbox_res = await xbox_adapter.power_on(xbox_dev)
        assert xbox_res.ok is True
        assert xbox_res.data["transport"] == "smartglass+wol"
    finally:
        com_sta.stop(timeout=1.0)
        db.close()


def test_network_module_reconciles_antidote2_fast_path_and_permissions(tmp_path: Path) -> None:
    app1 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    try:
        reconciled_initial = app1.start()
        assert "network" not in reconciled_initial
        assert app1.network_module.state == ModuleState.DISABLED

        # Activar `network` y persistir un dispositivo de prueba
        enabled_ok = app1.set_module_enabled("network", True).result(timeout=5.0)
        assert enabled_ok is True
        assert app1.network_module.state == ModuleState.ACTIVE  # type: ignore[comparison-overlap]
        assert app1.network_module.store is not None
        app1.network_module.store.upsert_device(
            NetworkDeviceRecord(
                device_uid="mac:00005e0053dd",
                alias="TV Comedor",
                device_type=DeviceType.SAMSUNG_TV,
                last_ip="203.0.113.10",
                mac_address="00:00:5E:00:53:DD",
                port=8002,
            ),
            preserve_alias=False,
        )
    finally:
        elapsed1 = app1.shutdown()
        assert elapsed1 < 3.0

    # Reinicio real (Antídoto #2): `network` debe reconciliarse automáticamente desde `settings.json`
    app2 = DeusExApplication(
        user_data_dir=tmp_path,
        enable_hotkeys=False,
        enable_earcons=False,
        fallback_to_os_env=False,
    )
    try:
        reconciled_second = app2.start()
        assert "network" in reconciled_second
        assert app2.network_module.state == ModuleState.ACTIVE

        tool_names = {spec.name for spec in app2.network_module.get_tools()}
        assert {
            "network_scan",
            "network_power",
            "network_volume",
            "network_send_youtube",
            "network_set_favorite",
        }.issubset(tool_names)

        assert app2.network_module.store is not None
        # Dispositivo Chromecast alfabéticamente anterior a "TV Comedor"
        app2.network_module.store.upsert_device(
            NetworkDeviceRecord(
                device_uid="chromecast:dormitorio",
                alias="Dormitorio",
                device_type=DeviceType.CHROMECAST,
                last_ip="203.0.113.20",
                mac_address="00:00:5E:00:53:DE",
            ),
            preserve_alias=False,
        )
        # Marcar "TV Comedor" como favorita
        fav_res = app2.runtime.async_core.run_sync(
            app2.network_module.set_favorite_device("TV Comedor", favorite=True),
            timeout=5.0,
        )
        assert fav_res.ok is True

        # "tv" genérico debe elegir la favorita ("TV Comedor") antes que "Dormitorio"
        resolved_tv = app2.network_module._resolve_device("tv")
        assert resolved_tv is not None
        assert resolved_tv.alias == "TV Comedor"
        assert resolved_tv.is_favorite is True

        # "tv samsung" por tokens debe resolver a "TV Comedor" (Samsung TV)
        resolved_samsung = app2.network_module._resolve_device("tv samsung")
        assert resolved_samsung is not None
        assert resolved_samsung.alias == "TV Comedor"

        # Activar dry_run para probar reglas Fast-Path sin tocar red real
        app2.settings.developer.dry_run = True
        app2.save_settings(section="developer")

        for command in (
            "escanea la red",
            "enciende la tv",
            "enciende la tv samsung",
            "sube el volumen de la tv",
            "pausa la tv",
            "proyecta mi pantalla a la tv",
            "manda este video a la tv",
        ):
            turn = app2.submit_user_message(command).result(timeout=5.0)
            assert turn.fast_path is True, f"No entró en Fast-Path: {command}"
            assert "Dry-Run" in turn.reply_text

        # Verificar bloqueo por permiso tipado `Permission.NETWORK_CONTROL` (Antídoto #3)
        app2.set_permission(Permission.NETWORK_CONTROL, False)
        denied = app2.runtime.async_core.run_sync(
            app2.network_module.control_power("TV Comedor", "on"),
            timeout=5.0,
        )
        assert denied.ok is False
        assert denied.error_code == "permission_denied"

        # Diagnósticos del Doctor
        diagnostics = app2.network_module.diagnose()
        assert len(diagnostics) >= 2
    finally:
        elapsed2 = app2.shutdown()
        assert elapsed2 < 3.0
