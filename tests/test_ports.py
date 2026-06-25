from __future__ import annotations

import sys
import types

from soarm_studio.hardware import detect_serial_ports
from soarm_studio.hardware.ports import DEFAULT_PROBE_SERVO_IDS, _build_info, probe_soarm_port


def test_public_detect_serial_ports_returns_port_info() -> None:
    ports = detect_serial_ports()

    assert isinstance(ports, list)
    if ports:
        assert hasattr(ports[0], "to_dict")


def test_port_info_prefers_cu_side() -> None:
    devices = {"/dev/cu.usbmodem123", "/dev/tty.usbmodem123"}

    cu = _build_info({"device": "/dev/cu.usbmodem123", "name": "cu.usbmodem123"}, devices)
    tty = _build_info({"device": "/dev/tty.usbmodem123", "name": "tty.usbmodem123"}, devices)

    assert cu.preferred_for_connection is True
    assert cu.soarm_candidate is True
    assert cu.paired_device == "/dev/tty.usbmodem123"
    assert tty.preferred_for_connection is False
    assert tty.soarm_candidate is False
    assert tty.paired_device == "/dev/cu.usbmodem123"
    assert tty.role_hint == "usb-serial"


def test_port_info_prefers_linux_tty_devices() -> None:
    info = _build_info({"device": "/dev/ttyACM0", "name": "ttyACM0"}, {"/dev/ttyACM0"})

    assert info.connection == "linux-tty"
    assert info.preferred_for_connection is True
    assert info.soarm_candidate is True


def test_port_info_filters_system_hint() -> None:
    info = _build_info(
        {"device": "/dev/cu.Bluetooth-Incoming-Port", "name": "cu.Bluetooth-Incoming-Port"},
        {"/dev/cu.Bluetooth-Incoming-Port"},
    )

    assert info.role_hint == "system"
    assert info.preferred_for_connection is False


def test_probe_soarm_port_uses_default_probe_ids_without_config(monkeypatch) -> None:
    captured = {}

    class FakeServoBus:
        def __init__(self, *, servo_ids, port, baudrate, auto_disable):
            captured["servo_ids"] = servo_ids
            captured["port"] = port
            captured["baudrate"] = baudrate
            captured["auto_disable"] = auto_disable

        def connect(self):
            captured["connected"] = True

        def scan(self, ids):
            captured["scan_ids"] = ids
            return {servo_id: True for servo_id in ids}

        def disconnect(self):
            captured["disconnected"] = True

    package = types.ModuleType("soarm_sdk")
    package.__path__ = []
    constants = types.ModuleType("soarm_sdk.constants")
    constants.DEFAULT_BAUDRATE = 123456
    hardware = types.ModuleType("soarm_sdk.hardware")
    hardware.ServoBus = FakeServoBus
    monkeypatch.setitem(sys.modules, "soarm_sdk", package)
    monkeypatch.setitem(sys.modules, "soarm_sdk.constants", constants)
    monkeypatch.setitem(sys.modules, "soarm_sdk.hardware", hardware)

    result = probe_soarm_port("/dev/cu.usbmodem123")

    assert result.ok is True
    assert result.expected_ids == DEFAULT_PROBE_SERVO_IDS
    assert result.online_ids == DEFAULT_PROBE_SERVO_IDS
    assert captured == {
        "auto_disable": False,
        "baudrate": 123456,
        "connected": True,
        "disconnected": True,
        "port": "/dev/cu.usbmodem123",
        "scan_ids": DEFAULT_PROBE_SERVO_IDS,
        "servo_ids": DEFAULT_PROBE_SERVO_IDS,
    }
