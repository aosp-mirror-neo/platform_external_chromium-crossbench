# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import dataclasses
import logging
import weakref
from typing import TYPE_CHECKING, ClassVar, Final, Iterable, Self, override

from google.protobuf import json_format

from crossbench import exception
from crossbench import path as pth
from crossbench.browsers.attributes import BrowserAttributes
from crossbench.config import ConfigObject, config_dir
from crossbench.helper import fs_helper
from crossbench.helper.collection_helper import close_matches_message
from crossbench.parse import PROTOBUF_ALL_SUFFIX, ObjectParser, PathParser
from crossbench.probes.cb_perfetto.chrome_triggers import (
  TRACING_BACKGROUND_CATEGORY,
  ChromeTriggerConfig,
  PerfettoTriggerSessionContext,
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
from crossbench.probes.cb_perfetto.start_tracing_sequence import (
  StartTracingSequence,
)
from crossbench.probes.probe import Probe, ProbeConfigParser, ProbeKeyT
from crossbench.probes.probe_error import ProbeIncompatibleBrowser
from crossbench.probes.result_location import ResultLocation
from crossbench.probes.trace_processor import profile_helper
from third_party.protoc import trace_config_pb2

if TYPE_CHECKING:
  from crossbench.browsers.browser import Browser
  from crossbench.env.runner_env import RunnerEnv
  from crossbench.probes.cb_perfetto.context.base import PerfettoProbeContext
  from crossbench.runner.groups.browsers import BrowsersRunGroup
  from crossbench.runner.groups.session import BrowserSessionRunGroup
  from crossbench.runner.run import Run
  from crossbench.runner.runner import Runner


@dataclasses.dataclass(frozen=True)
class TraceConfig(ConfigObject):
  """See https://perfetto.dev/docs/reference/trace-config-proto for more
  details."""

  VALID_EXTENSIONS: ClassVar[tuple[str, ...]] = PROTOBUF_ALL_SUFFIX
  trace_config: trace_config_pb2.TraceConfig = dataclasses.field(
    default_factory=trace_config_pb2.TraceConfig
  )

  @classmethod
  @override
  def parse_str(cls, value: str) -> Self:
    if ":" in value:
      return cls.parse_textproto(value)
    presets = cls.presets()
    if preset_file := presets.get(value):
      return cls.parse_path(preset_file)
    error_message, alternative = close_matches_message(
      value, presets.keys(), "TraceConfig preset"
    )
    if not alternative:
      raise ValueError(error_message)
    logging.error(error_message)
    preset_file = presets[alternative]
    return cls.parse_path(preset_file)

  @classmethod
  def parse_textproto(cls, value: str) -> Self:
    trace_config = trace_config_pb2.TraceConfig()
    ObjectParser.parse_text_or_binary_proto(trace_config, value.encode("utf-8"))
    return cls(trace_config)

  @classmethod
  @override
  def parse_dict(cls, config: dict[str, object], **kwargs) -> Self:
    cls.expect_no_extra_kwargs(kwargs)
    return cls.parse_json(config)

  @classmethod
  def parse_json(cls, config: dict[str, object]) -> Self:
    with exception.annotate_argparsing(
      f"Parsing {cls.__name__} dict as json proto"
    ):
      trace_config = trace_config_pb2.TraceConfig()
      json_format.ParseDict(config, trace_config)
      return cls(trace_config=trace_config)
    raise exception.UnreachableError

  @classmethod
  @override
  def parse_path(cls, path: pth.LocalPath, **kwargs) -> Self:
    trace_config = trace_config_pb2.TraceConfig()
    ObjectParser.parse_text_or_binary_proto_file(trace_config, path)
    return cls(trace_config, **kwargs)

  @classmethod
  def preset_dir(cls) -> pth.LocalPath:
    return config_dir() / "probe/perfetto/trace_config"

  @classmethod
  def presets(cls) -> dict[str, pth.LocalPath]:
    result: dict[str, pth.LocalPath] = {}
    for preset_config in cls.preset_dir().glob("*.txtpb"):
      result[preset_config.stem] = preset_config
    assert result, f"No trace_config presets found in {cls.preset_dir()}"
    return result

  @override
  def to_argument_value(self) -> trace_config_pb2.TraceConfig:
    return self.trace_config

  @classmethod
  @override
  def help_text_items(cls) -> list[tuple[str, str]]:
    help_items = super().help_text_items()
    preset_help: list[str] = []
    for name, path in cls.presets().items():
      help_str = cls._trace_config_help(name, path)
      preset_help.append(help_str)
    help_items.append(("presets", "\n".join(preset_help)))
    return help_items

  @classmethod
  def _trace_config_help(cls, name: str, path: pth.LocalPath) -> str:
    comments: list[str] = []
    with path.open(encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        if not line.startswith("#"):
          break
        comments.append(line[1:].strip())
    if comments:
      description = " ".join(comments)
      return f"{name}\n  {description}\n"
    return f"{name}\n"


def has_v8_code_data_source(trace_config: trace_config_pb2.TraceConfig) -> bool:
  return has_data_source(trace_config, "dev.v8.code")


def has_data_source(
  trace_config: trace_config_pb2.TraceConfig, name: str
) -> bool:
  return any(
    data_source.config.name == name for data_source in trace_config.data_sources
  )


class PerfettoProbe(Probe):
  """
  A probe to collect Perfetto system traces that can be viewed on
  https://ui.perfetto.dev/.

  Note: The `perfetto` probe replaces the outdated `tracing` probe.

  Recommended way to use presets (Easy):
  You can quickly use a predefined preset from the command line:
    --probe='perfetto:default'
    --probe='perfetto:v8'

  Advanced way to use custom configs:
  1. Go to https://ui.perfetto.dev/, click "Record new trace" and set up your
     preferred tracing options.
  2. Click "Recording command" and copy the textproto config part of the
     command.
  3. Paste it into the textproto field of the probe config. An example probe
     config can be found at config/doc/probe/perfetto.config.hjson.
  4. Specify the config via the --probe-config command-line flag.

  After the run, the trace will be found among the results as
  "perfetto.trace.pb.gz".
  """

  NAME: ClassVar = "perfetto"
  RESULT_LOCATION: ClassVar = ResultLocation.BROWSER

  @classmethod
  @override
  def help_text_items(cls) -> list[tuple[str, str]]:
    items = super().help_text_items()
    items.extend(TraceConfig.help_text_items())
    return items

  @classmethod
  @override
  def config_parser(cls) -> ProbeConfigParser[Self]:
    parser = super().config_parser()
    parser.add_default_argument(
      "trace_config",
      aliases=("config", "textproto", "preset"),
      type=TraceConfig,
      help=(
        "Serialized perfetto configuration. "
        "See probe instructions for more details"
      ),
    )
    parser.add_argument(
      "perfetto_bin",
      aliases=("perfetto",),
      type=PathParser.any_path,
      default=pth.AnyPath("perfetto"),
      help="Perfetto binary on the browser device (android, chrome-os)",
    )
    parser.add_argument(
      "tracebox_bin",
      aliases=("tracebox",),
      type=PathParser.any_path,
      default=pth.AnyPath("tracebox"),
      help="Tracebox binary on the browser device (linux, macos). "
      "Auto downloaded on local devices.",
    )
    parser.add_argument(
      "enabled_tags",
      aliases=("tags",),
      is_list=True,
      type=str,
      default=(),
      help="Enabled tags, will be combined with the 'trace_config'.",
    )
    parser.add_argument(
      "disabled_tags",
      is_list=True,
      type=str,
      default=(),
      help="Disabled tags, will be combined with the 'trace_config'.",
    )
    parser.add_argument(
      "enabled_categories",
      aliases=("categories",),
      is_list=True,
      type=str,
      default=(),
      help="Enabled categories, will be combined with the 'trace_config'.",
    )
    parser.add_argument(
      "disabled_categories",
      is_list=True,
      type=str,
      default=(),
      help="Disabled categories, will be combined with the 'trace_config'.",
    )
    parser.add_argument(
      "trace_browser_startup",
      type=ObjectParser.bool,
      default=False,
      help="Start perfetto tracing before launching the browser.",
    )
    parser.add_argument(
      "start_tracing_sequence",
      type=StartTracingSequence,
      default=StartTracingSequence.PROBE_START,
    )
    parser.add_argument(
      "config_via_stdin",
      type=ObjectParser.bool,
      default=False,
      help="Pass perfetto tracing config via stdin.",
    )
    parser.add_argument(
      "chrome_triggers",
      aliases=("triggers",),
      type=ChromeTriggerConfig,
      is_list=True,
      default=(),
      help=(
        "Chrome UMA histogram anomaly triggers that capture CLONE_SNAPSHOT "
        "traces and record entries in cb.anomalies.json on teardown."
      ),
    )
    return parser

  @classmethod
  def parse_str(cls: type[Self], value: str) -> Self:
    if ":" in value:
      return super().parse_str(value)
    if "," in value or value.startswith(("-", "+")):
      return cls.parse_tags(value)
    if not value:
      return cls.parse_str("default")
    return super().parse_str(value)

  @classmethod
  def parse_tags(cls, value: str) -> Self:
    enabled_tags: list[str] = []
    disabled_tags: list[str] = []
    for tag in ObjectParser.str_list(value):
      if tag.startswith("-"):
        disabled_tags.append(tag[1:])
      elif tag.startswith("+"):
        enabled_tags.append(tag[1:])
      else:
        enabled_tags.append(tag)
    return cls(
      trace_config=trace_config_pb2.TraceConfig(),
      enabled_tags=enabled_tags,
      disabled_tags=disabled_tags,
    )

  def __init__(
    self,
    trace_config: trace_config_pb2.TraceConfig | None = None,
    perfetto_bin: pth.AnyPath = pth.AnyPath("perfetto"),
    tracebox_bin: pth.AnyPath = pth.AnyPath("tracebox"),
    enabled_tags: Iterable[str] = (),
    disabled_tags: Iterable[str] = (),
    enabled_categories: Iterable[str] = (),
    disabled_categories: Iterable[str] = (),
    trace_browser_startup: bool = False,
    start_tracing_sequence: StartTracingSequence = StartTracingSequence.PROBE_START,
    config_via_stdin: bool = False,
    chrome_triggers: Iterable[ChromeTriggerConfig] = (),
  ) -> None:
    super().__init__()
    if not trace_config:
      trace_config = TraceConfig.parse_str("default").trace_config
    self._trace_config_obj: Final[trace_config_pb2.TraceConfig] = trace_config
    self._perfetto_bin: Final[pth.AnyPath] = perfetto_bin
    self._tracebox_bin: Final[pth.AnyPath] = tracebox_bin
    self._enabled_tags: Final[tuple[str, ...]] = tuple(enabled_tags)
    self._disabled_tags: Final[tuple[str, ...]] = tuple(disabled_tags)
    self._chrome_triggers: Final[tuple[ChromeTriggerConfig, ...]] = tuple(
      chrome_triggers
    )
    self._session_contexts: Final[
      weakref.WeakKeyDictionary[object, PerfettoTriggerSessionContext]
    ] = weakref.WeakKeyDictionary()
    categories = list(enabled_categories)
    if self._chrome_triggers and TRACING_BACKGROUND_CATEGORY not in categories:
      categories.append(TRACING_BACKGROUND_CATEGORY)
    self._enabled_categories: Final[tuple[str, ...]] = tuple(categories)
    self._disabled_categories: Final[tuple[str, ...]] = tuple(
      disabled_categories
    )
    self._trace_browser_startup: Final[bool] = trace_browser_startup
    self._start_tracing_sequence: Final[StartTracingSequence] = (
      start_tracing_sequence
    )
    self._config_via_stdin: Final[bool] = config_via_stdin
    self._needs_v8_code_logger: Final[bool] = has_v8_code_data_source(
      self.trace_config
    )
    self._validate_trace_config()

  def _validate_trace_config(self) -> None:
    if self.trace_config.ByteSize() == 0:
      raise argparse.ArgumentTypeError(
        "Perfetto trace config cannot be empty."
        "Either specify a trace_configs, tags or categories."
      )
    if not self._chrome_triggers:
      return
    ObjectParser.unique_sequence(
      [trigger.name for trigger in self._chrome_triggers],
      "chrome_triggers names",
    )
    if self._trace_config_obj.write_into_file:
      raise argparse.ArgumentTypeError(
        "Perfetto chrome_triggers uses CLONE_SNAPSHOT ring-buffer mode and "
        "cannot be combined with 'write_into_file: true'."
      )
    if self._trace_config_obj.HasField("trigger_config"):
      raise argparse.ArgumentTypeError(
        "Perfetto chrome_triggers cannot be combined with a manual "
        "'trigger_config' in trace_config."
      )

  @property
  @override
  def key(self) -> ProbeKeyT:
    return (
      *super().key,
      ("textproto", str(self._trace_config_obj)),
      ("perfetto_bin", str(self.perfetto_bin)),
      ("tracebox_bin", str(self.tracebox_bin)),
      ("trace_browser_startup", str(self.trace_browser_startup)),
      ("start_tracing_sequence", str(self.start_tracing_sequence)),
      ("config_via_stdin", str(self.config_via_stdin)),
      ("enabled_tags", self.enabled_tags),
      ("disabled_tags", self.disabled_tags),
      ("enabled_categories", self.enabled_categories),
      ("disabled_categories", self.disabled_categories),
      ("chrome_triggers", self.chrome_triggers),
    )

  @property
  def trace_config(self) -> trace_config_pb2.TraceConfig:
    base_config = self._trace_config_obj
    if (
      not self.enabled_tags
      and not self.disabled_tags
      and not self.enabled_categories
      and not self.disabled_categories
    ):
      return base_config
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.CopyFrom(base_config)
    track_event_config: trace_config_pb2.TraceConfig.DataSource | None = None
    for data_source in trace_config.data_sources:
      if data_source.config.name == "track_event":
        track_event_config = data_source
        break
    if not track_event_config:
      track_event_config = trace_config.data_sources.add()
      track_event_config.config.name = "track_event"
    te_config = track_event_config.config.track_event_config
    te_config.enabled_tags.extend(self.enabled_tags)
    te_config.disabled_tags.extend(self.disabled_tags)
    te_config.enabled_categories.extend(self.enabled_categories)
    te_config.disabled_categories.extend(self.disabled_categories)
    return trace_config

  @property
  def perfetto_bin(self) -> pth.AnyPath:
    return self._perfetto_bin

  @property
  def tracebox_bin(self) -> pth.AnyPath:
    return self._tracebox_bin

  @property
  def enabled_tags(self) -> tuple[str, ...]:
    return self._enabled_tags

  @property
  def disabled_tags(self) -> tuple[str, ...]:
    return self._disabled_tags

  @property
  def enabled_categories(self) -> tuple[str, ...]:
    return self._enabled_categories

  @property
  def disabled_categories(self) -> tuple[str, ...]:
    return self._disabled_categories

  @property
  def trace_browser_startup(self) -> bool:
    return self._trace_browser_startup

  @property
  def start_tracing_sequence(self) -> StartTracingSequence:
    return self._start_tracing_sequence

  @property
  def config_via_stdin(self) -> bool:
    return self._config_via_stdin

  @property
  def chrome_triggers(self) -> tuple[ChromeTriggerConfig, ...]:
    return self._chrome_triggers

  @property
  @override
  def result_path_name(self) -> str:
    return "perfetto.trace.pb"

  @override
  def validate_browser(self, env: RunnerEnv, browser: Browser) -> None:
    super().validate_browser(env, browser)
    self.expect_browser(browser, BrowserAttributes.CHROMIUM_BASED)
    if self._chrome_triggers and (
      browser.platform.is_win or browser.platform.is_macos
    ):
      raise ProbeIncompatibleBrowser(
        self,
        browser,
        "chrome_triggers requires system perfetto/tracebox "
        "(Linux, Android, ChromeOS)",
      )
    if browser.platform.is_win:
      self._validate_windows_trace_config(browser)

  def _validate_windows_trace_config(self, browser: Browser) -> None:
    if self.trace_config.write_into_file:
      # Chrome only supports writing startup traces directly into a file on
      # POSIX (crbug.com/40736989), and the tracing service rejects sessions
      # with write_into_file but no file.
      raise ProbeIncompatibleBrowser(
        self,
        browser,
        "Perfetto trace configs with write_into_file are not supported on "
        "Windows, please use a trace config without write_into_file.",
      )
    if unknown_names := self._unknown_target_buffer_names():
      # On Windows buffer names are replaced with indices (see
      # WindowsPerfettoProbeContext), so every name must match a buffer.
      raise ProbeIncompatibleBrowser(
        self,
        browser,
        "Perfetto trace config data sources use unknown target_buffer_name "
        f"values {unknown_names}, please use names of the configured buffers.",
      )

  def _unknown_target_buffer_names(self) -> list[str]:
    buffer_names = {buf.name for buf in self.trace_config.buffers if buf.name}
    return sorted(
      {
        name
        for ds in self.trace_config.data_sources
        if (name := ds.config.target_buffer_name) and name not in buffer_names
      }
    )

  @override
  def attach(self, browser: Browser) -> None:
    browser.features.enable("EnablePerfettoSystemTracing")
    if self._needs_v8_code_logger:
      logging.debug("Auto-enabling --perfetto-code-logger on %s", browser)
      browser.js_flags.set("--perfetto-code-logger")
    super().attach(browser)

  @override
  def log_run_result(self, run: Run) -> None:
    self._log_results([run])

  @override
  def log_browsers_result(self, group: BrowsersRunGroup) -> None:
    self._log_results(group.runs)

  def _log_results(self, runs: Iterable[Run]) -> None:
    logging.info("-" * 80)
    logging.critical("Perfetto trace results:")
    for run in runs:
      for result_file in run.results[self].file_list:
        logging.critical(
          "  - %s : %s", result_file, fs_helper.get_file_size(result_file)
        )

  @override
  def create_session_context(
    self: Self, session: BrowserSessionRunGroup
  ) -> PerfettoTriggerSessionContext | None:
    if not self._chrome_triggers:
      return None
    return self.get_or_create_session_context(session)

  def get_or_create_session_context(
    self, session: BrowserSessionRunGroup
  ) -> PerfettoTriggerSessionContext:
    if ctx := self._session_contexts.get(session):
      return ctx
    ctx = PerfettoTriggerSessionContext(self, session)
    self._session_contexts[session] = ctx
    return ctx

  @override
  def create_context(self, run: Run) -> PerfettoProbeContext:
    if run.browser_platform.is_chromeos:
      return ChromeOsPerfettoProbeContext(self, run)
    if run.browser_platform.is_android:
      return AndroidPerfettoProbeContext(self, run)
    if run.browser_platform.is_macos:
      return MacPerfettoProbeContext(self, run)
    if run.browser_platform.is_win:
      return WindowsPerfettoProbeContext(self, run)
    return DesktopPerfettoProbeContext(self, run)

  @override
  def get_extra_probes(self, runner: Runner) -> Iterable[Probe]:
    return profile_helper.get_extra_trace_processor(runner)
