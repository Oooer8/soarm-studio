from __future__ import annotations

import json

import pytest

from soarm_studio import assignment
from soarm_studio.assignment import assign_arm_roles, assign_camera_roles
from soarm_studio.config import load_config_mapping


def test_assign_arm_roles_writes_arm_configs_and_session(tmp_path) -> None:
    session_path = tmp_path / "session.yaml"
    base_path = tmp_path / "base_arm.yaml"
    leader_path = tmp_path / "leader.yaml"
    follower_path = tmp_path / "follower.yaml"
    session_path.write_text(
        json.dumps(
            {
                "leader": {"name": "leader", "mock": True},
                "follower": {"name": "follower", "mock": True},
            }
        )
    )
    base_path.write_text(
        json.dumps(
            {
                "arm": {"name": "template", "port": None, "baudrate": 1000000},
                "joints": {},
                "poses": {},
            }
        )
    )

    result = assign_arm_roles(
        session_config=session_path,
        leader_port="/dev/cu.leader",
        follower_port="/dev/cu.follower",
        base_arm_config=base_path,
        leader_arm_config=leader_path,
        follower_arm_config=follower_path,
        max_relative_target=0.05,
    )

    session = load_config_mapping(session_path)
    leader = load_config_mapping(leader_path)
    follower = load_config_mapping(follower_path)

    assert session["leader"]["config"] == str(leader_path)
    assert session["follower"]["config"] == str(follower_path)
    assert session["follower"]["max_relative_target"] == 0.05
    assert leader["arm"]["name"] == "soarm-sdk-leader"
    assert leader["arm"]["port"] == "/dev/cu.leader"
    assert follower["arm"]["name"] == "soarm-sdk-follower"
    assert follower["arm"]["port"] == "/dev/cu.follower"
    assert result["warnings"]


def test_assign_arm_roles_rewrites_base_include_paths(tmp_path) -> None:
    project = tmp_path / "project"
    sdk = tmp_path / "soarm-sdk"
    session_path = project / "configs" / "session.yaml"
    base_path = sdk / "configs" / "soarm-sdk.yaml"
    runtime_path = sdk / "configs" / "runtime.yaml"
    motor_path = sdk / "configs" / "motors" / "feetech_sts3215.yaml"
    leader_path = project / "configs" / "arms" / "leader.yaml"
    follower_path = project / "configs" / "arms" / "follower.yaml"

    session_path.parent.mkdir(parents=True)
    runtime_path.parent.mkdir(parents=True)
    motor_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({}))
    runtime_path.write_text(json.dumps({"arm": {"control_hz": 200}}))
    motor_path.write_text(json.dumps({"enabled": True}))
    base_path.write_text(
        json.dumps(
            {
                "includes": {
                    "runtime": "runtime.yaml",
                    "motor_profile": "motors/feetech_sts3215.yaml",
                },
                "arm": {"name": "template", "port": None},
                "joints": {},
            }
        )
    )

    assign_arm_roles(
        session_config=session_path,
        leader_port="/dev/cu.leader",
        follower_port="/dev/cu.follower",
        base_arm_config=base_path,
        leader_arm_config=leader_path,
        follower_arm_config=follower_path,
    )

    leader = load_config_mapping(leader_path)

    runtime_ref = leader["includes"]["runtime"]
    motor_ref = leader["includes"]["motor_profile"]
    assert (leader_path.parent / runtime_ref).resolve() == runtime_path
    assert (leader_path.parent / motor_ref).resolve() == motor_path


def test_assign_arm_roles_uses_packaged_default_without_local_sdk(tmp_path, monkeypatch) -> None:
    session_path = tmp_path / "session.yaml"
    leader_path = tmp_path / "configs" / "arms" / "leader.yaml"
    packaged_default = tmp_path / "site-packages" / "soarm_sdk" / "configs" / "soarm-sdk.yaml"
    session_path.write_text(json.dumps({"leader": {"name": "leader", "mock": True}}))
    packaged_default.parent.mkdir(parents=True)
    packaged_default.write_text("{}")
    monkeypatch.setattr(assignment, "_sdk_default_config_path", lambda: packaged_default)
    monkeypatch.setattr(
        assignment,
        "_load_packaged_soarm_config",
        lambda path: {"arm": {"baudrate": 1000000}, "joints": {}, "poses": {}},
    )

    result = assign_arm_roles(
        session_config=session_path,
        leader_port="/dev/cu.leader",
        follower_port=None,
        leader_arm_config=leader_path,
    )

    leader = load_config_mapping(leader_path)
    assert result["arm_configs"] == {"leader": str(leader_path)}
    assert leader["arm"]["name"] == "soarm-sdk-leader"
    assert leader["arm"]["port"] == "/dev/cu.leader"
    assert "includes" not in leader


def test_default_base_arm_config_uses_installed_sdk(tmp_path, monkeypatch) -> None:
    packaged_default = tmp_path / "site-packages" / "soarm_sdk" / "configs" / "soarm-sdk.yaml"
    packaged_default.parent.mkdir(parents=True)
    packaged_default.write_text("{}")
    monkeypatch.setattr(assignment, "_sdk_default_config_path", lambda: packaged_default)

    assert assignment.default_base_arm_config_path() == packaged_default


def test_assign_arm_roles_rejects_duplicate_ports(tmp_path) -> None:
    session_path = tmp_path / "session.yaml"
    session_path.write_text(json.dumps({}))

    with pytest.raises(ValueError, match="must be different"):
        assign_arm_roles(
            session_config=session_path,
            leader_port="/dev/cu.same",
            follower_port="/dev/cu.same",
        )


def test_assign_camera_roles_writes_wrist_and_third_person(tmp_path) -> None:
    session_path = tmp_path / "session.yaml"
    session_path.write_text(json.dumps({"cameras": {}}))

    result = assign_camera_roles(
        session_config=session_path,
        wrist_index=1,
        third_person_index=0,
        width=320,
        height=240,
        fps=15,
        use_detected_match=False,
    )

    session = load_config_mapping(session_path)

    assert session["cameras"]["wrist"]["device"] == 1
    assert session["cameras"]["third_person"]["device"] == 0
    assert session["cameras"]["third_person"]["width"] == 320
    assert session["cameras"]["third_person"]["height"] == 240
    assert session["cameras"]["third_person"]["fps"] == 15
    assert result["assigned"]["wrist"]["kind"] == "opencv"


def test_assign_uses_example_template_when_local_session_is_missing(tmp_path, monkeypatch) -> None:
    template_path = tmp_path / "configs" / "sessions" / "dual_soarm.example.yaml"
    session_path = tmp_path / "configs" / "session.yaml"
    template_path.parent.mkdir(parents=True)
    template_path.write_text(
        json.dumps(
            {
                "name": "dual-soarm",
                "leader": {"name": "leader", "mock": True},
                "follower": {"name": "follower", "mock": True},
                "cameras": {},
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    result = assign_camera_roles(
        session_config=session_path,
        wrist_index=0,
        third_person_index=None,
        use_detected_match=False,
    )

    session = load_config_mapping(session_path)
    assert result["session_config"] == str(session_path)
    assert session["name"] == "dual-soarm"
    assert session["cameras"]["wrist"]["device"] == 0
