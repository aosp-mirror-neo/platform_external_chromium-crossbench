# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""In-process screen capture for Windows.

Captures the primary display with Pillow (PIL.ImageGrab, which uses GDI) and
stores it as PNG, avoiding a slow powershell / .NET subprocess for every
screenshot.
"""

import abc
import ctypes
import sys
from typing import TYPE_CHECKING, Final, override

from PIL import Image, ImageGrab

from crossbench.plt.display_info import DisplayResolution

if TYPE_CHECKING:
  from crossbench import path as pth

HANDLE: Final = ctypes.c_void_p

SM_CXSCREEN: Final[int] = 0
SM_CYSCREEN: Final[int] = 1
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final[int] = -4

# Favor speed over size, screenshots are taken while stories run.
PNG_COMPRESSION_LEVEL: Final[int] = 1


class Win32ScreenCaptureApi(abc.ABC):
  """Thin abstraction over the calls used for screenshots."""

  @abc.abstractmethod
  def screen_resolution(self) -> DisplayResolution:
    """Size of the primary display in physical pixels."""

  @abc.abstractmethod
  def capture(self) -> Image.Image:
    """Screenshot of the primary display in physical pixels."""


class CtypesWin32ScreenCaptureApi(Win32ScreenCaptureApi):
  """Win32ScreenCaptureApi using user32.dll through ctypes and Pillow."""

  def __init__(self) -> None:
    self._user32: Final[ctypes.CDLL] = self._load_dll("user32")
    self._declare_signatures()

  def _load_dll(self, name: str) -> ctypes.CDLL:
    if sys.platform != "win32":
      raise RuntimeError("Win32 screen capture requires a Windows host.")
    # pyrefly only narrows sys.platform inside a positive check, not after the
    # early bailout above.
    # pyrefly: ignore[missing-attribute]
    return ctypes.WinDLL(name, use_last_error=True)

  def _declare_signatures(self) -> None:
    user32 = self._user32
    user32.SetThreadDpiAwarenessContext.argtypes = [HANDLE]
    user32.SetThreadDpiAwarenessContext.restype = HANDLE
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int

  def _set_dpi_awareness(self, context: int | None) -> int | None:
    return self._user32.SetThreadDpiAwarenessContext(context)

  @override
  def screen_resolution(self) -> DisplayResolution:
    # Report physical pixels, not DPI-virtualized ones.
    previous = self._set_dpi_awareness(
      DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    )
    try:
      return DisplayResolution(
        self._user32.GetSystemMetrics(SM_CXSCREEN),
        self._user32.GetSystemMetrics(SM_CYSCREEN),
      )
    finally:
      self._set_dpi_awareness(previous)

  @override
  def capture(self) -> Image.Image:
    # Pillow makes the capturing thread DPI aware, so the image has physical
    # pixels. Include layered (e.g. transparent) windows in the capture.
    return ImageGrab.grab(include_layered_windows=True)


class WinScreenCapture:
  """Takes primary display screenshots and stores them as PNG files."""

  def __init__(self, api: Win32ScreenCaptureApi) -> None:
    self._api: Final[Win32ScreenCaptureApi] = api

  def resolution(self) -> DisplayResolution:
    return self._api.screen_resolution()

  def save_png(self, path: pth.LocalPath) -> None:
    self._api.capture().save(
      path, format="PNG", compress_level=PNG_COMPRESSION_LEVEL
    )
