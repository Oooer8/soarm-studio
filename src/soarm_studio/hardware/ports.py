from __future__ import annotations

import glob
import contextlib
import io
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SerialPortInfo:
    device: str
    name: str
    basename: str
    connection: str
    preferred_for_connection: bool
    soarm_candidate: bool
    paired_device: str | None
    description: str | None = None
    hwid: str | None = None
    vid: str | None = None
    pid: str | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    product: str | None = None
    interface: str | None = None
    location: str | None = None
    role_hint: str = "unknown"
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SOARMPortProbe:
    device: str
    ok: bool
    expected_ids: list[int]
    online_ids: list[int]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_serial_ports(*, include_system: bool = False) -> list[SerialPortInfo]:
    raw_ports = _list_pyserial_ports()
    if not raw_ports:
        raw_ports = _list_glob_ports()

    devices = {item["device"] for item in raw_ports}
    infos = [_build_info(item, devices) for item in raw_ports]
    if not include_system:
        infos = [info for info in infos if info.role_hint != "system"]
    return sorted(infos, key=lambda info: (not info.preferred_for_connection, info.device))


def detect_serial_port_paths(*, include_system: bool = False) -> list[str]:
    return [info.device for info in detect_serial_ports(include_system=include_system)]


def probe_soarm_ports(
    ports: list[str],
    *,
    arm_config: str | Path,
    ids: list[int] | None = None,
) -> list[SOARMPortProbe]:
    return [probe_soarm_port(port, arm_config=arm_config, ids=ids) for port in ports]


def probe_soarm_port(
    port: str,
    *,
    arm_config: str | Path,
    ids: list[int] | None = None,
) -> SOARMPortProbe:
    try:
        from soarm import SOARMConfig
        from soarm.hardware import ServoBus
    except ModuleNotFoundError as exc:
        return SOARMPortProbe(
            device=port,
            ok=False,
            expected_ids=[],
            online_ids=[],
            error=f"soarm-sdk is not importable: {exc}",
        )

    try:
        config = SOARMConfig.from_file(arm_config).replace_arm_port(port)
        expected_ids = ids or [joint.id for joint in config.joints.values()]
        bus = ServoBus(
            servo_ids=expected_ids,
            port=port,
            baudrate=config.arm.baudrate,
            auto_disable=False,
        )
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                bus.connect()
                scan = bus.scan(expected_ids)
        finally:
            with contextlib.suppress(Exception), contextlib.redirect_stdout(io.StringIO()):
                bus.disconnect()
        online_ids = [servo_id for servo_id, online in scan.items() if online]
        return SOARMPortProbe(
            device=port,
            ok=bool(online_ids),
            expected_ids=expected_ids,
            online_ids=online_ids,
        )
    except Exception as exc:
        return SOARMPortProbe(
            device=port,
            ok=False,
            expected_ids=[] if ids is None else ids,
            online_ids=[],
            error=str(exc),
        )


def _list_pyserial_ports() -> list[dict[str, Any]]:
    try:
        from serial.tools import list_ports
    except ModuleNotFoundError:
        return []

    ports: list[dict[str, Any]] = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": port.device,
                "name": port.name,
                "description": _clean(getattr(port, "description", None)),
                "hwid": _clean(getattr(port, "hwid", None)),
                "vid": _hex(getattr(port, "vid", None)),
                "pid": _hex(getattr(port, "pid", None)),
                "serial_number": _clean(getattr(port, "serial_number", None)),
                "manufacturer": _clean(getattr(port, "manufacturer", None)),
                "product": _clean(getattr(port, "product", None)),
                "interface": _clean(getattr(port, "interface", None)),
                "location": _clean(getattr(port, "location", None)),
            }
        )
    return ports


def _list_glob_ports() -> list[dict[str, Any]]:
    patterns = [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
        "/dev/tty.usbmodem*",
        "/dev/tty.usbserial*",
        "/dev/tty.wchusbserial*",
        "/dev/ttyUSB*",
        "/dev/ttyACM*",
    ]
    devices: set[str] = set()
    for pattern in patterns:
        devices.update(glob.glob(pattern))
    return [{"device": device, "name": Path(device).name} for device in sorted(devices)]


def _build_info(item: dict[str, Any], devices: set[str]) -> SerialPortInfo:
    device = str(item["device"])
    name = str(item.get("name") or Path(device).name)
    basename = Path(device).name
    connection = _connection_kind(device)
    paired_device = _paired_device(device, devices)
    role_hint, notes = _role_hint(item, connection)
    return SerialPortInfo(
        device=device,
        name=name,
        basename=basename,
        connection=connection,
        preferred_for_connection=connection == "callout" and role_hint != "system",
        soarm_candidate=role_hint == "usb-serial" and connection in {"callout", "linux-tty"},
        paired_device=paired_device,
        description=item.get("description"),
        hwid=item.get("hwid"),
        vid=item.get("vid"),
        pid=item.get("pid"),
        serial_number=item.get("serial_number"),
        manufacturer=item.get("manufacturer"),
        product=item.get("product"),
        interface=item.get("interface"),
        location=item.get("location"),
        role_hint=role_hint,
        notes=tuple(notes),
    )


def _connection_kind(device: str) -> str:
    name = Path(device).name
    if name.startswith("cu."):
        return "callout"
    if name.startswith("tty."):
        return "dialin"
    if name.startswith("ttyUSB") or name.startswith("ttyACM"):
        return "linux-tty"
    return "unknown"


def _paired_device(device: str, devices: set[str]) -> str | None:
    path = Path(device)
    name = path.name
    if name.startswith("cu."):
        candidate = str(path.with_name("tty." + name.removeprefix("cu.")))
    elif name.startswith("tty."):
        candidate = str(path.with_name("cu." + name.removeprefix("tty.")))
    else:
        return None
    return candidate if candidate in devices else None


def _role_hint(item: dict[str, Any], connection: str) -> tuple[str, list[str]]:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("device", "name", "description", "hwid", "manufacturer", "product", "interface")
    ).lower()
    notes: list[str] = []

    if "bluetooth" in text or "debug-console" in text:
        return "system", ["not a USB servo bus"]

    if "camera" in text or "uvc" in text:
        notes.append("metadata looks camera-related; keep separate from servo bus config")
        return "camera-control", notes

    if any(token in text for token in ("usbmodem", "usbserial", "wchusbserial", "ch340", "cp210", "uart")):
        if connection == "dialin":
            notes.append("macOS usually connects outward through the paired /dev/cu.* device")
        return "usb-serial", notes

    if "/dev/ttyusb" in text or "/dev/ttyacm" in text:
        return "usb-serial", notes

    return "unknown", notes


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return None if text == "n/a" else text


def _hex(value) -> str | None:
    if value is None:
        return None
    return f"0x{int(value):04x}"
