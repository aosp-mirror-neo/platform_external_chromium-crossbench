# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import io
import subprocess
from typing import Final
from unittest import mock

from crossbench.cli.cli import CrossBenchCLI
from crossbench.cli.subcommand import setup_wpr_ethernet
from crossbench.cli.subcommand.setup_wpr_ethernet import (
  SetupNetwork,
  SetupWprEthernetSubcommand,
)
from tests import test_helper
from tests.crossbench.base import BaseCliTestCase, SysExitTestException
from tests.crossbench.mock_helper import MacOsMockPlatform

_SETUP_NETWORK_PATCH_TARGET: Final[str] = (
  "crossbench.cli.subcommand.setup_wpr_ethernet.SetupNetwork"
)


class SetupWprEthernetSubcommandTest(BaseCliTestCase):
  def setUp(self) -> None:
    super().setUp()
    self.cli_instance = CrossBenchCLI()
    self.subcommand = self.cli_instance.subcommands["setup_wpr_ethernet"]
    self.assertIsInstance(self.subcommand, SetupWprEthernetSubcommand)

  def test_aliases_registered(self) -> None:
    self.assertIs(
      self.cli_instance.subcommands["setup-wpr-ethernet"], self.subcommand
    )
    self.assertNotIn("setup_cross_platform_mode", self.cli_instance.subcommands)

  def test_missing_interface(self) -> None:
    with (
      mock.patch(_SETUP_NETWORK_PATCH_TARGET) as mock_setup,
      self.assertRaises(SysExitTestException),
    ):
      self.run_cli("setup_wpr_ethernet")
    mock_setup.assert_not_called()

  def test_run_primary_name(self) -> None:
    with mock.patch(_SETUP_NETWORK_PATCH_TARGET) as mock_setup:
      self.run_cli("setup_wpr_ethernet", "--interface=en7")
    mock_setup.assert_called_once_with("en7")
    mock_setup.return_value.run.assert_called_once()


class SetupNetworkIosDeviceTestCase(BaseCliTestCase):
  def setUp(self) -> None:
    super().setUp()
    macos_plt = MacOsMockPlatform()
    xcrun_path = macos_plt.path("/usr/bin/xcrun")
    self.enterContext(
      mock.patch.object(macos_plt, "which", return_value=xcrun_path)
    )
    self.enterContext(
      mock.patch.object(setup_wpr_ethernet, "PLATFORM", macos_plt)
    )

  def test_setup_device_ios_uses_devicectl_not_idevice_id(self) -> None:
    setup = SetupNetwork("en7")
    setup._is_android = False
    stdout = io.StringIO()
    with (
      mock.patch.object(
        setup_wpr_ethernet,
        "ios_devices",
        return_value={"00001111-AA": mock.Mock()},
      ),
      mock.patch.object(setup, "_sh") as mock_sh,
      mock.patch("sys.stdout", stdout),
    ):
      setup._setup_device()
    mock_sh.assert_not_called()
    self.assertIn("00001111-AA", stdout.getvalue())

  def test_setup_device_ios_fails_when_no_device(self) -> None:
    setup = SetupNetwork("en7")
    setup._is_android = False
    with (
      mock.patch.object(
        setup_wpr_ethernet,
        "ios_devices",
        return_value={},
      ) as mock_ios_devices,
      self.assertRaises(SystemExit),
    ):
      setup._setup_device()
    mock_ios_devices.assert_called_once()

  def test_setup_device_ios_fails_when_multiple_devices(self) -> None:
    setup = SetupNetwork("en7")
    setup._is_android = False
    with (
      mock.patch.object(
        setup_wpr_ethernet,
        "ios_devices",
        return_value={"udid-1": mock.Mock(), "udid-2": mock.Mock()},
      ),
      self.assertRaises(SystemExit),
    ):
      setup._setup_device()


class SetupNetworkBinaryCheckTestCase(BaseCliTestCase):
  def test_missing_dnsmasq_fails_early(self) -> None:
    setup = SetupNetwork("en7")
    macos_plt = MacOsMockPlatform()
    with (
      mock.patch.object(setup_wpr_ethernet, "PLATFORM", macos_plt),
      mock.patch.object(setup, "_check_device") as mock_check_device,
      self.assertRaises(SystemExit),
    ):
      setup._setup()
    mock_check_device.assert_not_called()


class SetupNetworkPortCheckTestCase(BaseCliTestCase):
  def test_loopback_listener_is_ignored(self) -> None:
    setup = SetupNetwork("en7")
    lsof_output = (
      b"COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
      b"beyondcorp-dns 885 root 17u IPv6 0x1 0t0 TCP [::1]:53 (LISTEN)\n"
      b"beyondcorp-dns 885 root 18u IPv4 0x2 0t0 TCP 127.0.0.1:53 (LISTEN)\n"
    )
    completed = subprocess.CompletedProcess(
      args=[], returncode=0, stdout=lsof_output, stderr=b""
    )
    with mock.patch.object(setup, "_sh", return_value=completed) as mock_sh:
      setup._check_port_available(53)
    mock_sh.assert_called_once_with("sudo", "lsof", "+c0", "-n", "-P", "-i:53")

  def test_wildcard_or_host_ip_listener_fails(self) -> None:
    setup = SetupNetwork("en7")
    for addr in ("*:53", "192.168.1.1:53"):
      line = f"dnsmasq 900 root 5u IPv4 0x1 0t0 TCP {addr} (LISTEN)\n"
      lsof_output = line.encode("utf-8")
      completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=lsof_output, stderr=b""
      )
      with (
        mock.patch.object(setup, "_sh", return_value=completed),
        self.assertRaises(SystemExit),
      ):
        setup._check_port_available(53)


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
