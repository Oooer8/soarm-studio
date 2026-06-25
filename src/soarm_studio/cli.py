from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, cast

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
    probe_camera_fps,
    probe_uvc_camera_fps,
    preview_camera_devices,
)
from .hardware.calibration import CalibrationRole, calibrate_session
from .hardware.ports import detect_serial_ports, probe_soarm_ports
from .hardware.runtime import HardwareSession, preflight_report_to_dict
from .recording import record_lerobot_episodes


CAMERA_BACKENDS = ["auto", "avfoundation", "v4l2", "default", "any"]


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
    scan.add_argument("--ids", default=None, help="Comma-separated servo ids for arm probing")
    scan.add_argument("--max-cameras", type=int, default=8)
    scan.add_argument("--preview-cameras", action="store_true")
    scan.add_argument("--camera-indices", default="0,1,2,3")
    scan.add_argument("--output-dir", default="previews/cameras")
    scan.add_argument("--width", type=int, default=640)
    scan.add_argument("--height", type=int, default=480)
    scan.add_argument("--fps", type=int, default=30)
    scan.add_argument("--frames", type=int, default=5)
    scan.add_argument("--backend", choices=CAMERA_BACKENDS, default="auto")
    scan.add_argument(
        "--fourcc",
        default=None,
        help="Optional four-character camera pixel format request, for example MJPG or YUYV.",
    )

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
    calibrate.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Print the full machine-readable calibration result",
    )
    calibrate.add_argument(
        "--debug",
        action="store_true",
        help="Include the full cleaned calibration report in human output",
    )

    teleop = subcommands.add_parser("teleop", help="Run leader-to-follower teleop")
    teleop.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    teleop.add_argument("--seconds", type=float, default=2.0)
    teleop.add_argument("--free-test", action="store_true")
    teleop.add_argument(
        "--debug",
        action="store_true",
        help="Include per-phase teleop latency statistics in the metrics JSON.",
    )
    teleop.add_argument(
        "--follower-readback-every",
        type=int,
        default=0,
        help="Read follower_after every N frames for diagnostics; 0 disables live readback.",
    )

    record = subcommands.add_parser("record", help="Record one teleop episode")
    record.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    record.add_argument("--seconds", type=float, default=2.0)
    record.add_argument("--task", default="mock task")
    record.add_argument("--overwrite", action="store_true")
    record.add_argument("--episodes", type=int, default=1)
    record.add_argument("--warmup", type=float, default=0.0)
    record.add_argument("--save-policy", choices=["auto", "manual"], default="auto")
    record.add_argument(
        "--debug",
        action="store_true",
        help="Write detailed per-episode camera timing sidecars.",
    )

    camera_fps = subcommands.add_parser(
        "camera-fps",
        help="Measure actual OpenCV camera frame rate",
    )
    camera_fps.add_argument("--config", default=DEFAULT_SESSION_CONFIG)
    camera_fps.add_argument(
        "--camera-indices",
        default=None,
        help="Comma-separated OpenCV indexes. If omitted, enabled OpenCV cameras from --config are used.",
    )
    camera_fps.add_argument("--width", type=int, default=640)
    camera_fps.add_argument("--height", type=int, default=480)
    camera_fps.add_argument("--fps", type=int, default=60)
    camera_fps.add_argument("--seconds", type=float, default=2.0)
    camera_fps.add_argument("--backend", choices=CAMERA_BACKENDS, default="auto")
    camera_fps.add_argument(
        "--fourcc",
        default=None,
        help="Optional four-character pixel format request, for example MJPG or YUYV.",
    )

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
        _handle_calibrate(
            load_session_config(args.config),
            role=args.role,
            output_json=args.output_json,
            debug=args.debug,
        )
    elif args.command == "teleop":
        _handle_teleop(
            load_session_config(args.config),
            seconds=args.seconds,
            free_test=args.free_test,
            debug=args.debug,
            follower_readback_every=args.follower_readback_every,
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
            debug=args.debug,
        )
    elif args.command == "camera-fps":
        _handle_camera_fps(args)
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
    parser.add_argument(
        "--fourcc",
        default=None,
        help="Optional four-character camera pixel format request, for example MJPG or YUYV.",
    )
    parser.add_argument("--no-detected-match", action="store_true")


def _handle_scan(args) -> None:
    ports = detect_serial_ports(include_system=args.include_system)
    if args.probe_arms:
        ids = _parse_ids(args.ids) if args.ids else None
        candidates = [port.device for port in ports if port.soarm_candidate]
        if not candidates:
            candidates = [port.device for port in ports if port.preferred_for_connection]
        arm_ports = [
            _compact_arm_probe(result.to_dict())
            for result in probe_soarm_ports(candidates, ids=ids)
        ]
        _print_json(
            {
                "summary": {
                    "ok": all(port.get("ok") for port in arm_ports) if arm_ports else False,
                    "probed_ports": len(arm_ports),
                    "online_ports": sum(1 for port in arm_ports if port.get("ok")),
                },
                "arm_ports": arm_ports,
                "next_steps": _probe_next_steps(arm_ports),
            }
        )
        return
    if args.preview_cameras:
        try:
            previews = preview_camera_devices(
                _parse_indices(args.camera_indices),
                output_dir=args.output_dir,
                width=args.width,
                height=args.height,
                fps=args.fps,
                frames=args.frames,
                backend=args.backend,
                fourcc=args.fourcc,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        camera_previews = [
            _compact_camera_preview(preview.to_dict()) for preview in previews
        ]
        _print_json(
            {
                "summary": {
                    "ok": any(preview.get("ok") for preview in camera_previews),
                    "previewed": len(camera_previews),
                    "saved_frames": sum(1 for preview in camera_previews if preview.get("ok")),
                },
                "camera_previews": camera_previews,
                "next_steps": _camera_preview_next_steps(camera_previews),
            }
        )
        return

    arm_ports = [
        _compact_serial_port(port.to_dict()) for port in ports if port.role_hint != "system"
    ]
    cameras = [
        _compact_camera(camera.to_dict())
        for camera in detect_camera_devices(
            max_devices=args.max_cameras,
            probe_opencv=False,
        )
    ]
    ignored_system_ports = sum(1 for port in ports if port.role_hint == "system")
    _print_json(
        {
            "summary": {
                "arm_ports": len(arm_ports),
                "cameras": len(cameras),
                "ignored_system_ports": ignored_system_ports,
            },
            "arm_ports": arm_ports,
            "cameras": cameras,
            "next_steps": _scan_next_steps(arm_ports, cameras),
        }
    )


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
        "bindings": _compact_bindings(verification["bindings"]),
    }
    if bindings_only:
        _print_json(payload)
        return

    try:
        report = hardware.preflight(dataset_overwrite=overwrite)
        payload["preflight"] = _compact_preflight(preflight_report_to_dict(report))
        if include_status and report.ok:
            payload["status"] = hardware.read_status()
    finally:
        hardware.disconnect()
    _print_json(payload)


def _handle_assign(args) -> None:
    try:
        if args.assign_target == "arms":
            result = assign_arm_roles(
                session_config=args.config,
                leader_port=args.leader_port,
                follower_port=args.follower_port,
                base_arm_config=args.base_arm_config,
                leader_arm_config=args.leader_arm_config,
                follower_arm_config=args.follower_arm_config,
                max_relative_target=args.max_relative_target,
            )
            _print_json(_compact_arm_assignment(result))
        elif args.assign_target == "cameras":
            result = assign_camera_roles(
                session_config=args.config,
                wrist_index=args.wrist_index,
                third_person_index=args.third_person_index,
                width=args.width,
                height=args.height,
                fps=args.fps,
                backend=args.backend,
                fourcc=args.fourcc,
                use_detected_match=not args.no_detected_match,
            )
            _print_json(_compact_camera_assignment(result))
        else:
            raise AssertionError(args.assign_target)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _handle_teleop(
    config: SessionConfig,
    *,
    seconds: float,
    free_test: bool,
    debug: bool,
    follower_readback_every: int,
) -> None:
    with HardwareSession(config) as hardware:
        if free_test:
            report = hardware.preflight(dataset_overwrite=True)
            if not report.ok:
                _print_json(preflight_report_to_dict(report))
                raise SystemExit("preflight failed")
        metrics = hardware.run_teleop(
            seconds=seconds,
            profile=debug,
            follower_readback_every=follower_readback_every,
        )
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
    debug: bool,
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
            debug=debug,
        )
    )


def _handle_camera_fps(args) -> None:
    result_payload: list[dict] = []
    results = []
    if args.camera_indices is None:
        config = load_session_config(args.config)
        for name, camera in config.cameras.items():
            if not camera.enabled or camera.kind not in {"opencv", "uvc"}:
                continue
            if camera.kind == "opencv" and (camera.device is None or not str(camera.device).isdigit()):
                result_payload.append(
                    {
                        "camera": name,
                        "opened": False,
                        "error": f"camera device {camera.device!r} is not an OpenCV index",
                    }
                )
                continue
            if camera.kind == "uvc":
                camera_results = probe_uvc_camera_fps(
                    [camera.device],
                    width=camera.width,
                    height=camera.height,
                    fps=camera.fps,
                    seconds=args.seconds,
                    bandwidth_factor=float(camera.match.get("bandwidth_factor", 2.0)),
                )
            else:
                camera_results = probe_camera_fps(
                    [int(camera.device)],
                    width=camera.width,
                    height=camera.height,
                    fps=camera.fps,
                    seconds=args.seconds,
                    backend=camera.backend,
                    fourcc=camera.fourcc,
                )
            results.extend(camera_results)
            for result in camera_results:
                rounded = _round_camera_probe(result.to_dict())
                rounded["camera"] = name
                result_payload.append(rounded)
    else:
        results = probe_camera_fps(
            _parse_indices(args.camera_indices),
            width=args.width,
            height=args.height,
            fps=args.fps,
            seconds=args.seconds,
            backend=args.backend,
            fourcc=args.fourcc,
        )
        result_payload = [_round_camera_probe(result.to_dict()) for result in results]
    payload = {
        "summary": _camera_fps_summary(results),
        "results": result_payload,
    }
    _print_json(payload)


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


def _handle_calibrate(
    config: SessionConfig,
    *,
    role: str,
    output_json: bool,
    debug: bool,
) -> None:
    calibration_role = cast(CalibrationRole, role)
    if output_json:
        result = calibrate_session(config, role=calibration_role)
        _print_json(result)
    else:
        print(f"标定机械臂: {_role_label(role)}")
        result = calibrate_session(
            config,
            role=calibration_role,
            announce=_print_calibration_event,
        )
        _print_calibration_summary(result, debug=debug)
    if not result.get("ok"):
        raise SystemExit(1)


def _print_json(data: dict) -> None:
    print(json.dumps(data, indent=2))


def _camera_fps_summary(results) -> dict:
    opened = [result for result in results if result.opened]
    requested_values = sorted({result.requested_fps for result in results})
    requested_fps: int | list[int] = (
        requested_values[0] if len(requested_values) == 1 else requested_values
    )
    if not opened:
        return {
            "ok": False,
            "opened": 0,
            "requested_fps": requested_fps,
            "min_observed_fps": 0.0,
        }
    min_observed = min(result.observed_fps for result in opened)
    ok = all(
        result.observed_fps >= result.requested_fps - max(1.0, result.requested_fps * 0.1)
        for result in opened
    )
    return {
        "ok": ok,
        "opened": len(opened),
        "requested_fps": requested_fps,
        "min_observed_fps": round(min_observed, 3),
    }


def _round_camera_probe(result: dict) -> dict:
    rounded = dict(result)
    for key in (
        "actual_width",
        "actual_height",
        "actual_fps",
        "elapsed_s",
        "observed_fps",
        "interval_avg_ms",
        "interval_min_ms",
        "interval_max_ms",
    ):
        value = rounded.get(key)
        if isinstance(value, float):
            rounded[key] = round(value, 3)
    return rounded


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SWEEP_RE = re.compile(
    r"^\s*(?P<joint>[A-Za-z0-9_]+): direction=(?P<direction>[+-]\d+) \| "
    r"raw=\[(?P<raw>[^\]]+)\] ticks \(range=(?P<range>\d+)\) \| "
    r"safe=\[(?P<safe>[^\]]+)\] rad(?P<extra>.*)$"
)


def _print_calibration_event(role: str, message: str) -> None:
    clean = _clean_report_line(message).strip()
    label = _role_label(role)
    if clean.startswith("Calibration step 1/4"):
        print(f"{label}: 关闭扭矩，进入手动标定。")
    elif clean.startswith("Calibration step 2/4"):
        print(f"{label}: 请按提示摆放物理零位。")
    elif clean.startswith("Zero ticks:"):
        print(f"{label}: 零位已记录。")
    elif clean.startswith("Calibration step 3/4"):
        print(f"{label}: 请按提示扫过安全运动范围。")
    elif clean.startswith("WARNING:"):
        print(f"{label}: 警告 - {clean.removeprefix('WARNING:').strip()}")
    elif clean.startswith("Calibration step 4/4"):
        print(f"{label}: 配置已保存。")


def _print_calibration_summary(result: dict[str, Any], *, debug: bool) -> None:
    status = "成功" if result.get("ok") else "失败"
    print("")
    print(f"标定结果: {status} ({_role_label(str(result.get('role', 'unknown')))})")
    for endpoint in result.get("results", []):
        _print_calibration_endpoint_summary(endpoint, debug=debug)
    if debug:
        print("")
        print("提示: 不带 --debug 可只显示摘要；--json 可导出完整机器可读结果。")
    else:
        print("")
        print("调试: 加 --debug 查看完整诊断报告；加 --json 导出完整 JSON。")


def _print_calibration_endpoint_summary(endpoint: dict[str, Any], *, debug: bool) -> None:
    role = str(endpoint.get("role", "unknown"))
    label = _role_label(role)
    status = "通过" if endpoint.get("ok") else "未通过"
    report = [str(line) for line in endpoint.get("report") or []]
    print(f"- {label}: {status}")
    if endpoint.get("config"):
        print(f"  配置: {endpoint['config']}")
    if endpoint.get("error"):
        print(f"  原因: {endpoint['error']}")

    zero_ticks = _extract_zero_ticks(report)
    if zero_ticks:
        print(f"  零位: {zero_ticks}")

    sweep_ranges = _extract_sweep_ranges(report)
    if sweep_ranges:
        print("  扫描范围:")
        for item in sweep_ranges:
            suffix = "，运动不足" if item["under_excited"] else ""
            print(
                "    "
                f"{item['joint']}: {item['range']} ticks, "
                f"safe=[{item['safe']}] rad{suffix}"
            )

    focus = _diagnostic_focus(report)
    if endpoint.get("ok"):
        print("  复查: 通过")
    elif focus:
        print("  诊断重点:")
        for line in focus[:6]:
            print(f"    {line}")

    guidance = _calibration_guidance(endpoint, focus)
    if guidance:
        print("  建议:")
        for line in guidance:
            print(f"    {line}")

    if debug and report:
        print("  完整报告:")
        for line in report:
            print(f"    {_clean_report_line(line)}")


def _extract_zero_ticks(report: list[str]) -> str | None:
    for line in report:
        clean = _clean_report_line(line).strip()
        if clean.startswith("Zero ticks:"):
            return clean.removeprefix("Zero ticks:").strip()
    return None


def _extract_sweep_ranges(report: list[str]) -> list[dict[str, Any]]:
    ranges: list[dict[str, Any]] = []
    for line in report:
        clean = _clean_report_line(line)
        match = _SWEEP_RE.match(clean)
        if match is None:
            continue
        ranges.append(
            {
                "joint": match.group("joint"),
                "range": match.group("range"),
                "safe": match.group("safe"),
                "under_excited": "UNDER-EXCITED" in match.group("extra"),
            }
        )
    return ranges


def _diagnostic_focus(report: list[str]) -> list[str]:
    focus: list[str] = []
    for line in report:
        clean = _clean_report_line(line).strip()
        if clean.startswith("summary:"):
            if not clean.startswith("summary: [PASS]"):
                focus.append(clean)
        elif clean.startswith("calibration readiness:"):
            if "[FAIL]" in clean:
                focus.append(clean)
    return _dedupe(focus)


def _calibration_guidance(endpoint: dict[str, Any], focus: list[str]) -> list[str]:
    if endpoint.get("ok"):
        role = str(endpoint.get("role", "unknown"))
        if role == "leader":
            return ["下一步: soarm-studio calibrate --config configs/session.yaml --role follower"]
        if role == "follower":
            return ["下一步: soarm-studio check --config configs/session.yaml --overwrite"]
        return ["下一步: soarm-studio check --config configs/session.yaml --overwrite"]

    text = "\n".join(focus + [str(endpoint.get("error", ""))]).lower()
    guidance: list[str] = []
    if "low voltage" in text or "voltage" in text:
        guidance.append("确认 low_voltage 阈值与实际供电档位匹配；当前共享配置为 7.0V。")
        guidance.append("如果读数仍低于阈值，检查电源、线材、总线连接和负载压降。")
    if "missing" in text or "offline" in text or "communication" in text:
        guidance.append("检查串口选择、电机 ID、总线接线、波特率和电源。")
    if "invalid raw" in text:
        guidance.append("检查编码器原始 tick 是否越界，必要时重新上电并重新读取。")
    if not guidance:
        guidance.append("用 --debug 查看完整报告，再按失败项处理。")
    return guidance


def _clean_report_line(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _dedupe(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result


def _role_label(role: str) -> str:
    return {
        "leader": "主臂",
        "follower": "从臂",
        "both": "主臂+从臂",
    }.get(role, role)


def _compact_serial_port(port: dict[str, Any]) -> dict[str, Any]:
    return _without_none(
        {
            "device": port.get("device"),
            "serial": port.get("serial_number"),
            "location": port.get("location"),
            "usb_id": _usb_id(port),
        }
    )


def _compact_camera(camera: dict[str, Any]) -> dict[str, Any]:
    return _without_none(
        {
            "name": camera.get("name"),
            "location_id": camera.get("location_id"),
            "usb_address": camera.get("usb_address"),
            "usb_id": _usb_id(camera),
        }
    )


def _compact_arm_probe(probe: dict[str, Any]) -> dict[str, Any]:
    return _without_none(
        {
            "device": probe.get("device"),
            "ok": probe.get("ok"),
            "expected_ids": _non_empty(probe.get("expected_ids")),
            "online_ids": _non_empty(probe.get("online_ids")),
            "error": probe.get("error"),
        }
    )


def _compact_camera_preview(preview: dict[str, Any]) -> dict[str, Any]:
    return _without_none(
        {
            "index": preview.get("index"),
            "ok": preview.get("ok"),
            "backend": preview.get("backend"),
            "path": preview.get("path"),
            "width": preview.get("width"),
            "height": preview.get("height"),
            "error": preview.get("error"),
        }
    )


def _compact_arm_assignment(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_config": result.get("session_config"),
        "arm_configs": result.get("arm_configs", {}),
        "warnings": result.get("warnings", []),
    }


def _compact_camera_assignment(result: dict[str, Any]) -> dict[str, Any]:
    assigned = {}
    for role, camera in dict(result.get("assigned") or {}).items():
        assigned[role] = _without_none(
            {
                "device": camera.get("device"),
                "backend": camera.get("backend"),
                "width": camera.get("width"),
                "height": camera.get("height"),
                "fps": camera.get("fps"),
                "fourcc": camera.get("fourcc"),
                "match": camera.get("match") or None,
            }
        )
    return {
        "session_config": result.get("session_config"),
        "assigned": assigned,
        "warnings": result.get("warnings", []),
    }


def _compact_bindings(bindings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    compact = {}
    for key, binding in bindings.items():
        compact[key] = _without_none(
            {
                "kind": binding.get("kind"),
                "device": binding.get("device"),
                "config": binding.get("config"),
                "ok": binding.get("ok"),
                "expected_ids": _non_empty(binding.get("expected_ids")),
                "notes": _non_empty(binding.get("notes")),
            }
        )
    return compact


def _compact_preflight(report: dict[str, Any]) -> dict[str, Any]:
    checks = list(report.get("checks") or [])
    failed_checks = [
        _without_none(
            {
                "name": check.get("name"),
                "severity": check.get("severity"),
                "detail": check.get("detail"),
            }
        )
        for check in checks
        if not check.get("ok")
    ]
    return {
        "ok": report.get("ok"),
        "state": report.get("state"),
        "failed_checks": failed_checks,
        "errors": report.get("errors", []),
        "warnings": report.get("warnings", []),
    }


def _without_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _non_empty(value):
    if value in (None, (), [], {}):
        return None
    return value


def _usb_id(item: dict[str, Any]) -> str | None:
    vid = item.get("vid")
    pid = item.get("pid")
    if vid and pid:
        return f"{vid}:{pid}"
    return None


def _scan_next_steps(arm_ports: list[dict[str, Any]], cameras: list[dict[str, Any]]) -> list[str]:
    steps: list[str] = []
    if arm_ports:
        steps.append("Probe arm ports, then decide which port is leader and which is follower.")
    else:
        steps.append("No SOARM serial ports were found; check power, USB cables, and permissions.")
    if cameras:
        steps.append("Preview cameras before assigning wrist and third-person roles.")
    else:
        steps.append(
            "No USB cameras were found; check camera cables and permissions "
            "(/dev/video* on Ubuntu, Camera permission on macOS)."
        )
    return steps


def _probe_next_steps(arm_ports: list[dict[str, Any]]) -> list[str]:
    if not arm_ports:
        return ["No arm ports were probed; run scan first and check USB connections."]
    if all(port.get("ok") for port in arm_ports):
        return ["Use the verified devices in setup arms as leader/follower ports."]
    if any("soarm_sdk" in str(port.get("error")) for port in arm_ports):
        return [
            "Install or activate soarm-sdk in this Python environment; "
            "the SDK imports as package 'soarm_sdk'.",
            "Then probe again before debugging hardware.",
        ]
    if any(
        "Operation not permitted" in str(port.get("error"))
        or "Permission denied" in str(port.get("error"))
        for port in arm_ports
    ):
        return [
            "This process cannot open the serial port. On Ubuntu, add your user to dialout "
            "and log out/in; on macOS, check Terminal permissions. Also close apps using "
            "the port and rerun from the soarm-studio env.",
        ]
    return ["Fix failed ports before setup arms; check power, bus wiring, IDs, and baudrate."]


def _camera_preview_next_steps(previews: list[dict[str, Any]]) -> list[str]:
    if any(preview.get("ok") for preview in previews):
        return ["Open the saved preview images, then use the confirmed indexes in setup cameras."]
    return [
        "No preview frames were saved; check Camera permission, backend "
        "(v4l2 on Ubuntu, avfoundation on macOS), and whether another app is using the cameras."
    ]


def _parse_ids(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_indices(value: str) -> list[int]:
    indices = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not indices:
        raise SystemExit("--indices must include at least one camera index")
    return indices
