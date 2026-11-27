# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from typing import TYPE_CHECKING, ClassVar, override

from crossbench.probes.internal.base import (
  InternalJsonResultProbe,
  InternalJsonResultProbeContext,
)
from crossbench.runner.anomalies import Anomalies

if TYPE_CHECKING:
  from crossbench.probes.results import ProbeResult
  from crossbench.runner.actions import Actions
  from crossbench.runner.groups.base import RunGroup
  from crossbench.runner.groups.browsers import BrowsersRunGroup
  from crossbench.runner.groups.repetitions import RepetitionsRunGroup
  from crossbench.runner.groups.stories import StoriesRunGroup
  from crossbench.types import Json


class AnomaliesProbe(InternalJsonResultProbe):
  """
  Runner-internal meta-probe: Collects runtime anomalies recorded during story
  execution and user probe teardown into cb.anomalies.json.
  """

  NAME: ClassVar = "cb.anomalies"

  # Override all three group-level merge hooks (like ErrorsProbe):
  # - JsonResultProbe.merge_repetitions feeds JSON dicts into MetricsMerger to
  #   compute numeric summary statistics and CSVs, which fails on an anomaly
  #   list.
  # - Probe.merge_stories and Probe.merge_browsers default to
  #   symlinked_single_run_result(), which returns EmptyProbeResult whenever a
  #   group has multiple runs instead of writing an aggregated cb.anomalies.json
  #   with per-run info_stack provenance.
  @override
  def merge_repetitions(self, group: RepetitionsRunGroup) -> ProbeResult:
    return self._merge_group(group)

  @override
  def merge_stories(self, group: StoriesRunGroup) -> ProbeResult:
    return self._merge_group(group)

  @override
  def merge_browsers(self, group: BrowsersRunGroup) -> ProbeResult:
    return self._merge_group(group)

  def _merge_group(self, group: RunGroup) -> ProbeResult:
    merged = Anomalies()
    for run in group.runs:
      for anomaly in run.anomalies:
        merged.append(anomaly.with_info_stack_prefix(run.info_stack))
    return self.write_group_result(group, merged.to_json(), csv_formatter=None)

  @override
  def get_context_cls(self) -> type[AnomaliesProbeContext]:
    return AnomaliesProbeContext


class AnomaliesProbeContext(InternalJsonResultProbeContext):
  @override
  def to_json(self, actions: Actions) -> Json:
    del actions
    return self.run.anomalies.to_json()
