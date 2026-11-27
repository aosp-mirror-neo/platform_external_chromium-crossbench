# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
import argparse
import base64
import datetime as dt
import logging
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from typing import TYPE_CHECKING, override
from unittest import mock

import crossbench.path as pth
from crossbench.browsers.attributes import BrowserAttributes
from crossbench.browsers.settings import Settings
from crossbench.cli.config.probe import ProbeConfig
from crossbench.cli.config.probe_list import ProbeListConfig
from crossbench.config import ConfigError
from crossbench.flags.chrome import ChromeFlags
from crossbench.plt.android_adb import Adb
from crossbench.plt.arch import MachineArch
from crossbench.probes.all import PerfettoProbe
from crossbench.probes.cb_perfetto import chrome_triggers
from crossbench.probes.cb_perfetto.chrome_triggers import (
  ChromeTriggerConfig,
  SnapshotTriggerInfo,
  apply_triggers_to_trace_config,
  inspect_triggered_snapshots,
)
from crossbench.probes.cb_perfetto.context.android import (
  AndroidPerfettoProbeContext,
)
from crossbench.probes.cb_perfetto.context.chromeos import (
  ChromeOsPerfettoProbeContext,
)
from crossbench.probes.cb_perfetto.context.desktop import (
  DesktopPerfettoProbeContext,
)
from crossbench.probes.cb_perfetto.context.macos import MacPerfettoProbeContext
from crossbench.probes.cb_perfetto.context.windows import (
  WindowsPerfettoProbeContext,
)
from crossbench.probes.cb_perfetto.downloader import PerfettoToolDownloader
from crossbench.probes.cb_perfetto.perfetto import TraceConfig
from crossbench.probes.probe_error import ProbeIncompatibleBrowser
from crossbench.runner.anomalies import Anomaly, AnomalySource
from tests import test_helper
from tests.crossbench.base import (
  CrossbenchConfigTestMixin,
  CrossbenchFakeFsTestCase,
)
from tests.crossbench.mock_browser import MockChromeStable
from tests.crossbench.mock_helper import (
  AndroidAdbMockPlatform,
  ChromeOsSshMockPlatform,
  LinuxMockPlatform,
  MacOsMockPlatform,
  MockPopen,
  MockPopenState,
  WinMockPlatform,
)
from tests.crossbench.runner.helper import MockRun
from third_party.protoc import scenario_config_pb2, trace_config_pb2

if TYPE_CHECKING:
  from crossbench.plt.base import Platform


class TraceConfigTestCase(unittest.TestCase):
  def test_parse_preset(self):
    config = TraceConfig.parse("v8")
    self.assertIsInstance(config, TraceConfig)
    # v8 preset has some data sources
    self.assertTrue(len(config.trace_config.data_sources) > 0)

  def test_parse_dict_raw_proto(self):
    config = TraceConfig.parse(
      {
        "buffers": [
          {
            "size_kb": 1024,
          }
        ],
        "data_sources": [
          {
            "config": {
              "name": "linux.process_stats",
            },
          }
        ],
      }
    )
    self.assertIsInstance(config, TraceConfig)
    self.assertEqual(len(config.trace_config.buffers), 1)
    self.assertEqual(config.trace_config.buffers[0].size_kb, 1024)
    self.assertEqual(len(config.trace_config.data_sources), 1)
    self.assertEqual(
      config.trace_config.data_sources[0].config.name, "linux.process_stats"
    )

  def test_parse_str_close_match(self):
    with self.assertLogs(level="ERROR") as cm:
      # "v8-profiling" is a valid preset, "v8-profiler" is close.
      config = TraceConfig.parse_str("v8-profiler")
      self.assertIsInstance(config, TraceConfig)
      # It should have picked "v8-profiling"
      self.assertEqual(config, TraceConfig.parse_str("v8-profiling"))
    self.assertIn("v8-profiling", cm.output[0])


class PerfettoProbeTestCase(unittest.TestCase):
  def test_parse_empty(self):
    probe = PerfettoProbe.parse_str("")
    self.assertIsInstance(probe, PerfettoProbe)
    self.assertEqual(
      probe.trace_config, TraceConfig.parse_str("default").trace_config
    )

  def test_create_empty(self):
    probe = PerfettoProbe()
    self.assertIsInstance(probe, PerfettoProbe)
    self.assertEqual(
      probe.trace_config, TraceConfig.parse_str("default").trace_config
    )

  def test_parse_tags_mixed(self):
    probe = PerfettoProbe.parse_tags("tag1,+tag2,-tag3")
    self.assertSequenceEqual(probe.enabled_tags, ("tag1", "tag2"))
    self.assertSequenceEqual(probe.disabled_tags, ("tag3",))

  def test_merged_simple(self):
    probe = PerfettoProbe.parse_str("v8")
    merged = probe.trace_config
    self.assertIsInstance(merged, trace_config_pb2.TraceConfig)

  def test_merged_tags(self):
    probe = PerfettoProbe.parse_str("tag1,-tag2")
    merged = probe.trace_config
    track_event_configs = [
      ds.config.track_event_config
      for ds in merged.data_sources
      if ds.config.name == "track_event"
    ]
    self.assertEqual(len(track_event_configs), 1)
    te_config = track_event_configs[0]
    self.assertIn("tag1", te_config.enabled_tags)
    self.assertIn("tag2", te_config.disabled_tags)

  def test_merged_categories(self):
    probe = PerfettoProbe.parse_dict(
      {
        "enabled_categories": ["cat1"],
        "disabled_categories": ["cat2"],
        "enabled_tags": ["cat4"],
      }
    )
    merged = probe.trace_config
    self.assertIsInstance(merged, trace_config_pb2.TraceConfig)
    track_event_configs = [
      ds.config.track_event_config
      for ds in merged.data_sources
      if ds.config.name == "track_event"
    ]
    self.assertEqual(len(track_event_configs), 1)
    te_config = track_event_configs[0]
    self.assertIn("cat1", te_config.enabled_categories)
    self.assertIn("cat2", te_config.disabled_categories)
    self.assertIn("cat4", te_config.enabled_tags)

  def test_parse_dict_combined(self):
    probe = PerfettoProbe.parse_dict(
      {
        "trace_config": "v8",
        "tags": ["tag1"],
        "categories": ["cat1"],
      }
    )
    self.assertIsInstance(probe, PerfettoProbe)
    # v8 preset has some data sources
    self.assertTrue(len(probe.trace_config.data_sources) > 0)
    merged = probe.trace_config
    track_event_configs = [
      ds.config.track_event_config
      for ds in merged.data_sources
      if ds.config.name == "track_event"
    ]
    self.assertEqual(len(track_event_configs), 1)
    te_config = track_event_configs[0]
    self.assertIn("tag1", te_config.enabled_tags)
    self.assertIn("cat1", te_config.enabled_categories)

  def test_empty_dict_config(self):
    probe = PerfettoProbe.parse_dict({})
    self.assertIsInstance(probe, PerfettoProbe)
    self.assertEqual(
      probe.trace_config, TraceConfig.parse_str("default").trace_config
    )

  def test_missing_config(self):
    with self.assertRaisesRegex(argparse.ArgumentTypeError, "unknown 1"):
      PerfettoProbe.parse_dict({"preset": "unknown 1"})
    with self.assertRaisesRegex(argparse.ArgumentTypeError, "unknown 1"):
      PerfettoProbe.parse_str("unknown 1")

  def test_parse_config(self):
    trace_config = """
        buffers: {
            size_kb: 1234
            fill_policy: DISCARD
        }
    """
    probe: PerfettoProbe = PerfettoProbe.parse_dict(
      {"trace_config": trace_config}
    )
    self.assertEqual(probe.trace_config.buffers[0].size_kb, 1234)
    self.assertEqual(pth.AnyPath("perfetto"), probe.perfetto_bin)

  def test_parse_example_config(self):
    config_file = test_helper.config_dir() / "doc/probe/perfetto.config.hjson"
    self.assertTrue(config_file.is_file())
    probes = ProbeListConfig.parse(config_file).probes
    self.assertEqual(len(probes), 1)
    probe = probes[0]
    self.assertIsInstance(probe, PerfettoProbe)

  def test_trace_config_preset_invalid_file(self):
    trace_config_dir = test_helper.config_dir() / "probe/perfetto/trace_config"
    for config_file in trace_config_dir.glob("*.pbtxt"):
      self.fail(f"Invalid preset file extension, use .textpb: {config_file}")

  def test_trace_config_preset(self):
    trace_config_dir = test_helper.config_dir() / "probe/perfetto/trace_config"
    preset_count = 0
    for config_file in trace_config_dir.glob("*.txtpb"):
      preset_count += 1
      with self.subTest(config_file=str(config_file)):
        probe_a = PerfettoProbe.parse_dict({"trace_config": config_file.stem})
        probe_b = PerfettoProbe.parse_str(config_file.stem)
        probe_c = PerfettoProbe.parse_str(str(config_file))
        self.assertEqual(probe_a.trace_config, probe_b.trace_config)
        self.assertEqual(probe_a.trace_config, probe_c.trace_config)
        for data_source in probe_b.trace_config.data_sources:
          config = data_source.config
          self.assertNotEqual(
            config.name,
            "org.chromium.trace_metadata",
            "Please use the new org.chromium.trace_metadata2 data_source "
            "without the added json-serialized categories.",
          )
          self.assertFalse(
            config.chrome_config and config.chrome_config.trace_config,
            "Please use the org.chromium.trace_metadata2 data source "
            "which does not require the json-serialized trace_config",
          )
    self.assertGreater(preset_count, 0)

  def test_preset_file_from_probe_config(self):
    trace_config_file = TraceConfig.preset_dir() / "v8.txtpb"
    self.assertTrue(trace_config_file.is_file())
    probe = PerfettoProbe.parse_str(str(trace_config_file))
    probe_a = ProbeConfig.parse("perfetto:v8").new_instance()
    self.assertTrue(trace_config_file.is_file())
    src_str = f"perfetto:{trace_config_file}"
    probe_b = ProbeConfig.parse(src_str).new_instance()
    self.assertEqual(probe, probe_a)
    self.assertEqual(probe, probe_b)


class PerfettoToolDownloaderTestCase(CrossbenchFakeFsTestCase):
  def test_download_linux(self):
    platform = LinuxMockPlatform()
    self._download_perfetto_tool(platform, "linux-arm64")
    platform = LinuxMockPlatform()
    platform.machine = MachineArch.ARM_32
    self._download_perfetto_tool(platform, "linux-arm")
    platform = LinuxMockPlatform()
    platform.machine = MachineArch.X64
    self._download_perfetto_tool(platform, "linux-x64")

  def test_download_macos(self):
    platform = MacOsMockPlatform()
    self._download_perfetto_tool(platform, "mac-arm64")
    platform = MacOsMockPlatform()
    platform.machine = MachineArch.X64
    self._download_perfetto_tool(platform, "mac-amd64")

  def test_download_win(self):
    platform = WinMockPlatform()
    platform.machine = MachineArch.X64
    self._download_perfetto_tool(platform, "windows-amd64")

  def test_download_win_invalid(self):
    platform = WinMockPlatform()
    platform.machine = MachineArch.ARM_64
    with self.assertRaises(ValueError):
      self._download_perfetto_tool(platform, "win-arm64")

  def _download_perfetto_tool(self, platform, key):
    platform.use_mock_name = False
    tool = "traceconv.exe" if platform.is_win else "traceconv"
    download_path = platform.cache_dir("perfetto") / f"v59.1/{tool}"
    platform.expect_download(
      "https://commondatastorage.googleapis.com/perfetto-luci-artifacts/"
      f"v59.1/{key}/{tool}",
      download_path,
    )
    platform.expect_sh(
      download_path,
      "--version",
      result=(
        "Perfetto v59.1-f5cb507 (f5cb5074e597348bffd1796b66a1da33cc1ea421d8)"
      ),
    )
    result = PerfettoToolDownloader("traceconv", platform=platform).download()
    self.assertTrue(platform.exists(result))
    # downloading the same will use the locally cached version
    result = PerfettoToolDownloader("traceconv", platform=platform).download()
    self.assertTrue(platform.exists(result))


class PerfettoProbeFunctionalTestCase(
  CrossbenchConfigTestMixin, CrossbenchFakeFsTestCase
):
  @override
  def setUp(self) -> None:
    super().setUp()
    self.setup_perfetto_config_presets()

  def test_attach_v8_code_logger(self):
    trace_config = trace_config_pb2.TraceConfig()
    data_source = trace_config.data_sources.add()
    data_source.config.name = "dev.v8.code"
    probe = PerfettoProbe(trace_config=trace_config)

    platform = LinuxMockPlatform()
    MockChromeStable.setup_fs(self.fs, platform)
    browser = MockChromeStable(
      "mock browser", settings=Settings(platform=platform)
    )

    probe.attach(browser)
    self.assertIn("--perfetto-code-logger", browser.js_flags)

  def test_log_results(self):
    probe = PerfettoProbe.parse_str("v8")
    run = mock.Mock()
    trace_file = self.create_file("perfetto.trace.pb")
    snapshot_file = self.create_file("perfetto_snapshot_000.trace.pb")
    run.results = {
      probe: mock.Mock(file=trace_file, file_list=[snapshot_file, trace_file]),
    }

    with self.assertLogs(level=logging.INFO) as cm:
      probe.log_run_result(run)
    self.assertIn("Perfetto trace results", cm.output[1])

    group = mock.Mock()
    group.runs = [run]
    with self.assertLogs(level=logging.INFO) as cm:
      probe.log_browsers_result(group)
    self.assertIn("Perfetto trace results", cm.output[1])

  def test_help_text_items(self):
    # Ensure there is at least one preset in the fake FS
    dummy_preset = TraceConfig.preset_dir() / "dummy_preset.txtpb"
    self.fs.create_file(dummy_preset)
    help_items = TraceConfig.help_text_items()
    self.assertTrue(any(k == "presets" for k, v in help_items))
    presets_str = dict(help_items)["presets"]
    self.assertIn("dummy_preset", presets_str)

  def test_create_context_desktop(self):
    probe = PerfettoProbe.parse_str("v8")
    host_platform = LinuxMockPlatform()

    run_linux = mock.Mock()
    run_linux.out_dir = pth.LocalPath("/tmp")
    run_linux.browser_platform = host_platform
    context = probe.create_context(run_linux)
    self.assertIsInstance(context, DesktopPerfettoProbeContext)

  def test_create_context_macos(self) -> None:
    probe = PerfettoProbe.parse_str("v8")
    run_macos = mock.Mock()
    run_macos.out_dir = pth.LocalPath("/tmp")
    run_macos.browser_platform = MacOsMockPlatform()
    context = probe.create_context(run_macos)
    self.assertIsInstance(context, MacPerfettoProbeContext)

  def test_create_context_win(self) -> None:
    probe = PerfettoProbe.parse_str("v8")
    run_win = mock.Mock()
    run_win.out_dir = pth.LocalPath("/tmp")
    run_win.browser_platform = WinMockPlatform()
    context = probe.create_context(run_win)
    self.assertIsInstance(context, WindowsPerfettoProbeContext)

  def test_create_context_android(self):
    probe = PerfettoProbe.parse_str("v8")
    host_platform = LinuxMockPlatform()

    adb_bin = host_platform.path("/usr/bin/adb")
    self.fs.create_file(adb_bin)

    adb = mock.Mock(spec=Adb)
    adb.host_platform = host_platform
    adb.serial_id = "777"
    run_android = mock.Mock()
    run_android.out_dir = pth.LocalPath("/tmp")
    run_android.browser_platform = AndroidAdbMockPlatform(
      host_platform=host_platform, adb=adb
    )
    context = probe.create_context(run_android)
    self.assertIsInstance(context, AndroidPerfettoProbeContext)

  def test_create_context_chrome_os(self):
    probe = PerfettoProbe.parse_str("v8")
    host_platform = LinuxMockPlatform()

    run_chromeos = mock.Mock()
    run_chromeos.out_dir = pth.LocalPath("/tmp")
    run_chromeos.browser_platform = ChromeOsSshMockPlatform(
      host_platform=host_platform,
      host="host",
      port=22,
      ssh_port=22,
      ssh_user="user",
    )
    context = probe.create_context(run_chromeos)
    self.assertIsInstance(context, ChromeOsPerfettoProbeContext)

  def test_get_extra_probes(self):
    probe = PerfettoProbe.parse_str("v8")
    runner = mock.Mock()
    extra_probes = list(probe.get_extra_probes(runner))
    self.assertIsInstance(extra_probes, list)


class PerfettoProbeContextTestCase(
  CrossbenchConfigTestMixin, CrossbenchFakeFsTestCase
):
  @override
  def setUp(self) -> None:
    super().setUp()
    self.setup_perfetto_config_presets()

  def _create_desktop_context(self, probe: PerfettoProbe):
    platform = LinuxMockPlatform(fake_fs=self.fs)
    platform.install_mock_binary("tracebox", "/usr/bin/tracebox")
    platform.install_mock_binary("perfetto", "/usr/bin/perfetto")

    tracebox_proc = MockPopen()
    platform.popens.append(tracebox_proc)

    MockChromeStable.setup_fs(self.fs, platform)
    browser = MockChromeStable(
      "mock browser", settings=Settings(platform=platform)
    )
    browser.performance_mark = mock.Mock()
    browser_session = mock.Mock()
    browser_session.browser = browser
    browser_session.root_dir = pth.LocalPath("/tmp/results")
    self.fs.create_dir(browser_session.root_dir)
    run = MockRun(
      runner=mock.Mock(),
      browser_session=browser_session,
      story=mock.Mock(),
      probe=probe,
    )
    # MockRun calculates out_dir from browser_session.root_dir
    self.fs.create_dir(run.out_dir)
    run.get_default_probe_result_path = mock.Mock(
      return_value=run.out_dir / "perfetto"
    )

    context = DesktopPerfettoProbeContext(probe, run)
    return context, platform, tracebox_proc

  def test_desktop_context_lifecycle(self):
    probe = PerfettoProbe.parse_str("v8")
    context, platform, tracebox_proc = self._create_desktop_context(probe)
    run = context.run

    # setup
    platform.expect_sh("tracebox", "traced", "traced_probes", result="")
    self.assertEqual(tracebox_proc.state, MockPopenState.UNUSED)
    context.setup()
    self.assertTrue(self.fs.exists(run.out_dir / "perfetto_config.textproto"))
    self.assertEqual(tracebox_proc.state, MockPopenState.RUNNING)

    # start
    platform.expect_sh(
      "tracebox",
      "perfetto",
      "--background",
      "--config",
      str(run.out_dir / "perfetto_config.textproto"),
      "--txt",
      "--out",
      str(run.out_dir / "perfetto.trace.pb"),
      result="123",
    )
    context.start()
    run.browser.performance_mark.assert_called_with(
      "probe-perfetto-start", detail=None, prefix="crossbench-"
    )
    self.assertEqual(platform.trace_marks, ["crossbench-probe-perfetto-start"])
    self.assertEqual(tracebox_proc.state, MockPopenState.RUNNING)

    context.stop()
    self.assertEqual(tracebox_proc.state, MockPopenState.RUNNING)
    run.browser.performance_mark.assert_called_with(
      "probe-perfetto-stop", detail=None, prefix="crossbench-"
    )
    self.assertEqual(
      platform.trace_marks,
      ["crossbench-probe-perfetto-start", "crossbench-probe-perfetto-stop"],
    )

    # teardown
    trace_file = run.browser_platform.local_path(context.result_path)
    self.fs.create_file(trace_file)
    platform.expect_sh("gzip", str(trace_file))
    self.fs.create_file(trace_file.with_suffix(".pb.gz"))

    context.teardown()
    self.assertEqual(tracebox_proc.state, MockPopenState.TERMINATED)

  def test_desktop_context_clone_snapshot(self):
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.unique_session_name = "test_session"
    probe = PerfettoProbe(trace_config=trace_config)
    context, platform, _ = self._create_desktop_context(probe)
    run = context.run

    platform.expect_sh("tracebox", "traced", "traced_probes", result="")
    context.setup()

    platform.expect_sh(
      "tracebox",
      "perfetto",
      "--background",
      "--config",
      str(run.out_dir / "perfetto_config.textproto"),
      "--txt",
      "--out",
      str(run.out_dir / "perfetto.trace.pb"),
      result="123",
    )
    context.start()

    tmp_dir: pth.LocalPath = run.out_dir / "tmp_snapshot"
    self.fs.create_dir(tmp_dir)
    tmp_snapshot_file: pth.LocalPath = (
      tmp_dir / "perfetto_snapshot_000_jank_1.trace.pb"
    )
    snapshot_file: pth.LocalPath = (
      run.out_dir / "perfetto_snapshot_000_jank_1.trace.pb"
    )
    platform.expect_sh(
      "tracebox",
      "perfetto",
      "--clone-by-name",
      "test_session",
      "--out",
      str(tmp_snapshot_file),
    )
    self.fs.create_file(tmp_snapshot_file, contents="snapshot_bytes")
    platform.expect_sh("gzip", str(snapshot_file))
    self.fs.create_file(snapshot_file.with_suffix(".pb.gz"))

    with mock.patch.object(platform, "mkdtemp", return_value=tmp_dir):
      context.invoke(("step_1",), label="jank_1")
    self.assertFalse(self.fs.exists(tmp_dir))

    context.stop()
    trace_file: pth.LocalPath = run.browser_platform.local_path(
      context.result_path
    )
    self.fs.create_file(trace_file)
    platform.expect_sh("gzip", str(trace_file))
    self.fs.create_file(trace_file.with_suffix(".pb.gz"))

    result = context.teardown()
    self.assertEqual(len(result.file_list), 2)
    self.assertEqual(len(result.perfetto_list), 1)
    mock_run = mock.Mock(results={probe: result})
    probe.log_run_result(mock_run)

  def test_desktop_context_sampling_heap_profiler_config(self) -> None:
    textproto = """
      buffers { size_kb: 1024 }
      data_sources {
        config {
          name: "org.chromium.native_heap_profiler"
          chromium_sampling_heap_profiler {
            sampling_interval_bytes: 4096
            sampling_interval_ms: 2000
          }
        }
      }
    """
    probe = PerfettoProbe.parse_dict({"textproto": textproto})
    ds_config = probe.trace_config.data_sources[0].config
    self.assertEqual(ds_config.name, "org.chromium.native_heap_profiler")
    heap_config = ds_config.chromium_sampling_heap_profiler
    self.assertEqual(heap_config.sampling_interval_bytes, 4096)
    self.assertEqual(heap_config.sampling_interval_ms, 2000)

    context, platform, _ = self._create_desktop_context(probe)
    platform.expect_sh("tracebox", "traced", "traced_probes", result="")
    context.setup()

    written = platform.cat(context.run.out_dir / "perfetto_config.textproto")
    roundtrip = TraceConfig.parse_textproto(written).trace_config
    rt_heap_config = roundtrip.data_sources[
      0
    ].config.chromium_sampling_heap_profiler
    self.assertEqual(rt_heap_config.sampling_interval_bytes, 4096)
    self.assertEqual(rt_heap_config.sampling_interval_ms, 2000)

  def test_windows_context_setup(self) -> None:
    trace_config = trace_config_pb2.TraceConfig()
    buf0 = trace_config.buffers.add()
    buf0.name = "metadata"
    buf0.size_kb = 1024
    buf1 = trace_config.buffers.add()
    buf1.name = "track_event"
    buf1.size_kb = 4096
    ds = trace_config.data_sources.add()
    ds.config.name = "track_event"
    ds.config.target_buffer_name = "track_event"
    probe = PerfettoProbe(trace_config=trace_config)

    platform = WinMockPlatform(fake_fs=self.fs)
    browser = mock.Mock(
      platform=platform, host_platform=platform, unique_name="mock_chrome"
    )
    browser_session = mock.Mock()
    browser_session.browser = browser
    browser_session.root_dir = pth.LocalPath("/tmp/results")
    self.fs.create_dir(browser_session.root_dir)
    run = MockRun(
      runner=mock.Mock(), browser_session=browser_session, story=mock.Mock()
    )
    self.fs.create_dir(run.out_dir)
    run.get_default_probe_result_path = mock.Mock(
      return_value=run.out_dir / "perfetto"
    )

    context = WindowsPerfettoProbeContext(probe, run)
    with self.assertLogs(level="WARNING") as logs:
      context.setup()
    self.assertIn("replacing trace config buffer names", logs.output[0])

    config_pb_path = run.out_dir / "perfetto_config.pb"
    self.assertTrue(self.fs.exists(config_pb_path))
    parsed = trace_config_pb2.TraceConfig()
    parsed.ParseFromString(platform.read_bytes(config_pb_path))
    # Buffer sizes are left as configured.
    self.assertEqual(parsed.buffers[0].size_kb, 1024)
    self.assertEqual(parsed.buffers[1].size_kb, 4096)
    for buf in parsed.buffers:
      self.assertFalse(buf.HasField("name"))
    self.assertEqual(parsed.data_sources[0].config.target_buffer, 1)
    self.assertFalse(
      parsed.data_sources[0].config.HasField("target_buffer_name")
    )

  def _validate_browser(
    self, trace_config: trace_config_pb2.TraceConfig, platform: Platform
  ) -> None:
    probe = PerfettoProbe(trace_config=trace_config)
    browser = mock.Mock(platform=platform)
    browser.attributes.return_value = BrowserAttributes.CHROMIUM_BASED
    probe.validate_browser(mock.Mock(), browser)

  def _write_into_file_config(self) -> trace_config_pb2.TraceConfig:
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.buffers.add().size_kb = 1024
    trace_config.write_into_file = True
    return trace_config

  def test_validate_write_into_file_on_windows(self) -> None:
    with self.assertRaisesRegex(
      ProbeIncompatibleBrowser, "write_into_file are not supported on Windows"
    ):
      self._validate_browser(self._write_into_file_config(), WinMockPlatform())

  def test_validate_write_into_file_on_macos(self) -> None:
    # Chrome streams startup traces into the file on macOS.
    self._validate_browser(self._write_into_file_config(), MacOsMockPlatform())

  def test_validate_without_write_into_file_on_windows(self) -> None:
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.buffers.add().size_kb = 1024
    self._validate_browser(trace_config, WinMockPlatform())

  def _unknown_buffer_name_config(self) -> trace_config_pb2.TraceConfig:
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.buffers.add().name = "track_event"
    ds = trace_config.data_sources.add()
    ds.config.name = "track_event"
    ds.config.target_buffer_name = "missing"
    return trace_config

  def test_validate_unknown_buffer_name_on_windows(self) -> None:
    with self.assertRaisesRegex(
      ProbeIncompatibleBrowser,
      r"unknown target_buffer_name values \['missing'\]",
    ):
      self._validate_browser(
        self._unknown_buffer_name_config(), WinMockPlatform()
      )

  def test_validate_unknown_buffer_name_on_macos(self) -> None:
    # Only Windows replaces buffer names with indices.
    self._validate_browser(
      self._unknown_buffer_name_config(), MacOsMockPlatform()
    )

  def test_validate_known_buffer_name_on_windows(self) -> None:
    trace_config = self._unknown_buffer_name_config()
    trace_config.data_sources[0].config.target_buffer_name = "track_event"
    self._validate_browser(trace_config, WinMockPlatform())

  def test_desktop_context_chrome_triggers_records_run_anomalies(self) -> None:
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.unique_session_name = "alice_ring_buffer"
    probe = PerfettoProbe(
      trace_config=trace_config,
      chrome_triggers=(
        ChromeTriggerConfig(
          name="laggy_input",
          histogram_name="EventLatency.TotalLatency",
          min_value=500000,
          stop_delay=dt.timedelta(seconds=2),
          max_triggers=5,
        ),
      ),
    )
    context, platform, tracebox_proc = self._create_desktop_context(probe)
    run = context.run
    chrome_flags = ChromeFlags()
    run.session.extra_flags = chrome_flags

    platform.expect_sh("tracebox", "traced", "traced_probes", result="")
    context.setup()

    enabled_features = chrome_flags["--enable-features"]
    assert enabled_features is not None
    self.assertTrue(enabled_features.startswith("TracingTriggers:config/"))

    written_cfg = TraceConfig.parse_textproto(
      platform.cat(run.out_dir / "perfetto_config.textproto")
    ).trace_config
    self.assertEqual(
      written_cfg.trigger_config.trigger_mode,
      trace_config_pb2.TraceConfig.TriggerConfig.CLONE_SNAPSHOT,
    )
    self.assertEqual(len(written_cfg.trigger_config.triggers), 1)
    salted_name = written_cfg.trigger_config.triggers[0].name
    self.assertTrue(salted_name.startswith("laggy_input__cb_"))

    platform.expect_sh(
      "tracebox",
      "perfetto",
      "--background",
      "--config",
      str(run.out_dir / "perfetto_config.textproto"),
      "--txt",
      "--out",
      str(run.out_dir / "perfetto.trace.pb"),
      result="123",
    )
    context.start()

    # Record an independent visual agent anomaly during the story run.
    run.anomalies.append(
      Anomaly(
        timestamp=dt.datetime(2026, 11, 17, 1, 0, 6, tzinfo=dt.UTC),
        source=AnomalySource.AGENT,
        description="Visual spinner stuck",
        perfetto_snapshot_label="episode_000_step_002_anomaly",
      )
    )

    # stop() clones the live ring buffer to perfetto.trace.pb.final before
    # stopping perfetto.
    final_clone = run.out_dir / "perfetto.trace.pb.final"
    platform.expect_sh(
      "tracebox",
      "perfetto",
      "--clone-by-name",
      "alice_ring_buffer",
      "--out",
      str(final_clone),
    )
    self.fs.create_file(final_clone, contents="main_ring_buffer_bytes")
    self.fs.create_file(run.out_dir / "perfetto.trace.pb", contents="")
    context.stop()

    # Simulate two triggered snapshot files (.0 and .1).
    self.fs.create_file(
      run.out_dir / "perfetto.trace.pb.0", contents="snap_0_bytes"
    )
    self.fs.create_file(
      run.out_dir / "perfetto.trace.pb.1", contents="snap_1_bytes"
    )

    main_trace = run.out_dir / "perfetto.trace.pb"
    snap_0_out = (
      run.out_dir / "perfetto_snapshot_000_trigger_laggy_input.trace.pb"
    )
    snap_1_out = (
      run.out_dir / "perfetto_snapshot_001_trigger_laggy_input_2.trace.pb"
    )
    platform.expect_sh("gzip", str(main_trace))
    self.fs.create_file(main_trace.with_suffix(".pb.gz"))
    platform.expect_sh("gzip", str(snap_0_out))
    self.fs.create_file(snap_0_out.with_suffix(".pb.gz"))
    platform.expect_sh("gzip", str(snap_1_out))
    self.fs.create_file(snap_1_out.with_suffix(".pb.gz"))

    rule = probe.chrome_triggers[0]
    inspected = [
      SnapshotTriggerInfo(
        rule=rule,
        timestamp=dt.datetime(2026, 11, 17, 1, 0, 2, tzinfo=dt.UTC),
        histogram_value=620000,
      ),
      SnapshotTriggerInfo(
        rule=rule,
        timestamp=dt.datetime(2026, 11, 17, 1, 0, 3, tzinfo=dt.UTC),
        histogram_value=750000,
      ),
    ]
    with mock.patch.object(
      chrome_triggers,
      "inspect_triggered_snapshots",
      return_value=inspected,
    ):
      result = context.teardown()

    self.assertEqual(tracebox_proc.state, MockPopenState.TERMINATED)
    self.assertEqual(len(result.perfetto_list), 1)
    self.assertEqual(
      [p.name for p in result.file_list],
      [
        "perfetto_snapshot_000_trigger_laggy_input.trace.pb.gz",
        "perfetto_snapshot_001_trigger_laggy_input_2.trace.pb.gz",
        "perfetto.trace.pb.gz",
      ],
    )
    self.assertFalse(self.fs.exists(run.out_dir / "perfetto.trace.pb.0"))
    self.assertFalse(self.fs.exists(run.out_dir / "perfetto.trace.pb.1"))

    anomalies_json = run.anomalies.to_json()
    self.assertEqual(len(anomalies_json), 3)
    self.assertEqual(
      [entry["source"] for entry in anomalies_json],
      ["perfetto_trigger", "perfetto_trigger", "agent"],
    )
    self.assertEqual(
      anomalies_json[0]["perfetto_snapshot_label"], "trigger_laggy_input"
    )
    self.assertEqual(
      anomalies_json[0]["description"],
      "Perfetto UMA trigger 'laggy_input': "
      "EventLatency.TotalLatency = 620000 (>= 500000)",
    )
    self.assertEqual(
      anomalies_json[0]["details"],
      {
        "trigger_name": "laggy_input",
        "histogram_name": "EventLatency.TotalLatency",
        "histogram_value": 620000,
        "min_value": 500000,
        "max_value": None,
      },
    )
    self.assertEqual(
      anomalies_json[1]["perfetto_snapshot_label"], "trigger_laggy_input_2"
    )


class ChromeTriggersConfigTestCase(unittest.TestCase):
  def test_parse_chrome_trigger_config_and_descriptions(self) -> None:
    rule = ChromeTriggerConfig.parse_dict(
      {
        "name": "laggy_input",
        "histogram_name": "EventLatency.TotalLatency",
        "min_value": 500000,
        "max_triggers": 3,
      }
    )
    self.assertEqual(rule.name, "laggy_input")
    self.assertEqual(rule.histogram_name, "EventLatency.TotalLatency")
    self.assertEqual(rule.min_value, 500000)
    self.assertIsNone(rule.max_value)
    self.assertEqual(rule.stop_delay, dt.timedelta(seconds=2))
    self.assertEqual(rule.max_triggers, 3)
    self.assertEqual(
      rule.description(),
      "Perfetto UMA trigger 'laggy_input': "
      "EventLatency.TotalLatency (>= 500000)",
    )
    self.assertEqual(
      rule.description(642000),
      "Perfetto UMA trigger 'laggy_input': "
      "EventLatency.TotalLatency = 642000 (>= 500000)",
    )

    shorthand = ChromeTriggerConfig.parse("Browser.Tabs.RendererCrash")
    self.assertEqual(shorthand.name, "Browser.Tabs.RendererCrash")
    self.assertEqual(shorthand.histogram_name, "Browser.Tabs.RendererCrash")
    self.assertIsNone(shorthand.min_value)
    self.assertIsNone(shorthand.max_value)
    self.assertEqual(shorthand.bounds_summary(), "any sample")
    self.assertEqual(
      shorthand.description(),
      "Perfetto UMA trigger 'Browser.Tabs.RendererCrash': "
      "Browser.Tabs.RendererCrash (any sample)",
    )

    exact = ChromeTriggerConfig(
      name="exact_rule",
      histogram_name="Foo.Bar",
      min_value=50,
      max_value=50,
      stop_delay=dt.timedelta(milliseconds=0),
    )
    self.assertEqual(exact.bounds_summary(), "in [50, 50]")
    upper_only = ChromeTriggerConfig(
      name="upper_rule",
      histogram_name="Foo.LowFps",
      max_value=15,
    )
    self.assertEqual(upper_only.bounds_summary(), "<= 15")
    self.assertEqual(
      upper_only.description(),
      "Perfetto UMA trigger 'upper_rule': Foo.LowFps (<= 15)",
    )

  def test_chrome_trigger_config_validation_errors(self) -> None:
    with self.assertRaises(argparse.ArgumentTypeError):
      ChromeTriggerConfig.parse_str("")
    with self.assertRaises(argparse.ArgumentTypeError):
      ChromeTriggerConfig.parse_dict(
        {"name": "", "histogram_name": "Foo.Bar", "min_value": 1}
      )
    with self.assertRaisesRegex(ConfigError, "__cb_"):
      ChromeTriggerConfig(
        name="bad__cb_salt", histogram_name="Foo.Bar", min_value=1
      )
    with self.assertRaises(argparse.ArgumentTypeError):
      ChromeTriggerConfig.parse_dict(
        {"name": "valid_name", "histogram_name": "", "min_value": 1}
      )
    with self.assertRaises(ConfigError):
      ChromeTriggerConfig(
        name="inverted", histogram_name="Foo.Bar", min_value=50, max_value=10
      )
    with self.assertRaises(ConfigError):
      ChromeTriggerConfig(
        name="overflow", histogram_name="Foo.Bar", min_value=2**63
      )
    with self.assertRaises(argparse.ArgumentTypeError):
      ChromeTriggerConfig.parse_dict(
        {
          "name": "neg_delay",
          "histogram_name": "Foo.Bar",
          "min_value": 1,
          "stop_delay": "-1s",
        }
      )
    with self.assertRaises(argparse.ArgumentTypeError):
      ChromeTriggerConfig.parse_dict(
        {
          "name": "zero_max",
          "histogram_name": "Foo.Bar",
          "min_value": 1,
          "max_triggers": 0,
        }
      )

  def test_perfetto_probe_chrome_triggers_validation(self) -> None:
    with self.assertRaisesRegex(argparse.ArgumentTypeError, "duplicates"):
      PerfettoProbe.parse_dict(
        {
          "chrome_triggers": [
            {"name": "dup", "histogram_name": "A.B", "min_value": 1},
            {"name": "dup", "histogram_name": "C.D", "min_value": 2},
          ]
        }
      )

    write_file_cfg = trace_config_pb2.TraceConfig(write_into_file=True)
    write_file_cfg.buffers.add(size_kb=1024)
    with self.assertRaisesRegex(argparse.ArgumentTypeError, "write_into_file"):
      PerfettoProbe(
        trace_config=write_file_cfg,
        chrome_triggers=(
          ChromeTriggerConfig(name="r1", histogram_name="A.B", min_value=1),
        ),
      )

    manual_trig_cfg = trace_config_pb2.TraceConfig()
    manual_trig_cfg.buffers.add(size_kb=1024)
    manual_trig_cfg.trigger_config.trigger_timeout_ms = 1000
    with self.assertRaisesRegex(argparse.ArgumentTypeError, "trigger_config"):
      PerfettoProbe(
        trace_config=manual_trig_cfg,
        chrome_triggers=(
          ChromeTriggerConfig(name="r1", histogram_name="A.B", min_value=1),
        ),
      )

    probe = PerfettoProbe(
      chrome_triggers=(
        ChromeTriggerConfig(name="r1", histogram_name="A.B", min_value=1),
      )
    )
    for bad_platform in (MacOsMockPlatform(), WinMockPlatform()):
      mock_browser = mock.Mock()
      mock_browser.attributes.return_value = BrowserAttributes.CHROMIUM_BASED
      mock_browser.platform = bad_platform
      with self.assertRaises(ProbeIncompatibleBrowser):
        probe.validate_browser(mock.Mock(), mock_browser)

  def test_session_salting_and_feature_encoding_roundtrip(self) -> None:
    triggers = (
      ChromeTriggerConfig(
        name="laggy_input",
        histogram_name="EventLatency.TotalLatency",
        min_value=500000,
        stop_delay=dt.timedelta(milliseconds=1500),
        max_triggers=4,
      ),
      ChromeTriggerConfig(
        name="dropped_frames",
        histogram_name="Graphics.Smoothness.PercentDroppedFrames4.AllSequences",
        min_value=50,
        max_value=100,
      ),
    )
    base_cfg = trace_config_pb2.TraceConfig(duration_ms=10000)
    base_cfg.buffers.add(
      size_kb=2048,
      fill_policy=trace_config_pb2.TraceConfig.BufferConfig.DISCARD,
    )
    probe = PerfettoProbe(trace_config=base_cfg, chrome_triggers=triggers)
    self.assertIn("tracing.background", probe.enabled_categories)

    session_a = mock.Mock(extra_flags=ChromeFlags())
    session_b = mock.Mock(extra_flags=ChromeFlags())
    session_ctx_a1 = probe.create_session_context(session_a)
    session_ctx_a2 = probe.get_or_create_session_context(session_a)
    session_ctx_b = probe.create_session_context(session_b)
    assert session_ctx_a1 is not None
    assert session_ctx_b is not None
    session_ctx_a1.setup()
    session_ctx_a2.setup()
    session_ctx_b.setup()

    self.assertIs(session_ctx_a1, session_ctx_a2)
    self.assertEqual(session_ctx_a1.salt, session_ctx_a2.salt)
    self.assertNotEqual(session_ctx_a1.salt, session_ctx_b.salt)

    feature_str = session_a.extra_flags["--enable-features"]
    assert feature_str is not None
    self.assertTrue(feature_str.startswith("TracingTriggers:config/"))
    escaped_b64 = feature_str.split(":config/", 1)[1]
    for unsafe_char in ("/", "+", "=", ",", ":", "<"):
      self.assertNotIn(unsafe_char, escaped_b64)

    raw_proto = base64.b64decode(urllib.parse.unquote(escaped_b64))
    parsed_rules = scenario_config_pb2.TracingTriggerRulesConfig()
    parsed_rules.ParseFromString(raw_proto)
    self.assertEqual(len(parsed_rules.rules), 2)
    self.assertEqual(
      parsed_rules.rules[0].name, f"laggy_input__cb_{session_ctx_a1.salt}"
    )
    self.assertEqual(
      parsed_rules.rules[0].histogram.histogram_name,
      "EventLatency.TotalLatency",
    )
    self.assertEqual(parsed_rules.rules[0].histogram.min_value, 500000)
    self.assertFalse(parsed_rules.rules[0].histogram.HasField("max_value"))
    self.assertEqual(parsed_rules.rules[1].histogram.min_value, 50)
    self.assertEqual(parsed_rules.rules[1].histogram.max_value, 100)

    merged_cfg = apply_triggers_to_trace_config(
      probe.trace_config, probe.chrome_triggers, session_ctx_a1.salt
    )
    self.assertFalse(merged_cfg.HasField("duration_ms"))
    self.assertEqual(
      merged_cfg.unique_session_name, f"cb_perfetto_{session_ctx_a1.salt}"
    )
    self.assertEqual(
      merged_cfg.buffers[0].fill_policy,
      trace_config_pb2.TraceConfig.BufferConfig.RING_BUFFER,
    )
    self.assertEqual(
      merged_cfg.trigger_config.trigger_mode,
      trace_config_pb2.TraceConfig.TriggerConfig.CLONE_SNAPSHOT,
    )
    self.assertEqual(
      merged_cfg.trigger_config.trigger_timeout_ms, 24 * 60 * 60 * 1000
    )
    self.assertEqual(merged_cfg.trigger_config.triggers[0].stop_delay_ms, 1500)
    self.assertEqual(merged_cfg.trigger_config.triggers[0].max_per_24_h, 4)
    te_cats = merged_cfg.data_sources[
      0
    ].config.track_event_config.enabled_categories
    self.assertIn("tracing.background", te_cats)

  def test_query_snapshot_trigger_info_real_trace_processor(self) -> None:
    # Build the binary Perfetto Trace in a subprocess so perfetto_trace_pb2 does
    # not collide with third_party.protoc in the global protobuf DescriptorPool.
    trace_bytes = subprocess.check_output(
      [
        sys.executable,
        "-c",
        (
          "import hashlib, struct, sys\n"
          "from perfetto.protos.perfetto.trace import perfetto_trace_pb2\n"
          "u64 = struct.unpack('>Q', hashlib.md5(\n"
          "  b'EventLatency.TotalLatency').digest()[:8])[0]\n"
          "trace = perfetto_trace_pb2.Trace()\n"
          "pkt_clk = trace.packet.add()\n"
          "c1 = pkt_clk.clock_snapshot.clocks.add()\n"
          "c1.clock_id = 6; c1.timestamp = 10_000_000_000\n"
          "c2 = pkt_clk.clock_snapshot.clocks.add()\n"
          "c2.clock_id = 1; c2.timestamp = 1_760_000_000_000_000_000\n"
          "pkt_hist = trace.packet.add()\n"
          "pkt_hist.timestamp = 11_999_000_000\n"
          "pkt_hist.trusted_packet_sequence_id = 1\n"
          "pkt_hist.track_event.type = "
          "perfetto_trace_pb2.TrackEvent.TYPE_INSTANT\n"
          "pkt_hist.track_event.track_uuid = 42\n"
          "pkt_hist.track_event.name = 'HistogramSampleTrigger'\n"
          "pkt_hist.track_event.chrome_histogram_sample.name_hash = u64\n"
          "pkt_hist.track_event.chrome_histogram_sample.sample = 642000\n"
          "pkt_trig = trace.packet.add()\n"
          "pkt_trig.timestamp = 12_000_000_000\n"
          "pkt_trig.clone_snapshot_trigger.trigger_name = "
          "'laggy_input__cb_deadbeef'\n"
          "pkt_trig.clone_snapshot_trigger.producer_name = 'org.chromium'\n"
          "pkt_trig.clone_snapshot_trigger.trusted_producer_uid = 1000\n"
          "pkt_after = trace.packet.add()\n"
          "pkt_after.timestamp = 12_500_000_000\n"
          "pkt_after.trusted_packet_sequence_id = 1\n"
          "pkt_after.track_event.type = "
          "perfetto_trace_pb2.TrackEvent.TYPE_INSTANT\n"
          "pkt_after.track_event.track_uuid = 42\n"
          "pkt_after.track_event.name = 'HistogramSampleTrigger'\n"
          "pkt_after.track_event.chrome_histogram_sample.name_hash = u64\n"
          "pkt_after.track_event.chrome_histogram_sample.sample = 999999\n"
          "sys.stdout.buffer.write(trace.SerializeToString())\n"
        ),
      ]
    )
    rule = ChromeTriggerConfig(
      name="laggy_input",
      histogram_name="EventLatency.TotalLatency",
      min_value=500000,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
      snap_path = pth.LocalPath(tmp_dir) / "perfetto.trace.pb.0"
      snap_path.write_bytes(trace_bytes)
      try:
        infos = inspect_triggered_snapshots([snap_path], (rule,), "deadbeef")
      except (OSError, urllib.error.URLError) as e:
        self.skipTest(f"trace_processor binary unavailable offline: {e}")

    self.assertEqual(len(infos), 1)
    info = infos[0]
    self.assertEqual(info.rule, rule)
    self.assertEqual(
      info.timestamp,
      dt.datetime(2025, 10, 9, 8, 53, 22, tzinfo=dt.UTC),
    )
    self.assertEqual(info.histogram_value, 642000)


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
