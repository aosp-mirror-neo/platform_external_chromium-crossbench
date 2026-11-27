# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import ctypes
import dataclasses
import datetime as dt
import sys
import unittest
from typing import Final, Sequence, override
from unittest import mock

from crossbench.action_runner.action.enums import ButtonClick
from crossbench.action_runner.input_events import (
  KeyEvent,
  MouseButtonEvent,
  MouseMoveEvent,
  TouchEvent,
  WaitEvent,
)
from crossbench.action_runner.virtual_device.keyboard import (
  KeyboardVirtualDeviceConfig,
)
from crossbench.action_runner.virtual_device.mouse import (
  MouseVirtualDeviceConfig,
)
from crossbench.action_runner.virtual_device.touchscreen import (
  TouchscreenVirtualDeviceConfig,
)
from crossbench.benchmarks.loading.point import Point
from crossbench.plt.win import WinPlatform
from crossbench.plt.win_input import (
  DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
  INPUT_DRAIN_BUFFER,
  INPUT_KEYBOARD,
  INPUT_MOUSE,
  KEYEVENTF_EXTENDEDKEY,
  KEYEVENTF_KEYUP,
  KEYEVENTF_SCANCODE,
  MAX_TOUCH_CONTACTS,
  MOUSE_ABSOLUTE_SPAN,
  MOUSEEVENTF_ABSOLUTE,
  MOUSEEVENTF_LEFTDOWN,
  MOUSEEVENTF_LEFTUP,
  MOUSEEVENTF_MIDDLEDOWN,
  MOUSEEVENTF_MOVE,
  MOUSEEVENTF_RIGHTUP,
  MOUSEEVENTF_VIRTUALDESK,
  POINTER_FEEDBACK_NONE,
  POINTER_FLAG_TOUCH_DOWN,
  POINTER_FLAG_TOUCH_UPDATE,
  POINTER_FLAG_UP,
  PT_TOUCH,
  SM_CXVIRTUALSCREEN,
  SM_CYVIRTUALSCREEN,
  SM_XVIRTUALSCREEN,
  SM_YVIRTUALSCREEN,
  TOUCH_KEEPALIVE_INTERVAL,
  TOUCH_MASK_CONTACTAREA,
  TOUCH_MASK_ORIENTATION,
  TOUCH_MASK_PRESSURE,
  UINT32,
  W3C_TO_WIN_SCAN_CODE,
  CtypesWin32InputApi,
  Input,
  KeybdInput,
  PointerInfo,
  PointerTouchInfo,
  PointerTypeInfo,
  VirtualScreen,
  Win32InputApi,
  WinInputInjector,
)
from tests import test_helper
from tests.crossbench.mock_helper import WinMockPlatform


@dataclasses.dataclass(frozen=True)
class RecordedKey:
  virtual_key: int
  scan_code: int
  flags: int


@dataclasses.dataclass(frozen=True)
class RecordedMouse:
  dx: int
  dy: int
  flags: int


@dataclasses.dataclass(frozen=True)
class RecordedTouch:
  pointer_id: int
  flags: int
  x: int
  y: int


class FakeWin32InputApi(Win32InputApi):
  """Records the native input that would be injected."""

  def __init__(
    self, screen: VirtualScreen = VirtualScreen(0, 0, 1921, 1081)
  ) -> None:
    self.screen: VirtualScreen = screen
    self.inputs: Final[list[RecordedKey | RecordedMouse]] = []
    self.touch_frames: Final[list[tuple[RecordedTouch, ...]]] = []
    self.created_devices: Final[list[int]] = []
    self.destroyed_devices: Final[list[int]] = []
    # Last position of every active contact, by (device, pointer id).
    self.contact_positions: Final[dict[tuple[int, int], tuple[int, int]]] = {}

  @override
  def send_input(self, inputs: Sequence[Input]) -> None:
    for native_input in inputs:
      if native_input.type == INPUT_KEYBOARD:
        ki = native_input.u.ki
        self.inputs.append(RecordedKey(ki.wVk, ki.wScan, ki.dwFlags))
        continue
      assert native_input.type == INPUT_MOUSE
      mi = native_input.u.mi
      self.inputs.append(RecordedMouse(mi.dx, mi.dy, mi.dwFlags))

  @override
  def virtual_screen(self) -> VirtualScreen:
    return self.screen

  @override
  def create_touch_device(self, max_contacts: int) -> int:
    assert max_contacts == MAX_TOUCH_CONTACTS
    device = 100 + len(self.created_devices)
    self.created_devices.append(device)
    return device

  @override
  def inject_touch(
    self, device: int, contacts: Sequence[PointerTypeInfo]
  ) -> None:
    assert device in self.created_devices
    assert device not in self.destroyed_devices
    frame: list[RecordedTouch] = []
    for contact in contacts:
      assert contact.type == PT_TOUCH
      self._check_contact(device, contact.u.touchInfo)
      info = contact.u.touchInfo.pointerInfo
      frame.append(
        RecordedTouch(
          info.pointerId,
          info.pointerFlags,
          info.ptPixelLocation.x,
          info.ptPixelLocation.y,
        )
      )
    self.touch_frames.append(tuple(frame))

  def _check_contact(self, device: int, touch: PointerTouchInfo) -> None:
    """Rejects packets that InjectSyntheticPointerInput() would reject."""
    info = touch.pointerInfo
    assert info.pointerType == PT_TOUCH
    point = (info.ptPixelLocation.x, info.ptPixelLocation.y)
    screen = self.screen
    right = screen.left + screen.width - 1
    bottom = screen.top + screen.height - 1
    assert screen.left <= point[0] <= right, point
    assert screen.top <= point[1] <= bottom, point
    assert touch.touchMask == (
      TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    )
    rect = touch.rcContact
    assert screen.left <= rect.left <= point[0] <= rect.right <= right
    assert screen.top <= rect.top <= point[1] <= rect.bottom <= bottom
    assert 0 <= touch.orientation < 360
    key = (device, info.pointerId)
    if info.pointerFlags == POINTER_FLAG_UP:
      assert touch.pressure == 0
      # UP must be at the contact's last position.
      assert self.contact_positions.pop(key) == point
      return
    assert 0 < touch.pressure <= 1024
    if info.pointerFlags == POINTER_FLAG_TOUCH_DOWN:
      assert key not in self.contact_positions
    else:
      assert key in self.contact_positions
    self.contact_positions[key] = point

  @override
  def destroy_touch_device(self, device: int) -> None:
    self.destroyed_devices.append(device)


class FakeClock:
  def __init__(self) -> None:
    self.now: float = 10.0
    self.sleeps: Final[list[float]] = []

  def monotonic(self) -> float:
    return self.now

  def sleep(self, seconds: float) -> None:
    self.sleeps.append(seconds)
    self.now += seconds


class WinInputInjectorTestCase(unittest.TestCase):
  @override
  def setUp(self) -> None:
    super().setUp()
    self.api = FakeWin32InputApi()
    self.clock = FakeClock()
    self.injector = WinInputInjector(
      self.api, sleep=self.clock.sleep, monotonic=self.clock.monotonic
    )
    self.injector.setup_virtual_devices(
      (
        KeyboardVirtualDeviceConfig(name="keyboard"),
        MouseVirtualDeviceConfig(name="mouse"),
        TouchscreenVirtualDeviceConfig(name="touch"),
      )
    )

  def test_setup_creates_touch_device_only(self):
    self.assertListEqual(self.api.created_devices, [100])

  def test_setup_is_idempotent(self):
    self.injector.setup_virtual_devices(
      (TouchscreenVirtualDeviceConfig(name="touch"),)
    )
    self.assertListEqual(self.api.created_devices, [100])

  def test_teardown_destroys_touch_device(self):
    self.injector.teardown_virtual_devices()
    self.assertListEqual(self.api.destroyed_devices, [100])
    with self.assertRaisesRegex(RuntimeError, "not initialized"):
      self.injector.inject_input_events(
        "touch", (TouchEvent(Point(1, 1), is_down=True),)
      )

  def test_key(self):
    self.injector.inject_input_events(
      "keyboard",
      (
        KeyEvent("KeyA", is_down=True),
        KeyEvent("KeyA", is_down=False),
      ),
    )
    self.assertListEqual(
      self.api.inputs,
      [
        RecordedKey(0, 0x1E, KEYEVENTF_SCANCODE),
        RecordedKey(0, 0x1E, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP),
      ],
    )

  def test_key_shifted_sequence(self):
    self.injector.inject_input_events(
      "keyboard",
      (
        KeyEvent("ShiftLeft", is_down=True),
        KeyEvent("Digit1", is_down=True),
        KeyEvent("Digit1", is_down=False),
        KeyEvent("ShiftLeft", is_down=False),
      ),
    )
    self.assertListEqual(
      [
        key.scan_code for key in self.api.inputs if isinstance(key, RecordedKey)
      ],
      [0x2A, 0x02, 0x02, 0x2A],
    )

  def test_extended_key(self):
    self.injector.inject_input_events(
      "keyboard",
      (
        KeyEvent("ArrowUp", is_down=True),
        KeyEvent("ArrowUp", is_down=False),
      ),
    )
    self.assertListEqual(
      self.api.inputs,
      [
        RecordedKey(0, 0x48, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY),
        RecordedKey(
          0,
          0x48,
          KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP,
        ),
      ],
    )

  def test_extended_modifier_keys(self):
    # MapVirtualKeyW() returns no usable scan code for these keys.
    self.injector.inject_input_events(
      "keyboard",
      tuple(
        KeyEvent(key, is_down=True)
        for key in ("MetaLeft", "MetaRight", "ControlRight", "AltRight")
      ),
    )
    extended = KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY
    self.assertListEqual(
      self.api.inputs,
      [
        RecordedKey(0, 0x5B, extended),
        RecordedKey(0, 0x5C, extended),
        RecordedKey(0, 0x1D, extended),
        RecordedKey(0, 0x38, extended),
      ],
    )

  def test_scan_codes_are_unique(self):
    scan_codes = list(W3C_TO_WIN_SCAN_CODE.values())
    self.assertEqual(len(scan_codes), len(set(scan_codes)))

  def test_unsupported_key(self):
    with self.assertRaisesRegex(ValueError, "NumpadEnter"):
      self.injector.inject_input_events(
        "keyboard", (KeyEvent("NumpadEnter", is_down=True),)
      )
    self.assertListEqual(self.api.inputs, [])

  def test_mouse_move(self):
    self.injector.inject_input_events(
      "mouse",
      (
        MouseMoveEvent(Point(0, 0)),
        MouseMoveEvent(Point(960, 540)),
        MouseMoveEvent(Point(1920, 1080)),
        MouseMoveEvent(Point(5000, -10)),
      ),
    )
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    # Pixel centers; (0, 0) stays a non-zero move and off-screen positions are
    # clamped to the last pixel.
    self.assertListEqual(
      self.api.inputs,
      [
        RecordedMouse(17, 30, flags),
        RecordedMouse(32768, 32768, flags),
        RecordedMouse(65518, 65505, flags),
        RecordedMouse(65518, 30, flags),
      ],
    )

  def test_mouse_move_maps_back_to_the_same_pixel(self):
    for size in (1, 2, 3, 1080, 1081, 1920, 1921, 3840):
      self.api.screen = VirtualScreen(left=0, top=0, width=size, height=size)
      for pixel in range(size):
        self.api.inputs.clear()
        self.injector.inject_input_events(
          "mouse", (MouseMoveEvent(Point(pixel, pixel)),)
        )
        mouse = self.api.inputs[0]
        assert isinstance(mouse, RecordedMouse)
        # Windows maps absolute coordinates back to pixels by truncation.
        self.assertEqual(mouse.dx * size // MOUSE_ABSOLUTE_SPAN, pixel)

  def test_mouse_move_reads_screen_for_every_event(self):
    self.injector.inject_input_events("mouse", (MouseMoveEvent(Point(0, 0)),))
    self.api.screen = VirtualScreen(left=-1920, top=0, width=3841, height=1081)
    self.injector.inject_input_events("mouse", (MouseMoveEvent(Point(0, 0)),))
    first, second = self.api.inputs
    assert isinstance(first, RecordedMouse)
    assert isinstance(second, RecordedMouse)
    self.assertNotEqual(first.dx, second.dx)

  def test_mouse_move_multi_monitor(self):
    # Secondary monitor on the left of the primary one.
    self.api.screen = VirtualScreen(left=-1000, top=0, width=3001, height=11)
    self.injector.inject_input_events(
      "mouse",
      (
        MouseMoveEvent(Point(-1000, 0)),
        MouseMoveEvent(Point(0, 10)),
      ),
    )
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    self.assertListEqual(
      self.api.inputs,
      [
        RecordedMouse(10, 2978, flags),
        RecordedMouse(21848, 62557, flags),
      ],
    )

  def test_mouse_buttons(self):
    self.injector.inject_input_events(
      "mouse",
      (
        MouseButtonEvent(ButtonClick.LEFT, is_down=True),
        MouseButtonEvent(ButtonClick.LEFT, is_down=False),
        MouseButtonEvent(ButtonClick.RIGHT, is_down=False),
        MouseButtonEvent(ButtonClick.MIDDLE, is_down=True),
      ),
    )
    self.assertListEqual(
      [mouse.flags for mouse in self.api.inputs],
      [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_RIGHTUP,
        MOUSEEVENTF_MIDDLEDOWN,
      ],
    )

  def test_touch_tap(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 20), is_down=True),
        TouchEvent(Point(10, 20), is_down=False),
      ),
    )
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 10, 20),),
        (RecordedTouch(0, POINTER_FLAG_UP, 10, 20),),
      ],
    )

  def test_touch_swipe(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 100), is_down=True),
        TouchEvent(Point(10, 50), is_down=True),
        TouchEvent(Point(10, 0), is_down=True),
        TouchEvent(Point(10, 0), is_down=False),
      ),
    )
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 10, 100),),
        (RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 10, 50),),
        (RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 10, 0),),
        (RecordedTouch(0, POINTER_FLAG_UP, 10, 0),),
      ],
    )

  def test_touch_multi_slot_frames(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 10), is_down=True, slot=0),
        TouchEvent(Point(50, 50), is_down=True, slot=1),
        TouchEvent(Point(20, 20), is_down=True, slot=0),
        TouchEvent(Point(20, 20), is_down=False, slot=0),
        TouchEvent(Point(50, 50), is_down=False, slot=1),
      ),
    )
    # Every frame contains all active contacts.
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 10, 10),),
        (
          RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 10, 10),
          RecordedTouch(1, POINTER_FLAG_TOUCH_DOWN, 50, 50),
        ),
        (
          RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 20, 20),
          RecordedTouch(1, POINTER_FLAG_TOUCH_UPDATE, 50, 50),
        ),
        (
          RecordedTouch(0, POINTER_FLAG_UP, 20, 20),
          RecordedTouch(1, POINTER_FLAG_TOUCH_UPDATE, 50, 50),
        ),
        (RecordedTouch(1, POINTER_FLAG_UP, 50, 50),),
      ],
    )

  def test_touch_release_without_down(self):
    with self.assertRaisesRegex(ValueError, "released but not down"):
      self.injector.inject_input_events(
        "touch", (TouchEvent(Point(1, 1), is_down=False),)
      )

  def test_touch_slot_out_of_range(self):
    # TouchEvent() itself already rejects negative slots.
    event = TouchEvent(Point(1, 1), is_down=True, slot=MAX_TOUCH_CONTACTS)
    with self.assertRaisesRegex(ValueError, f"slot {event.slot} is outside"):
      self.injector.inject_input_events("touch", (event,))
    self.assertListEqual(self.api.touch_frames, [])

  def test_touch_on_keyboard_device(self):
    with self.assertRaisesRegex(ValueError, "expected a touchscreen device"):
      self.injector.inject_input_events(
        "keyboard", (TouchEvent(Point(1, 1), is_down=True),)
      )

  def test_touch_frames_are_spaced(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 20), is_down=True),
        TouchEvent(Point(10, 20), is_down=False),
      ),
    )
    # A tap without WaitEvent still leaves 1ms between the two frames.
    self.assertAlmostEqual(self.clock.sleeps[0], 0.001)

  def test_touch_keepalive_during_long_wait(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 20), is_down=True),
        WaitEvent(dt.timedelta(milliseconds=350)),
        TouchEvent(Point(10, 20), is_down=False),
      ),
    )
    keepalive = (RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 10, 20),)
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 10, 20),),
        keepalive,
        keepalive,
        keepalive,
        (RecordedTouch(0, POINTER_FLAG_UP, 10, 20),),
      ],
    )
    self.assertAlmostEqual(
      self.clock.sleeps[0], TOUCH_KEEPALIVE_INTERVAL.total_seconds()
    )

  def test_touch_no_keepalive_without_contacts(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 20), is_down=True),
        TouchEvent(Point(10, 20), is_down=False),
        WaitEvent(dt.timedelta(milliseconds=350)),
      ),
    )
    self.assertEqual(len(self.api.touch_frames), 2)

  def test_touch_release_moves_contact_first(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(10, 20), is_down=True),
        TouchEvent(Point(30, 40), is_down=False),
      ),
    )
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 10, 20),),
        (RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 30, 40),),
        (RecordedTouch(0, POINTER_FLAG_UP, 30, 40),),
      ],
    )

  def test_touch_contacts_are_clamped_to_screen(self):
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(-5, 0), is_down=True),
        TouchEvent(Point(5000, 5000), is_down=True),
        TouchEvent(Point(5000, 5000), is_down=False),
      ),
    )
    # The fake API validates that points and contact areas are on screen.
    self.assertListEqual(
      self.api.touch_frames,
      [
        (RecordedTouch(0, POINTER_FLAG_TOUCH_DOWN, 0, 0),),
        (RecordedTouch(0, POINTER_FLAG_TOUCH_UPDATE, 1920, 1080),),
        (RecordedTouch(0, POINTER_FLAG_UP, 1920, 1080),),
      ],
    )

  def test_touch_sequence_without_release(self):
    with self.assertLogs(level="WARNING") as logs:
      self.injector.inject_input_events(
        "touch", (TouchEvent(Point(10, 20), is_down=True),)
      )
    self.assertIn("Releasing touch contacts [0]", logs.output[0])
    self.assertTupleEqual(
      self.api.touch_frames[-1], (RecordedTouch(0, POINTER_FLAG_UP, 10, 20),)
    )
    # The next tap starts a new contact instead of moving a phantom one.
    self.injector.inject_input_events(
      "touch",
      (
        TouchEvent(Point(1, 2), is_down=True),
        TouchEvent(Point(1, 2), is_down=False),
      ),
    )
    self.assertEqual(
      self.api.touch_frames[-2][0].flags, POINTER_FLAG_TOUCH_DOWN
    )

  def test_touch_state_after_failed_frame(self):
    original_inject_touch = self.api.inject_touch
    calls = 0

    def failing_inject_touch(device, contacts):
      nonlocal calls
      calls += 1
      if calls == 2:
        raise RuntimeError("InjectSyntheticPointerInput failed.")
      original_inject_touch(device, contacts)

    self.api.inject_touch = failing_inject_touch
    with (
      self.assertLogs(level="WARNING"),
      self.assertRaisesRegex(RuntimeError, "InjectSyntheticPointerInput"),
    ):
      self.injector.inject_input_events(
        "touch",
        (
          TouchEvent(Point(10, 20), is_down=True),
          TouchEvent(Point(30, 40), is_down=True, slot=1),
        ),
      )
    # Slot 1 was never committed; slot 0 was released after the error.
    self.assertTupleEqual(
      self.api.touch_frames[-1], (RecordedTouch(0, POINTER_FLAG_UP, 10, 20),)
    )
    self.assertDictEqual(self.api.contact_positions, {})

  def test_device_type_mismatch(self):
    with self.assertRaisesRegex(ValueError, "Cannot inject KeyEvent on mouse"):
      self.injector.inject_input_events(
        "mouse", (KeyEvent("KeyA", is_down=True),)
      )
    with self.assertRaisesRegex(
      ValueError, "Cannot inject MouseMoveEvent on keyboard"
    ):
      self.injector.inject_input_events(
        "keyboard", (MouseMoveEvent(Point(1, 1)),)
      )
    self.assertListEqual(self.api.inputs, [])

  def test_lazy_default_device(self):
    with self.assertLogs(level="WARNING") as logs:
      self.injector.inject_input_events(
        "default_touchscreen",
        (
          TouchEvent(Point(1, 1), is_down=True),
          TouchEvent(Point(1, 1), is_down=False),
        ),
      )
    self.assertIn("Lazily initializing", logs.output[0])
    self.assertListEqual(self.api.created_devices, [100, 101])
    self.assertEqual(len(self.api.touch_frames), 2)

  def test_lazy_default_device_uses_setup_callback(self):
    setup_devices = mock.Mock()
    injector = WinInputInjector(
      self.api,
      sleep=self.clock.sleep,
      monotonic=self.clock.monotonic,
      setup_devices=setup_devices,
    )
    setup_devices.side_effect = injector.setup_virtual_devices
    with self.assertLogs(level="WARNING"):
      injector.inject_input_events(
        "default_mouse", (MouseMoveEvent(Point(1, 1)),)
      )
    setup_devices.assert_called_once()
    (configs,) = setup_devices.call_args.args
    self.assertEqual([config.name for config in configs], ["default_mouse"])

  def test_unknown_device(self):
    with self.assertRaisesRegex(RuntimeError, "not initialized"):
      self.injector.inject_input_events(
        "unknown", (KeyEvent("KeyA", is_down=True),)
      )

  def test_wait_events_use_deadlines(self):
    start = self.clock.now
    self.injector.inject_input_events(
      "keyboard",
      (
        KeyEvent("KeyA", is_down=True),
        WaitEvent(dt.timedelta(milliseconds=50)),
        KeyEvent("KeyA", is_down=False),
        WaitEvent(dt.timedelta(milliseconds=20)),
      ),
    )
    self.assertEqual(len(self.api.inputs), 2)
    self.assertAlmostEqual(self.clock.sleeps[0], 0.05)
    self.assertAlmostEqual(self.clock.sleeps[1], 0.02)
    self.assertAlmostEqual(
      self.clock.sleeps[2], INPUT_DRAIN_BUFFER.total_seconds()
    )
    self.assertAlmostEqual(
      self.clock.now - start, 0.07 + INPUT_DRAIN_BUFFER.total_seconds()
    )

  def test_wait_events_compensate_injection_time(self):

    def slow_send_input(inputs: Sequence[Input]) -> None:
      del inputs
      self.clock.now += 0.03

    self.api.send_input = slow_send_input
    self.injector.inject_input_events(
      "keyboard",
      (
        KeyEvent("KeyA", is_down=True),
        WaitEvent(dt.timedelta(milliseconds=50)),
        KeyEvent("KeyA", is_down=False),
      ),
    )
    # Only the remaining 20ms of the 50ms wait are slept.
    self.assertAlmostEqual(self.clock.sleeps[0], 0.02)

  def test_only_wait_events(self):
    self.injector.inject_input_events(
      "keyboard", (WaitEvent(dt.timedelta(milliseconds=10)),)
    )
    self.assertEqual(len(self.clock.sleeps), 1)
    self.assertAlmostEqual(self.clock.sleeps[0], 0.01)


class Win32StructTestCase(unittest.TestCase):
  @unittest.skipUnless(
    ctypes.sizeof(ctypes.c_void_p) == 8, "Requires a 64-bit host"
  )
  def test_struct_sizes_64bit(self):
    # Must match the Windows SDK x64/arm64 layouts.
    self.assertEqual(ctypes.sizeof(Input), 40)
    self.assertEqual(ctypes.sizeof(PointerInfo), 96)
    self.assertEqual(ctypes.sizeof(PointerTouchInfo), 144)
    self.assertEqual(ctypes.sizeof(PointerTypeInfo), 152)

  @unittest.skipIf(sys.platform == "win32", "Requires a non-Windows host")
  def test_ctypes_api_requires_windows(self):
    with self.assertRaisesRegex(RuntimeError, "Windows"):
      CtypesWin32InputApi()


class CtypesWin32InputApiTestCase(unittest.TestCase):
  @override
  def setUp(self) -> None:
    super().setUp()
    self.user32 = mock.MagicMock()
    self.load_user32_patcher = mock.patch.object(
      CtypesWin32InputApi, "_load_user32", return_value=self.user32
    )
    self.load_user32 = self.load_user32_patcher.start()
    self.addCleanup(self.load_user32_patcher.stop)
    # user32 is mocked, so ctypes.get_last_error() is meaningless: use the
    # non-Windows error path (RuntimeError) on every host, including Windows.
    platform_patcher = mock.patch.object(sys, "platform", "linux")
    platform_patcher.start()
    self.addCleanup(platform_patcher.stop)

  def test_win_platform_create_win32_input_api(self):
    # WinMockPlatform returns a mock API, check the real implementation.
    api = WinPlatform._create_win32_input_api(WinMockPlatform())
    self.assertIsInstance(api, CtypesWin32InputApi)
    # DPI awareness is only changed while calling user32.
    self.user32.SetThreadDpiAwarenessContext.assert_not_called()

  def test_load_user32_on_win32(self):
    self.load_user32_patcher.stop()
    fake_windll = mock.MagicMock(return_value=self.user32)
    with (
      mock.patch.object(sys, "platform", "win32"),
      mock.patch.object(ctypes, "WinDLL", fake_windll, create=True),
    ):
      CtypesWin32InputApi()
    fake_windll.assert_called_once_with("user32", use_last_error=True)

  def test_send_input_success_and_error(self):
    api = CtypesWin32InputApi()
    key_input = Input(type=INPUT_KEYBOARD)
    key_input.u.ki = KeybdInput(wVk=0x41, wScan=0x1E, dwFlags=0)
    self.user32.SendInput.return_value = 1
    api.send_input((key_input,))
    self.assertEqual(self.user32.SendInput.call_count, 1)
    count, array, size = self.user32.SendInput.call_args.args
    self.assertEqual(count, 1)
    self.assertEqual(size, ctypes.sizeof(Input))
    self.assertEqual(array[0].u.ki.wVk, 0x41)

    self.user32.SendInput.return_value = 0
    with self.assertRaisesRegex(RuntimeError, "SendInput failed"):
      api.send_input((key_input,))

  def test_send_input_without_inputs(self):
    api = CtypesWin32InputApi()
    api.send_input(())
    self.user32.SendInput.assert_not_called()

  def test_send_input_scopes_dpi_awareness(self):
    api = CtypesWin32InputApi()
    self.user32.SetThreadDpiAwarenessContext.return_value = 17
    self.user32.SendInput.return_value = 1
    api.send_input((Input(type=INPUT_KEYBOARD),))
    self.assertListEqual(
      self.user32.SetThreadDpiAwarenessContext.call_args_list,
      [
        mock.call(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2),
        mock.call(17),
      ],
    )

  def test_send_input_dpi_awareness_without_previous_context(self):
    api = CtypesWin32InputApi()
    self.user32.SetThreadDpiAwarenessContext.return_value = None
    self.user32.SendInput.return_value = 1
    api.send_input((Input(type=INPUT_KEYBOARD),))
    self.user32.SetThreadDpiAwarenessContext.assert_called_once_with(
      DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    )

  def test_raise_last_error_on_win32(self):
    api = CtypesWin32InputApi()
    self.user32.SendInput.return_value = 0
    with (
      mock.patch.object(sys, "platform", "win32"),
      mock.patch.object(ctypes, "get_last_error", return_value=5, create=True),
      mock.patch.object(
        ctypes, "FormatError", return_value="Access is denied.", create=True
      ),
    ):
      with self.assertRaisesRegex(
        OSError, r"SendInput failed \(0/1 inputs injected\): Access"
      ):
        api.send_input((Input(type=INPUT_KEYBOARD),))

  def test_send_input_blocked_without_error_code_on_win32(self):
    api = CtypesWin32InputApi()
    self.user32.SendInput.return_value = 0
    with (
      mock.patch.object(sys, "platform", "win32"),
      mock.patch.object(ctypes, "get_last_error", return_value=0, create=True),
    ):
      with self.assertRaisesRegex(RuntimeError, r"0/1 inputs.*UIPI.*locked"):
        api.send_input((Input(type=INPUT_KEYBOARD),))

  def test_virtual_screen(self):
    api = CtypesWin32InputApi()
    metrics = {
      SM_XVIRTUALSCREEN: -1920,
      SM_YVIRTUALSCREEN: 0,
      SM_CXVIRTUALSCREEN: 3840,
      SM_CYVIRTUALSCREEN: 1080,
    }
    self.user32.GetSystemMetrics.side_effect = metrics.__getitem__
    self.assertEqual(api.virtual_screen(), VirtualScreen(-1920, 0, 3840, 1080))

  def test_touch_device_lifecycle_and_inject(self):
    api = CtypesWin32InputApi()
    self.user32.CreateSyntheticPointerDevice.return_value = 42
    handle = api.create_touch_device(MAX_TOUCH_CONTACTS)
    self.assertEqual(handle, 42)
    self.user32.CreateSyntheticPointerDevice.assert_called_once_with(
      PT_TOUCH, MAX_TOUCH_CONTACTS, POINTER_FEEDBACK_NONE
    )
    # ULONG maxCount is 32-bit on Windows.
    self.assertIs(self.user32.CreateSyntheticPointerDevice.argtypes[1], UINT32)

    contact = PointerTypeInfo(type=PT_TOUCH)
    self.user32.InjectSyntheticPointerInput.return_value = 1
    api.inject_touch(handle, (contact,))
    dev_arg, array_arg, count_arg = (
      self.user32.InjectSyntheticPointerInput.call_args.args
    )
    self.assertEqual(dev_arg, 42)
    self.assertEqual(count_arg, 1)
    self.assertEqual(array_arg[0].type, PT_TOUCH)

    self.user32.InjectSyntheticPointerInput.return_value = 0
    with self.assertRaisesRegex(
      RuntimeError, "InjectSyntheticPointerInput failed"
    ):
      api.inject_touch(handle, (contact,))

    api.destroy_touch_device(handle)
    self.user32.DestroySyntheticPointerDevice.assert_called_once_with(42)

    self.user32.CreateSyntheticPointerDevice.return_value = 0
    with self.assertRaisesRegex(
      RuntimeError, "CreateSyntheticPointerDevice failed"
    ):
      api.create_touch_device(MAX_TOUCH_CONTACTS)


class WinPlatformInputTestCase(unittest.TestCase):
  @override
  def setUp(self) -> None:
    super().setUp()
    self.api = FakeWin32InputApi()
    patcher = mock.patch.object(
      WinMockPlatform, "_create_win32_input_api", return_value=self.api
    )
    self.create_api = patcher.start()
    self.addCleanup(patcher.stop)
    self.platform = WinMockPlatform()

  def test_teardown_without_setup(self):
    self.platform.teardown_virtual_devices()
    self.create_api.assert_not_called()

  def test_setup_without_devices(self):
    self.platform.setup_virtual_devices(())
    self.platform.teardown_virtual_devices()
    self.create_api.assert_not_called()

  def test_inject_input_events(self):
    self.platform.setup_virtual_devices(
      (TouchscreenVirtualDeviceConfig(name="touch"),)
    )
    self.platform.inject_input_events(
      "touch",
      (
        TouchEvent(Point(3, 4), is_down=True),
        TouchEvent(Point(3, 4), is_down=False),
      ),
    )
    self.assertEqual(len(self.api.touch_frames), 2)
    self.platform.teardown_virtual_devices()
    self.assertListEqual(self.api.destroyed_devices, [100])
    self.create_api.assert_called_once()

  def test_uses_platform_sleep(self):
    self.platform.setup_virtual_devices(
      (KeyboardVirtualDeviceConfig(name="keyboard"),)
    )
    self.platform.inject_input_events(
      "keyboard", (KeyEvent("KeyA", is_down=True),)
    )
    # WinMockPlatform records sleeps instead of blocking.
    self.assertIn(INPUT_DRAIN_BUFFER.total_seconds(), self.platform.sleeps)

  def test_lazy_default_device_is_listed_by_platform(self):
    with self.assertLogs(level="WARNING"):
      self.platform.inject_input_events(
        "default_keyboard", (KeyEvent("KeyA", is_down=True),)
      )
    self.assertIn("default_keyboard", self.platform.virtual_devices)


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
