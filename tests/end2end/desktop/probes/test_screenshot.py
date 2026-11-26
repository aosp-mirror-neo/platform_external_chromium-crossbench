# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from typing import TYPE_CHECKING

import pytest
from PIL import Image

from crossbench import plt
from crossbench.cli.cli import CrossBenchCLI
from tests import test_helper

if TYPE_CHECKING:
  from tests.test_helper import TestEnv


@pytest.mark.skipif(
  not plt.PLATFORM.is_win, reason="Tests the in-process Windows screenshots"
)
def test_screenshot(test_env: TestEnv, test_chrome_name: str) -> None:
  cli = CrossBenchCLI()
  cli.run(
    [
      "loading",
      "--url=blank",
      f"--browser={test_chrome_name}",
      "--probe=screenshot:{}",
      "--throw",
      f"--out-dir={test_env.results_dir}",
      "--env-validation=skip",
      *list(test_env.cq_flags),
    ]
  )

  screenshots = list(test_env.results_dir.rglob("*/screenshot/*.png"))
  assert {f.name for f in screenshots} == {
    "start.png",
    "start_story.png",
    "stop.png",
    "stop_story.png",
  }
  display = plt.PLATFORM.display_resolution()
  for f in screenshots:
    with Image.open(f) as image:
      assert image.format == "PNG"
      assert image.size == (display.width, display.height)


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
