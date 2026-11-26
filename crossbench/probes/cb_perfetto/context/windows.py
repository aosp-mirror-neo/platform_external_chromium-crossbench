# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
from typing import TYPE_CHECKING, override

from crossbench.probes.cb_perfetto.constants import PERFETTO_TRACE_NAME
from crossbench.probes.cb_perfetto.context.base import PerfettoProbeContext
from third_party.protoc import trace_config_pb2

if TYPE_CHECKING:
  from crossbench import path as pth
  from crossbench.plt.types import TupleCmdArgs
  from crossbench.probes.cb_perfetto.perfetto import PerfettoProbe
  from crossbench.runner.run import Run


class WindowsPerfettoProbeContext(PerfettoProbeContext):
  def __init__(self, probe: PerfettoProbe, run: Run) -> None:
    super().__init__(probe, run)

  @override
  def get_browser_config_path(self) -> pth.AnyPath:
    return self.run.out_dir / "perfetto_config.pb"

  @override
  def get_default_result_path(self) -> pth.AnyPath:
    return self.run.out_dir / PERFETTO_TRACE_NAME

  @override
  def setup(self) -> None:
    self._setup_push_perfetto_config()
    flags = self.session.extra_flags
    flags.set(
      "--trace-perfetto-config-file", str(self.get_browser_config_path())
    )
    flags.set("--trace-startup-file", str(self.result_path))

  @override
  def _setup_push_perfetto_config(self) -> None:
    trace_config = trace_config_pb2.TraceConfig()
    trace_config.CopyFrom(self.probe.trace_config)
    self._replace_buffer_names_with_indices(trace_config)
    super()._setup_push_perfetto_config()
    # --trace-perfetto-config-file only supports the binary format.
    self.host_platform.write_bytes(
      self.get_browser_config_path(),
      trace_config.SerializeToString(),
    )

  def _replace_buffer_names_with_indices(
    self, trace_config: trace_config_pb2.TraceConfig
  ) -> None:
    # Buffer names are stripped when passing the config via Mojo to child
    # processes, causing startup sessions that specify buffer names to fail
    # adoption by the tracing service.
    buf_index_by_name = {
      buf.name: idx for idx, buf in enumerate(trace_config.buffers) if buf.name
    }
    if buf_index_by_name:
      logging.warning(
        "Perfetto: replacing trace config buffer names with indices %s, "
        "Chrome's internal tracing service does not support buffer names "
        "for startup tracing.",
        buf_index_by_name,
      )
    for buf in trace_config.buffers:
      buf.ClearField("name")
    for ds in trace_config.data_sources:
      # PerfettoProbe.validate_browser() rejects unknown buffer names.
      if target_name := ds.config.target_buffer_name:
        ds.config.target_buffer = buf_index_by_name[target_name]
        ds.config.ClearField("target_buffer_name")

  @override
  def start(self) -> None:
    self.run.performance_mark("probe-perfetto-start")

  @override
  def stop(self) -> None:
    self.run.performance_mark("probe-perfetto-stop")

  @override
  def _setup_validate_bin(self) -> None:
    pass

  @override
  def _start_perfetto(self) -> None:
    pass

  @override
  def _stop_perfetto(self) -> None:
    pass

  @property
  @override
  def perfetto_cmd(self) -> TupleCmdArgs:
    return ()
