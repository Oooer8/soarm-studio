from __future__ import annotations

import json

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
    assert leader["arm"]["name"] == "soarm-leader"
    assert leader["arm"]["port"] == "/dev/cu.leader"
    assert follower["arm"]["name"] == "soarm-follower"
    assert follower["arm"]["port"] == "/dev/cu.follower"
    assert result["warnings"]


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
    local_path = tmp_path / "configs" / "sessions" / "local.yaml"
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
        session_config=local_path,
        wrist_index=0,
        third_person_index=None,
        use_detected_match=False,
    )

    session = load_config_mapping(local_path)
    assert result["session_config"] == str(local_path)
    assert session["name"] == "dual-soarm"
    assert session["cameras"]["wrist"]["device"] == 0
