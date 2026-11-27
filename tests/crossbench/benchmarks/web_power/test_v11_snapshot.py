# Copyright 2026 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Snapshot tests that pin exactly what Web Power v1.1 runs.

##########################################################################
#  READ THIS BEFORE EDITING THIS FILE (humans and AI agents alike).     #
##########################################################################

Web Power v1.1 results must stay comparable across crossbench revisions
while v1.2 and later are developed alongside it. These tests freeze the
v1.1 stories, parameters, browser flags, actions and generated input. A
failure here means that a production change altered v1.1.

Before you update any expectation in this file:
- If your change targets v1.2 or later, it should not need an edit here.
  If it does, the change is leaking into v1.1: fix the production code
  (e.g. scope the change to the newer version) instead of this file.
- If you intend to change v1.1, update this file in the same CL, bump
  VERSION_STRING in crossbench/benchmarks/web_power/base.py if needed,
  and explain the change and its effect on v1.1 results in the CL
  description.
- Never regenerate expected values (such as the EVEMU digest) just to
  make a failing test pass. First understand why they changed.
"""

import contextlib
import dataclasses
import datetime as dt
import hashlib
from typing import TYPE_CHECKING, Final, Iterator, Mapping, Sequence, override
from unittest import mock

from crossbench.action_runner.action.enums import WindowTarget
from crossbench.benchmarks.web_power import media_playback
from crossbench.benchmarks.web_power.base import (
  WebPowerBenchmarkBase,
  WebPowerSiteConfig,
  WebPowerStory,
)
from crossbench.benchmarks.web_power.consolidated import WebPowerBenchmark
from crossbench.benchmarks.web_power.idle import (
  WebPowerIdleBenchmark,
  WebPowerIdleStory,
)
from crossbench.benchmarks.web_power.media_playback import (
  AmbientMode,
  WebPowerMediaPlaybackBenchmark,
  WebPowerMediaPlaybackStory,
)
from crossbench.benchmarks.web_power.page_load import (
  WebPowerPageLoadBenchmark,
  WebPowerPageLoadStory,
)
from crossbench.benchmarks.web_power.scroll import (
  WebPowerScrollBenchmark,
  WebPowerScrollStory,
)
from crossbench.benchmarks.web_power.scroll_gen import GeneratorConfig
from crossbench.benchmarks.web_power.volume_helper import VolumeMode
from crossbench.browsers.attributes import BrowserAttributes
from crossbench.cli.config.network import NetworkConfig
from crossbench.cli.parser import CBArgumentParser
from crossbench.parse import ObjectParser
from crossbench.plt.display_info import DisplayResolution
from crossbench.plt.ios import IOSPlatform
from tests import test_helper
from tests.crossbench.benchmarks.web_power.test_base import (
  BaseWebPowerBenchmarkTestCase,
)

if TYPE_CHECKING:
  import argparse

# ---------------------------------------------------------------------------
# Story names and tags.
# ---------------------------------------------------------------------------

_EXPECTED_V11_DEFAULT_STORIES: Final[tuple[str, ...]] = (
  "idle-ajnews",
  "idle-cnn",
  "idle-msn",
  "media-playback-youtube",
  "page-load-ajnews",
  "page-load-cnn",
  "page-load-msn",
  "scroll-ajnews",
  "scroll-cnn",
  "scroll-msn",
)

_EXPECTED_V11_ALL_STORIES: Final[tuple[str, ...]] = (
  "idle-ajnews",
  "idle-allrecipes",
  "idle-cnn",
  "idle-msn",
  "idle-sfgate",
  "idle-telegraph",
  "idle-yahoo",
  "media-playback-youtube",
  "page-load-ajnews",
  "page-load-allrecipes",
  "page-load-cnn",
  "page-load-msn",
  "page-load-sfgate",
  "page-load-telegraph",
  "page-load-yahoo",
  "scroll-ajnews",
  "scroll-allrecipes",
  "scroll-cnn",
  "scroll-msn",
  "scroll-sfgate",
  "scroll-telegraph",
  "scroll-yahoo",
)

_EXPECTED_V11_TAGS: Final[dict[str, tuple[str, ...]]] = {
  "idle-ajnews": ("ajnews", "idle", "canonical"),
  "idle-allrecipes": ("allrecipes",),
  "idle-cnn": ("cnn", "idle", "canonical"),
  "idle-msn": ("msn", "idle", "canonical"),
  "idle-sfgate": ("sfgate",),
  "idle-telegraph": ("telegraph",),
  "idle-yahoo": ("yahoo",),
  "media-playback-youtube": ("youtube", "media-playback", "canonical"),
  "page-load-ajnews": ("ajnews", "page-load", "canonical"),
  "page-load-allrecipes": ("allrecipes",),
  "page-load-cnn": ("cnn", "page-load", "canonical"),
  "page-load-msn": ("msn", "page-load", "canonical"),
  "page-load-sfgate": ("sfgate",),
  "page-load-telegraph": ("telegraph",),
  "page-load-yahoo": ("yahoo",),
  "scroll-ajnews": ("ajnews", "scroll", "canonical"),
  "scroll-allrecipes": ("allrecipes",),
  "scroll-cnn": ("cnn", "scroll", "canonical"),
  "scroll-msn": ("msn", "scroll", "canonical"),
  "scroll-sfgate": ("sfgate",),
  "scroll-telegraph": ("telegraph",),
  "scroll-yahoo": ("yahoo",),
}

# ---------------------------------------------------------------------------
# Site configs (URLs and WPR archives) of the canonical sites.
# ---------------------------------------------------------------------------

_GCS: Final[str] = "gs://chrome-partner-loadline/power"
_LEGACY_ARCHIVE: Final[str] = (
  f"{_GCS}/CHROME_EFFICIENCY_KPI_2026_04_03.wprgo#1775177008532597"
)
_LEGACY_MD5: Final[bytes] = ObjectParser.md5_hash(
  "8375843a6828b282eef16042c21e903b"
)

_EXPECTED_V11_SITES: Final[dict[str, WebPowerSiteConfig]] = {
  "ajnews": WebPowerSiteConfig(
    url="https://aljazeera.com",
    archive=_LEGACY_ARCHIVE,
    archive_md5_hash=_LEGACY_MD5,
    default_stabilization_time=dt.timedelta(seconds=10),
  ),
  "cnn": WebPowerSiteConfig(
    url="https://www.cnn.com",
    archive=f"{_GCS}/cnn_20260513.wprgo#1778683962813986",
    archive_md5_hash=ObjectParser.md5_hash("04d258d7a6c207b41c591fbb55c53397"),
    default_stabilization_time=dt.timedelta(seconds=10),
  ),
  "msn": WebPowerSiteConfig(
    url="https://msn.com/en-us",
    archive=_LEGACY_ARCHIVE,
    archive_md5_hash=_LEGACY_MD5,
    default_stabilization_time=dt.timedelta(seconds=60),
  ),
  "youtube": WebPowerSiteConfig(
    url="https://www.youtube.com/watch?v=XITHbsUUlYI",
    archive=f"{_GCS}/youtube_2026_05_18.wprgo#1779096503585939",
    archive_md5_hash=ObjectParser.md5_hash("fcfa2979d058d5a5d8e4900cfc994570"),
    default_stabilization_time=dt.timedelta(seconds=10),
  ),
}

# ---------------------------------------------------------------------------
# Browser flags.
# ---------------------------------------------------------------------------

_EXPECTED_V11_FLAGS: Final[dict[str, str | None]] = {
  "--remote-allow-origins": "*",
  "--disable-background-timer-throttling": None,
  "--disable-component-update": None,
  "--disable-external-intent-requests": None,
  "--disable-optimization-guide-model-downloads-for-benchmarking": None,
  "--disable-renderer-backgrounding": None,
  "--disable-stack-profiler": None,
  "--disable-gesture-requirement-for-presentation": None,
  "--disable-notifications": None,
}
_EXPECTED_V11_MEDIA_FLAGS: Final[dict[str, str | None]] = {
  "--autoplay-policy": "no-user-gesture-required",
  **_EXPECTED_V11_FLAGS,
}

# ---------------------------------------------------------------------------
# Per-story parameters.
# ---------------------------------------------------------------------------

_V11_GENERATOR_CONFIG: Final[GeneratorConfig] = GeneratorConfig(
  input_rate=240,
  scroll_count=5,
  swipes_per_direction=5,
  swipe_duration=dt.timedelta(milliseconds=750),
  settle_duration=dt.timedelta(milliseconds=150),
  lift_duration=dt.timedelta(milliseconds=100),
)
_V11_FAST_GENERATOR_CONFIG: Final[GeneratorConfig] = dataclasses.replace(
  _V11_GENERATOR_CONFIG, scroll_count=1
)


def _seconds(value: float) -> dt.timedelta:
  return dt.timedelta(seconds=value)


@dataclasses.dataclass(frozen=True)
class _StoryExpectation:
  """Frozen v1.1 values of one instantiated story."""

  story_cls: type[WebPowerStory]
  site: str
  duration: dt.timedelta
  stabilization_time: dt.timedelta
  params: dict[str, object]


def _idle(
  site: str, idle: float, stabilization: float, total: float
) -> _StoryExpectation:
  return _StoryExpectation(
    WebPowerIdleStory,
    site,
    _seconds(total),
    _seconds(stabilization),
    {"idle_duration": _seconds(idle), "window_target": WindowTarget.SELF},
  )


def _media(
  playback: float, stabilization: float, total: float
) -> _StoryExpectation:
  return _StoryExpectation(
    WebPowerMediaPlaybackStory,
    "youtube",
    _seconds(total),
    _seconds(stabilization),
    {
      "playback_duration": _seconds(playback),
      "fullscreen": True,
      "stats": False,
      "volume": VolumeMode.ON,
      "ambient_mode": AmbientMode.OFF,
    },
  )


def _page_load(
  site: str, loads: int, interval: float, stabilization: float, total: float
) -> _StoryExpectation:
  return _StoryExpectation(
    WebPowerPageLoadStory,
    site,
    _seconds(total),
    _seconds(stabilization),
    {
      "page_load_count": loads,
      "interval": _seconds(interval),
      "window_target": WindowTarget.NEW_TAB,
    },
  )


def _scroll(
  site: str, config: GeneratorConfig, stabilization: float, total: float
) -> _StoryExpectation:
  return _StoryExpectation(
    WebPowerScrollStory,
    site,
    _seconds(total),
    _seconds(stabilization),
    {"generator_config": config, "requires_ios_wda": True},
  )


_EXPECTED_V11_CANONICAL_STORIES: Final[dict[str, _StoryExpectation]] = {
  "web-power-idle-ajnews": _idle("ajnews", 80, 10, 110),
  "web-power-idle-cnn": _idle("cnn", 80, 10, 110),
  "web-power-idle-msn": _idle("msn", 80, 60, 160),
  "web-power-media-playback-youtube": _media(120, 10, 170),
  "web-power-page-load-ajnews": _page_load("ajnews", 40, 3, 10, 150),
  "web-power-page-load-cnn": _page_load("cnn", 10, 3, 10, 60),
  "web-power-page-load-msn": _page_load("msn", 40, 3, 60, 200),
  "web-power-scroll-ajnews": _scroll("ajnews", _V11_GENERATOR_CONFIG, 10, 80),
  "web-power-scroll-cnn": _scroll("cnn", _V11_GENERATOR_CONFIG, 10, 80),
  "web-power-scroll-msn": _scroll("msn", _V11_GENERATOR_CONFIG, 60, 130),
}

# With --fast, every story uses a 3s stabilization time.
_EXPECTED_V11_FAST_CANONICAL_STORIES: Final[dict[str, _StoryExpectation]] = {
  "web-power-idle-ajnews": _idle("ajnews", 3, 3, 26),
  "web-power-idle-cnn": _idle("cnn", 3, 3, 26),
  "web-power-idle-msn": _idle("msn", 3, 3, 26),
  "web-power-media-playback-youtube": _media(3, 3, 46),
  "web-power-page-load-ajnews": _page_load("ajnews", 2, 1, 3, 25),
  "web-power-page-load-cnn": _page_load("cnn", 2, 1, 3, 25),
  "web-power-page-load-msn": _page_load("msn", 2, 1, 3, 25),
  "web-power-scroll-ajnews": _scroll(
    "ajnews", _V11_FAST_GENERATOR_CONFIG, 3, 33
  ),
  "web-power-scroll-cnn": _scroll("cnn", _V11_FAST_GENERATOR_CONFIG, 3, 33),
  "web-power-scroll-msn": _scroll("msn", _V11_FAST_GENERATOR_CONFIG, 3, 33),
}


def _story_params(story: WebPowerStory) -> dict[str, object]:
  if isinstance(story, WebPowerIdleStory):
    return {
      "idle_duration": story.idle_duration,
      "window_target": story.WINDOW_TARGET,
    }
  if isinstance(story, WebPowerMediaPlaybackStory):
    return {
      "playback_duration": story.playback_duration,
      "fullscreen": story.fullscreen,
      "stats": story.stats,
      "volume": story.volume,
      "ambient_mode": story.ambient_mode,
    }
  if isinstance(story, WebPowerPageLoadStory):
    return {
      "page_load_count": story.page_load_count,
      "interval": story.interval,
      "window_target": story.WINDOW_TARGET,
    }
  if isinstance(story, WebPowerScrollStory):
    return {
      "generator_config": story.config,
      "requires_ios_wda": story.REQUIRES_IOS_WDA,
    }
  raise AssertionError(f"Unexpected story class: {type(story)}")


# ---------------------------------------------------------------------------
# --fast overrides and standalone scenario commands.
# ---------------------------------------------------------------------------

_FAST_BASE: Final[dict[str, object]] = {
  "duration": _seconds(3),
  "repetitions": 1,
  "stabilization_time": _seconds(3),
}
_FAST_PAGE_LOAD: Final[dict[str, object]] = {
  "page_load_count": 2,
  "interval": _seconds(1),
}
_FAST_SCROLL: Final[dict[str, object]] = {"scroll_count": 1}

_EXPECTED_V11_FAST_OVERRIDES: Final[
  dict[type[WebPowerBenchmarkBase], dict[str, object]]
] = {
  WebPowerBenchmark: {**_FAST_BASE, **_FAST_PAGE_LOAD, **_FAST_SCROLL},
  WebPowerIdleBenchmark: _FAST_BASE,
  WebPowerMediaPlaybackBenchmark: _FAST_BASE,
  WebPowerPageLoadBenchmark: {**_FAST_BASE, **_FAST_PAGE_LOAD},
  WebPowerScrollBenchmark: {**_FAST_BASE, **_FAST_SCROLL},
}

_SITE_STORIES: Final[tuple[str, ...]] = ("ajnews", "cnn", "msn")
_ALL_SITE_STORIES: Final[tuple[str, ...]] = (
  "ajnews",
  "allrecipes",
  "cnn",
  "msn",
  "sfgate",
  "telegraph",
  "yahoo",
)


@dataclasses.dataclass(frozen=True)
class _StandaloneExpectation:
  """Frozen v1.1 surface of one standalone scenario command."""

  name: str
  default_story_names: tuple[str, ...]
  all_story_names: tuple[str, ...]


_EXPECTED_V11_STANDALONE: Final[
  dict[type[WebPowerBenchmarkBase], _StandaloneExpectation]
] = {
  WebPowerIdleBenchmark: _StandaloneExpectation(
    "web-power-idle", _SITE_STORIES, _ALL_SITE_STORIES
  ),
  WebPowerMediaPlaybackBenchmark: _StandaloneExpectation(
    "web-power-media-playback", ("youtube",), ("youtube",)
  ),
  WebPowerPageLoadBenchmark: _StandaloneExpectation(
    "web-power-page-load", _SITE_STORIES, _ALL_SITE_STORIES
  ),
  WebPowerScrollBenchmark: _StandaloneExpectation(
    "web-power-scroll", _SITE_STORIES, _ALL_SITE_STORIES
  ),
}

# ---------------------------------------------------------------------------
# Media playback setup and measurement actions.
# ---------------------------------------------------------------------------

_VIDEO: Final[str] = "document.querySelector('video')"
_SETTINGS: Final[str] = (
  "document.querySelector('button[aria-label*=\"Settings\"]')"
)
_ACCEPT: Final[str] = "document.querySelector('button[aria-label*=\"Accept\"]')"
_SCREEN: Final[str] = "document.querySelector('button[aria-label*=\"screen\"]')"
_CLOSE: Final[str] = (
  "document.querySelector("
  "'bottom-sheet-container button[aria-label*=\"Close\"]')"
)
_CONTROLS_HIDDEN: Final[str] = (
  f"let el = {_SETTINGS}; return !el || el.offsetParent === null;"
)
_PAGE_COMPLETE: Final[str] = "return document.readyState === 'complete';"

type _Step = tuple[object, ...]
type _Block = tuple[str, str | None, tuple[_Step, ...]]


def _normalize_js(code: str) -> str:
  """Collapses whitespace so multi-line JS snippets compare as one line."""
  return " ".join(code.split())


def _span(text: str) -> str:
  return (
    "Array.from(document.querySelectorAll('span'))"
    f".find(el => el.textContent.trim() === '{text}')"
  )


_AMBIENT_TOGGLE: Final[str] = _span("Ambient mode")
_HAS_AMBIENT: Final[str] = f"return !!({_AMBIENT_TOGGLE});"
_IS_AMBIENT_ON: Final[str] = (
  f"const el = ({_AMBIENT_TOGGLE})?.closest('[aria-pressed]'); "
  "return el ? el.getAttribute('aria-pressed') === 'true' : null;"
)


def _js(code: str) -> _Step:
  return ("js", code)


def _wait_js(code: str) -> _Step:
  return ("wait_js", code, 0.2, 5.0)


def _wait(seconds: float) -> _Step:
  return ("wait", _seconds(seconds))


def _click(selector: str) -> tuple[_Step, _Step]:
  return (_wait_js(f"return !!({selector});"), _js(f"({selector}).click();"))


def _show_controls(controls_hidden: bool) -> tuple[_Step, ...]:
  if controls_hidden:
    return (_js(_CONTROLS_HIDDEN), *_click(_VIDEO))
  return (_js(_CONTROLS_HIDDEN),)


def _enter_settings(controls_hidden: bool) -> tuple[_Step, ...]:
  return (*_show_controls(controls_hidden), *_click(_SETTINGS))


def _set_ambient_mode(ambient_mode_on: bool | None) -> tuple[_Step, ...]:
  """Steps after opening the settings; None means there is no toggle."""
  if ambient_mode_on is None:
    return (_js(_HAS_AMBIENT), *_click(_CLOSE))
  check = (_js(_HAS_AMBIENT), _wait_js(_HAS_AMBIENT), _js(_IS_AMBIENT_ON))
  # v1.1 turns ambient mode off, so only an enabled toggle is clicked.
  if ambient_mode_on:
    return (*check, *_click(_AMBIENT_TOGGLE))
  return (*check, *_click(_CLOSE))


def _media_actions(
  controls_hidden: bool, ambient_mode_on: bool | None
) -> tuple[_Block, ...]:
  """The v1.1 media-playback actions for one page state."""
  return (
    (
      "Show URL",
      None,
      (("show_url", _EXPECTED_V11_SITES["youtube"].url, WindowTarget.SELF),),
    ),
    ("Stabilization", None, (_wait(10),)),
    (
      "Consent_Banner",
      None,
      (
        _wait_js(_PAGE_COMPLETE),
        _js("window.__waiting_for_reload = true;"),
        *_click(_ACCEPT),
        _wait_js("return !window.__waiting_for_reload;"),
        _wait_js(_PAGE_COMPLETE),
      ),
    ),
    ("Focus_Tap", None, _click(_VIDEO)),
    (
      "Pause_Video",
      None,
      (_wait_js(f"return !!({_VIDEO});"), _js(f"{_VIDEO}.pause();"), _wait(1)),
    ),
    (
      "Full_Screen",
      None,
      (
        *_show_controls(controls_hidden),
        _wait_js(f"return !!({_SCREEN});"),
        ("gesture", f"({_SCREEN}).click();"),
      ),
    ),
    (
      "Set_Quality",
      None,
      (
        *_enter_settings(controls_hidden),
        *_click(_span("Quality")),
        *_click(_span("1080p")),
      ),
    ),
    (
      "Set_Ambient_Mode",
      None,
      (
        *_enter_settings(controls_hidden),
        *_set_ambient_mode(ambient_mode_on),
      ),
    ),
    (
      "Seek_To_Start",
      None,
      (
        *_show_controls(controls_hidden),
        _wait_js(f"return !!({_VIDEO});"),
        _js(f"{_VIDEO}.currentTime = 0;"),
      ),
    ),
    (
      "Resume_Playback",
      None,
      (_wait_js(f"return !!({_VIDEO});"), _js(f"{_VIDEO}.play();"), _wait(1)),
    ),
    ("Media_Playback", WebPowerStory.MEASUREMENT_MARK, (_wait(120),)),
  )


@dataclasses.dataclass(frozen=True)
class _MediaPageState:
  """A YouTube page state and the v1.1 actions expected for it."""

  name: str
  js_results: Mapping[str, object]
  expected_actions: tuple[_Block, ...]


# Every page check that is not listed in js_results answers False.
_V11_MEDIA_PAGE_STATES: Final[tuple[_MediaPageState, ...]] = (
  _MediaPageState(
    "controls visible, no ambient mode toggle",
    {},
    _media_actions(controls_hidden=False, ambient_mode_on=None),
  ),
  _MediaPageState(
    "controls hidden, ambient mode on",
    {_CONTROLS_HIDDEN: True, _HAS_AMBIENT: True, _IS_AMBIENT_ON: True},
    _media_actions(controls_hidden=True, ambient_mode_on=True),
  ),
  _MediaPageState(
    "controls visible, ambient mode off",
    {_HAS_AMBIENT: True, _IS_AMBIENT_ON: False},
    _media_actions(controls_hidden=False, ambient_mode_on=False),
  ),
)


class _ActionRecorder:
  """Records run.actions() blocks and the action calls made inside them.

  Method signatures mirror how the v1.1 stories call Actions, so a change in
  calling convention also fails the snapshot. JS snippets are recorded with
  whitespace collapsed, actions.js() answers come from js_results (False by
  default), and the verbose flag of run.actions() is ignored.
  """

  def __init__(self, js_results: Mapping[str, object] | None = None) -> None:
    self._js_results: Final[Mapping[str, object]] = js_results or {}
    self._blocks: list[tuple[str, str | None, list[_Step]]] = []

  @contextlib.contextmanager
  def actions(
    self,
    name: str,
    verbose: bool = False,
    performance_mark: str | None = None,
  ) -> Iterator[_ActionRecorder]:
    del verbose
    self._blocks.append((name, performance_mark, []))
    yield self

  def _record(self, *step: object) -> None:
    self._blocks[-1][2].append(step)

  def js(self, code: str) -> object:
    code = _normalize_js(code)
    self._record("js", code)
    return self._js_results.get(code, False)

  def wait_js_condition(
    self, code: str, min_interval: float, timeout: float
  ) -> None:
    self._record("wait_js", _normalize_js(code), min_interval, timeout)

  def wait(self, duration: dt.timedelta) -> None:
    self._record("wait", duration)

  def show_url(self, url: str, target: WindowTarget) -> None:
    self._record("show_url", url, target)

  def evaluate_with_gesture(self, browser: object, js_code: str) -> None:
    del browser
    self._record("gesture", _normalize_js(js_code))

  def blocks(self) -> tuple[_Block, ...]:
    return tuple(
      (name, mark, tuple(steps)) for name, mark, steps in self._blocks
    )


# ---------------------------------------------------------------------------
# Scroll touch sequences (Android EVEMU and iOS WebDriverAgent).
# ---------------------------------------------------------------------------

_DISPLAY: Final[DisplayResolution] = DisplayResolution(1080, 2400)
_REMOTE_EVEMU_PATH: Final[str] = "/data/local/tmp/scrolling_sequence.evemu"
# Implied by the digest, but checked first so that a failure shows whether
# events were added or removed (count changes) or only their values changed.
_EXPECTED_V11_EVEMU_LINE_COUNT: Final[int] = 75885
_EXPECTED_V11_EVEMU_SHA256: Final[str] = (
  "e823a5005007d92024e3d27f9a256fdb8d57841c711a8408a06374e31b4f6613"
)
_EXPECTED_V11_SCROLL_STORIES: Final[tuple[str, ...]] = (
  "web-power-scroll-ajnews",
  "web-power-scroll-cnn",
  "web-power-scroll-msn",
)

# On a 1080x2400 display: x = 50% of width, swipes between 80% and 20% of
# the height. Each scroll is 5 swipes down then 5 up; v1.1 runs 5 scrolls.
_IOS_SWIPE_DOWN: Final[tuple[int, int, int, int]] = (540, 1920, 540, 480)
_IOS_SWIPE_UP: Final[tuple[int, int, int, int]] = (540, 480, 540, 1920)
_EXPECTED_V11_IOS_SWIPES: Final[tuple[tuple[int, int, int, int], ...]] = (
  (_IOS_SWIPE_DOWN,) * 5 + (_IOS_SWIPE_UP,) * 5
) * 5


def _scroll_setup_actions(
  site: str, stabilization: float
) -> tuple[_Block, ...]:
  return (
    (
      "Show URL",
      None,
      (("show_url", _EXPECTED_V11_SITES[site].url, WindowTarget.SELF),),
    ),
    ("Stabilization", None, (_wait(stabilization),)),
  )


def _scroll_run_actions() -> tuple[_Block, ...]:
  return (
    ("Run", WebPowerStory.MEASUREMENT_MARK, ()),
    ("Scroll", None, ()),
  )


class WebPowerV11SnapshotTestCase(BaseWebPowerBenchmarkTestCase):
  """Snapshot parity tests freezing Web Power v1.1 (1.1.17) workloads."""

  @property
  @override
  def benchmark_cls(self) -> type[WebPowerBenchmark]:
    return WebPowerBenchmark

  def _parse_for(
    self,
    benchmark_cls: type[WebPowerBenchmarkBase],
    *args: str,
    fast: bool = False,
  ) -> argparse.Namespace:
    parser = CBArgumentParser()
    parser.set_defaults(config=None)
    benchmark_cls.add_cli_arguments(parser)
    if fast:
      parser.set_defaults(**benchmark_cls.fast_mode_default_overrides())
    parsed_args = parser.parse_args(list(args))
    parsed_args.browser = ()
    parsed_args.network_config = NetworkConfig.default()
    parsed_args.probe_config = None
    parsed_args.probe = ()
    return parsed_args

  def _create(
    self,
    benchmark_cls: type[WebPowerBenchmarkBase],
    *args: str,
    fast: bool = False,
  ) -> WebPowerBenchmarkBase:
    return benchmark_cls.from_cli_args(
      self._parse_for(benchmark_cls, *args, fast=fast)
    )

  def _assert_stories(
    self,
    benchmark: WebPowerBenchmarkBase,
    expected: dict[str, _StoryExpectation],
    names: Sequence[str],
  ) -> None:
    self.assertEqual(tuple(s.name for s in benchmark.stories), tuple(names))
    for story in benchmark.stories:
      with self.subTest(story=story.name):
        assert isinstance(story, WebPowerStory)
        expectation = expected[story.name]
        self.assertIs(type(story), expectation.story_cls)
        self.assertEqual(
          story.site_config, _EXPECTED_V11_SITES[expectation.site]
        )
        self.assertEqual(story.duration, expectation.duration)
        self.assertEqual(
          story.stabilization_time, expectation.stabilization_time
        )
        self.assertEqual(_story_params(story), expectation.params)
        flags = benchmark.extra_flags(BrowserAttributes.CHROMIUM_BASED, story)
        expected_flags = _EXPECTED_V11_FLAGS
        if isinstance(story, WebPowerMediaPlaybackStory):
          expected_flags = _EXPECTED_V11_MEDIA_FLAGS
        self.assertEqual(flags.to_dict(), expected_flags)

  def test_v11_story_names_and_order(self) -> None:
    story_cls = self.benchmark_cls.DEFAULT_STORY_CLS
    self.assertEqual(
      story_cls.default_story_names(), _EXPECTED_V11_DEFAULT_STORIES
    )
    self.assertEqual(story_cls.all_story_names(), _EXPECTED_V11_ALL_STORIES)

  def test_v11_tags_lookup(self) -> None:
    tags_lookup = self.benchmark_cls.all_story_tags_lookup()
    normalized = {name: tuple(tags) for name, tags in tags_lookup.items()}
    self.assertEqual(normalized, _EXPECTED_V11_TAGS)

  def test_v11_canonical_stories(self) -> None:
    benchmark = self._create(WebPowerBenchmark)
    self._assert_stories(
      benchmark,
      _EXPECTED_V11_CANONICAL_STORIES,
      tuple(_EXPECTED_V11_CANONICAL_STORIES),
    )

  def test_v11_fast_mode_overrides(self) -> None:
    for benchmark_cls, expected in _EXPECTED_V11_FAST_OVERRIDES.items():
      with self.subTest(benchmark=benchmark_cls.NAME):
        self.assertEqual(benchmark_cls.fast_mode_default_overrides(), expected)

  def test_v11_fast_mode_canonical_stories(self) -> None:
    benchmark = self._create(WebPowerBenchmark, fast=True)
    self._assert_stories(
      benchmark,
      _EXPECTED_V11_FAST_CANONICAL_STORIES,
      tuple(_EXPECTED_V11_FAST_CANONICAL_STORIES),
    )

  def test_v11_standalone_commands(self) -> None:
    for benchmark_cls, expected in _EXPECTED_V11_STANDALONE.items():
      with self.subTest(benchmark=expected.name):
        story_cls = benchmark_cls.DEFAULT_STORY_CLS
        self.assertEqual(benchmark_cls.NAME, expected.name)
        self.assertEqual(
          story_cls.default_story_names(), expected.default_story_names
        )
        self.assertEqual(story_cls.all_story_names(), expected.all_story_names)
        self.assertEqual(dict(benchmark_cls.all_story_tags_lookup()), {})
        names = tuple(
          f"{expected.name}-{site}" for site in expected.default_story_names
        )
        self._assert_stories(
          self._create(benchmark_cls), _EXPECTED_V11_CANONICAL_STORIES, names
        )
        self._assert_stories(
          self._create(benchmark_cls, fast=True),
          _EXPECTED_V11_FAST_CANONICAL_STORIES,
          names,
        )

  def _media_story(self) -> WebPowerMediaPlaybackStory:
    benchmark = self._create(
      WebPowerBenchmark, "--stories=media-playback-youtube"
    )
    self.assertEqual(len(benchmark.stories), 1)
    story = benchmark.stories[0]
    assert isinstance(story, WebPowerMediaPlaybackStory)
    return story

  def test_v11_media_playback_action_sequence(self) -> None:
    for page_state in _V11_MEDIA_PAGE_STATES:
      with self.subTest(page_state=page_state.name):
        story = self._media_story()
        recorder = _ActionRecorder(page_state.js_results)
        run = mock.Mock()
        run.actions.side_effect = recorder.actions
        run.browser_platform.is_android = True
        with (
          mock.patch.object(
            media_playback, "AndroidVolumeController"
          ) as volume_controller_cls,
          mock.patch.object(
            WebPowerMediaPlaybackStory,
            "_evaluate_with_gesture",
            side_effect=recorder.evaluate_with_gesture,
          ),
        ):
          story.setup(run)
          story.run(run)

        volume_controller_cls.assert_called_once_with(run.browser_platform)
        configure_volume = volume_controller_cls.return_value.configure_volume
        configure_volume.assert_called_once_with(VolumeMode.ON)
        self.assertEqual(recorder.blocks(), page_state.expected_actions)

  def _run_android_scroll(
    self, story: WebPowerScrollStory
  ) -> tuple[_ActionRecorder, mock.Mock, list[str]]:
    """Runs a scroll story on a mocked Android device.

    Returns the recorded actions, the mocked platform and the contents of
    every file pushed to the device.
    """
    recorder = _ActionRecorder()
    run = mock.Mock()
    run.actions.side_effect = recorder.actions
    platform = run.browser_platform
    platform.is_android = True
    platform.display_resolution.return_value = _DISPLAY
    pushed_files: list[str] = []
    platform.push.side_effect = lambda local, remote: pushed_files.append(
      local.read_text(encoding="utf-8")
    )
    story.setup(run)
    story.run(run)
    return recorder, platform, pushed_files

  def test_v11_android_scroll_sequence(self) -> None:
    benchmark = self._create(WebPowerBenchmark, "--stories=#scroll")
    self.assertEqual(
      tuple(s.name for s in benchmark.stories), _EXPECTED_V11_SCROLL_STORIES
    )
    for story in benchmark.stories:
      with self.subTest(story=story.name):
        assert isinstance(story, WebPowerScrollStory)
        expectation = _EXPECTED_V11_CANONICAL_STORIES[story.name]
        recorder, platform, pushed_files = self._run_android_scroll(story)

        self.assertEqual(
          recorder.blocks(),
          (
            ("Generate_Scrolls", None, ()),
            ("Push_Scrolls", None, ()),
            *_scroll_setup_actions(
              expectation.site, expectation.stabilization_time.total_seconds()
            ),
            *_scroll_run_actions(),
          ),
        )
        platform.path.assert_called_once_with(_REMOTE_EVEMU_PATH)
        remote_file = platform.path.return_value
        platform.push.assert_called_once_with(mock.ANY, remote_file)
        platform.sh.assert_called_once_with("uinput", remote_file)
        platform.rm.assert_called_once_with(remote_file, missing_ok=True)
        local_file = platform.push.call_args.args[0]
        self.assertFalse(local_file.exists())

        self.assertEqual(len(pushed_files), 1)
        evemu_data = pushed_files[0]
        self.assertEqual(
          len(evemu_data.splitlines()), _EXPECTED_V11_EVEMU_LINE_COUNT
        )
        digest = hashlib.sha256(evemu_data.encode("utf-8")).hexdigest()
        self.assertEqual(digest, _EXPECTED_V11_EVEMU_SHA256)

  def test_v11_ios_scroll_sequence(self) -> None:
    benchmark = self._create(WebPowerBenchmark, "--stories=scroll-cnn")
    self.assertEqual(len(benchmark.stories), 1)
    story = benchmark.stories[0]
    assert isinstance(story, WebPowerScrollStory)
    recorder = _ActionRecorder()
    run = mock.Mock()
    run.actions.side_effect = recorder.actions
    run.browser_platform = mock.MagicMock(spec=IOSPlatform)
    platform = run.browser_platform
    platform.display_resolution.return_value = _DISPLAY

    story.setup(run)
    story.run(run)

    self.assertEqual(
      recorder.blocks(),
      (
        ("Init_WDA", None, ()),
        *_scroll_setup_actions("cnn", 10),
        *_scroll_run_actions(),
      ),
    )
    platform.wda.ensure_session.assert_called_once_with()
    platform.wda.swipe_sequence.assert_called_once_with(
      list(_EXPECTED_V11_IOS_SWIPES),
      duration=dt.timedelta(milliseconds=750),
      settle_duration=dt.timedelta(milliseconds=150),
      lift_duration=dt.timedelta(milliseconds=100),
    )
    platform.push.assert_not_called()
    platform.sh.assert_not_called()


if __name__ == "__main__":
  test_helper.run_pytest(__file__)
