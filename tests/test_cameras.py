from __future__ import annotations

import sys
import time
from types import SimpleNamespace

from soarm_studio.config import CameraConfig
from soarm_studio.hardware.cameras import (
    LatestFrameCamera,
    _camera_infos_from_ioreg,
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
        def isOpened(self) -> bool:
            return True

        def set(self, _prop, _value) -> None:
            return None

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
