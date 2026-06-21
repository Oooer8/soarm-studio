from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from soarm_studio.config import CameraConfig
from soarm_studio.types import CameraFrame


class Camera(Protocol):
    name: str
    width: int
    height: int

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read(self) -> CameraFrame: ...


@dataclass(frozen=True)
class CameraDeviceInfo:
    name: str
    source: str
    role_hint: str
    product: str | None = None
    vendor: str | None = None
    vid: str | None = None
    pid: str | None = None
    serial_number: str | None = None
    location_id: str | None = None
    usb_address: int | None = None
    device_class: int | None = None
    device_subclass: int | None = None
    device_protocol: int | None = None
    opencv_index: int | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CameraPreviewInfo:
    index: int
    ok: bool
    backend: str | None = None
    path: str | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MockCamera:
    def __init__(self, config: CameraConfig) -> None:
        self.name = config.name
        self.width = config.width
        self.height = config.height
        self.connected = False
        self._frame_index = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def read(self) -> CameraFrame:
        if not self.connected:
            raise RuntimeError(f"Camera {self.name} is not connected")
        self._frame_index += 1
        tick = self._frame_index % 256
        pixel = bytes((tick, (tick * 3) % 256, (tick * 7) % 256))
        rgb = pixel * self.width * self.height
        return CameraFrame(
            self.name,
            self.width,
            self.height,
            rgb,
            time.time(),
            time.monotonic_ns(),
        )


class OpenCVCamera:
    def __init__(self, config: CameraConfig) -> None:
        self.name = config.name
        self.width = config.width
        self.height = config.height
        self.fps = config.fps
        self.device = 0 if config.device is None else config.device
        self.backend = config.backend
        self._capture = None

    def connect(self) -> None:
        try:
            import cv2  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("OpenCV is required for real camera capture") from exc

        device = (
            int(self.device)
            if isinstance(self.device, int) or str(self.device).isdigit()
            else self.device
        )
        failures: list[str] = []
        for backend_name, backend_api in _camera_backend_candidates(cv2, self.backend):
            capture = _open_video_capture(cv2, device, backend_api)
            if capture.isOpened():
                self._capture = capture
                self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                fps_prop = getattr(cv2, "CAP_PROP_FPS", None)
                if fps_prop is not None:
                    self._capture.set(fps_prop, self.fps)
                return
            failures.append(f"{backend_name}: camera did not open")
            capture.release()
        raise RuntimeError(f"Failed to open camera {self.device!r}: {'; '.join(failures)}")

    def disconnect(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def read(self) -> CameraFrame:
        if self._capture is None:
            raise RuntimeError(f"Camera {self.name} is not connected")
        ok, frame = self._capture.read()
        if not ok:
            raise RuntimeError(f"Failed to read camera {self.name}")
        try:
            import cv2  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("OpenCV is required for real camera capture") from exc

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        return CameraFrame(
            self.name,
            int(width),
            int(height),
            rgb.tobytes(),
            time.time(),
            time.monotonic_ns(),
        )


class LatestFrameCamera:
    def __init__(
        self,
        camera: Camera,
        *,
        fps: int,
        initial_timeout_s: float = 2.0,
    ) -> None:
        self._camera = camera
        self.name = camera.name
        self.width = camera.width
        self.height = camera.height
        self.fps = max(1, int(fps))
        self.initial_timeout_s = float(initial_timeout_s)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: CameraFrame | None = None
        self._last_error: str | None = None
        self._record_history = False
        self._history: list[CameraFrame] = []

    def connect(self) -> None:
        self._camera.connect()
        self._ready.clear()
        self._stop.clear()
        self._latest = None
        self._last_error = None
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"soarm-camera-{self.name}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(self.initial_timeout_s):
            with self._lock:
                error = self._last_error
            self.disconnect()
            detail = f": {error}" if error else ""
            raise RuntimeError(f"Timed out waiting for first frame from camera {self.name}{detail}")
        with self._lock:
            latest = self._latest
            error = self._last_error
        if latest is None and error is not None:
            self.disconnect()
            raise RuntimeError(f"Failed to read first frame from camera {self.name}: {error}")

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._lock:
            self._record_history = False
        self._camera.disconnect()

    def read(self) -> CameraFrame:
        with self._lock:
            frame = self._latest
            error = self._last_error
        if frame is None:
            if error is not None:
                raise RuntimeError(f"Camera {self.name} has no frame: {error}")
            raise RuntimeError(f"Camera {self.name} has no frame yet")
        return frame

    def start_history(self, *, seed_latest: bool = False) -> None:
        with self._lock:
            self._history = []
            if seed_latest and self._latest is not None:
                self._history.append(self._latest)
            self._record_history = True

    def stop_history(self) -> list[CameraFrame]:
        with self._lock:
            self._record_history = False
            frames = list(self._history)
            self._history = []
        return frames

    def _capture_loop(self) -> None:
        period_s = 1.0 / self.fps
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                frame = self._camera.read()
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                    has_frame = self._latest is not None
                if has_frame:
                    self._ready.set()
            else:
                with self._lock:
                    self._latest = frame
                    self._last_error = None
                    if self._record_history:
                        self._history.append(frame)
                self._ready.set()

            remaining = period_s - (time.monotonic() - started)
            if remaining > 0:
                self._stop.wait(remaining)
            elif self._last_error is not None:
                self._stop.wait(min(period_s, 0.1))


def create_cameras(configs: dict[str, CameraConfig]) -> dict[str, Camera]:
    cameras: dict[str, Camera] = {}
    for name, config in configs.items():
        if not config.enabled:
            continue
        cameras[name] = create_camera(config)
    return cameras


def create_camera(config: CameraConfig) -> Camera:
    if config.kind == "mock":
        return MockCamera(config)
    if config.kind == "opencv":
        return LatestFrameCamera(OpenCVCamera(config), fps=config.fps)
    raise ValueError(f"Unsupported camera kind for {config.name}: {config.kind}")


def detect_camera_devices(
    max_devices: int = 8,
    *,
    probe_opencv: bool = False,
) -> list[CameraDeviceInfo]:
    devices = _detect_macos_usb_cameras()
    if probe_opencv:
        for index in _detect_opencv_camera_indexes(max_devices):
            devices.append(
                CameraDeviceInfo(
                    name=f"opencv:{index}",
                    source="opencv",
                    role_hint="opencv-index",
                    opencv_index=index,
                    notes=("OpenCV opened this camera index.",),
                )
            )
    return devices


def preview_camera_devices(
    indices: list[int],
    *,
    output_dir: str | Path,
    width: int = 640,
    height: int = 480,
    frames: int = 5,
    backend: str = "auto",
) -> list[CameraPreviewInfo]:
    try:
        import cv2  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("OpenCV is required for camera preview") from exc

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    previews: list[CameraPreviewInfo] = []
    frame_count = max(1, int(frames))
    backends = _camera_backend_candidates(cv2, backend)

    with _suppress_native_stderr():
        for index in indices:
            preview: CameraPreviewInfo | None = None
            failures: list[str] = []
            for backend_name, backend_api in backends:
                capture = _open_video_capture(cv2, int(index), backend_api)
                try:
                    if not capture.isOpened():
                        failures.append(f"{backend_name}: camera did not open")
                        continue
                    capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
                    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))

                    frame = None
                    for _ in range(frame_count):
                        ok, candidate = capture.read()
                        if ok:
                            frame = candidate
                    if frame is None:
                        failures.append(f"{backend_name}: camera opened but no frame was read")
                        continue

                    path = output_path / f"camera_{index}.jpg"
                    if not cv2.imwrite(str(path), frame):
                        failures.append(f"{backend_name}: failed to write {path}")
                        continue
                    frame_height, frame_width = _frame_shape(frame)
                    preview = CameraPreviewInfo(
                        index=int(index),
                        ok=True,
                        backend=backend_name,
                        path=str(path),
                        width=frame_width,
                        height=frame_height,
                    )
                    break
                finally:
                    capture.release()
            if preview is None:
                previews.append(
                    CameraPreviewInfo(
                        index=int(index),
                        ok=False,
                        error="; ".join(failures) if failures else "camera did not open",
                    )
                )
            else:
                previews.append(preview)
    return previews


def _camera_backend_candidates(cv2, backend: str) -> list[tuple[str, int | None]]:
    backend = backend.lower().strip()
    if backend == "auto":
        candidates: list[tuple[str, int | None]] = []
        avfoundation = getattr(cv2, "CAP_AVFOUNDATION", None)
        if sys.platform == "darwin" and avfoundation is not None:
            candidates.append(("avfoundation", int(avfoundation)))
        candidates.append(("default", None))
        return candidates
    if backend == "default":
        return [("default", None)]
    if backend == "any":
        return [("any", int(getattr(cv2, "CAP_ANY", 0)))]
    if backend == "avfoundation":
        avfoundation = getattr(cv2, "CAP_AVFOUNDATION", None)
        if avfoundation is None:
            raise RuntimeError("This OpenCV build does not expose CAP_AVFOUNDATION")
        return [("avfoundation", int(avfoundation))]
    raise RuntimeError(f"Unsupported camera backend: {backend}")


def _open_video_capture(cv2, index: int, backend_api: int | None):
    if backend_api is None:
        return cv2.VideoCapture(index)
    return cv2.VideoCapture(index, backend_api)


def _detect_macos_usb_cameras() -> list[CameraDeviceInfo]:
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return _camera_infos_from_ioreg(result.stdout)


def _detect_opencv_camera_indexes(max_devices: int) -> list[int]:
    try:
        import cv2  # type: ignore
    except ModuleNotFoundError:
        return []

    devices: list[int] = []
    with _suppress_native_stderr():
        for index in range(max_devices):
            capture = cv2.VideoCapture(index)
            try:
                if capture.isOpened():
                    devices.append(index)
            finally:
                capture.release()
    return devices


def _camera_infos_from_ioreg(output: str) -> list[CameraDeviceInfo]:
    return [
        _camera_info_from_ioreg_block(block)
        for block in _parse_ioreg_usb_devices(output)
        if _is_camera_like(block)
    ]


def _parse_ioreg_usb_devices(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in output.splitlines():
        device_match = re.search(r"\+-o\s+(.+?)@\S+\s+<class IOUSBHostDevice", line)
        if device_match:
            if current is not None:
                devices.append(current)
            current = {"ioreg_name": device_match.group(1).strip()}
            continue
        if current is None:
            continue
        property_match = re.search(r'"([^"]+)"\s*=\s*(.+)$', line)
        if not property_match:
            continue
        value = _parse_ioreg_value(property_match.group(2).strip())
        if value is not None:
            current[property_match.group(1)] = value
    if current is not None:
        devices.append(current)
    return devices


def _parse_ioreg_value(raw_value: str) -> str | int | None:
    quoted = re.match(r'^"([^"]*)"', raw_value)
    if quoted:
        return quoted.group(1)
    integer = re.match(r"^-?\d+", raw_value)
    if integer:
        return int(integer.group(0))
    return None


def _camera_info_from_ioreg_block(block: dict[str, Any]) -> CameraDeviceInfo:
    return CameraDeviceInfo(
        name=str(block.get("USB Product Name") or block.get("ioreg_name") or "unknown-camera"),
        source="ioreg",
        role_hint="usb-camera",
        product=_string_or_none(block.get("USB Product Name")),
        vendor=_string_or_none(block.get("USB Vendor Name")),
        vid=_hex(block.get("idVendor"), width=4),
        pid=_hex(block.get("idProduct"), width=4),
        serial_number=_string_or_none(block.get("USB Serial Number")),
        location_id=_hex(block.get("locationID"), width=8),
        usb_address=_int_or_none(block.get("USB Address")),
        device_class=_int_or_none(block.get("bDeviceClass")),
        device_subclass=_int_or_none(block.get("bDeviceSubClass")),
        device_protocol=_int_or_none(block.get("bDeviceProtocol")),
        notes=tuple(_camera_notes(block)),
    )


def _is_camera_like(block: dict[str, Any]) -> bool:
    text = _device_text(block)
    if any(token in text for token in ("hub", "serial", "uart", "ethernet", "lan", "ax88179")):
        return False

    device_class = _int_or_none(block.get("bDeviceClass"))
    device_subclass = _int_or_none(block.get("bDeviceSubClass"))
    device_protocol = _int_or_none(block.get("bDeviceProtocol"))
    if device_class == 14:
        return True
    if device_class == 239 and device_subclass == 2 and device_protocol == 1:
        return True
    return any(token in text for token in ("camera", "webcam", "uvc", "usb video"))


def _camera_notes(block: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    device_class = _int_or_none(block.get("bDeviceClass"))
    device_subclass = _int_or_none(block.get("bDeviceSubClass"))
    device_protocol = _int_or_none(block.get("bDeviceProtocol"))
    if device_class == 239 and device_subclass == 2 and device_protocol == 1:
        notes.append("USB video-class composite device.")
    elif device_class == 14:
        notes.append("USB video-class device.")
    if any(token in _device_text(block) for token in ("camera", "webcam", "uvc", "usb video")):
        notes.append("Camera-like USB product metadata.")
    return notes


def _device_text(block: dict[str, Any]) -> str:
    return " ".join(
        str(block.get(key) or "")
        for key in ("ioreg_name", "USB Product Name", "USB Vendor Name", "kUSBProductString")
    ).lower()


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _hex(value: Any, *, width: int) -> str | None:
    if not isinstance(value, int):
        return None
    return f"0x{value:0{width}x}"


def _frame_shape(frame: Any) -> tuple[int | None, int | None]:
    shape = getattr(frame, "shape", None)
    if shape is None or len(shape) < 2:
        return None, None
    return int(shape[0]), int(shape[1])


@contextlib.contextmanager
def _suppress_native_stderr():
    saved_fd = os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(null_fd)
