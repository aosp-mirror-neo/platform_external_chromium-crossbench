# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""OS-level input injection for Windows.

Translates crossbench InputEvents into native Windows input:
- KeyEvent:         SendInput() keyboard input (physical scan code).
- MouseMoveEvent /
  MouseButtonEvent: SendInput() absolute mouse input.
- TouchEvent:       InjectSyntheticPointerInput() on a synthetic PT_TOUCH
                    pointer device (Windows 10 1809+).

Unlike JS-dispatched events, these go through the regular OS input pipeline,
so the browser records them like real user input (e.g. EventLatency metrics).

Keys are injected as physical scan codes, like the Linux key codes used by
evemu and the macOS virtual key codes: the active keyboard layout of the
target window turns them into characters. Text actions are converted to keys
assuming a US layout, see crossbench.action_runner.keyboard_layout.

Injection only works from an interactive, unlocked desktop session and the
target window (the browser) must not run at a higher integrity level than
crossbench (UIPI silently drops such input).
"""

import abc
import contextlib
import ctypes
import dataclasses
import datetime as dt
import logging
import sys
import time
from typing import (
  TYPE_CHECKING,
  Callable,
  ClassVar,
  Final,
  Iterable,
  Iterator,
  Mapping,
  NoReturn,
  Sequence,
  override,
)

from immutabledict import immutabledict

from crossbench.action_runner.action.enums import ButtonClick
from crossbench.action_runner.input_events import (
  InputEvent,
  KeyEvent,
  MouseButtonEvent,
  MouseMoveEvent,
  TouchEvent,
  WaitEvent,
)
from crossbench.action_runner.virtual_device.all import DEFAULT_VIRTUAL_DEVICES
from crossbench.action_runner.virtual_device.virtual_device_type import (
  VirtualDeviceType,
)
from crossbench.plt.evemu_platform_mixin import INPUT_DRAIN_BUFFER

if TYPE_CHECKING:
  from crossbench.action_runner.virtual_device.virtual_device_config import (
    VirtualDeviceConfig,
  )
  from crossbench.benchmarks.loading.point import Point

# Fixed-width aliases for Win32 types, so that the structure layouts below are
# identical on every host (c_ulong is 8 bytes on 64-bit posix, but DWORD is
# always 4 bytes).
DWORD: Final = ctypes.c_uint32
WORD: Final = ctypes.c_uint16
LONG: Final = ctypes.c_int32
UINT32: Final = ctypes.c_uint32
INT32: Final = ctypes.c_int32
UINT64: Final = ctypes.c_uint64
ULONG_PTR: Final = ctypes.c_size_t
HANDLE: Final = ctypes.c_void_p

# The structures below mirror the Win32 SDK structures of the same name
# (POINT, RECT, MOUSEINPUT, KEYBDINPUT, HARDWAREINPUT, INPUT, POINTER_INFO,
# POINTER_TOUCH_INFO and POINTER_TYPE_INFO).


class Win32Point(ctypes.Structure):
  _fields_ = [("x", LONG), ("y", LONG)]


class Win32Rect(ctypes.Structure):
  _fields_ = [("left", LONG), ("top", LONG), ("right", LONG), ("bottom", LONG)]


class MouseInput(ctypes.Structure):
  _fields_ = [
    ("dx", LONG),
    ("dy", LONG),
    ("mouseData", DWORD),
    ("dwFlags", DWORD),
    ("time", DWORD),
    ("dwExtraInfo", ULONG_PTR),
  ]


class KeybdInput(ctypes.Structure):
  _fields_ = [
    ("wVk", WORD),
    ("wScan", WORD),
    ("dwFlags", DWORD),
    ("time", DWORD),
    ("dwExtraInfo", ULONG_PTR),
  ]


class HardwareInput(ctypes.Structure):
  _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _InputUnion(ctypes.Union):
  _fields_ = [("mi", MouseInput), ("ki", KeybdInput), ("hi", HardwareInput)]


class Input(ctypes.Structure):
  _fields_ = [("type", DWORD), ("u", _InputUnion)]


class PointerInfo(ctypes.Structure):
  _fields_ = [
    ("pointerType", DWORD),
    ("pointerId", UINT32),
    ("frameId", UINT32),
    ("pointerFlags", UINT32),
    ("sourceDevice", HANDLE),
    ("hwndTarget", HANDLE),
    ("ptPixelLocation", Win32Point),
    ("ptHimetricLocation", Win32Point),
    ("ptPixelLocationRaw", Win32Point),
    ("ptHimetricLocationRaw", Win32Point),
    ("dwTime", DWORD),
    ("historyCount", UINT32),
    ("InputData", INT32),
    ("dwKeyStates", DWORD),
    ("PerformanceCount", UINT64),
    ("ButtonChangeType", INT32),
  ]


class PointerTouchInfo(ctypes.Structure):
  _fields_ = [
    ("pointerInfo", PointerInfo),
    ("touchFlags", UINT32),
    ("touchMask", UINT32),
    ("rcContact", Win32Rect),
    ("rcContactRaw", Win32Rect),
    ("orientation", UINT32),
    ("pressure", UINT32),
  ]


class _PointerTypeInfoUnion(ctypes.Union):
  # POINTER_PEN_INFO is smaller than PointerTouchInfo, so the union size is
  # determined by the touch member alone.
  _fields_ = [("touchInfo", PointerTouchInfo)]


class PointerTypeInfo(ctypes.Structure):
  _fields_ = [("type", DWORD), ("u", _PointerTypeInfoUnion)]


INPUT_MOUSE: Final[int] = 0
INPUT_KEYBOARD: Final[int] = 1

KEYEVENTF_EXTENDEDKEY: Final[int] = 0x0001
KEYEVENTF_KEYUP: Final[int] = 0x0002
KEYEVENTF_SCANCODE: Final[int] = 0x0008

MOUSEEVENTF_MOVE: Final[int] = 0x0001
MOUSEEVENTF_LEFTDOWN: Final[int] = 0x0002
MOUSEEVENTF_LEFTUP: Final[int] = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final[int] = 0x0008
MOUSEEVENTF_RIGHTUP: Final[int] = 0x0010
MOUSEEVENTF_MIDDLEDOWN: Final[int] = 0x0020
MOUSEEVENTF_MIDDLEUP: Final[int] = 0x0040
MOUSEEVENTF_VIRTUALDESK: Final[int] = 0x4000
MOUSEEVENTF_ABSOLUTE: Final[int] = 0x8000
# SendInput() maps absolute coordinates in [0, 65536) onto the virtual desktop.
MOUSE_ABSOLUTE_SPAN: Final[int] = 65536

PT_TOUCH: Final[int] = 2
# No touch rings or trails: DWM rendering them adds compositor work during
# latency measurements and shows up in screenshots.
POINTER_FEEDBACK_NONE: Final[int] = 3
POINTER_FLAG_INRANGE: Final[int] = 0x00000002
POINTER_FLAG_INCONTACT: Final[int] = 0x00000004
POINTER_FLAG_DOWN: Final[int] = 0x00010000
POINTER_FLAG_UPDATE: Final[int] = 0x00020000
POINTER_FLAG_UP: Final[int] = 0x00040000
POINTER_FLAG_TOUCH_DOWN: Final[int] = (
  POINTER_FLAG_DOWN | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
)
POINTER_FLAG_TOUCH_UPDATE: Final[int] = (
  POINTER_FLAG_UPDATE | POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
)
TOUCH_MASK_CONTACTAREA: Final[int] = 0x00000001
TOUCH_MASK_ORIENTATION: Final[int] = 0x00000002
TOUCH_MASK_PRESSURE: Final[int] = 0x00000004

# Synthetic touch contact parameters (pressure range is [0, 1024]).
TOUCH_CONTACT_RADIUS: Final[int] = 2
TOUCH_PRESSURE: Final[int] = 512
TOUCH_ORIENTATION: Final[int] = 90
# Maximum number of simultaneous contacts of the synthetic touch device.
MAX_TOUCH_CONTACTS: Final[int] = 10
# InjectSyntheticPointerInput() assigns timestamps itself and fails with
# ERROR_NOT_READY when frames are injected less than ~0.1ms apart.
MIN_TOUCH_FRAME_INTERVAL: Final[dt.timedelta] = dt.timedelta(milliseconds=1)
# Windows cancels touch contacts that receive no update for about a second,
# keep held contacts alive like Chromium (remoting/host/touch_injector_win.cc).
TOUCH_KEEPALIVE_INTERVAL: Final[dt.timedelta] = dt.timedelta(milliseconds=100)

SM_XVIRTUALSCREEN: Final[int] = 76
SM_YVIRTUALSCREEN: Final[int] = 77
SM_CXVIRTUALSCREEN: Final[int] = 78
SM_CYVIRTUALSCREEN: Final[int] = 79
# Positions are physical screen pixels, use per-monitor DPI awareness so that
# Windows does not rescale them.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final[int] = -4

# Scan codes with this prefix live on the extended part of the keyboard and
# need KEYEVENTF_EXTENDEDKEY, otherwise e.g. "ArrowUp" is delivered as Numpad8.
EXTENDED_SCAN_CODE_PREFIX: Final[int] = 0xE000

# W3C KeyboardEvent.code -> physical (scan code set 1) key position.
# See https://www.w3.org/TR/uievents-code/ and Chromium's
# ui/events/keycodes/dom/dom_code_data.inc.
W3C_TO_WIN_SCAN_CODE: Final[immutabledict[str, int]] = immutabledict(
  {
    "Escape": 0x01,
    **{f"Digit{d}": 0x01 + d for d in range(1, 10)},
    "Digit0": 0x0B,
    "Minus": 0x0C,
    "Equal": 0x0D,
    "Backspace": 0x0E,
    "Tab": 0x0F,
    **{f"Key{c}": 0x10 + i for i, c in enumerate("QWERTYUIOP")},
    "BracketLeft": 0x1A,
    "BracketRight": 0x1B,
    "Enter": 0x1C,
    "ControlLeft": 0x1D,
    **{f"Key{c}": 0x1E + i for i, c in enumerate("ASDFGHJKL")},
    "Semicolon": 0x27,
    "Quote": 0x28,
    "Backquote": 0x29,
    "ShiftLeft": 0x2A,
    "Backslash": 0x2B,
    **{f"Key{c}": 0x2C + i for i, c in enumerate("ZXCVBNM")},
    "Comma": 0x33,
    "Period": 0x34,
    "Slash": 0x35,
    "ShiftRight": 0x36,
    "AltLeft": 0x38,
    "Space": 0x39,
    "CapsLock": 0x3A,
    **{f"F{n}": 0x3A + n for n in range(1, 11)},
    "F11": 0x57,
    "F12": 0x58,
    "ControlRight": 0xE01D,
    "AltRight": 0xE038,
    "Home": 0xE047,
    "ArrowUp": 0xE048,
    "PageUp": 0xE049,
    "ArrowLeft": 0xE04B,
    "ArrowRight": 0xE04D,
    "End": 0xE04F,
    "ArrowDown": 0xE050,
    "PageDown": 0xE051,
    "Insert": 0xE052,
    "Delete": 0xE053,
    "MetaLeft": 0xE05B,
    "MetaRight": 0xE05C,
  }
)

BUTTON_CLICK_TO_WIN: Final[immutabledict[ButtonClick, tuple[int, int]]] = (
  immutabledict(
    {
      ButtonClick.LEFT: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
      ButtonClick.RIGHT: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
      ButtonClick.MIDDLE: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
  )
)


@dataclasses.dataclass(frozen=True)
class VirtualScreen:
  """Bounds of the Windows virtual desktop in physical pixels."""

  left: int
  top: int
  width: int
  height: int


class Win32InputApi(abc.ABC):
  """Thin abstraction over the user32 calls used for input injection."""

  @abc.abstractmethod
  def send_input(self, inputs: Sequence[Input]) -> None:
    pass

  @abc.abstractmethod
  def virtual_screen(self) -> VirtualScreen:
    pass

  @abc.abstractmethod
  def create_touch_device(self, max_contacts: int) -> int:
    pass

  @abc.abstractmethod
  def inject_touch(
    self, device: int, contacts: Sequence[PointerTypeInfo]
  ) -> None:
    pass

  @abc.abstractmethod
  def destroy_touch_device(self, device: int) -> None:
    pass


class CtypesWin32InputApi(Win32InputApi):
  """Win32InputApi implementation calling user32.dll through ctypes."""

  def __init__(self) -> None:
    self._user32: Final[ctypes.CDLL] = self._load_user32()
    self._declare_signatures()

  def _load_user32(self) -> ctypes.CDLL:
    if sys.platform == "win32":
      return ctypes.WinDLL("user32", use_last_error=True)
    raise RuntimeError("Win32 input injection requires a Windows host.")

  def _declare_signatures(self) -> None:
    user32 = self._user32
    user32.SendInput.argtypes = [
      ctypes.c_uint,
      ctypes.POINTER(Input),
      ctypes.c_int,
    ]
    user32.SendInput.restype = ctypes.c_uint
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetThreadDpiAwarenessContext.argtypes = [HANDLE]
    user32.SetThreadDpiAwarenessContext.restype = HANDLE
    user32.CreateSyntheticPointerDevice.argtypes = [
      DWORD,
      UINT32,
      DWORD,
    ]
    user32.CreateSyntheticPointerDevice.restype = HANDLE
    user32.InjectSyntheticPointerInput.argtypes = [
      HANDLE,
      ctypes.POINTER(PointerTypeInfo),
      UINT32,
    ]
    user32.InjectSyntheticPointerInput.restype = ctypes.c_int
    user32.DestroySyntheticPointerDevice.argtypes = [HANDLE]
    user32.DestroySyntheticPointerDevice.restype = None

  def _raise_last_error(self, function: str, details: str = "") -> NoReturn:
    if sys.platform == "win32":
      if error := ctypes.get_last_error():
        raise OSError(
          error, f"{function} failed{details}: {ctypes.FormatError(error)}"
        )
      # UIPI and locked or disconnected desktops drop input without setting
      # an error code.
      raise RuntimeError(
        f"{function} failed{details} without an error code: input is blocked "
        "because the target window runs at a higher integrity level (UIPI) "
        "or the desktop is locked or disconnected."
      )
    raise RuntimeError(f"{function} failed{details}.")

  @contextlib.contextmanager
  def _physical_pixels(self) -> Iterator[None]:
    # Positions are in physical pixels: disable DPI virtualization for the
    # calling thread only, and restore its previous awareness afterwards.
    previous = self._user32.SetThreadDpiAwarenessContext(
      DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    )
    try:
      yield
    finally:
      if previous:
        self._user32.SetThreadDpiAwarenessContext(previous)

  @override
  def send_input(self, inputs: Sequence[Input]) -> None:
    if not inputs:
      return
    array = (Input * len(inputs))(*inputs)
    with self._physical_pixels():
      sent = self._user32.SendInput(len(inputs), array, ctypes.sizeof(Input))
    if sent != len(inputs):
      self._raise_last_error(
        "SendInput", f" ({sent}/{len(inputs)} inputs injected)"
      )

  @override
  def virtual_screen(self) -> VirtualScreen:
    metrics = self._user32.GetSystemMetrics
    with self._physical_pixels():
      return VirtualScreen(
        left=metrics(SM_XVIRTUALSCREEN),
        top=metrics(SM_YVIRTUALSCREEN),
        width=metrics(SM_CXVIRTUALSCREEN),
        height=metrics(SM_CYVIRTUALSCREEN),
      )

  @override
  def create_touch_device(self, max_contacts: int) -> int:
    device = self._user32.CreateSyntheticPointerDevice(
      PT_TOUCH, max_contacts, POINTER_FEEDBACK_NONE
    )
    if not device:
      self._raise_last_error("CreateSyntheticPointerDevice")
    return device

  @override
  def inject_touch(
    self, device: int, contacts: Sequence[PointerTypeInfo]
  ) -> None:
    array = (PointerTypeInfo * len(contacts))(*contacts)
    if not self._user32.InjectSyntheticPointerInput(
      device, array, len(contacts)
    ):
      self._raise_last_error("InjectSyntheticPointerInput")

  @override
  def destroy_touch_device(self, device: int) -> None:
    self._user32.DestroySyntheticPointerDevice(device)


@dataclasses.dataclass
class _WinVirtualDevice:
  name: str
  device_type: VirtualDeviceType
  touch_device: int | None = None
  # Active touch contacts by slot (only used for touchscreens).
  contacts: dict[int, Point] = dataclasses.field(default_factory=dict)
  # Monotonic time of the last injected touch frame.
  last_touch_frame: float | None = None


class WinInputInjector:
  """Injects crossbench InputEvents as native Windows input."""

  _DEFAULT_DEVICES: ClassVar[Mapping[str, VirtualDeviceConfig]] = immutabledict(
    {device.name: device for device in DEFAULT_VIRTUAL_DEVICES.values()}
  )
  _EVENT_DEVICE_TYPES: ClassVar[
    Mapping[type[InputEvent], VirtualDeviceType]
  ] = immutabledict(
    {
      KeyEvent: VirtualDeviceType.KEYBOARD,
      MouseMoveEvent: VirtualDeviceType.MOUSE,
      MouseButtonEvent: VirtualDeviceType.MOUSE,
      TouchEvent: VirtualDeviceType.TOUCHSCREEN,
    }
  )

  def __init__(
    self,
    api: Win32InputApi,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.perf_counter,
    setup_devices: Callable[[tuple[VirtualDeviceConfig, ...]], None]
    | None = None,
  ) -> None:
    self._api: Final[Win32InputApi] = api
    self._sleep: Final[Callable[[float], None]] = sleep
    self._monotonic: Final[Callable[[], float]] = monotonic
    # Lazily created default devices are set up through the platform, so that
    # Platform.virtual_devices also lists them (see WinPlatform).
    self._setup_devices: Final[
      Callable[[tuple[VirtualDeviceConfig, ...]], None]
    ] = setup_devices or self.setup_virtual_devices
    self._devices: Final[dict[str, _WinVirtualDevice]] = {}

  def setup_virtual_devices(
    self, virtual_devices: Iterable[VirtualDeviceConfig]
  ) -> None:
    for device_config in virtual_devices:
      self._init_device(device_config.name, device_config.device_type)

  def teardown_virtual_devices(self) -> None:
    for device in self._devices.values():
      if touch_device := device.touch_device:
        self._api.destroy_touch_device(touch_device)
    self._devices.clear()

  def _init_device(
    self, name: str, device_type: VirtualDeviceType
  ) -> _WinVirtualDevice:
    if device := self._devices.get(name):
      return device
    touch_device: int | None = None
    if device_type == VirtualDeviceType.TOUCHSCREEN:
      touch_device = self._api.create_touch_device(MAX_TOUCH_CONTACTS)
    elif device_type not in (
      VirtualDeviceType.KEYBOARD,
      VirtualDeviceType.MOUSE,
    ):
      raise ValueError(f"Unsupported virtual device type: {device_type}")
    device = _WinVirtualDevice(
      name=name, device_type=device_type, touch_device=touch_device
    )
    logging.debug(
      "Initialized Windows virtual %s '%s'", device_type.value, name
    )
    self._devices[name] = device
    return device

  def _get_device(self, name: str) -> _WinVirtualDevice:
    if device := self._devices.get(name):
      return device
    if device_config := self._DEFAULT_DEVICES.get(name):
      logging.warning(
        "Lazily initializing virtual device '%s' during test execution. "
        "Consider defining virtual devices in ActionRunnerConfig so they "
        "are initialized during test setup.",
        name,
      )
      self._setup_devices((device_config,))
      return self._devices[name]
    raise RuntimeError(
      f"Virtual device '{name}' was not initialized. "
      "Call setup_virtual_devices() first."
    )

  def inject_input_events(
    self, device_name: str, events: Iterable[InputEvent]
  ) -> None:
    """Injects events in order, honoring WaitEvent delays.

    Blocks until all events have been injected plus a short drain buffer.
    """
    device = self._get_device(device_name)
    try:
      injected = self._inject_events(device, events)
    finally:
      self._release_touch_contacts(device)
    if injected:
      self._sleep(INPUT_DRAIN_BUFFER.total_seconds())

  def _inject_events(
    self, device: _WinVirtualDevice, events: Iterable[InputEvent]
  ) -> bool:
    deadline = self._monotonic()
    injected = False
    for event in events:
      if isinstance(event, WaitEvent):
        deadline += event.duration.total_seconds()
        continue
      self._wait_until(deadline, device)
      self._inject_event(device, event)
      injected = True
    self._wait_until(deadline, device)
    return injected

  def _wait_until(self, deadline: float, device: _WinVirtualDevice) -> None:
    # Sleep against an absolute deadline so that per-event injection costs do
    # not accumulate as timing drift over long event sequences.
    while (remaining := deadline - self._monotonic()) > 0:
      if not device.contacts:
        self._sleep(remaining)
        return
      keepalive = TOUCH_KEEPALIVE_INTERVAL.total_seconds()
      if remaining <= keepalive:
        self._sleep(remaining)
        return
      self._sleep(keepalive)
      self._inject_touch_frame(device, self._touch_update_frame(device))

  def _release_touch_contacts(self, device: _WinVirtualDevice) -> None:
    """Lifts contacts that are still held after a sequence or an error.

    Contacts are not kept alive between sequences, Windows would cancel them,
    and leftover contacts would turn later taps into moves.
    """
    if not device.contacts:
      return
    logging.warning(
      "Releasing touch contacts %s still held on '%s'.",
      sorted(device.contacts),
      device.name,
    )
    frame = [
      self._touch_contact(slot, position, POINTER_FLAG_UP)
      for slot, position in sorted(device.contacts.items())
    ]
    try:
      self._inject_touch_frame(device, frame)
    except (OSError, RuntimeError) as e:
      logging.warning("Could not release touch contacts: %s", e)
    finally:
      device.contacts.clear()

  def _inject_event(self, device: _WinVirtualDevice, event: InputEvent) -> None:
    self._check_device_type(device, event)
    match event:
      case KeyEvent():
        self._inject_key(event)
      case MouseMoveEvent():
        self._inject_mouse_move(event)
      case MouseButtonEvent():
        self._inject_mouse_button(event)
      case TouchEvent():
        self._inject_touch(device, event)
      case _:
        raise ValueError(f"Unsupported event type: {type(event).__name__}")

  def _check_device_type(
    self, device: _WinVirtualDevice, event: InputEvent
  ) -> None:
    try:
      device_type = self._EVENT_DEVICE_TYPES[type(event)]
    except KeyError as e:
      raise ValueError(f"Unsupported event type: {type(event).__name__}") from e
    if device.device_type != device_type:
      raise ValueError(
        f"Cannot inject {type(event).__name__} on {device.device_type.value} "
        f"device '{device.name}', expected a {device_type.value} device."
      )

  def _inject_key(self, event: KeyEvent) -> None:
    try:
      scan_code = W3C_TO_WIN_SCAN_CODE[event.key_code]
    except KeyError as e:
      raise ValueError(f"Key code '{event.key_code}' is not supported.") from e
    # Inject the physical key position only: Windows derives the virtual-key
    # code (and the character) from the active keyboard layout, like for a
    # hardware keyboard.
    flags = KEYEVENTF_SCANCODE
    if scan_code & EXTENDED_SCAN_CODE_PREFIX:
      flags |= KEYEVENTF_EXTENDEDKEY
    if not event.is_down:
      flags |= KEYEVENTF_KEYUP
    key_input = Input(type=INPUT_KEYBOARD)
    key_input.u.ki = KeybdInput(wVk=0, wScan=scan_code & 0xFF, dwFlags=flags)
    self._api.send_input((key_input,))

  def _inject_mouse_move(self, event: MouseMoveEvent) -> None:
    screen = self._virtual_screen()
    mouse_input = Input(type=INPUT_MOUSE)
    mouse_input.u.mi = MouseInput(
      dx=self._normalize(event.position.x, screen.left, screen.width),
      dy=self._normalize(event.position.y, screen.top, screen.height),
      dwFlags=(
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
      ),
    )
    self._api.send_input((mouse_input,))

  def _virtual_screen(self) -> VirtualScreen:
    # Not cached: GetSystemMetrics() is cheap and the virtual desktop can
    # change, e.g. when the browser switches to fullscreen.
    return self._api.virtual_screen()

  def _normalize(self, value: int, origin: int, size: int) -> int:
    if size <= 0:
      return 0
    # Windows maps [0, 65536) onto the pixels by truncation: target the pixel
    # center so that rounding never lands on a neighboring pixel. This also
    # keeps (0, 0) a non-zero move, which Windows would otherwise ignore.
    clamped = max(0, min(size - 1, value - origin))
    return int((clamped + 0.5) * MOUSE_ABSOLUTE_SPAN // size)

  def _inject_mouse_button(self, event: MouseButtonEvent) -> None:
    try:
      down_flag, up_flag = BUTTON_CLICK_TO_WIN[event.button]
    except KeyError as e:
      raise ValueError(
        f"Button click '{event.button}' is not supported."
      ) from e
    mouse_input = Input(type=INPUT_MOUSE)
    mouse_input.u.mi = MouseInput(
      dwFlags=down_flag if event.is_down else up_flag
    )
    self._api.send_input((mouse_input,))

  def _inject_touch(self, device: _WinVirtualDevice, event: TouchEvent) -> None:
    if not 0 <= event.slot < MAX_TOUCH_CONTACTS:
      raise ValueError(
        f"Touch slot {event.slot} is outside of [0, {MAX_TOUCH_CONTACTS - 1}]"
      )
    if event.is_down:
      self._touch_down_or_move(device, event)
    else:
      self._touch_up(device, event)

  def _touch_down_or_move(
    self, device: _WinVirtualDevice, event: TouchEvent
  ) -> None:
    contacts = dict(device.contacts)
    is_new = event.slot not in contacts
    contacts[event.slot] = event.position
    if is_new:
      changed_flags = POINTER_FLAG_TOUCH_DOWN
    else:
      changed_flags = POINTER_FLAG_TOUCH_UPDATE
    frame = [
      self._touch_contact(
        slot,
        position,
        changed_flags if slot == event.slot else POINTER_FLAG_TOUCH_UPDATE,
      )
      for slot, position in sorted(contacts.items())
    ]
    self._inject_touch_frame(device, frame)
    # Only commit the new state once Windows accepted the frame.
    device.contacts[event.slot] = event.position

  def _touch_up(self, device: _WinVirtualDevice, event: TouchEvent) -> None:
    if event.slot not in device.contacts:
      raise ValueError(f"Touch slot {event.slot} released but not down.")
    if device.contacts[event.slot] != event.position:
      # The UP frame must be at the contact's last position: move it first.
      self._touch_down_or_move(device, dataclasses.replace(event, is_down=True))
    others = sorted(
      (slot, pos) for slot, pos in device.contacts.items() if slot != event.slot
    )
    frame = [self._touch_contact(event.slot, event.position, POINTER_FLAG_UP)]
    frame.extend(
      self._touch_contact(slot, pos, POINTER_FLAG_TOUCH_UPDATE)
      for slot, pos in others
    )
    self._inject_touch_frame(device, frame)
    del device.contacts[event.slot]

  def _touch_update_frame(
    self, device: _WinVirtualDevice
  ) -> list[PointerTypeInfo]:
    return [
      self._touch_contact(slot, position, POINTER_FLAG_TOUCH_UPDATE)
      for slot, position in sorted(device.contacts.items())
    ]

  def _inject_touch_frame(
    self, device: _WinVirtualDevice, frame: Sequence[PointerTypeInfo]
  ) -> None:
    if not device.touch_device:
      raise ValueError(f"Device '{device.name}' has no synthetic touch device")
    if device.last_touch_frame is not None:
      self._wait_until_time(
        device.last_touch_frame + MIN_TOUCH_FRAME_INTERVAL.total_seconds()
      )
    self._api.inject_touch(device.touch_device, frame)
    device.last_touch_frame = self._monotonic()

  def _wait_until_time(self, deadline: float) -> None:
    if (remaining := deadline - self._monotonic()) > 0:
      self._sleep(remaining)

  def _touch_contact(
    self, slot: int, position: Point, flags: int
  ) -> PointerTypeInfo:
    # Windows rejects contacts whose point or area leaves the virtual desktop.
    screen = self._virtual_screen()
    right = screen.left + screen.width - 1
    bottom = screen.top + screen.height - 1
    x = max(screen.left, min(right, position.x))
    y = max(screen.top, min(bottom, position.y))
    contact = PointerTypeInfo(type=PT_TOUCH)
    touch = contact.u.touchInfo
    touch.pointerInfo.pointerType = PT_TOUCH
    touch.pointerInfo.pointerId = slot
    touch.pointerInfo.pointerFlags = flags
    touch.pointerInfo.ptPixelLocation = Win32Point(x, y)
    touch.touchMask = (
      TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE
    )
    touch.rcContact = Win32Rect(
      max(screen.left, x - TOUCH_CONTACT_RADIUS),
      max(screen.top, y - TOUCH_CONTACT_RADIUS),
      min(right, x + TOUCH_CONTACT_RADIUS),
      min(bottom, y + TOUCH_CONTACT_RADIUS),
    )
    touch.orientation = TOUCH_ORIENTATION
    # W3C PointerEvent.pressure is 0 once the contact is lifted.
    touch.pressure = 0 if flags == POINTER_FLAG_UP else TOUCH_PRESSURE
    return contact
