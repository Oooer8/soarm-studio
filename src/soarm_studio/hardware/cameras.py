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


@dataclass(frozen=True)
class CameraCaptureInfo:
    name: str
    device: int | str | None
    backend: str
    requested_width: int
    requested_height: int
    requested_fps: int
    requested_fourcc: str | None
    actual_width: float | None = None
    actual_height: float | None = None
    actual_fps: float | None = None
    actual_fourcc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CameraFpsProbeResult:
    index: int
    backend: str
    requested_width: int
    requested_height: int
    requested_fps: int
    requested_fourcc: str | None
    opened: bool
    actual_width: float | None = None
    actual_height: float | None = None
    actual_fps: float | None = None
    actual_fourcc: str | None = None
    frames: int = 0
    elapsed_s: float = 0.0
    observed_fps: float = 0.0
    interval_avg_ms: float | None = None
    interval_min_ms: float | None = None
    interval_max_ms: float | None = None
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
        self.fourcc = config.fourcc
        self._capture = None
        self._capture_info: CameraCaptureInfo | None = None

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
            params = _capture_open_params(
                cv2,
                width=self.width,
                height=self.height,
                fps=self.fps,
                fourcc=self.fourcc,
            )
            capture = _open_video_capture(cv2, device, backend_api, params=params)
            if capture.isOpened():
                self._capture = capture
                _configure_capture(
                    cv2,
                    self._capture,
                    width=self.width,
                    height=self.height,
                    fps=self.fps,
                    fourcc=self.fourcc,
                )
                self._capture_info = _capture_info(
                    cv2,
                    self._capture,
                    name=self.name,
                    device=self.device,
                    backend=backend_name,
                    requested_width=self.width,
                    requested_height=self.height,
                    requested_fps=self.fps,
                    requested_fourcc=self.fourcc,
                )
                return
            failures.append(f"{backend_name}: camera did not open")
            capture.release()
        raise RuntimeError(f"Failed to open camera {self.device!r}: {'; '.join(failures)}")

    def disconnect(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._capture_info = None

    def capture_info(self) -> dict[str, Any] | None:
        if self._capture_info is None:
            return None
        return self._capture_info.to_dict()

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


class UVCCamera:
    def __init__(self, config: CameraConfig) -> None:
        self.name = config.name
        self.width = config.width
        self.height = config.height
        self.fps = config.fps
        self.device = config.device
        self.bandwidth_factor = float(config.match.get("bandwidth_factor", 2.0))
        self._uvc = None
        self._capture = None
        self._capture_info: CameraCaptureInfo | None = None

    def connect(self) -> None:
        try:
            import uvc  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "pupil-labs-uvc is required for kind='uvc'. "
                "Install it with: python -m pip install pupil-labs-uvc"
            ) from exc

        self._uvc = uvc
        devices = _uvc_device_list(uvc)
        if not devices:
            raise RuntimeError("No UVC devices were found by libuvc")
        device = _select_uvc_device(devices, self.device)
        try:
            capture = uvc.Capture(device["uid"])
        except Exception as exc:
            raise RuntimeError(
                "Failed to open UVC camera via libuvc. On Ubuntu, check USB device "
                "permissions or udev rules and close other camera apps. On macOS, "
                "libusb may need elevated access because it cannot use the Apple UVC driver "
                f"as a normal user: {exc}"
            ) from exc

        capture.bandwidth_factor = self.bandwidth_factor
        mode = _select_uvc_mode(capture.available_modes, self.width, self.height, self.fps)
        if mode is None:
            available = ", ".join(str(item) for item in capture.available_modes)
            capture.close()
            raise RuntimeError(
                f"UVC camera {self.name!r} does not expose "
                f"{self.width}x{self.height}@{self.fps}; available modes: {available}"
            )
        try:
            capture.frame_mode = mode
        except Exception:
            capture.close()
            raise

        self._capture = capture
        self._capture_info = CameraCaptureInfo(
            name=self.name,
            device=device.get("uid", self.device),
            backend="uvc",
            requested_width=self.width,
            requested_height=self.height,
            requested_fps=self.fps,
            requested_fourcc="MJPG",
            actual_width=float(_uvc_mode_width(mode)),
            actual_height=float(_uvc_mode_height(mode)),
            actual_fps=float(_uvc_mode_fps(mode)),
            actual_fourcc="MJPG",
        )

    def disconnect(self) -> None:
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        self._capture_info = None

    def capture_info(self) -> dict[str, Any] | None:
        if self._capture_info is None:
            return None
        return self._capture_info.to_dict()

    def read(self) -> CameraFrame:
        if self._capture is None:
            raise RuntimeError(f"UVC camera {self.name} is not connected")
        frame = self._capture.get_frame_robust()
        rgb = _uvc_frame_rgb(frame)
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

    def capture_info(self) -> dict[str, Any] | None:
        capture_info = getattr(self._camera, "capture_info", None)
        if not callable(capture_info):
            return None
        return capture_info()

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
    if config.kind == "uvc":
        return LatestFrameCamera(UVCCamera(config), fps=config.fps)
    raise ValueError(f"Unsupported camera kind for {config.name}: {config.kind}")


def detect_camera_devices(
    max_devices: int = 8,
    *,
    probe_opencv: bool = False,
) -> list[CameraDeviceInfo]:
    devices = _detect_macos_usb_cameras()
    devices.extend(_detect_linux_video_devices())
    if probe_opencv:
        for index in _detect_opencv_camera_indexes(max_devices):
            if any(device.opencv_index == index for device in devices):
                continue
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
    fps: int = 30,
    frames: int = 5,
    backend: str = "auto",
    fourcc: str | None = None,
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
                params = _capture_open_params(
                    cv2,
                    width=width,
                    height=height,
                    fps=fps,
                    fourcc=fourcc,
                )
                capture = _open_video_capture(cv2, int(index), backend_api, params=params)
                try:
                    if not capture.isOpened():
                        failures.append(f"{backend_name}: camera did not open")
                        continue
                    _configure_capture(
                        cv2,
                        capture,
                        width=width,
                        height=height,
                        fps=fps,
                        fourcc=fourcc,
                    )

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


def probe_camera_fps(
    indices: list[int],
    *,
    width: int = 640,
    height: int = 480,
    fps: int = 60,
    seconds: float = 2.0,
    backend: str = "auto",
    fourcc: str | None = None,
) -> list[CameraFpsProbeResult]:
    try:
        import cv2  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("OpenCV is required for camera FPS probing") from exc

    results: list[CameraFpsProbeResult] = []
    duration = max(0.1, float(seconds))
    backends = _camera_backend_candidates(cv2, backend)
    with _suppress_native_stderr():
        for index in indices:
            for backend_name, backend_api in backends:
                params = _capture_open_params(
                    cv2,
                    width=width,
                    height=height,
                    fps=fps,
                    fourcc=fourcc,
                )
                capture = _open_video_capture(cv2, int(index), backend_api, params=params)
                try:
                    if not capture.isOpened():
                        results.append(
                            CameraFpsProbeResult(
                                index=int(index),
                                backend=backend_name,
                                requested_width=int(width),
                                requested_height=int(height),
                                requested_fps=int(fps),
                                requested_fourcc=fourcc,
                                opened=False,
                                error="camera did not open",
                            )
                        )
                        continue
                    _configure_capture(
                        cv2,
                        capture,
                        width=width,
                        height=height,
                        fps=fps,
                        fourcc=fourcc,
                    )
                    info = _capture_info(
                        cv2,
                        capture,
                        name=f"camera_{index}",
                        device=int(index),
                        backend=backend_name,
                        requested_width=int(width),
                        requested_height=int(height),
                        requested_fps=int(fps),
                        requested_fourcc=fourcc,
                    )
                    frames = 0
                    intervals_ms: list[float] = []
                    last_frame_at: float | None = None
                    started = time.monotonic()
                    deadline = started + duration
                    while time.monotonic() < deadline:
                        ok, _frame = capture.read()
                        now = time.monotonic()
                        if not ok:
                            break
                        frames += 1
                        if last_frame_at is not None:
                            intervals_ms.append((now - last_frame_at) * 1000.0)
                        last_frame_at = now
                    elapsed = max(0.0, time.monotonic() - started)
                    results.append(
                        CameraFpsProbeResult(
                            index=int(index),
                            backend=backend_name,
                            requested_width=int(width),
                            requested_height=int(height),
                            requested_fps=int(fps),
                            requested_fourcc=fourcc,
                            opened=True,
                            actual_width=info.actual_width,
                            actual_height=info.actual_height,
                            actual_fps=info.actual_fps,
                            actual_fourcc=info.actual_fourcc,
                            frames=frames,
                            elapsed_s=elapsed,
                            observed_fps=frames / elapsed if elapsed > 0 else 0.0,
                            interval_avg_ms=(
                                sum(intervals_ms) / len(intervals_ms) if intervals_ms else None
                            ),
                            interval_min_ms=min(intervals_ms) if intervals_ms else None,
                            interval_max_ms=max(intervals_ms) if intervals_ms else None,
                            error="camera opened but no frame was read" if frames == 0 else None,
                        )
                    )
                finally:
                    capture.release()
    return results


def probe_uvc_camera_fps(
    devices: list[int | str | None],
    *,
    width: int = 640,
    height: int = 480,
    fps: int = 60,
    seconds: float = 2.0,
    bandwidth_factor: float = 2.0,
) -> list[CameraFpsProbeResult]:
    try:
        import uvc  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("pupil-labs-uvc is required for UVC FPS probing") from exc

    try:
        available_devices = _uvc_device_list(uvc)
    except Exception as exc:
        return [
            CameraFpsProbeResult(
                index=index,
                backend="uvc",
                requested_width=int(width),
                requested_height=int(height),
                requested_fps=int(fps),
                requested_fourcc="MJPG",
                opened=False,
                error=str(exc),
            )
            for index, _device in enumerate(devices)
        ]
    results: list[CameraFpsProbeResult] = []
    duration = max(0.1, float(seconds))
    for index, device_selector in enumerate(devices):
        try:
            device = _select_uvc_device(available_devices, device_selector)
        except Exception as exc:
            results.append(
                CameraFpsProbeResult(
                    index=index,
                    backend="uvc",
                    requested_width=int(width),
                    requested_height=int(height),
                    requested_fps=int(fps),
                    requested_fourcc="MJPG",
                    opened=False,
                    error=str(exc),
                )
            )
            continue

        capture = None
        try:
            capture = uvc.Capture(device["uid"])
            capture.bandwidth_factor = float(bandwidth_factor)
            mode = _select_uvc_mode(capture.available_modes, width, height, fps)
            if mode is None:
                available = ", ".join(str(item) for item in capture.available_modes)
                raise RuntimeError(
                    f"UVC device {device.get('uid')} does not expose "
                    f"{width}x{height}@{fps}; available modes: {available}"
                )
            capture.frame_mode = mode
            frames = 0
            intervals_ms: list[float] = []
            last_frame_at: float | None = None
            started = time.monotonic()
            deadline = started + duration
            while time.monotonic() < deadline:
                try:
                    capture.get_frame_robust()
                except TimeoutError:
                    continue
                now = time.monotonic()
                frames += 1
                if last_frame_at is not None:
                    intervals_ms.append((now - last_frame_at) * 1000.0)
                last_frame_at = now
            elapsed = max(0.0, time.monotonic() - started)
            results.append(
                CameraFpsProbeResult(
                    index=index,
                    backend="uvc",
                    requested_width=int(width),
                    requested_height=int(height),
                    requested_fps=int(fps),
                    requested_fourcc="MJPG",
                    opened=True,
                    actual_width=float(_uvc_mode_width(mode)),
                    actual_height=float(_uvc_mode_height(mode)),
                    actual_fps=float(_uvc_mode_fps(mode)),
                    actual_fourcc="MJPG",
                    frames=frames,
                    elapsed_s=elapsed,
                    observed_fps=frames / elapsed if elapsed > 0 else 0.0,
                    interval_avg_ms=(
                        sum(intervals_ms) / len(intervals_ms) if intervals_ms else None
                    ),
                    interval_min_ms=min(intervals_ms) if intervals_ms else None,
                    interval_max_ms=max(intervals_ms) if intervals_ms else None,
                    error="camera opened but no frame was read" if frames == 0 else None,
                )
            )
        except Exception as exc:
            results.append(
                CameraFpsProbeResult(
                    index=index,
                    backend="uvc",
                    requested_width=int(width),
                    requested_height=int(height),
                    requested_fps=int(fps),
                    requested_fourcc="MJPG",
                    opened=False,
                    error=str(exc),
                )
            )
        finally:
            if capture is not None:
                capture.close()
    return results


def _select_uvc_device(devices: list[dict], device: int | str | None) -> dict:
    if device is None:
        return devices[0]
    if isinstance(device, int) or str(device).isdigit():
        index = int(device)
        if index < 0 or index >= len(devices):
            raise RuntimeError(f"UVC device index {index} is out of range; found {len(devices)}")
        return devices[index]
    requested = str(device)
    for candidate in devices:
        if requested in {
            str(candidate.get("uid")),
            str(candidate.get("serialNumber")),
            str(candidate.get("name")),
        }:
            return candidate
    available = ", ".join(
        f"{item.get('uid')}:{item.get('name')}:{item.get('serialNumber')}" for item in devices
    )
    raise RuntimeError(f"UVC device {requested!r} was not found; available devices: {available}")


def _uvc_device_list(uvc) -> list[dict]:
    devices = uvc.device_list()
    if devices is None:
        raise RuntimeError(
            "libuvc could not initialize. On Ubuntu, check libusb installation and USB "
            "device permissions. On macOS, libusb may not be able to access the camera "
            "service as the current user."
        )
    return list(devices)


def _select_uvc_mode(
    modes,
    width: int,
    height: int,
    fps: int,
) -> object | None:
    for mode in modes:
        if (
            _uvc_mode_width(mode),
            _uvc_mode_height(mode),
            _uvc_mode_fps(mode),
        ) == (int(width), int(height), int(fps)):
            return mode
    return None


def _uvc_mode_width(mode) -> int:
    if hasattr(mode, "width"):
        return int(mode.width)
    return int(mode[0])


def _uvc_mode_height(mode) -> int:
    if hasattr(mode, "height"):
        return int(mode.height)
    return int(mode[1])


def _uvc_mode_fps(mode) -> int:
    if hasattr(mode, "fps"):
        return int(mode.fps)
    return int(mode[2])


def _uvc_frame_rgb(frame):
    try:
        import cv2  # type: ignore
        import numpy as np
    except ModuleNotFoundError as exc:
        raise RuntimeError("OpenCV and NumPy are required to convert UVC frames to RGB") from exc

    if hasattr(frame, "rgb"):
        rgb = frame.rgb
    elif hasattr(frame, "bgr"):
        rgb = cv2.cvtColor(frame.bgr, cv2.COLOR_BGR2RGB)
    elif hasattr(frame, "img"):
        image = frame.img
        if getattr(image, "ndim", 0) == 2:
            rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        else:
            rgb = image
    elif hasattr(frame, "gray"):
        rgb = cv2.cvtColor(frame.gray, cv2.COLOR_GRAY2RGB)
    else:
        raise RuntimeError("UVC frame did not expose rgb, bgr, img, or gray data")

    array = np.asarray(rgb)
    if array.ndim != 3 or array.shape[2] != 3:
        raise RuntimeError(f"Unexpected UVC frame shape: {array.shape}")
    return array.astype(np.uint8, copy=False)


def _camera_backend_candidates(cv2, backend: str) -> list[tuple[str, int | None]]:
    backend = backend.lower().strip()
    if backend == "auto":
        candidates: list[tuple[str, int | None]] = []
        avfoundation = getattr(cv2, "CAP_AVFOUNDATION", None)
        if sys.platform == "darwin" and avfoundation is not None:
            candidates.append(("avfoundation", int(avfoundation)))
        v4l2 = getattr(cv2, "CAP_V4L2", None)
        if sys.platform.startswith("linux") and v4l2 is not None:
            candidates.append(("v4l2", int(v4l2)))
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
    if backend == "v4l2":
        v4l2 = getattr(cv2, "CAP_V4L2", None)
        if v4l2 is None:
            raise RuntimeError("This OpenCV build does not expose CAP_V4L2")
        return [("v4l2", int(v4l2))]
    raise RuntimeError(f"Unsupported camera backend: {backend}")


def _capture_open_params(
    cv2,
    *,
    width: int,
    height: int,
    fps: int,
    fourcc: str | None,
) -> list[int | float]:
    params: list[int | float] = [
        int(cv2.CAP_PROP_FRAME_WIDTH),
        int(width),
        int(cv2.CAP_PROP_FRAME_HEIGHT),
        int(height),
    ]
    fps_prop = getattr(cv2, "CAP_PROP_FPS", None)
    if fps_prop is not None:
        params.extend([int(fps_prop), float(fps)])
    fourcc_prop = getattr(cv2, "CAP_PROP_FOURCC", None)
    if fourcc_prop is not None and fourcc:
        params.extend([int(fourcc_prop), _fourcc_value(cv2, fourcc)])
    return params


def _configure_capture(
    cv2,
    capture,
    *,
    width: int,
    height: int,
    fps: int,
    fourcc: str | None,
) -> None:
    fourcc_prop = getattr(cv2, "CAP_PROP_FOURCC", None)
    if fourcc_prop is not None and fourcc:
        capture.set(fourcc_prop, _fourcc_value(cv2, fourcc))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
    fps_prop = getattr(cv2, "CAP_PROP_FPS", None)
    if fps_prop is not None:
        capture.set(fps_prop, float(fps))


def _capture_info(
    cv2,
    capture,
    *,
    name: str,
    device: int | str | None,
    backend: str,
    requested_width: int,
    requested_height: int,
    requested_fps: int,
    requested_fourcc: str | None,
) -> CameraCaptureInfo:
    return CameraCaptureInfo(
        name=name,
        device=device,
        backend=backend,
        requested_width=int(requested_width),
        requested_height=int(requested_height),
        requested_fps=int(requested_fps),
        requested_fourcc=requested_fourcc,
        actual_width=_capture_get(capture, getattr(cv2, "CAP_PROP_FRAME_WIDTH", None)),
        actual_height=_capture_get(capture, getattr(cv2, "CAP_PROP_FRAME_HEIGHT", None)),
        actual_fps=_capture_get(capture, getattr(cv2, "CAP_PROP_FPS", None)),
        actual_fourcc=_fourcc_string(
            _capture_get(capture, getattr(cv2, "CAP_PROP_FOURCC", None))
        ),
    )


def _capture_get(capture, prop: int | None) -> float | None:
    if prop is None or not hasattr(capture, "get"):
        return None
    try:
        return float(capture.get(prop))
    except Exception:
        return None


def _fourcc_value(cv2, fourcc: str) -> int:
    normalized = fourcc.strip().upper()
    if len(normalized) != 4:
        raise ValueError("camera fourcc must be a four-character code such as MJPG or YUYV")
    return int(cv2.VideoWriter_fourcc(*normalized))


def _fourcc_string(value: float | None) -> str | None:
    if value is None:
        return None
    integer = int(value)
    if integer <= 0:
        return None
    chars = "".join(chr((integer >> (8 * index)) & 0xFF) for index in range(4))
    if any(ord(char) < 32 or ord(char) > 126 for char in chars):
        return str(integer)
    return chars


def _open_video_capture(cv2, index: int | str, backend_api: int | None, *, params=None):
    if params:
        try:
            api = int(getattr(cv2, "CAP_ANY", 0)) if backend_api is None else int(backend_api)
            capture = cv2.VideoCapture(index, api, params)
            if capture.isOpened():
                return capture
            capture.release()
        except Exception:
            pass
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


def _detect_linux_video_devices() -> list[CameraDeviceInfo]:
    if not sys.platform.startswith("linux"):
        return []
    devices: list[CameraDeviceInfo] = []
    for path in sorted(Path("/dev").glob("video*"), key=_video_device_sort_key):
        index = _video_device_index(path)
        if index is None:
            continue
        product = _read_text(Path("/sys/class/video4linux") / path.name / "name")
        devices.append(
            CameraDeviceInfo(
                name=product or path.name,
                source="v4l2",
                role_hint="video-device",
                product=product,
                opencv_index=index,
                notes=(f"Linux V4L2 device {path}.",),
            )
        )
    return devices


def _video_device_sort_key(path: Path) -> tuple[int, str]:
    index = _video_device_index(path)
    return (index if index is not None else 1_000_000, path.name)


def _video_device_index(path: Path) -> int | None:
    match = re.fullmatch(r"video(\d+)", path.name)
    return int(match.group(1)) if match else None


def _read_text(path: Path) -> str | None:
    try:
        text = path.read_text(errors="replace").strip()
    except OSError:
        return None
    return text or None


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
