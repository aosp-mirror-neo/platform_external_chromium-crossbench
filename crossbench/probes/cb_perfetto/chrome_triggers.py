# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import base64
import dataclasses
import datetime as dt
import functools
import hashlib
import os
import secrets
import urllib.parse
from typing import (
  TYPE_CHECKING,
  ClassVar,
  Final,
  Mapping,
  NamedTuple,
  Self,
  Sequence,
  override,
)

from perfetto.batch_trace_processor.api import BatchTraceProcessor

from crossbench import path as pth
from crossbench.config import ConfigError, ConfigObject, ConfigParser
from crossbench.flags.chrome import ChromeFlags
from crossbench.helper.path_finder import TraceProcessorFinder
from crossbench.parse import DurationParser, NumberParser, ObjectParser
from crossbench.probes.probe_context import ProbeSessionContext
from crossbench.probes.trace_processor.helper import CBBatchTraceProcessorConfig
from crossbench.runner.anomalies import Anomaly, AnomalySource
from third_party.protoc import scenario_config_pb2, trace_config_pb2

if TYPE_CHECKING:
  from perfetto.trace_processor.api import TraceProcessor

  from crossbench.probes.cb_perfetto.perfetto import PerfettoProbe
  from crossbench.probes.results import ProbeResult
  from crossbench.runner.groups.session import BrowserSessionRunGroup
  from crossbench.runner.run import Run

# Perfetto's system tracing daemon (traced) enforces Trigger.max_per_24_h in a
# rolling 24-hour table keyed globally by trigger name across all tracing
# sessions on the device. Appending a random suffix ("<name>__cb_<salt>") makes
# each session's trigger names unique to traced so max_triggers behaves as a
# per-session cap instead of exhausting the device's quota for the next 24h.
SALT_SEPARATOR: Final[str] = "__cb_"
DEFAULT_TRIGGER_STOP_DELAY: Final[dt.timedelta] = dt.timedelta(seconds=2)
DEFAULT_MAX_TRIGGERS: Final[int] = 5
DEFAULT_TRIGGER_TIMEOUT: Final[dt.timedelta] = dt.timedelta(hours=24)
TRACING_BACKGROUND_CATEGORY: Final[str] = "tracing.background"


@dataclasses.dataclass(frozen=True)
class ChromeTriggerConfig(ConfigObject):
  """Configures a Chrome UMA histogram Perfetto CLONE_SNAPSHOT trigger.

  Watches a Chrome UMA histogram via Chrome's TracingTriggers feature and
  clones a Perfetto ring-buffer snapshot whenever a recorded sample matches
  the configured threshold, recording an Anomaly in cb.anomalies.json on
  teardown.

  Fields:
    histogram_name: Exact Chrome UMA histogram name to watch (default arg for
      compact string shorthand).
    name: Unique identifier for this trigger rule (defaults to histogram_name).
    min_value: Optional inclusive lower sample bound that fires this trigger.
    max_value: Optional inclusive upper sample bound that fires this trigger.
      When neither min_value nor max_value is set, any sample recorded to
      histogram_name fires the trigger.
    stop_delay: Delay after trigger activation before cloning the ring buffer
      (default: 2s).
    max_triggers: Maximum number of snapshots this rule may capture per test
      session (default: 5).

  Example shorthand configuration:
    "Browser.Tabs.RendererCrash"

  Example HJSON configuration:
    {
      name: "laggy_input"
      histogram_name: "EventLatency.TotalLatency"
      min_value: 500000
      stop_delay: "2s"
      max_triggers: 5
    }
  """

  name: str
  histogram_name: str
  min_value: int | None = None
  max_value: int | None = None
  stop_delay: dt.timedelta = DEFAULT_TRIGGER_STOP_DELAY
  max_triggers: int = DEFAULT_MAX_TRIGGERS

  @classmethod
  @override
  def create(
    cls,
    histogram_name: str,
    name: str | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
    stop_delay: dt.timedelta = DEFAULT_TRIGGER_STOP_DELAY,
    max_triggers: int = DEFAULT_MAX_TRIGGERS,
  ) -> Self:
    return cls(
      name=name or histogram_name,
      histogram_name=histogram_name,
      min_value=min_value,
      max_value=max_value,
      stop_delay=stop_delay,
      max_triggers=max_triggers,
    )

  @override
  def validate(self) -> None:
    if SALT_SEPARATOR in self.name:
      raise ConfigError(
        f"Trigger name {self.name!r} cannot contain {SALT_SEPARATOR!r}."
      )
    if (
      self.min_value is not None
      and self.max_value is not None
      and self.min_value > self.max_value
    ):
      raise ConfigError(
        f"min_value ({self.min_value}) cannot exceed "
        f"max_value ({self.max_value})."
      )
    try:
      self.to_proto_rule("")
    except ValueError as e:
      raise ConfigError(str(e)) from e

  @classmethod
  @override
  @functools.cache
  def config_parser(cls) -> ConfigParser[Self]:
    parser = ConfigParser(cls)
    parser.add_default_argument(
      "histogram_name",
      type=ObjectParser.non_empty_str,
      required=True,
      help="Exact Chrome UMA histogram name to watch.",
    )
    parser.add_argument(
      "name",
      type=ObjectParser.non_empty_str,
      default=None,
      help=(
        "Unique identifier for this anomaly trigger rule "
        "(defaults to histogram_name)."
      ),
    )
    parser.add_argument(
      "min_value",
      type=NumberParser.optional_int,
      default=None,
      help="Minimum inclusive histogram sample value that fires this trigger.",
    )
    parser.add_argument(
      "max_value",
      type=NumberParser.optional_int,
      default=None,
      help="Maximum inclusive histogram sample value that fires this trigger.",
    )
    parser.add_argument(
      "stop_delay",
      type=DurationParser.positive_or_zero_duration,
      default=DEFAULT_TRIGGER_STOP_DELAY,
      help="Delay between trigger activation and cloning the ring buffer.",
    )
    parser.add_argument(
      "max_triggers",
      type=NumberParser.positive_int,
      default=DEFAULT_MAX_TRIGGERS,
      help="Maximum number of snapshots this trigger may capture per test.",
    )
    return parser

  @classmethod
  @override
  def parse_str(cls, value: str) -> Self:
    return cls.config_parser().parse_str(value)

  @property
  def histogram_name_hash(self) -> int:
    # Chrome's base::HashMetricName() uses the first 8 big-endian bytes of MD5;
    # SQLite / TraceProcessor stores uint64 protobuf fields as signed int64.
    digest = hashlib.md5(
      self.histogram_name.encode("utf-8"), usedforsecurity=False
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)

  def salted_name(self, salt: str) -> str:
    return f"{self.name}{SALT_SEPARATOR}{salt}"

  def to_proto_rule(self, salt: str) -> scenario_config_pb2.TriggerRule:
    rule = scenario_config_pb2.TriggerRule()
    rule.name = self.salted_name(salt)
    rule.histogram.histogram_name = self.histogram_name
    if self.min_value is not None:
      rule.histogram.min_value = self.min_value
    if self.max_value is not None:
      rule.histogram.max_value = self.max_value
    return rule

  def bounds_summary(self) -> str:
    if self.min_value is not None and self.max_value is not None:
      return f"in [{self.min_value}, {self.max_value}]"
    if self.min_value is not None:
      return f">= {self.min_value}"
    if self.max_value is not None:
      return f"<= {self.max_value}"
    return "any sample"

  def description(self, histogram_value: int | None = None) -> str:
    bounds = self.bounds_summary()
    if histogram_value is not None:
      return (
        f"Perfetto UMA trigger '{self.name}': "
        f"{self.histogram_name} = {histogram_value} ({bounds})"
      )
    return (
      f"Perfetto UMA trigger '{self.name}': {self.histogram_name} ({bounds})"
    )


class SnapshotTriggerInfo(NamedTuple):
  rule: ChromeTriggerConfig
  timestamp: dt.datetime
  histogram_value: int | None

  def to_anomaly(self, snapshot_label: str) -> Anomaly:
    return Anomaly(
      timestamp=self.timestamp,
      source=AnomalySource.PERFETTO_TRIGGER,
      description=self.rule.description(self.histogram_value),
      perfetto_snapshot_label=snapshot_label,
      details={
        "trigger_name": self.rule.name,
        "histogram_name": self.rule.histogram_name,
        "histogram_value": self.histogram_value,
        "min_value": self.rule.min_value,
        "max_value": self.rule.max_value,
      },
    )


class PerfettoTriggerSessionContext(ProbeSessionContext["PerfettoProbe"]):
  """Configures per-BrowserSession trigger salting and Chrome startup flags."""

  PRODUCES_DATA: ClassVar[bool] = False

  def __init__(
    self, probe: PerfettoProbe, session: BrowserSessionRunGroup
  ) -> None:
    super().__init__(probe, session)
    self._salt: Final[str] = secrets.token_hex(4)
    self._configured: bool = False

  @property
  def salt(self) -> str:
    return self._salt

  @override
  def get_default_result_path(self) -> pth.AnyPath:
    return pth.AnyPath("perfetto_triggers")

  @override
  def setup(self) -> None:
    if self._configured:
      return
    self._configured = True
    feature_value = encode_triggers_feature(
      self.probe.chrome_triggers, self._salt
    )
    extra_flags = self.session.extra_flags
    if isinstance(extra_flags, ChromeFlags):
      extra_flags.features.enable(feature_value)
    else:
      extra_flags.set("--enable-features", feature_value)

  @override
  def start(self) -> None:
    pass

  @override
  def stop(self) -> None:
    pass

  @override
  def teardown(self) -> ProbeResult:
    return self.empty_result()


def encode_triggers_feature(
  triggers: Sequence[ChromeTriggerConfig], salt: str
) -> str:
  rules_config = scenario_config_pb2.TracingTriggerRulesConfig()
  for trigger in triggers:
    rules_config.rules.append(trigger.to_proto_rule(salt))
  b64_config = base64.b64encode(rules_config.SerializeToString()).decode(
    "ascii"
  )
  escaped_b64 = urllib.parse.quote(b64_config, safe="")
  return f"TracingTriggers:config/{escaped_b64}"


def apply_triggers_to_trace_config(
  base_config: trace_config_pb2.TraceConfig,
  triggers: Sequence[ChromeTriggerConfig],
  salt: str,
) -> trace_config_pb2.TraceConfig:
  trace_config = trace_config_pb2.TraceConfig()
  trace_config.CopyFrom(base_config)
  trace_config.ClearField("duration_ms")
  if not trace_config.unique_session_name:
    trace_config.unique_session_name = f"cb_perfetto_{salt}"
  for buf in trace_config.buffers:
    if buf.fill_policy == trace_config_pb2.TraceConfig.BufferConfig.DISCARD:
      buf.fill_policy = trace_config_pb2.TraceConfig.BufferConfig.RING_BUFFER

  trigger_cfg = trace_config.trigger_config
  trigger_cfg.trigger_mode = (
    trace_config_pb2.TraceConfig.TriggerConfig.CLONE_SNAPSHOT
  )
  trigger_cfg.trigger_timeout_ms = round(
    DEFAULT_TRIGGER_TIMEOUT.total_seconds() * 1000
  )
  trigger_cfg.ClearField("triggers")
  for rule in triggers:
    entry = trigger_cfg.triggers.add()
    entry.name = rule.salted_name(salt)
    entry.stop_delay_ms = round(rule.stop_delay.total_seconds() * 1000)
    entry.max_per_24_h = rule.max_triggers
  return trace_config


def record_triggered_anomalies(
  run: Run,
  snapshot_paths: Sequence[pth.LocalPath],
  triggers: Sequence[ChromeTriggerConfig],
  salt: str,
) -> list[str]:
  """Inspects triggered snapshots, records Run anomalies, and returns labels."""
  inspected = inspect_triggered_snapshots(snapshot_paths, triggers, salt)
  label_counts: dict[str, int] = {}
  labels: list[str] = []
  for info in inspected:
    base_label = f"trigger_{info.rule.name}"
    count = label_counts.get(base_label, 0) + 1
    label_counts[base_label] = count
    raw_label = base_label if count == 1 else f"{base_label}_{count}"
    label = pth.safe_filename(raw_label)
    run.anomalies.append(info.to_anomaly(label))
    labels.append(label)
  return labels


def inspect_triggered_snapshots(
  snapshot_paths: Sequence[pth.LocalPath],
  triggers: Sequence[ChromeTriggerConfig],
  salt: str,
) -> list[SnapshotTriggerInfo]:
  """Queries CLONE_SNAPSHOT traces in batch for firing rules and UMA samples."""
  if not snapshot_paths:
    return []
  triggers_by_name: dict[str, ChromeTriggerConfig] = {
    trigger.name: trigger for trigger in triggers
  }
  btp_cfg = CBBatchTraceProcessorConfig(
    bin_path=TraceProcessorFinder.local_binary(None)
  )
  with BatchTraceProcessor(
    [os.fspath(path) for path in snapshot_paths], config=btp_cfg
  ) as btp:
    return btp.execute(
      lambda tp: _extract_trigger_info(tp, triggers_by_name, salt)
    )


def _extract_trigger_info(
  tp: TraceProcessor,
  triggers_by_name: Mapping[str, ChromeTriggerConfig],
  salt: str,
) -> SnapshotTriggerInfo:
  row = next(
    iter(
      tp.query(
        "SELECT "
        "(SELECT str_value FROM metadata "
        "WHERE name = 'trace_trigger') AS trigger_name, "
        "to_realtime((SELECT value FROM stats "
        "WHERE name = 'traced_clone_trigger_timestamp_ns')) "
        "AS realtime_ns"
      )
    )
  )
  if not isinstance(row.trigger_name, str) or not isinstance(
    row.realtime_ns, int
  ):
    raise ValueError("Snapshot is missing CLONE_SNAPSHOT trigger metadata")
  clean_name = row.trigger_name.removesuffix(f"{SALT_SEPARATOR}{salt}")
  rule = triggers_by_name[clean_name]
  sample_rows = tp.query(
    "SELECT "
    "extract_arg(arg_set_id, 'chrome_histogram_sample.name') AS name, "
    "extract_arg(arg_set_id, 'chrome_histogram_sample.name_hash') "
    "AS name_hash, "
    "extract_arg(arg_set_id, 'chrome_histogram_sample.sample') AS sample "
    "FROM slice "
    "WHERE name = 'HistogramSampleTrigger' "
    "AND ts <= ("
    "SELECT value FROM stats "
    "WHERE name = 'traced_clone_trigger_timestamp_ns'"
    ") "
    "ORDER BY ts DESC"
  )
  target_hash = rule.histogram_name_hash
  histogram_value = next(
    (
      s.sample
      for s in sample_rows
      if isinstance(s.sample, int)
      and (s.name == rule.histogram_name or s.name_hash == target_hash)
    ),
    None,
  )
  timestamp = dt.datetime.fromtimestamp(
    row.realtime_ns / 1_000_000_000, tz=dt.UTC
  )
  return SnapshotTriggerInfo(rule, timestamp, histogram_value)
