from __future__ import annotations

import json

import pytest

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
    captured = {}

    def fake_probe_soarm_ports(*args, **kwargs):
        captured["kwargs"] = kwargs
        return [
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
        ]

    monkeypatch.setattr(
        cli,
        "probe_soarm_ports",
        fake_probe_soarm_ports,
    )

    cli.main(["scan", "--probe-arms"])

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
    assert captured["kwargs"] == {"ids": None}
    assert payload["next_steps"] == [
        "Install or activate soarm-sdk in this Python environment; "
        "the SDK imports as package 'soarm_sdk'.",
        "Then probe again before debugging hardware.",
    ]


def test_probe_arms_output_passes_custom_ids(monkeypatch, capsys) -> None:
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
    captured = {}

    def fake_probe_soarm_ports(*args, **kwargs):
        captured["kwargs"] = kwargs
        return [
            SOARMPortProbe(
                device="/dev/cu.usbmodem5A7C1190351",
                ok=True,
                expected_ids=[1, 2, 3],
                online_ids=[1, 2, 3],
            )
        ]

    monkeypatch.setattr(cli, "probe_soarm_ports", fake_probe_soarm_ports)

    cli.main(["scan", "--probe-arms", "--ids", "1,2,3"])

    assert captured["kwargs"] == {"ids": [1, 2, 3]}
    payload = json.loads(capsys.readouterr().out)
    assert payload["next_steps"] == [
        "Use the verified devices in setup arms as leader/follower ports.",
    ]


def test_probe_arms_output_guides_serial_permission_error(monkeypatch, capsys) -> None:
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
                    "[Errno 1] could not open port /dev/cu.usbmodem5A7C1190351: "
                    "[Errno 1] Operation not permitted"
                ),
            )
        ],
    )

    cli.main(["scan", "--probe-arms"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_steps"] == [
        "This process cannot open the serial port. On Ubuntu, add your user to dialout "
        "and log out/in; on macOS, check Terminal permissions. Also close apps using "
        "the port and rerun from the soarm-studio env.",
    ]


def test_calibrate_exits_nonzero_when_result_fails(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "session.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "test",
                "leader": {"mock": True},
                "follower": {"mock": True},
            }
        )
    )
    monkeypatch.setattr(
        cli,
        "calibrate_session",
        lambda config, *, role, announce=None: {"ok": False, "role": role, "results": []},
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["calibrate", "--config", str(config_path), "--role", "leader"])

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "标定机械臂: 主臂" in output
    assert "标定结果: 失败 (主臂)" in output
    assert "调试: 加 --debug 查看完整诊断报告；加 --json 导出完整 JSON。" in output


def test_calibrate_debug_includes_cleaned_full_report(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "session.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "test",
                "leader": {"mock": True},
                "follower": {"mock": True},
            }
        )
    )
    monkeypatch.setattr(
        cli,
        "calibrate_session",
        lambda config, *, role, announce=None: {
            "ok": True,
            "role": role,
            "results": [
                {
                    "role": "leader",
                    "ok": True,
                    "report": ["\x1b[32mdebug line\x1b[0m"],
                }
            ],
        },
    )

    cli.main(["calibrate", "--config", str(config_path), "--role", "leader", "--debug"])

    output = capsys.readouterr().out
    assert "完整报告:" in output
    assert "debug line" in output
    assert "提示: 不带 --debug 可只显示摘要；--json 可导出完整机器可读结果。" in output


def test_calibrate_rejects_legacy_verbose_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["calibrate", "--verbose"])

    assert exc.value.code == 2
    assert "unrecognized arguments: --verbose" in capsys.readouterr().err


def test_teleop_debug_enables_detailed_metrics(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "session.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "test",
                "leader": {"mock": True},
                "follower": {"mock": True},
            }
        )
    )
    captured = {}

    def fake_handle_teleop(config, *, seconds, free_test, debug, follower_readback_every):
        captured.update(
            {
                "session": config.name,
                "seconds": seconds,
                "free_test": free_test,
                "debug": debug,
                "follower_readback_every": follower_readback_every,
            }
        )

    monkeypatch.setattr(cli, "_handle_teleop", fake_handle_teleop)

    cli.main(
        [
            "teleop",
            "--config",
            str(config_path),
            "--seconds",
            "3",
            "--free-test",
            "--debug",
            "--follower-readback-every",
            "10",
        ]
    )

    assert captured == {
        "session": "test",
        "seconds": 3.0,
        "free_test": True,
        "debug": True,
        "follower_readback_every": 10,
    }


def test_teleop_rejects_legacy_profile_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["teleop", "--profile"])

    assert exc.value.code == 2
    assert "unrecognized arguments: --profile" in capsys.readouterr().err


def test_record_manual_requires_interactive_terminal(tmp_path) -> None:
    config_path = tmp_path / "session.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "test",
                "leader": {"mock": True},
                "follower": {"mock": True},
            }
        )
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["record", "--config", str(config_path), "--save-policy", "manual"])

    assert str(exc.value) == "--save-policy manual requires an interactive terminal"


def test_calibrate_json_preserves_machine_readable_result(tmp_path, monkeypatch, capsys) -> None:
    config_path = tmp_path / "session.json"
    config_path.write_text(
        json.dumps(
            {
                "name": "test",
                "leader": {"mock": True},
                "follower": {"mock": True},
            }
        )
    )
    monkeypatch.setattr(
        cli,
        "calibrate_session",
        lambda config, *, role, announce=None: {"ok": False, "role": role, "results": []},
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["calibrate", "--config", str(config_path), "--role", "leader", "--json"])

    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "role": "leader", "results": []}
