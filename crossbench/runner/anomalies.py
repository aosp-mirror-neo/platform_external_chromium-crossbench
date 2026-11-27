# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import dataclasses
import datetime as dt
import enum
from typing import TYPE_CHECKING, Final, Iterable, Iterator, Mapping, Self

from immutabledict import immutabledict

from crossbench.str_enum_with_help import StrEnumWithHelp

if TYPE_CHECKING:
  from crossbench.types import Json, JsonDict


@enum.unique
class AnomalySource(StrEnumWithHelp):
  AGENT = (
    "agent",
    "Visual or behavioral anomaly reported by an LLM agent action.",
  )
  PERFETTO_TRIGGER = (
    "perfetto_trigger",
    "Automatic Chrome UMA histogram anomaly trigger via Perfetto.",
  )


@dataclasses.dataclass(frozen=True)
class Anomaly:
  """Structured record of a runtime anomaly observed during a Crossbench Run."""

  timestamp: dt.datetime
  source: AnomalySource
  description: str
  perfetto_snapshot_label: str | None = None
  info_stack: tuple[str, ...] = ()
  details: Mapping[str, Json] = dataclasses.field(default_factory=immutabledict)

  def with_info_stack_prefix(self, prefix: Iterable[str]) -> Self:
    return dataclasses.replace(self, info_stack=(*prefix, *self.info_stack))

  def utc_timestamp(self) -> dt.datetime:
    if self.timestamp.tzinfo is None:
      return self.timestamp.replace(tzinfo=dt.UTC)
    return self.timestamp.astimezone(dt.UTC)

  def sort_key(self) -> tuple[dt.datetime, str, str]:
    return (self.utc_timestamp(), str(self.source), self.description)

  def to_json(self) -> JsonDict:
    return {
      "timestamp": self.timestamp.isoformat(),
      "source": str(self.source),
      "description": self.description,
      "perfetto_snapshot_label": self.perfetto_snapshot_label,
      "info_stack": list(self.info_stack),
      "details": dict(self.details),
    }


class Anomalies:
  """Chronologically ordered collection of anomalies recorded during a Run."""

  def __init__(self, items: Iterable[Anomaly] = ()) -> None:
    self._items: Final[list[Anomaly]] = []
    self.extend(items)

  def append(self, anomaly: Anomaly) -> None:
    if not isinstance(anomaly, Anomaly):
      raise TypeError(f"Expected Anomaly, got {type(anomaly).__name__}")
    self._items.append(anomaly)

  def extend(self, anomalies: Iterable[Anomaly]) -> None:
    for anomaly in anomalies:
      self.append(anomaly)

  def __len__(self) -> int:
    return len(self._items)

  def __bool__(self) -> bool:
    return bool(self._items)

  def __iter__(self) -> Iterator[Anomaly]:
    return iter(sorted(self._items, key=Anomaly.sort_key))

  def to_json(self) -> list[JsonDict]:
    return [anomaly.to_json() for anomaly in self]
