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
    assert payload["summary"] == {
        "arm_ports": 1,
        "cameras": 1,
        "ignored_system_ports": 1,
    }
    assert payload["arm_ports"] == [
        {
            "device": "/dev/cu.usbmodem5A7C1190351",
            "location": "0-1.4",
            "serial": "5A7C119035",
            "usb_id": "0x1a86:0x55d3",
        }
    ]
    assert payload["cameras"][0]["location_id"] == "0x00110000"
    assert payload["cameras"][0]["usb_id"] == "0x0c45:0x64ab"
    assert "ignored_ports" not in payload
    assert "preferred_ports" not in payload
    assert "soarm_candidate_ports" not in payload
    assert "notes" not in payload
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
                error=(
                    "Cannot import SDK package 'soarm_sdk' from soarm-sdk: "
                    "No module named 'soarm_sdk'"
                ),
            )
        ],
    )

    cli.main(["scan", "--probe-arms", "--arm-config", "../soarm-sdk/configs/soarm-sdk.yaml"])

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"summary", "arm_ports", "next_steps"}
    assert payload["summary"] == {
        "ok": False,
        "online_ports": 0,
        "probed_ports": 1,
    }
    assert payload["arm_ports"] == [
        {
            "device": "/dev/cu.usbmodem5A7C1190351",
            "ok": False,
            "error": (
                "Cannot import SDK package 'soarm_sdk' from soarm-sdk: "
                "No module named 'soarm_sdk'"
            ),
        }
    ]
    assert payload["next_steps"] == [
        "Install or activate soarm-sdk in this Python environment; "
        "the SDK imports as package 'soarm_sdk'.",
        "Then probe again before debugging hardware.",
    ]
