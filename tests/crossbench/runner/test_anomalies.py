# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import datetime as dt
import json
import unittest
from typing import override

from crossbench.probes.internal.anomalies import AnomaliesProbe
from crossbench.runner.anomalies import Anomalies, Anomaly, AnomalySource
from tests import test_helper
from tests.crossbench.mock_helper import MockBenchmark, MockStory
from tests.crossbench.runner.helper import (
  BaseRunnerTestCase,
  MockProbe,
  MockProbeContext,
)


class AnomalyTestCase(unittest.TestCase):
  def test_to_json_and_info_stack_prefix(self) -> None:
    anomaly = Anomaly(
      timestamp=dt.datetime(2026, 11, 17, 3, 0, 0, tzinfo=dt.UTC),
      source=AnomalySource.PERFETTO_TRIGGER,
      description="EventLatency >= 500ms",
      perfetto_snapshot_label="trigger_laggy_input",
      info_stack=("playback_0", "step_1"),
      details={
        "trigger_name": "laggy_input",
        "histogram_value": 642000,
      },
    )
    self.assertEqual(
      anomaly.to_json(),
      {
        "timestamp": "2026-11-17T03:00:00+00:00",
        "source": "perfetto_trigger",
        "description": "EventLatency >= 500ms",
        "perfetto_snapshot_label": "trigger_laggy_input",
        "info_stack": ["playback_0", "step_1"],
        "details": {
          "trigger_name": "laggy_input",
          "histogram_value": 642000,
        },
      },
    )

    prefixed = anomaly.with_info_stack_prefix(("Run(0)", "story=story_1"))
    self.assertEqual(
      prefixed.info_stack,
      ("Run(0)", "story=story_1", "playback_0", "step_1"),
    )

  def test_anomalies_collection_chronological_sort(self) -> None:
    later = Anomaly(
      timestamp=dt.datetime(2026, 11, 17, 12, 0, 5, tzinfo=dt.UTC),
      source=AnomalySource.AGENT,
      description="Later visual stall",
    )
    earlier = Anomaly(
      timestamp=dt.datetime(2026, 11, 17, 12, 0, 1),
      source=AnomalySource.PERFETTO_TRIGGER,
      description="Earlier trigger",
      details={"trigger_name": "laggy_input"},
    )
    collection = Anomalies([later])
    collection.append(earlier)
    self.assertEqual(len(collection), 2)
    self.assertTrue(bool(collection))
    self.assertEqual(list(collection), [earlier, later])


class _TeardownAnomalyMockProbeContext(MockProbeContext):
  @override
  def teardown(self):
    self.run.anomalies.append(
      Anomaly(
        timestamp=dt.datetime(
          2026, 11, 17, 1, 0, self.run.index, tzinfo=dt.UTC
        ),
        source=AnomalySource.PERFETTO_TRIGGER,
        description=f"Trigger on run {self.run.index}",
      )
    )
    return super().teardown()


class _TeardownAnomalyMockProbe(MockProbe):
  @override
  def get_context_cls(self):
    return _TeardownAnomalyMockProbeContext


class AnomaliesProbeRunnerTestCase(BaseRunnerTestCase):
  def test_empty_anomalies_writes_empty_json_list(self) -> None:
    runner = self.default_runner(browsers=[self.mock_chrome_dev])
    runner.run()
    self.assertTrue(runner.is_success)
    for run in runner.runs:
      run_anomalies_file = run.out_dir / f"{AnomaliesProbe.NAME}.json"
      self.assertTrue(run_anomalies_file.is_file())
      self.assertEqual(
        json.loads(run_anomalies_file.read_text(encoding="utf-8")), []
      )
    root_anomalies_file = runner.out_dir / f"{AnomaliesProbe.NAME}.json"
    self.assertTrue(root_anomalies_file.is_file())
    self.assertEqual(
      json.loads(root_anomalies_file.read_text(encoding="utf-8")), []
    )

  def test_collects_story_and_user_probe_teardown_anomalies(self) -> None:
    class AnomalyStory(MockStory):
      @override
      def run(self, run) -> None:
        super().run(run)
        run.anomalies.append(
          Anomaly(
            timestamp=dt.datetime(2026, 11, 17, 2, 0, run.index, tzinfo=dt.UTC),
            source=AnomalySource.AGENT,
            description=f"Agent anomaly on run {run.index}",
            info_stack=("step_1",),
          )
        )

    stories = [AnomalyStory("story_1"), AnomalyStory("story_2")]
    benchmark = MockBenchmark(stories)
    user_probe = _TeardownAnomalyMockProbe("user_trigger_probe")
    runner = self.default_runner(
      browsers=[self.mock_chrome_dev], benchmark=benchmark, probes=[user_probe]
    )
    runner.run()
    self.assertTrue(runner.is_success)

    # User probe teardown happens before AnomaliesProbe teardown, so both
    # teardown-registered triggers and story-run agent anomalies are captured.
    first_run = runner.runs[0]
    run_entries = json.loads(
      (first_run.out_dir / "cb.anomalies.json").read_text(encoding="utf-8")
    )
    self.assertEqual(
      [entry["source"] for entry in run_entries],
      ["perfetto_trigger", "agent"],
    )
    self.assertEqual(run_entries[0]["info_stack"], [])
    self.assertEqual(run_entries[1]["info_stack"], ["step_1"])

    root_entries = json.loads(
      (runner.out_dir / "cb.anomalies.json").read_text(encoding="utf-8")
    )
    self.assertEqual(len(root_entries), 4)
    self.assertEqual(
      [entry["description"] for entry in root_entries],
      [
        "Trigger on run 0",
        "Trigger on run 1",
        "Agent anomaly on run 0",
        "Agent anomaly on run 1",
      ],
    )
    # Group-merged entries carry run provenance in info_stack.
    self.assertEqual(
      root_entries[0]["info_stack"], list(runner.runs[0].info_stack)
    )
    self.assertEqual(
      root_entries[2]["info_stack"],
      [*runner.runs[0].info_stack, "step_1"],
    )


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
