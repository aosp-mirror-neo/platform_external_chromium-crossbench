# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import unittest
from unittest import mock

from crossbench.probes.downloads import DownloadsProbe
from crossbench.probes.probe_error import ProbeIncompatibleBrowser
from tests import test_helper


class DownloadsProbeValidateBrowserTestCase(unittest.TestCase):
  def _validate(
    self, is_android: bool = False, is_chromeos: bool = False
  ) -> None:
    platform = mock.Mock(is_android=is_android, is_chromeos=is_chromeos)
    DownloadsProbe().validate_browser(mock.Mock(), mock.Mock(platform=platform))

  def test_android(self) -> None:
    self._validate(is_android=True)

  def test_chromeos(self) -> None:
    self._validate(is_chromeos=True)

  def test_desktop_fails_validation(self) -> None:
    with self.assertRaisesRegex(
      ProbeIncompatibleBrowser, "use --no-probe=downloads"
    ):
      self._validate()


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
