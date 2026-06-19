from __future__ import annotations

import sys
from types import SimpleNamespace

from soarm_studio.hardware.cameras import (
    _camera_infos_from_ioreg,
    preview_camera_devices,
)


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
