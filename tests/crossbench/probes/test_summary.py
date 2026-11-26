# Copyright 2025 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
import json
import tempfile
import unittest
from unittest import mock

from crossbench import path as pth
from crossbench.probes.internal.summary import ResultsSummaryProbe
from crossbench.probes.results import (
  EmptyProbeResult,
  LocalProbeResult,
  ProbeResultDict,
)
from tests import test_helper


class ResultsSummaryProbeTestCase(unittest.TestCase):
  def test_merge_missing(self):
    group = mock.Mock()
    group.first_run.results = ProbeResultDict(pth.AnyPath("test/out/results"))
    probe = ResultsSummaryProbe()
    result = probe.merge_cache_temperatures(group)
    self.assertTrue(result.is_empty)

  def test_merge_empty(self):
    group = mock.Mock()
    first_run = mock.Mock()
    group.runs = [first_run]
    first_run.results = ProbeResultDict(pth.AnyPath("test/out/results"))
    probe = ResultsSummaryProbe()
    first_run.results[probe] = EmptyProbeResult()
    with mock.patch.object(probe, "write_group_result") as mock_write:
      probe.merge_repetitions(group)
    mock_write.assert_called()

  def test_merge_repetitions_missing(self):
    probe = ResultsSummaryProbe()
    with tempfile.TemporaryDirectory() as tmp_dir:
      result_file = pth.LocalPath(tmp_dir) / "cb.results.json"
      browser_data = {"log": {}}
      run_data = {
        "benchmark": {
          "name": "test",
          "version": "1.0",
        },
        "browser": browser_data,
        "cwd": "run/0",
        "probes": {},
        "success": True,
        "errors": [],
      }
      with result_file.open("w", encoding="utf-8") as f:
        json.dump(run_data, f)
      good_run = mock.Mock()
      good_run.results = ProbeResultDict(pth.AnyPath("test/out/results/0"))
      good_run.results[probe] = LocalProbeResult(json=(result_file,))
      failed_run = mock.Mock()
      failed_run.results = ProbeResultDict(pth.AnyPath("test/out/results/1"))
      group = mock.Mock()
      group.runs = [good_run, failed_run]
      with mock.patch.object(probe, "write_group_result") as mock_write:
        probe.merge_repetitions(group)
    mock_write.assert_called_once()
    merged_data = mock_write.call_args.args[1]
    self.assertEqual(len(merged_data["repetitions"]), 1)
    self.assertEqual(merged_data["repetitions"][0]["cwd"], "run/0")

  def test_merge_propagates_benchmark_metadata(self) -> None:
    probe = ResultsSummaryProbe()
    benchmark_meta = {
      "name": "web-power",
      "version": "1.1.15",
    }

    with tempfile.TemporaryDirectory() as tmp_dir:
      root = pth.LocalPath(tmp_dir)
      rep_file = root / "run.json"
      with rep_file.open("w", encoding="utf-8") as f:
        json.dump(
          {
            "browser": {
              "unique_name": "chrome_v1",
              "log": {},
            },
            "cwd": "run/0",
            "benchmark": benchmark_meta,
            "probes": {},
            "success": True,
            "errors": [],
          },
          f,
        )

      run = mock.Mock()
      run.results = ProbeResultDict(root / "run")
      run.results[probe] = LocalProbeResult(json=(rep_file,))

      rep_group = mock.Mock()
      rep_group.runs = [run]
      rep_group.path = root / "story_0"
      rep_group.story.details_json.return_value = {
        "name": "story_0",
        "duration": 10.0,
      }
      rep_group.info = {}
      rep_group.results.to_json.return_value = {}
      rep_group.is_success = True
      rep_group.exceptions.error_messages.return_value = []

      merged_rep_file = root / "story_0_merged.json"

      def write_rep_result(_, data, csv_formatter=None):
        merged_rep_file.write_text(json.dumps(data), encoding="utf-8")
        return LocalProbeResult(json=(merged_rep_file,))

      with mock.patch.object(
        probe,
        "write_group_result",
        side_effect=write_rep_result,
      ):
        res = probe.merge_repetitions(rep_group)

      merged_rep = json.loads(res.json.read_text())
      self.assertEqual(merged_rep["benchmark"], benchmark_meta)

      story_group = mock.Mock()
      story_group.results = {
        probe: LocalProbeResult(json=(merged_rep_file,)),
      }
      stories_group = mock.Mock()
      stories_group.repetitions_groups = [story_group]
      stories_group.path = root / "browser"
      stories_group.info = {}
      stories_group.results.to_json.return_value = {}
      stories_group.is_success = True
      stories_group.exceptions.error_messages.return_value = []

      merged_stories_file = root / "browser_merged.json"

      def write_stories_result(_, data, csv_formatter=None):
        merged_stories_file.write_text(json.dumps(data), encoding="utf-8")
        return LocalProbeResult(json=(merged_stories_file,))

      with mock.patch.object(
        probe,
        "write_group_result",
        side_effect=write_stories_result,
      ):
        probe.merge_stories(stories_group)

      browser_data = json.loads(merged_stories_file.read_text())
      self.assertEqual(browser_data["benchmark"], benchmark_meta)

      browsers_group = mock.Mock()
      browsers_story_group = mock.Mock()
      browsers_story_group.results = {
        probe: LocalProbeResult(json=(merged_stories_file,)),
      }
      browsers_group.story_groups = [browsers_story_group]
      browsers_group.path = root
      browsers_group.results.to_json.return_value = {}
      browsers_group.is_success = True
      browsers_group.exceptions.error_messages.return_value = []

      with mock.patch.object(probe, "write_group_result") as mock_write:
        probe.merge_browsers(browsers_group)

      top_data = mock_write.call_args.args[1]
      self.assertEqual(top_data["benchmark"], benchmark_meta)


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
