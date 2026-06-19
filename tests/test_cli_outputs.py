from __future__ import annotations

import json

from soarm_studio import cli
from soarm_studio.hardware.cameras import CameraDeviceInfo
from soarm_studio.hardware.ports import SOARMPortProbe, _build_info


def test_scan_output_is_compact(monkeypatch, capsys) -> None:
    ports = [
        _build_info(
            {
                "device": "/dev/cu.usbmodem5A7C1190351",
                "name": "cu.usbmodem5A7C1190351",
                "description": "USB Single Serial",
                "hwid": "USB VID:PID=1A86:55D3 SER=5A7C119035 LOCATION=0-1.4",
                "vid": "0x1a86",
                "pid": "0x55d3",
                "serial_number": "5A7C119035",
                "product": "USB Single Serial",
                "location": "0-1.4",
            },
            {"/dev/cu.usbmodem5A7C1190351"},
        ),
        _build_info(
            {
                "device": "/dev/cu.Bluetooth-Incoming-Port",
                "name": "cu.Bluetooth-Incoming-Port",
            },
            {"/dev/cu.Bluetooth-Incoming-Port"},
        ),
    ]
    cameras = [
        CameraDeviceInfo(
            name="DSJ-2062-309",
            source="ioreg",
            role_hint="usb-camera",
            vid="0x0c45",
            pid="0x64ab",
            location_id="0x00110000",
            usb_address=3,
            device_class=239,
            device_subclass=2,
            device_protocol=1,
            notes=("USB video-class composite device.",),
        )
    ]
    monkeypatch.setattr(cli, "detect_serial_ports", lambda *, include_system: ports)
    monkeypatch.setattr(cli, "detect_camera_devices", lambda **kwargs: cameras)

    cli.main(["scan", "--include-system"])

    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload)
    assert payload["ports"] == [
        {
            "device": "/dev/cu.usbmodem5A7C1190351",
            "location": "0-1.4",
            "pid": "0x55d3",
            "preferred_for_connection": True,
            "product": "USB Single Serial",
            "serial_number": "5A7C119035",
            "soarm_candidate": True,
            "vid": "0x1a86",
        }
    ]
    assert payload["ignored_ports"][0]["device"] == "/dev/cu.Bluetooth-Incoming-Port"
    assert payload["cameras"][0]["location_id"] == "0x00110000"
    assert "USB VID:PID=1A86:55D3" not in rendered
    assert "device_class" not in rendered


def test_probe_arms_output_omits_repeated_scan_inventory(monkeypatch, capsys) -> None:
    ports = [
        _build_info(
            {
                "device": "/dev/cu.usbmodem5A7C1190351",
                "name": "cu.usbmodem5A7C1190351",
            },
            {"/dev/cu.usbmodem5A7C1190351"},
        )
    ]
    monkeypatch.setattr(cli, "detect_serial_ports", lambda *, include_system: ports)
    monkeypatch.setattr(
        cli,
        "probe_soarm_ports",
        lambda *args, **kwargs: [
            SOARMPortProbe(
                device="/dev/cu.usbmodem5A7C1190351",
                ok=False,
                expected_ids=[],
                online_ids=[],
                error="soarm-sdk is not importable: No module named 'soarm'",
            )
        ],
    )

    cli.main(["scan", "--probe-arms", "--arm-config", "../soarm-sdk/configs/soarm.yaml"])

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"arm_probe", "notes", "preferred_ports"}
    assert payload["arm_probe"] == [
        {
            "device": "/dev/cu.usbmodem5A7C1190351",
            "error": "soarm-sdk is not importable: No module named 'soarm'",
            "ok": False,
        }
    ]
