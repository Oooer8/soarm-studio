from __future__ import annotations

from soarm_studio.hardware import detect_serial_ports
from soarm_studio.hardware.ports import _build_info


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


def test_port_info_filters_system_hint() -> None:
    info = _build_info(
        {"device": "/dev/cu.Bluetooth-Incoming-Port", "name": "cu.Bluetooth-Incoming-Port"},
        {"/dev/cu.Bluetooth-Incoming-Port"},
    )

    assert info.role_hint == "system"
    assert info.preferred_for_connection is False
