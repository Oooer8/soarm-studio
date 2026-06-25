from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import soarm_studio.hardware.cameras as camera_module
from soarm_studio.config import CameraConfig
from soarm_studio.hardware.cameras import (
    LatestFrameCamera,
    _camera_backend_candidates,
    _camera_infos_from_ioreg,
    _detect_linux_video_devices,
    probe_camera_fps,
    probe_uvc_camera_fps,
    preview_camera_devices,
)
from soarm_studio.types import CameraFrame


def test_ioreg_camera_detection_filters_serial_device() -> None:
    output = """
+-o DSJ-2062-309@00110000  <class IOUSBHostDevice, id 0x1>
  | {
  |   "idProduct" = 25771
  |   "bDeviceClass" = 239
  |   "USB Product Name" = "DSJ-2062-309"
  |   "locationID" = 1114112
  |   "bDeviceSubClass" = 2
  |   "USB Address" = 4
  |   "bDeviceProtocol" = 1
  |   "USB Vendor Name" = "DSJ-250318-J"
  |   "idVendor" = 3141
  | }
+-o USB Single Serial@00140000  <class IOUSBHostDevice, id 0x2>
    {
      "idProduct" = 21971
      "bDeviceClass" = 2
      "USB Product Name" = "USB Single Serial"
      "locationID" = 1310720
      "bDeviceSubClass" = 0
      "USB Address" = 5
      "bDeviceProtocol" = 0
      "idVendor" = 6790
      "USB Serial Number" = "5A7C119035"
    }
"""

    cameras = _camera_infos_from_ioreg(output)

    assert len(cameras) == 1
    assert cameras[0].name == "DSJ-2062-309"
    assert cameras[0].vendor == "DSJ-250318-J"
    assert cameras[0].vid == "0x0c45"
    assert cameras[0].pid == "0x64ab"
    assert cameras[0].location_id == "0x00110000"
    assert cameras[0].role_hint == "usb-camera"


def test_preview_camera_devices_writes_preview_images(tmp_path, monkeypatch) -> None:
    class FakeFrame:
        shape = (240, 320, 3)

    class FakeCapture:
        def __init__(self, index: int) -> None:
            self.index = index
            self.released = False

        def isOpened(self) -> bool:
            return self.index == 0

        def set(self, _prop, _value) -> None:
            return None

        def read(self):
            return True, FakeFrame()

        def release(self) -> None:
            self.released = True

    def fake_imwrite(path: str, _frame) -> bool:
        with open(path, "wb") as handle:
            handle.write(b"fake-jpeg")
        return True

    fake_cv2 = SimpleNamespace(
        VideoCapture=FakeCapture,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        imwrite=fake_imwrite,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    previews = preview_camera_devices([0, 1], output_dir=tmp_path, frames=2)

    assert previews[0].ok is True
    assert previews[0].path == str(tmp_path / "camera_0.jpg")
    assert previews[0].backend == "default"
    assert previews[0].width == 320
    assert previews[0].height == 240
    assert (tmp_path / "camera_0.jpg").read_bytes() == b"fake-jpeg"
    assert previews[1].ok is False
    assert previews[1].error == "default: camera did not open"


def test_auto_backend_prefers_v4l2_on_linux(monkeypatch) -> None:
    fake_cv2 = SimpleNamespace(CAP_ANY=0, CAP_V4L2=200)
    monkeypatch.setattr(camera_module.sys, "platform", "linux")

    assert _camera_backend_candidates(fake_cv2, "auto") == [("v4l2", 200), ("default", None)]
    assert _camera_backend_candidates(fake_cv2, "v4l2") == [("v4l2", 200)]


def test_linux_camera_detection_lists_video_devices(monkeypatch) -> None:
    monkeypatch.setattr(camera_module.sys, "platform", "linux")

    def fake_glob(self, pattern):
        assert str(self) == "/dev"
        assert pattern == "video*"
        return [Path("/dev/video2"), Path("/dev/video0")]

    monkeypatch.setattr(Path, "glob", fake_glob)
    monkeypatch.setattr(camera_module, "_read_text", lambda _path: None)

    devices = _detect_linux_video_devices()

    assert [device.name for device in devices] == ["video0", "video2"]
    assert [device.source for device in devices] == ["v4l2", "v4l2"]
    assert [device.opencv_index for device in devices] == [0, 2]


def test_probe_camera_fps_reports_actual_capture_rate(monkeypatch) -> None:
    class FakeCapture:
        def __init__(self, *_args) -> None:
            self.frames = 0
            self.props = {
                3: 640.0,
                4: 480.0,
                5: 60.0,
            }

        def isOpened(self) -> bool:
            return True

        def set(self, prop, value) -> None:
            self.props[prop] = float(value)

        def get(self, prop) -> float:
            return self.props.get(prop, 0.0)

        def read(self):
            self.frames += 1
            return self.frames <= 3, object()

        def release(self) -> None:
            return None

    fake_cv2 = SimpleNamespace(
        VideoCapture=FakeCapture,
        CAP_ANY=0,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    results = probe_camera_fps([0], width=640, height=480, fps=60, seconds=0.1)

    assert len(results) == 1
    assert results[0].opened is True
    assert results[0].actual_fps == 60.0
    assert results[0].frames == 3


def test_latest_frame_camera_serves_cached_frame_without_blocking() -> None:
    class SlowCamera:
        name = "slow"
        width = 2
        height = 2

        def __init__(self) -> None:
            self.connected = False
            self.frames = 0

        def connect(self) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def read(self) -> CameraFrame:
            time.sleep(0.03)
            self.frames += 1
            return CameraFrame(
                name=self.name,
                width=self.width,
                height=self.height,
                rgb=b"\x00\x00\x00" * self.width * self.height,
            )

    camera = SlowCamera()
    latest = LatestFrameCamera(camera, fps=30, initial_timeout_s=0.5)

    try:
        latest.connect()
        started = time.monotonic()
        frame = latest.read()
        elapsed_ms = (time.monotonic() - started) * 1000.0
    finally:
        latest.disconnect()

    assert frame.name == "slow"
    assert elapsed_ms < 5.0


def test_latest_frame_camera_records_episode_history() -> None:
    class CountingCamera:
        name = "history"
        width = 2
        height = 2

        def __init__(self) -> None:
            self.connected = False
            self.frames = 0

        def connect(self) -> None:
            self.connected = True

        def disconnect(self) -> None:
            self.connected = False

        def read(self) -> CameraFrame:
            self.frames += 1
            pixel = bytes((self.frames % 256, 0, 0))
            return CameraFrame(
                name=self.name,
                width=self.width,
                height=self.height,
                rgb=pixel * self.width * self.height,
            )

    camera = CountingCamera()
    latest = LatestFrameCamera(camera, fps=60, initial_timeout_s=0.5)

    try:
        latest.connect()
        cached = latest.read()
        latest.start_history()
        deadline = time.monotonic() + 1.0
        while camera.frames < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        history = latest.stop_history()
    finally:
        latest.disconnect()

    assert len(history) >= 2
    assert all(frame is not cached for frame in history)
    assert [frame.monotonic_time_ns for frame in history] == sorted(
        frame.monotonic_time_ns for frame in history
    )


def test_create_cameras_wraps_opencv_in_latest_frame_camera(monkeypatch) -> None:
    class FakeCapture:
        def __init__(self) -> None:
            self.props = {}

        def isOpened(self) -> bool:
            return True

        def set(self, prop, value) -> None:
            self.props[prop] = value

        def get(self, prop) -> float:
            return float(self.props.get(prop, 0.0))

        def read(self):
            return True, SimpleNamespace(shape=(2, 2, 3), tobytes=lambda: b"\x00" * 12)

        def release(self) -> None:
            return None

    fake_cv2 = SimpleNamespace(
        VideoCapture=lambda *_args: FakeCapture(),
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        COLOR_BGR2RGB=1,
        cvtColor=lambda frame, _code: frame,
    )
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    from soarm_studio.hardware.cameras import create_cameras

    cameras = create_cameras(
        {
            "wrist": CameraConfig(
                name="wrist",
                kind="opencv",
                device=0,
                width=2,
                height=2,
                fps=30,
            )
        }
    )

    camera = cameras["wrist"]
    assert isinstance(camera, LatestFrameCamera)
    camera.connect()
    try:
        assert camera.read().name == "wrist"
    finally:
        camera.disconnect()


def test_create_cameras_wraps_uvc_in_latest_frame_camera(monkeypatch) -> None:
    import numpy as np

    class FakeFrame:
        rgb = np.zeros((2, 2, 3), dtype=np.uint8)

    class FakeCapture:
        def __init__(self, uid: str) -> None:
            self.uid = uid
            self.name = "fake-uvc"
            self.available_modes = [SimpleNamespace(width=2, height=2, fps=60, format_native=7)]
            self.frame_mode = None
            self.bandwidth_factor = 0.0

        def get_frame_robust(self):
            return FakeFrame()

        def close(self) -> None:
            return None

    fake_uvc = SimpleNamespace(
        device_list=lambda: [{"uid": "1:4", "name": "fake-uvc"}],
        Capture=FakeCapture,
    )
    monkeypatch.setitem(sys.modules, "uvc", fake_uvc)

    from soarm_studio.hardware.cameras import create_cameras

    cameras = create_cameras(
        {
            "wrist": CameraConfig(
                name="wrist",
                kind="uvc",
                device=0,
                width=2,
                height=2,
                fps=60,
            )
        }
    )

    camera = cameras["wrist"]
    assert isinstance(camera, LatestFrameCamera)
    camera.connect()
    try:
        assert camera.read().name == "wrist"
        assert getattr(camera._camera._capture.frame_mode, "format_native") == 7
    finally:
        camera.disconnect()


def test_probe_uvc_camera_fps_reports_init_failure(monkeypatch) -> None:
    fake_uvc = SimpleNamespace(device_list=lambda: None)
    monkeypatch.setitem(sys.modules, "uvc", fake_uvc)

    results = probe_uvc_camera_fps([0], width=2, height=2, fps=60, seconds=0.1)

    assert len(results) == 1
    assert results[0].opened is False
    assert "libuvc could not initialize" in (results[0].error or "")
