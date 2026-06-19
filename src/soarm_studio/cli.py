from __future__ import annotations

import argparse
import json
from pathlib import Path

from .assignment import (
    DEFAULT_FOLLOWER_ARM_CONFIG,
    DEFAULT_LEADER_ARM_CONFIG,
    DEFAULT_SESSION_CONFIG,
    assign_arm_roles,
    assign_camera_roles,
)
from .config import SessionConfig, load_session_config
from .datasets.tools import export_rerun_dataset, inspect_dataset, validate_dataset
from .hardware import (
    detect_camera_devices,
    preview_camera_devices,
)
from .hardware.calibration import calibrate_session
from .hardware.ports import detect_serial_ports, probe_soarm_ports
from .hardware.runtime import HardwareSession, preflight_report_to_dict
from .recording import record_lerobot_episodes


CAMERA_BACKENDS = ["auto", "avfoundation", "default", "any"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="soarm-studio",
        description="SOARM Studio hardware setup, readiness checks, teleop, and recording.",
        epilog=(
            "Recommended flow: scan -> setup arms -> scan --preview-cameras -> "
            "setup cameras -> check -> calibrate -> teleop -> record"
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    scan = subcommands.add_parser("scan", help="Find connected arms and cameras")
    scan.add_argument("--include-system", action="store_true")
    scan.add_argument("--probe-arms", action="store_true")
    scan.add_argument("--arm-config")
    scan.add_argument("--ids", default=None, help="Comma-separated servo ids for arm probing")
    scan.add_argument("--max-cameras", type=int, default=8)
    scan.add_argument("--preview-cameras", action="store_true")
    scan.add_argument("--camera-indices", default="0,1,2,3")
    scan.add_argument("--output-dir", default="previews/cameras")
    scan.add_argument("--width", type=int, default=640)
    scan.add_argument("--height", type=int, default=480)
    scan.add_argument("--frames", type=int, default=5)
    scan.add_argument("--backend", choices=CAMERA_BACKENDS, default="auto")

    setup = subcommands.add_parser("setup", help="Save arm or camera role assignments")
    setup_sub = setup.add_subparsers(dest="setup_target", required=True)
    setup_arms = setup_sub.add_parser("arms", help="Save leader/follower arm ports")
    _add_arm_setup_args(setup_arms)
    setup_cameras = setup_sub.add_parser("cameras", help="Save wrist/third-person camera indexes")
    _add_camera_setup_args(setup_cameras)

    check = subcommands.add_parser("check", help="Verify bindings and run readiness checks")
    check.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    check.add_argument("--overwrite", action="store_true")
    check.add_argument("--bindings-only", action="store_true")
    check.add_argument("--status", action="store_true", help="Include a live status sample")

    calibrate = subcommands.add_parser("calibrate", help="Run SOARM calibration workflow")
    calibrate.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    calibrate.add_argument(
        "--role",
        choices=["leader", "follower", "both"],
        default="both",
    )

    teleop = subcommands.add_parser("teleop", help="Run leader-to-follower teleop")
    teleop.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    teleop.add_argument("--seconds", type=float, default=2.0)
    teleop.add_argument("--free-test", action="store_true")

    record = subcommands.add_parser("record", help="Record one teleop episode")
    record.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    record.add_argument("--seconds", type=float, default=2.0)
    record.add_argument("--task", default="mock task")
    record.add_argument("--overwrite", action="store_true")
    record.add_argument("--episodes", type=int, default=1)
    record.add_argument("--warmup", type=float, default=0.0)
    record.add_argument("--save-policy", choices=["auto", "manual"], default="auto")

    dataset = subcommands.add_parser("dataset", help="Dataset tools")
    dataset_sub = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_inspect = dataset_sub.add_parser("inspect")
    dataset_inspect.add_argument("root")
    dataset_validate = dataset_sub.add_parser("validate")
    dataset_validate.add_argument("root")
    dataset_rerun = dataset_sub.add_parser("rerun")
    dataset_rerun.add_argument("root")
    dataset_rerun.add_argument("--output")

    web = subcommands.add_parser("web", help="Run local SOARM Studio web UI")
    web.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "scan":
        _handle_scan(args)
    elif args.command == "setup":
        _handle_setup(args)
    elif args.command == "check":
        _handle_check(
            load_session_config(args.config),
            overwrite=args.overwrite,
            bindings_only=args.bindings_only,
            include_status=args.status,
        )
    elif args.command == "calibrate":
        _print_json(calibrate_session(load_session_config(args.config), role=args.role))
    elif args.command == "teleop":
        _handle_teleop(
            load_session_config(args.config),
            seconds=args.seconds,
            free_test=args.free_test,
        )
    elif args.command == "record":
        _handle_record(
            load_session_config(args.config),
            seconds=args.seconds,
            task=args.task,
            overwrite=args.overwrite,
            episodes=args.episodes,
            warmup=args.warmup,
            save_policy=args.save_policy,
        )
    elif args.command == "dataset":
        _handle_dataset(args)
    elif args.command == "web":
        _handle_web(load_session_config(args.config), host=args.host, port=args.port)
    else:
        raise AssertionError(args.command)


def _add_arm_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    parser.add_argument("--leader-port")
    parser.add_argument("--follower-port")
    parser.add_argument("--base-arm-config")
    parser.add_argument("--leader-arm-config", default=DEFAULT_LEADER_ARM_CONFIG)
    parser.add_argument("--follower-arm-config", default=DEFAULT_FOLLOWER_ARM_CONFIG)
    parser.add_argument("--max-relative-target", type=float)


def _add_camera_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    parser.add_argument("--wrist-index", type=int)
    parser.add_argument("--third-person-index", type=int)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--backend", choices=CAMERA_BACKENDS, default="auto")
    parser.add_argument("--no-detected-match", action="store_true")


def _handle_scan(args) -> None:
    ports = detect_serial_ports(include_system=args.include_system)
    payload: dict[str, object] = {
        "ports": [port.to_dict() for port in ports],
        "preferred_ports": [port.device for port in ports if port.preferred_for_connection],
        "soarm_candidate_ports": [port.device for port in ports if port.soarm_candidate],
        "cameras": [
            camera.to_dict()
            for camera in detect_camera_devices(max_devices=args.max_cameras, probe_opencv=False)
        ],
        "notes": _scan_notes(ports),
    }
    if args.probe_arms:
        if not args.arm_config:
            raise SystemExit("--probe-arms requires --arm-config")
        ids = _parse_ids(args.ids) if args.ids else None
        candidates = [port.device for port in ports if port.soarm_candidate]
        if not candidates:
            candidates = [port.device for port in ports if port.preferred_for_connection]
        payload["arm_probe"] = [
            result.to_dict()
            for result in probe_soarm_ports(candidates, arm_config=args.arm_config, ids=ids)
        ]
    if args.preview_cameras:
        try:
            previews = preview_camera_devices(
                _parse_indices(args.camera_indices),
                output_dir=args.output_dir,
                width=args.width,
                height=args.height,
                frames=args.frames,
                backend=args.backend,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        payload["camera_previews"] = [preview.to_dict() for preview in previews]
        notes = list(payload["notes"])
        notes.extend(_camera_preview_notes(previews))
        payload["notes"] = notes
    _print_json(payload)


def _handle_setup(args) -> None:
    args.assign_target = args.setup_target
    _handle_assign(args)


def _handle_check(
    config: SessionConfig,
    *,
    overwrite: bool,
    bindings_only: bool,
    include_status: bool,
) -> None:
    hardware = HardwareSession(config)
    verification = hardware.verify_bindings()
    payload: dict[str, object] = {
        "bindings_ok": verification["ok"],
        "verified_at": verification["verified_at"],
        "bindings": verification["bindings"],
    }
    if bindings_only:
        _print_json(payload)
        return

    try:
        report = hardware.preflight(dataset_overwrite=overwrite)
        payload["preflight"] = preflight_report_to_dict(report)
        if include_status and report.ok:
            payload["status"] = hardware.read_status()
    finally:
        hardware.disconnect()
    _print_json(payload)


def _handle_assign(args) -> None:
    try:
        if args.assign_target == "arms":
            _print_json(
                assign_arm_roles(
                    session_config=args.config,
                    leader_port=args.leader_port,
                    follower_port=args.follower_port,
                    base_arm_config=args.base_arm_config,
                    leader_arm_config=args.leader_arm_config,
                    follower_arm_config=args.follower_arm_config,
                    max_relative_target=args.max_relative_target,
                )
            )
        elif args.assign_target == "cameras":
            _print_json(
                assign_camera_roles(
                    session_config=args.config,
                    wrist_index=args.wrist_index,
                    third_person_index=args.third_person_index,
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    backend=args.backend,
                    use_detected_match=not args.no_detected_match,
                )
            )
        else:
            raise AssertionError(args.assign_target)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _handle_teleop(config: SessionConfig, *, seconds: float, free_test: bool) -> None:
    with HardwareSession(config) as hardware:
        if free_test:
            report = hardware.preflight(dataset_overwrite=True)
            if not report.ok:
                _print_json(preflight_report_to_dict(report))
                raise SystemExit("preflight failed")
        metrics = hardware.run_teleop(seconds=seconds)
        _print_json({"session": config.name, "state": hardware.state.value, "metrics": metrics})


def _handle_record(
    config: SessionConfig,
    *,
    seconds: float,
    task: str,
    overwrite: bool,
    episodes: int,
    warmup: float,
    save_policy: str,
) -> None:
    if save_policy == "manual":
        raise SystemExit("--save-policy manual is reserved for the Web UI in this version")
    _print_json(
        record_lerobot_episodes(
            config,
            seconds=seconds,
            task=task,
            overwrite=overwrite,
            warmup=warmup,
            episodes=episodes,
        )
    )


def _handle_dataset(args) -> None:
    root = Path(args.root)
    if args.dataset_command == "inspect":
        _print_json(inspect_dataset(root))
    elif args.dataset_command == "validate":
        errors = validate_dataset(root)
        _print_json({"valid": not errors, "errors": errors})
    elif args.dataset_command == "rerun":
        try:
            _print_json(export_rerun_dataset(root, output=args.output))
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        raise AssertionError(args.dataset_command)


def _handle_web(config: SessionConfig, *, host: str, port: int) -> None:
    from .web import run_web

    run_web(config, host=host, port=port)


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_indices(value: str) -> list[int]:
    indices = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not indices:
        raise SystemExit("--indices must include at least one camera index")
    return indices


def _scan_notes(ports) -> list[str]:
    notes = [
        "Use /dev/cu.* on macOS for SOARM arm ports.",
        "Use `setup arms` to save leader/follower ports after probing or testing one arm at a time.",
        "Use `scan --preview-cameras` before `setup cameras`; camera USB metadata alone is not enough.",
    ]
    preferred = [port.device for port in ports if port.preferred_for_connection]
    soarm_candidates = [port.device for port in ports if port.soarm_candidate]
    if len(preferred) > 1 or len(soarm_candidates) > 1:
        notes.append("Multiple serial ports were found; label the hub ports or cables before saving roles.")
    if not preferred:
        notes.append("No preferred USB serial callout port was found.")
    return notes


def _camera_preview_notes(previews) -> list[str]:
    notes = [
        "Inspect the saved preview images, then run `setup cameras` with the confirmed wrist "
        "and third-person indexes."
    ]
    if not any(preview.ok for preview in previews):
        notes.extend(
            [
                "USB detection can succeed while OpenCV preview fails; this means the failure "
                "is in camera permissions, backend selection, camera busy state, or OpenCV index mapping.",
                "On macOS, grant Camera permission to the terminal app you run from, then restart that terminal.",
                "Try a wider scan: `soarm-studio scan --preview-cameras --camera-indices 0,1,2,3,4,5 --backend avfoundation`.",
            ]
        )
    return notes
