# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# TODO(crbug.com/586521681): Remove once depot_tools is migrated to 3.14.
from __future__ import annotations

import pathlib
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from collections.abc import Sequence

  from presubmit_support import InputApi, OutputApi


def GetNodeExecutable(input_api: InputApi) -> str:
  node_base: pathlib.Path = (
    pathlib.Path(input_api.change.RepositoryRoot()) / "third_party" / "node"
  )

  if input_api.platform == "win32":
    return str(node_base / "node.exe")
  return str(node_base / "bin" / "node")


def FormatHjsonFile(input_api: InputApi, hjson_file: pathlib.Path) -> str:
  node_bin = GetNodeExecutable(input_api)

  hjson_js_bin = str(
    pathlib.Path(input_api.change.RepositoryRoot())
    / "third_party"
    / "hjson_js"
    / "bin"
    / "hjson"
  )

  try:
    return subprocess.run(
      [
        node_bin,
        hjson_js_bin,
        "-rt",
        "-sl",
        "-nocol",
        "-cond=0",
        "-quote=all",
        "-ml",
        str(hjson_file),
      ],
      check=True,
      capture_output=True,
    ).stdout.decode(encoding="utf-8")
  except subprocess.CalledProcessError as e:
    error = e.stderr.decode(encoding="utf-8")
    raise ValueError(f"Failed to parse hjson file: {error}") from e


def FormatHjsonFiles(
  input_api: InputApi,
  output_api: OutputApi,
  modified_hjson_files: Sequence[str],
) -> list[object]:
  results: list[object] = []
  for hjson_file in modified_hjson_files:
    full_hjson_path = (
      pathlib.Path(input_api.change.RepositoryRoot()) / hjson_file
    )

    try:
      formatted_contents: str = FormatHjsonFile(input_api, full_hjson_path)
    except ValueError as e:
      results.append(
        output_api.PresubmitPromptWarning(
          "Malformed hjson file:",
          items=[str(full_hjson_path)],
          long_text=str(e),
        )
      )
      continue

    original_contents = input_api.ReadFile(str(full_hjson_path), "r")
    if original_contents != formatted_contents:
      full_hjson_path.write_text(formatted_contents)
      results.append(
        output_api.PresubmitPromptWarning(
          "Unformatted hjson file:",
          items=[str(full_hjson_path)],
          long_text="Please update your commit with the formatted file.",
        )
      )
  return results
