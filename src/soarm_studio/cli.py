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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="soarm-studio")
    subcommands = parser.add_subparsers(dest="command", required=True)

    detect = subcommands.add_parser("detect", help="Detect local hardware")
    detect_sub = detect.add_subparsers(dest="target", required=True)
    detect_ports = detect_sub.add_parser("ports", help="Detect serial ports")
    detect_ports.add_argument("--include-system", action="store_true")
    detect_ports.add_argument("--paths-only", action="store_true")
    detect_ports.add_argument("--probe-soarm", action="store_true")
    detect_ports.add_argument("--arm-config")
    detect_ports.add_argument("--ids", default=None, help="Comma-separated servo ids for probing")
    detect_cameras = detect_sub.add_parser("cameras", help="Detect camera USB devices")
    detect_cameras.add_argument("--probe-opencv", action="store_true")
    detect_cameras.add_argument("--max-devices", type=int, default=8)

    assign = subcommands.add_parser("assign", help="Assign detected hardware roles")
    assign_sub = assign.add_subparsers(dest="assign_target", required=True)
    assign_arms = assign_sub.add_parser("arms", help="Assign leader/follower arm ports")
    assign_arms.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    assign_arms.add_argument("--leader-port")
    assign_arms.add_argument("--follower-port")
    assign_arms.add_argument("--base-arm-config")
    assign_arms.add_argument("--leader-arm-config", default=DEFAULT_LEADER_ARM_CONFIG)
    assign_arms.add_argument("--follower-arm-config", default=DEFAULT_FOLLOWER_ARM_CONFIG)
    assign_arms.add_argument("--max-relative-target", type=float)
    assign_cameras = assign_sub.add_parser("cameras", help="Assign wrist/third-person cameras")
    assign_cameras.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    assign_cameras.add_argument("--wrist-index", type=int)
    assign_cameras.add_argument("--third-person-index", type=int)
    assign_cameras.add_argument("--width", type=int, default=640)
    assign_cameras.add_argument("--height", type=int, default=480)
    assign_cameras.add_argument("--fps", type=int, default=30)
    assign_cameras.add_argument(
        "--backend",
        choices=["auto", "avfoundation", "default", "any"],
        default="auto",
    )
    assign_cameras.add_argument("--no-detected-match", action="store_true")

    bind = subcommands.add_parser("bind", help="Bind detected hardware roles")
    bind_sub = bind.add_subparsers(dest="bind_target", required=True)
    bind_arms = bind_sub.add_parser("arms", help="Bind leader/follower arm ports")
    bind_arms.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    bind_arms.add_argument("--leader-port")
    bind_arms.add_argument("--follower-port")
    bind_arms.add_argument("--base-arm-config")
    bind_arms.add_argument("--leader-arm-config", default=DEFAULT_LEADER_ARM_CONFIG)
    bind_arms.add_argument("--follower-arm-config", default=DEFAULT_FOLLOWER_ARM_CONFIG)
    bind_arms.add_argument("--max-relative-target", type=float)
    bind_cameras = bind_sub.add_parser("cameras", help="Bind wrist/third-person cameras")
    bind_cameras.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    bind_cameras.add_argument("--wrist-index", type=int)
    bind_cameras.add_argument("--third-person-index", type=int)
    bind_cameras.add_argument("--width", type=int, default=640)
    bind_cameras.add_argument("--height", type=int, default=480)
    bind_cameras.add_argument("--fps", type=int, default=30)
    bind_cameras.add_argument(
        "--backend",
        choices=["auto", "avfoundation", "default", "any"],
        default="auto",
    )
    bind_cameras.add_argument("--no-detected-match", action="store_true")

    verify = subcommands.add_parser("verify", help="Verify saved hardware bindings")
    verify_sub = verify.add_subparsers(dest="verify_target", required=True)
    verify_bindings = verify_sub.add_parser("bindings")
    verify_bindings.add_argument("--config", default=DEFAULT_SESSION_CONFIG)

    preview = subcommands.add_parser("preview", help="Preview local hardware")
    preview_sub = preview.add_subparsers(dest="preview_target", required=True)
    preview_cameras = preview_sub.add_parser("cameras", help="Capture camera preview frames")
    preview_cameras.add_argument("--indices", default="0,1,2,3")
    preview_cameras.add_argument("--output-dir", default="previews/cameras")
    preview_cameras.add_argument("--width", type=int, default=640)
    preview_cameras.add_argument("--height", type=int, default=480)
    preview_cameras.add_argument("--frames", type=int, default=5)
    preview_cameras.add_argument(
        "--backend",
        choices=["auto", "avfoundation", "default", "any"],
        default="auto",
    )

    status = subcommands.add_parser("status", help="Read dual-arm status")
    status.add_argument("--config", default="configs/sessions/mock.yaml")

    preflight = subcommands.add_parser("preflight", help="Run recording readiness checks")
    preflight.add_argument("--config", default="configs/sessions/mock.yaml")
    preflight.add_argument("--overwrite", action="store_true")

    calibrate = subcommands.add_parser("calibrate", help="Run SOARM calibration workflow")
    calibrate.add_argument("--config", default="configs/sessions/mock.yaml")
    calibrate.add_argument(
        "--role",
        choices=["leader", "follower", "both"],
        default="both",
    )

    teleop = subcommands.add_parser("teleop", help="Run leader-to-follower teleop")
    teleop.add_argument("--config", default="configs/sessions/mock.yaml")
    teleop.add_argument("--seconds", type=float, default=2.0)
    teleop.add_argument("--free-test", action="store_true")

    record = subcommands.add_parser("record", help="Record one teleop episode")
    record.add_argument("--config", default="configs/sessions/mock.yaml")
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
    web.add_argument("--config", default="configs/sessions/mock.yaml")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "detect":
        _handle_detect(args)
    elif args.command == "assign":
        _handle_assign(args)
    elif args.command == "bind":
        _handle_bind(args)
    elif args.command == "verify":
        _handle_verify(args)
    elif args.command == "preview":
        _handle_preview(args)
    elif args.command == "status":
        _handle_status(load_session_config(args.config))
    elif args.command == "preflight":
        _handle_preflight(load_session_config(args.config), overwrite=args.overwrite)
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


def _handle_detect(args) -> None:
    if args.target == "ports":
        ports = detect_serial_ports(include_system=args.include_system)
        if args.paths_only:
            _print_json({"ports": [port.device for port in ports]})
            return
        payload = {
            "ports": [port.to_dict() for port in ports],
            "preferred_ports": [port.device for port in ports if port.preferred_for_connection],
            "soarm_candidate_ports": [port.device for port in ports if port.soarm_candidate],
            "notes": _port_detection_notes(ports),
        }
        if args.probe_soarm:
            if not args.arm_config:
                raise SystemExit("--probe-soarm requires --arm-config")
            ids = _parse_ids(args.ids) if args.ids else None
            candidates = [port.device for port in ports if port.soarm_candidate]
            if not candidates:
                candidates = [port.device for port in ports if port.preferred_for_connection]
            payload["soarm_probe"] = [
                result.to_dict()
                for result in probe_soarm_ports(candidates, arm_config=args.arm_config, ids=ids)
            ]
        _print_json(payload)
    elif args.target == "cameras":
        cameras = detect_camera_devices(
            max_devices=args.max_devices,
            probe_opencv=args.probe_opencv,
        )
        _print_json(
            {
                "cameras": [camera.to_dict() for camera in cameras],
                "notes": _camera_detection_notes(args.probe_opencv),
            }
        )
    else:
        raise AssertionError(args.target)


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


def _handle_bind(args) -> None:
    args.assign_target = args.bind_target
    _handle_assign(args)


def _handle_verify(args) -> None:
    if args.verify_target != "bindings":
        raise AssertionError(args.verify_target)
    session = HardwareSession(load_session_config(args.config))
    _print_json(session.verify_bindings())


def _handle_preview(args) -> None:
    if args.preview_target == "cameras":
        try:
            previews = preview_camera_devices(
                _parse_indices(args.indices),
                output_dir=args.output_dir,
                width=args.width,
                height=args.height,
                frames=args.frames,
                backend=args.backend,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        _print_json(
            {
                "previews": [preview.to_dict() for preview in previews],
                "notes": _camera_preview_notes(previews),
            }
        )
    else:
        raise AssertionError(args.preview_target)


def _handle_status(config: SessionConfig) -> None:
    with HardwareSession(config) as hardware:
        _print_json(hardware.read_status())


def _handle_preflight(config: SessionConfig, *, overwrite: bool) -> None:
    with HardwareSession(config) as hardware:
        _print_json(preflight_report_to_dict(hardware.preflight(dataset_overwrite=overwrite)))


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


def _metrics_view(metrics) -> dict:
    return {
        "iterations": metrics.iterations,
        "target_hz": metrics.target_hz,
        "observed_hz": round(metrics.observed_hz, 3),
        "last_latency_ms": round(metrics.last_latency_ms, 3),
        "max_latency_ms": round(metrics.max_latency_ms, 3),
        "elapsed_s": round(metrics.elapsed_s, 3),
    }


def _print_metrics(session: str, metrics: dict) -> None:
    _print_json({"session": session, "metrics": metrics})


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_indices(value: str) -> list[int]:
    indices = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not indices:
        raise SystemExit("--indices must include at least one camera index")
    return indices


def _port_detection_notes(ports) -> list[str]:
    notes = [
        "Use /dev/cu.* on macOS when the program initiates the serial connection.",
        "Cameras are USB video devices, not arm serial ports; keep them out of arm configs.",
    ]
    preferred = [port.device for port in ports if port.preferred_for_connection]
    soarm_candidates = [port.device for port in ports if port.soarm_candidate]
    if len(preferred) > 1:
        notes.append(
            "Multiple connectable serial ports were found; use `--probe-soarm --arm-config ...` "
            "or test one arm at a time before saving a config."
        )
    if len(soarm_candidates) > 1:
        notes.append(
            "Multiple SOARM-looking USB serial ports were found; probe them and save explicit "
            "leader/follower ports."
        )
    if not preferred:
        notes.append("No preferred USB serial callout port was found.")
    return notes


def _camera_detection_notes(probe_opencv: bool) -> list[str]:
    notes = [
        "Default camera detection reads USB metadata and does not import OpenCV.",
        "Use arm serial ports from `detect ports`; do not expect USB video cameras under /dev/cu.*.",
    ]
    if probe_opencv:
        notes.append("OpenCV probing was requested explicitly and may be slower than USB metadata detection.")
    else:
        notes.append("Use `--probe-opencv` only when you need OpenCV camera indexes for a config.")
    return notes


def _camera_preview_notes(previews) -> list[str]:
    notes = [
        "Inspect the saved preview images, then run `assign cameras` with the confirmed wrist "
        "and third-person indexes."
    ]
    if not any(preview.ok for preview in previews):
        notes.extend(
            [
                "USB detection can succeed while OpenCV preview fails; this means the failure "
                "is in camera permissions, backend selection, camera busy state, or OpenCV index mapping.",
                "On macOS, grant Camera permission to the terminal app you run from, then restart that terminal.",
                "Try a wider scan: `soarm-studio preview cameras --indices 0,1,2,3,4,5 --backend avfoundation`.",
            ]
        )
    return notes
