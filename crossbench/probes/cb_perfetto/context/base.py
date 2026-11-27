# Copyright 2025 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import abc
import atexit
import datetime as dt
import logging
import subprocess
from typing import TYPE_CHECKING, Final, override

import google.protobuf.text_format as proto_text_format

from crossbench import path as pth
from crossbench.parse import NumberParser, ObjectParser
from crossbench.probes.cb_perfetto.chrome_triggers import (
  apply_triggers_to_trace_config,
  record_triggered_anomalies,
)
from crossbench.probes.cb_perfetto.constants import (
  PERFETTO_CONFIG_NAME,
  PERFETTO_TRACE_NAME,
)
from crossbench.probes.cb_perfetto.start_tracing_sequence import (
  StartTracingSequence,
)
from crossbench.probes.probe_context import ProbeContext
from crossbench.probes.results import LocalProbeResult, ProbeResult

if TYPE_CHECKING:
  from crossbench import exception
  from crossbench.plt.types import TupleCmdArgs
  from crossbench.probes.cb_perfetto.perfetto import PerfettoProbe
  from crossbench.runner.run import Run
  from third_party.protoc import trace_config_pb2

PERFETTO_STOP_TIMEOUT: Final[dt.timedelta] = dt.timedelta(seconds=30)


class PerfettoProbeContext(
  ProbeContext["PerfettoProbe"], metaclass=abc.ABCMeta
):
  def __init__(self, probe: PerfettoProbe, run: Run) -> None:
    self._file_prefix: Final[str] = dt.datetime.now().strftime(
      "%Y-%m-%d_%H%M%S"
    )
    super().__init__(probe, run)
    self._host_config_file: Final[pth.LocalPath] = (
      run.out_dir / PERFETTO_CONFIG_NAME
    )
    self._perfetto_pid: int | None = None
    self._snapshots: Final[list[pth.LocalPath]] = []
    self._active_trace_config: trace_config_pb2.TraceConfig = probe.trace_config
    self._trigger_salt: str = ""
    self._final_clone_path: pth.AnyPath | None = None
    self._remote_trigger_snapshots: Final[list[pth.AnyPath]] = []

  def setup(self) -> None:
    assert self._perfetto_pid is None
    if self.probe.chrome_triggers:
      session_ctx = self.probe.get_or_create_session_context(self.session)
      session_ctx.setup()
      self._trigger_salt = session_ctx.salt
      self._active_trace_config = apply_triggers_to_trace_config(
        self.probe.trace_config,
        self.probe.chrome_triggers,
        self._trigger_salt,
      )
    for p in self.browser_platform.processes():
      if p["name"] == "perfetto":
        logging.warning("PERFETTO: killing existing session pid: %s", p["pid"])
        self.browser_platform.terminate(p["pid"])
    self._setup_validate_bin()
    self._setup_push_perfetto_config()
    if self.probe.trace_browser_startup:
      self._start_perfetto()

  def _setup_validate_bin(self) -> None:
    binary = self.perfetto_cmd[0]
    if not self.browser_platform.which(binary):
      raise ValueError(f"{binary!r} cannot be found on {self.browser_platform}")

  def _setup_push_perfetto_config(self) -> None:
    self.host_platform.write_text(
      self._host_config_file,
      proto_text_format.MessageToString(self._active_trace_config),
    )
    if not self.probe.config_via_stdin:
      self.browser_platform.push(
        self._host_config_file, self.get_browser_config_path()
      )

  @abc.abstractmethod
  def get_browser_config_path(self) -> pth.AnyPath:
    pass

  @abc.abstractmethod
  def get_default_result_path(self) -> pth.AnyPath:
    pass

  @property
  def perfetto_cmd(self) -> TupleCmdArgs:
    return (self.probe.perfetto_bin,)

  def start(self) -> None:
    if self.probe.trace_browser_startup:
      if not self._perfetto_pid:
        raise RuntimeError("Perfetto was not started")
      return
    if self.probe.start_tracing_sequence == StartTracingSequence.PROBE_START:
      self._try_start_perfetto()

  @override
  def start_story_setup(self) -> None:
    if self.probe.start_tracing_sequence == StartTracingSequence.STORY_SETUP:
      self._try_start_perfetto()

  @override
  def start_story_run(self) -> None:
    super().start_story_run()
    if self.probe.start_tracing_sequence == StartTracingSequence.STORY_RUN:
      self._try_start_perfetto()

  @override
  def invoke(
    self,
    info_stack: exception.TInfoStack,
    timeout: dt.timedelta | None = None,
    label: str | None = None,
    **kwargs,
  ) -> None:
    del timeout
    self.expect_no_extra_kwargs(kwargs)
    snapshot_label: str
    if label:
      snapshot_label = ObjectParser.non_empty_str(label, "label")
    else:
      snapshot_label = "_".join(info_stack) or "snapshot"
    self.clone_snapshot(snapshot_label)

  def clone_snapshot(self, label: str = "snapshot") -> None:
    safe_label: str = pth.safe_filename(label)
    local_name: str = (
      f"perfetto_snapshot_{len(self._snapshots):03d}_{safe_label}.trace.pb"
    )

    if not self._perfetto_pid:
      logging.warning(
        "PERFETTO: cannot clone snapshot %s, session not active", local_name
      )
      return

    try:
      with self.browser_platform.TemporaryDirectory(
        dir=self.result_path.parent
      ) as tmp_dir:
        snapshot_path: pth.AnyPath = tmp_dir / local_name
        self._clone(snapshot_path)
        compressed_snapshot: pth.LocalPath = self._fetch_and_compress_snapshot(
          snapshot_path, local_name
        )
      self._snapshots.append(compressed_snapshot)
      logging.info("PERFETTO: cloned snapshot to %s", compressed_snapshot)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
      logging.warning("PERFETTO: failed to clone snapshot %r: %s", label, e)

  def _clone(self, out_path: pth.AnyPath) -> None:
    clone_args: TupleCmdArgs
    if session_name := self._active_trace_config.unique_session_name:
      clone_args = ("--clone-by-name", session_name)
    else:
      clone_args = ("--clone", "-1")
    cmd: TupleCmdArgs = (*self.perfetto_cmd, *clone_args, "--out", out_path)
    self.browser_platform.sh(*cmd, capture_output=True)

  def _fetch_and_compress_snapshot(
    self, snapshot_path: pth.AnyPath, local_name: str
  ) -> pth.LocalPath:
    local_snapshot: pth.LocalPath = self.run.out_dir / local_name
    self.browser_platform.pull(snapshot_path, local_snapshot)
    try:
      return self._compress_trace_file(local_snapshot)
    except Exception:
      self.host_platform.rm(local_snapshot, missing_ok=True)
      raise

  def _compress_trace_file(self, trace_file: pth.LocalPath) -> pth.LocalPath:
    compressed_file = self.host_platform.local_path(
      self.host_platform.gzip(trace_file)
    )
    assert compressed_file.is_file(), f"Could not compress {compressed_file}"
    return compressed_file

  def stop(self) -> None:
    self.run.performance_mark("probe-perfetto-stop")
    logging.info("PERFETTO: stopping")
    if not self._perfetto_pid:
      raise RuntimeError("Perfetto was not started")
    if self.probe.chrome_triggers:
      # traced skips ReadBuffers() on the main session when CLONE_SNAPSHOT is
      # active, so clone the live ring buffer before terminating perfetto.
      self._final_clone_path = self.result_path.with_name(
        f"{self.result_path.name}.final"
      )
      self._clone(self._final_clone_path)
    self._stop_perfetto()

  def _try_start_perfetto(self) -> None:
    if self._perfetto_pid:
      raise RuntimeError("Perfetto was already started")
    self._start_perfetto()
    self.run.performance_mark("probe-perfetto-start")

  def _start_perfetto(self) -> None:
    logging.info("PERFETTO: starting")
    cmd: TupleCmdArgs = (
      *self.perfetto_cmd,
      "--background",
      "--config",
      "-" if self.probe.config_via_stdin else self.get_browser_config_path(),
      "--txt",
      "--out",
      self.result_path,
    )
    try:
      if self.probe.config_via_stdin:
        with self._host_config_file.open() as f:
          proc = self.browser_platform.sh(*cmd, stdin=f, capture_output=True)
      else:
        proc = self.browser_platform.sh(*cmd, capture_output=True)
    except subprocess.CalledProcessError as e:
      logging.error(
        "perfetto command failed with stderr: %s",
        e.stderr.decode(encoding="utf-8"),
      )
      raise

    self._perfetto_pid = NumberParser.positive_int(
      proc.stdout.decode("utf-8").rstrip(), "perfetto pid"
    )
    atexit.register(self._stop_perfetto)

  def _stop_perfetto(self) -> None:
    if not self._perfetto_pid:
      return
    atexit.unregister(self._stop_perfetto)
    # TODO(cbruni): replace with terminate_gracefully
    self.browser_platform.terminate(self._perfetto_pid)
    try:
      for _ in self.run.wait_range(
        1, PERFETTO_STOP_TIMEOUT
      ).wait_with_backoff():
        if not self.browser_platform.process_info(self._perfetto_pid):
          break
    except TimeoutError:
      logging.error(
        "perfetto process did not stop after %sThe trace might be incomplete.",
        PERFETTO_STOP_TIMEOUT,
      )
    self._perfetto_pid = None

  def teardown(self) -> ProbeResult:
    try:
      return self._transfer_results()
    finally:
      if self.browser_platform.is_remote:
        self._cleanup_remote_perfetto_files()

  def _transfer_results(self) -> ProbeResult:
    source_path: pth.AnyPath
    if self._final_clone_path:
      source_path = self._final_clone_path
      if not self.browser_platform.is_remote:
        self.host_platform.rm(
          self.host_platform.local_path(self.result_path),
          missing_ok=True,
        )
    else:
      source_path = self.result_path
    browser_result = self.browser_result(file=[source_path])
    local_result_file = browser_result.file
    assert local_result_file.is_file(), (
      f"Could not copy perfetto results: {local_result_file}"
    )
    renamed_result_file = local_result_file.with_name(PERFETTO_TRACE_NAME)
    if local_result_file != renamed_result_file:
      self.host_platform.rename(local_result_file, renamed_result_file)

    compressed_result_file = self._compress_trace_file(renamed_result_file)
    logging.info("Perfetto trace compressed: %s", compressed_result_file)

    if self.probe.chrome_triggers:
      self._collect_triggered_snapshots()

    return LocalProbeResult(
      perfetto=(compressed_result_file,),
      file=tuple(self._snapshots),
    )

  def _pull_triggered_snapshots(self) -> list[pth.LocalPath]:
    raw_snapshots: list[pth.LocalPath] = []
    idx = 0
    while True:
      remote_snap = self.result_path.with_name(f"{self.result_path.name}.{idx}")
      if not self.browser_platform.is_file(remote_snap):
        break
      if self.browser_platform.is_remote:
        self._remote_trigger_snapshots.append(remote_snap)
        local_raw = self.run.out_dir / f"_triggered_snapshot_{idx:03d}.trace.pb"
        self.browser_platform.pull(remote_snap, local_raw)
      else:
        local_raw = self.host_platform.local_path(remote_snap)
      raw_snapshots.append(local_raw)
      idx += 1
    return raw_snapshots

  def _collect_triggered_snapshots(self) -> None:
    raw_snapshots = self._pull_triggered_snapshots()
    if not raw_snapshots:
      return
    labels = record_triggered_anomalies(
      self.run,
      raw_snapshots,
      self.probe.chrome_triggers,
      self._trigger_salt,
    )
    for local_raw, label in zip(raw_snapshots, labels, strict=True):
      snap_name = (
        f"perfetto_snapshot_{len(self._snapshots):03d}_{label}.trace.pb"
      )
      final_uncompressed = self.run.out_dir / snap_name
      self.host_platform.rename(local_raw, final_uncompressed)
      try:
        self._snapshots.append(self._compress_trace_file(final_uncompressed))
      except Exception:
        self.host_platform.rm(final_uncompressed, missing_ok=True)
        raise

  def _cleanup_remote_perfetto_files(self) -> None:
    # Especially on android, the perfetto files are not in the default tmp dir.
    self.browser_platform.rm(self.result_path, missing_ok=True)
    self.browser_platform.rm(self.get_browser_config_path(), missing_ok=True)
    if self._final_clone_path:
      self.browser_platform.rm(self._final_clone_path, missing_ok=True)
    for snap_path in self._remote_trigger_snapshots:
      self.browser_platform.rm(snap_path, missing_ok=True)
