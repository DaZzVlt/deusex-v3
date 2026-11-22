# FILE: tests/unit/test_network_store_discovery.py
from __future__ import annotations

from pathlib import Path
import struct

import pytest

from deusex.core.config import SecretsManager
from deusex.core.db import Database
from deusex.core.paths import resolve_app_paths
from deusex.modules.network.adapters import DeviceType, NetworkDeviceRecord, normalize_mac
from deusex.modules.network.discovery import (
    DiscoveredDevice,
    NetworkDiscoveryService,
    _parse_upnp_device_xml,
    _parse_xbox_discovery_response,
    parse_arp_output,
)
from deusex.modules.network.store import DeviceAliasStore


def _make_store(tmp_path: Path) -> tuple[Database, SecretsManager, DeviceAliasStore]:
    paths = resolve_app_paths(user_data_override=tmp_path)
    paths.ensure_user_dirs()
    db = Database(paths.db_file)
    db.open()
    secrets = SecretsManager(paths, fallback_to_os_env=False)
    store = DeviceAliasStore(db, secrets)
    return db, secrets, store


def test_normalize_mac_handles_formats_and_rejects_invalid() -> None:
    assert normalize_mac("00-00-5e-00-53-af") == "00:00:5E:00:53:AF"
    assert normalize_mac("0000.5e00.5311") == "00:00:5E:00:53:11"
    assert normalize_mac("00:00:00:00:00:00") == ""
    assert normalize_mac("FF:FF:FF:FF:FF:FF") == ""
    assert normalize_mac("invalid") == ""


def test_device_alias_store_persists_aliases_and_isolates_tokens_in_env(tmp_path: Path) -> None:
    db, secrets, store = _make_store(tmp_path)
    try:
        record = NetworkDeviceRecord(
            device_uid="mac:00005e005301",
            alias="TV Living",
            device_type=DeviceType.SAMSUNG_TV,
            last_ip="192.0.2.10",
            mac_address="00-00-5e-00-53-01",
            port=8002,
            metadata={
                "model": "UN55",
                "auth_token": "MUST_NOT_BE_IN_SQLITE",
                "nested": {"secret_key": "HIDDEN", "os": "Tizen"},
            },
        )
        saved = store.upsert_device(record, preserve_alias=True)
        assert saved.mac_address == "00:00:5E:00:53:01"
        assert "auth_token" not in saved.metadata
        assert saved.metadata["nested"] == {"os": "Tizen"}

        # Guardar token aislado exclusivamente en `.env`
        secret_token = "tizen_ephemeral_token_987654"
        env_key = store.set_token(saved.device_uid, secret_token, kind="tizen")
        assert store.get_token(saved.device_uid, kind="tizen") == secret_token
        assert secrets.get(env_key) == secret_token

        # Verificar que el token no aparece en ninguna columna de SQLite
        row = db.fetchone(
            "SELECT device_uid, alias, metadata_json FROM device_aliases WHERE device_uid = ?",
            (saved.device_uid,),
        )
        assert row is not None
        assert secret_token not in str(row["metadata_json"])
        assert "MUST_NOT_BE_IN_SQLITE" not in str(row["metadata_json"])

        # Re-upsert conservando alias personalizado del usuario
        rediscovered = record.with_updates(alias="Samsung Smart TV", last_ip="192.0.2.25")
        merged = store.upsert_device(rediscovered, preserve_alias=True)
        assert merged.alias == "TV Living"
        assert merged.last_ip == "192.0.2.25"

        # Renombrado explícito y búsqueda tolerante a acentos/parciales
        renamed = store.rename_alias(saved.device_uid, "Tele Salón")
        assert renamed is not None
        assert renamed.alias == "Tele Salón"
        found = store.find_by_query("tele salon")
        assert found is not None
        assert found.device_uid == saved.device_uid

        # Borrado con purga de tokens en `.env`
        assert store.delete_device(saved.device_uid, purge_tokens=True) is True
        assert store.get_device(saved.device_uid) is None
        assert store.get_token(saved.device_uid, kind="tizen") is None
    finally:
        db.close()


def test_parse_arp_upnp_and_xbox_helpers() -> None:
    arp_sample = """
    Interface: 192.0.2.1 --- 0x4
      Internet Address      Physical Address      Type
      192.0.2.10            00-00-5e-00-53-10     dynamic
      192.0.2.11            00-00-5e-00-53-11     dynamic
      224.0.0.22            01-00-5e-00-00-16     static
      169.254.1.5           00-00-5e-00-53-99     dynamic
    """
    parsed_arp = parse_arp_output(arp_sample)
    assert parsed_arp == {
        "192.0.2.10": "00:00:5E:00:53:10",
        "192.0.2.11": "00:00:5E:00:53:11",
    }

    xml_doc = """<?xml version="1.0"?>
    <root xmlns="urn:schemas-upnp-org:device-1-0">
      <device>
        <friendlyName>Chromecast Sala</friendlyName>
        <manufacturer>Google Inc.</manufacturer>
        <modelName>Eureka Dongle</modelName>
        <UDN>uuid:12345678-1234-1234-1234-1234567890ab</UDN>
      </device>
    </root>
    """
    upnp = _parse_upnp_device_xml(xml_doc)
    assert upnp["friendly_name"] == "Chromecast Sala"
    assert upnp["manufacturer"] == "Google Inc."
    assert upnp["model"] == "Eureka Dongle"

    xbox_payload = b"XBOX_SERIES_TEST\x00FD00112233445566\x00"
    xbox_pkt = struct.pack("!HHH", 0xDD01, len(xbox_payload), 0x0002) + xbox_payload
    xbox_dev = _parse_xbox_discovery_response(xbox_pkt, "192.0.2.30")
    assert xbox_dev is not None
    assert xbox_dev.device_type == DeviceType.XBOX
    assert xbox_dev.metadata["live_id"] == "FD00112233445566"


def test_discovery_deduplicate_merges_protocols_by_priority() -> None:
    cand_generic = DiscoveredDevice(
        device_uid="generic:192.0.2.40",
        friendly_name="generic (192.0.2.40)",
        device_type=DeviceType.GENERIC,
        ip="192.0.2.40",
        port=8001,
        mac_address="00:00:5E:00:53:40",
        discovery_sources=("ssdp",),
        metadata={"ssdp_st": "urn:dial"},
    )
    cand_samsung = DiscoveredDevice(
        device_uid="mac:00005e005340",
        friendly_name="Samsung QLED",
        device_type=DeviceType.SAMSUNG_TV,
        ip="192.0.2.40",
        port=8002,
        mac_address="00:00:5E:00:53:40",
        manufacturer="Samsung",
        model="Q60",
        discovery_sources=("active_http",),
        metadata={"token_auth_support": True},
    )

    deduped = NetworkDiscoveryService.deduplicate_devices([cand_generic, cand_samsung])
    assert len(deduped) == 1
    dev = deduped[0]
    assert dev.device_type == DeviceType.SAMSUNG_TV
    assert dev.friendly_name == "Samsung QLED"
    assert dev.port == 8002
    assert dev.discovery_sources == ("active_http", "ssdp")
    assert dev.metadata["token_auth_support"] is True


async def test_discovery_scan_persists_and_preserves_custom_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, _secrets, store = _make_store(tmp_path)
    try:
        # Alias previo guardado por el usuario
        store.upsert_device(
            NetworkDeviceRecord(
                device_uid="mac:00005e005377",
                alias="Mi Tele Favorita",
                device_type=DeviceType.SAMSUNG_TV,
                last_ip="192.0.2.70",
                mac_address="00:00:5E:00:53:77",
                port=8002,
            ),
            preserve_alias=False,
        )

        service = NetworkDiscoveryService(store)

        async def _fake_mdns(timeout_sec: float) -> list[DiscoveredDevice]:
            _ = timeout_sec
            return [
                DiscoveredDevice(
                    device_uid="chromecast:demo",
                    friendly_name="Chromecast Estudio",
                    device_type=DeviceType.CHROMECAST,
                    ip="192.0.2.88",
                    port=8009,
                    discovery_sources=("mdns",),
                )
            ]

        async def _fake_ssdp(timeout_sec: float) -> list[DiscoveredDevice]:
            _ = timeout_sec
            return []

        async def _fake_xbox(targets: list[str], timeout_sec: float) -> list[DiscoveredDevice]:
            _ = (targets, timeout_sec)
            return []

        async def _fake_active(ips: list[str], *, timeout_sec: float = 0.65) -> list[DiscoveredDevice]:
            _ = (ips, timeout_sec)
            return [
                DiscoveredDevice(
                    device_uid="mac:00005e005377",
                    friendly_name="Samsung Series 7",
                    device_type=DeviceType.SAMSUNG_TV,
                    ip="192.0.2.77",
                    port=8002,
                    mac_address="00:00:5E:00:53:77",
                    discovery_sources=("active_http",),
                )
            ]

        monkeypatch.setattr("deusex.modules.network.discovery.get_local_ipv4_networks", lambda: [])
        monkeypatch.setattr(
            "deusex.modules.network.discovery.read_system_arp_table",
            lambda *, timeout_sec=1.5: {"192.0.2.88": "00:00:5E:00:53:88"},
        )
        monkeypatch.setattr(service, "_discover_mdns", _fake_mdns)
        monkeypatch.setattr(service, "_discover_ssdp", _fake_ssdp)
        monkeypatch.setattr(service, "_discover_xbox", _fake_xbox)
        monkeypatch.setattr(service, "_active_probe_hosts", _fake_active)

        found = await service.scan(timeout_sec=1.0, active_subnet_scan=False, persist=True)
        assert len(found) == 2

        tv = store.get_device("mac:00005e005377")
        assert tv is not None
        assert tv.alias == "Mi Tele Favorita"
        assert tv.last_ip == "192.0.2.77"

        cast = store.find_by_query("chromecast estudio")
        assert cast is not None
        assert cast.mac_address == "00:00:5E:00:53:88"
    finally:
        db.close()


def test_discovery_and_store_deduplicate_same_ip_with_different_macs(tmp_path: Path) -> None:
    db, _secrets, store = _make_store(tmp_path)
    try:
        cand_arp = DiscoveredDevice(
            device_uid="mac:00005e005350",
            friendly_name="Samsung TV (192.0.2.50)",
            device_type=DeviceType.GENERIC,
            ip="192.0.2.50",
            port=8001,
            mac_address="00:00:5E:00:53:50",
            discovery_sources=("arp",),
        )
        cand_rest = DiscoveredDevice(
            device_uid="samsung_tv:192.0.2.50",
            friendly_name="Samsung QLED 65",
            device_type=DeviceType.SAMSUNG_TV,
            ip="192.0.2.50",
            port=8002,
            mac_address="00:00:5E:00:53:51",
            manufacturer="Samsung",
            model="Q60",
            discovery_sources=("active_http",),
            metadata={"wifi_mac": "00:00:5E:00:53:51"},
        )
        deduped = NetworkDiscoveryService.deduplicate_devices([cand_arp, cand_rest])
        assert len(deduped) == 1
        dev = deduped[0]
        assert dev.device_type == DeviceType.SAMSUNG_TV
        assert dev.friendly_name == "Samsung QLED 65"
        assert dev.ip == "192.0.2.50"
        assert "00:00:5E:00:53:51" in dev.metadata.get("alt_macs", [])

        # Probar limpieza en SQLite de filas duplicadas en la misma IP
        rec1 = NetworkDeviceRecord(
            device_uid="generic:192.0.2.50",
            alias="Dispositivo Viejo",
            device_type=DeviceType.GENERIC,
            last_ip="192.0.2.50",
            mac_address="00:00:5E:00:53:50",
        )
        store.upsert_device(rec1, preserve_alias=False)
        store.set_token("generic:192.0.2.50", "token_legacy", kind="tizen")

        rec2 = NetworkDeviceRecord(
            device_uid="mac:00005e005350",
            alias="Samsung QLED 65",
            device_type=DeviceType.SAMSUNG_TV,
            last_ip="192.0.2.50",
            mac_address="00:00:5E:00:53:50",
            port=8002,
            metadata={"alt_macs": ["00:00:5E:00:53:51"]},
        )
        saved2 = store.upsert_device(rec2, preserve_alias=True)
        assert saved2.device_uid == "mac:00005e005350"
        # Verificar migración de token y eliminación de fila obsoleta
        assert store.get_token("mac:00005e005350", kind="tizen") == "token_legacy"
        assert store.get_device("generic:192.0.2.50") is None
        all_ip = [d for d in store.list_devices() if d.last_ip == "192.0.2.50"]
        assert len(all_ip) == 1
    finally:
        db.close()


def test_store_set_favorite_exclusive_and_token_search_inverted(tmp_path: Path) -> None:
    db, _secrets, store = _make_store(tmp_path)
    try:
        cast = NetworkDeviceRecord(
            device_uid="chromecast:dormitorio",
            alias="Dormitorio",
            device_type=DeviceType.CHROMECAST,
            last_ip="192.0.2.11",
            mac_address="00:00:5E:00:53:11",
        )
        store.upsert_device(cast, preserve_alias=False)

        tv = NetworkDeviceRecord(
            device_uid="mac:00005e005322",
            alias="Smart TV Living",
            device_type=DeviceType.SAMSUNG_TV,
            last_ip="192.0.2.22",
            mac_address="00:00:5E:00:53:22",
            metadata={"friendly_name": "[TV] Samsung QLED", "model": "Q60"},
        )
        store.upsert_device(tv, preserve_alias=False)

        xbox = NetworkDeviceRecord(
            device_uid="xbox:live123",
            alias="Xbox Series X",
            device_type=DeviceType.XBOX,
            last_ip="192.0.2.33",
            mac_address="00:00:5E:00:53:33",
        )
        store.upsert_device(xbox, preserve_alias=False)

        devs_initial = store.list_devices()
        assert devs_initial[0].alias == "Dormitorio"
        assert not any(d.is_favorite for d in devs_initial)

        fav_tv = store.set_favorite("mac:00005e005322", favorite=True)
        assert fav_tv is not None
        assert fav_tv.is_favorite is True

        devs_after_tv_fav = store.list_devices()
        assert devs_after_tv_fav[0].alias == "Smart TV Living"
        assert devs_after_tv_fav[0].is_favorite is True

        fav_xbox = store.set_favorite("xbox:live123", favorite=True)
        assert fav_xbox is not None
        assert fav_xbox.is_favorite is True

        devs_after_xbox_fav = store.list_devices()
        assert devs_after_xbox_fav[0].alias == "Xbox Series X"
        assert devs_after_xbox_fav[0].is_favorite is True
        tv_reloaded = store.get_device("mac:00005e005322")
        assert tv_reloaded is not None
        assert tv_reloaded.is_favorite is False

        unfav_xbox = store.set_favorite("xbox:live123", favorite=False)
        assert unfav_xbox is not None
        assert unfav_xbox.is_favorite is False
        assert not any(d.is_favorite for d in store.list_devices())

        match1 = store.find_by_query("tv samsung")
        assert match1 is not None
        assert match1.device_uid == "mac:00005e005322"

        match2 = store.find_by_query("tele samsung")
        assert match2 is not None
        assert match2.device_uid == "mac:00005e005322"

        match3 = store.find_by_query("la tv de samsung")
        assert match3 is not None
        assert match3.device_uid == "mac:00005e005322"
    finally:
        db.close()

