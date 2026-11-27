# Using Your Own Power-Measurement Tools with Web Power

This guide is for engineers who already have power-measurement hardware, and
software to drive it, and who want to measure the [Web Power](README.md)
benchmark with that setup. It explains why the benchmark itself has to start
and stop the measurement, and the two ways of getting
[Crossbench](../../../README.md) to do so with your tooling:

1. **A built-in shell probe**, which requires no code changes. Start here.
2. **A dedicated probe**, modelled on the existing one for Google's Kibble.

## Why the Benchmark Has to Drive the Power Measurement

Power can be measured with on-device hardware, such as the On Device Power
Monitor (ODPM) found in Pixel phones, or with external hardware, such as
Google's Kibble. Other companies have their own proprietary hardware, and
matching software to manage it.

Whichever instrument is used, a benchmark has one specific portion of its run
that has to be measured. A measurement can miss this window in two ways:

- **Measuring outside the window** includes work that is not part of the
  benchmark, such as loading the page or cleaning up afterwards.
- **Not covering all of the window** leaves out part of the workload.

Either way, the result is invalid: it cannot be compared to other runs of
that benchmark.

Hitting the window is a machine's job, not a human's:

- People are not good at consistently and accurately starting and stopping a
  measurement.
- People cannot be relied upon to look at a power trace afterwards and decide
  objectively when the benchmark started or stopped.

Crossbench, on the other hand, knows exactly when the window opens and
closes. It should therefore be Crossbench that starts and stops the
measurement.

Web Power's default measurement already works this way for on-device rails:
it reads them through Perfetto, using a query per device model (see
[ODPM](README.md#odpm)). The rest of this guide covers the other case, an
instrument that is driven by its own software.

## Terminology: Stories, Probes and the Measured Window

A **story** is a scripted browser workload. In Web Power, it is a scenario
applied to a site, such as `idle-cnn` or `scroll-msn`. A story has three
phases: its *setup* loads the page and waits for it to settle, its *run* is
the actual workload (idling, scrolling, loading pages or playing media), and
its *teardown* cleans up. **The run phase, and only the run phase, is the
measured window.** Web Power repeats every story five times by default; each
repetition is a separate *run*, in a freshly started browser.

A **probe** is Crossbench's unit of measurement. Probes are attached to a
benchmark. Crossbench then calls each of them back at fixed points of every
run, and each probe produces its results at the end. These are the callbacks,
in order, with what happens in between:

```text
setup
    (the browser starts)
start
start_story_setup
    (story setup: the page loads and settles)
start_story_run      <-- start measuring
    (story run: the measured window)
stop_story_run       <-- stop measuring
    (story teardown)
stop
    (the browser quits)
teardown
```

A probe is free to start and stop measuring at any of these points. For power,
the right pair is `start_story_run` and `stop_story_run`.

One external setup that Crossbench already supports serves as an example:
Kibble (hardware) paired with Pixel-Bits (software, "Bits" for short).
Passing `--bits-path` to Web Power attaches the
[`BitsProbe`](../../../crossbench/probes/bits/__init__.py), which starts a
Bits collection in `start_story_run` and stops it in `stop_story_run`.

You can hook your own hardware and software into the same two points.

## What Your Tool Has to Do

Crossbench waits for your tool's start command to return before it begins the
workload, and issues the stop command as soon as the workload ends. Therefore:

- **The start command should return once sampling has actually started**: not
  before, and not long after. Crossbench is blocked until it returns, so a
  start command that never returns hangs the benchmark. If your tool records
  until it is interrupted, wrap it in a script that leaves the recorder
  running in the background.
- **The stop command should stop the sampling first**, and do slow work, such
  as exporting the capture, afterwards.
- **Failures should be loud.** Have your tool exit with a non-zero status
  whenever it could not measure; a silent failure looks like a successful run.
  Crossbench then reports the run as failed, and `cb.py` itself exits with an
  error. The story still runs, and the remaining commands are still issued, so
  the stop command should cope with a measurement that never started.

## Option 1: A Shell Probe (No Crossbench Code Changes)

Crossbench ships with a probe whose callbacks do nothing but run commands of
your choosing. It comes in two flavours, both defined in
[`shell.py`](../../../crossbench/probes/shell.py):

- **`local_shell`** runs the commands on the host machine, which is where the
  software for external hardware normally lives. This is the one to use.
- **`shell`** takes the same options, but runs the commands on the device that
  hosts the browser; for an Android browser, that means on the Android device
  itself, through adb. Use it only if your measurement has to be triggered
  there.

Give the probe your tool's start and stop commands, and Crossbench invokes
them around the run phase of every story. Nothing has to be added to
Crossbench, and there is no fork to maintain.

### Example: Web Power on an Android Device

```bash
./cb.py web-power \
  --browser=adb:chrome \
  --no-probe=perfetto \
  --probe='local_shell:{
      start-story: "/path/to/tool --start XYZ",
      stop-story: "/path/to/tool --stop XYZ"
    }'
```

For every run, Crossbench executes `/path/to/tool --start XYZ` immediately
before the story's run phase, and `/path/to/tool --stop XYZ` immediately after
it. The other parts of the command line are:

- `--browser=adb:chrome` selects Chrome on the attached Android device. If
  several devices are attached, use `--browser=<serial>:chrome`.
- `--no-probe=perfetto` switches off Web Power's default measurement, as
  explained under "Running Web Power" below.

The probe configuration can also be kept in a file, and passed with
`--probe-config=<file>` instead of `--probe=...`:

```hjson
{
  probes: {
    local_shell: {
      // Issued immediately before the run phase of every story.
      start-story: ["/path/to/tool", "--start", "XYZ"],
      // Issued immediately after the run phase of every story.
      stop-story: ["/path/to/tool", "--stop", "XYZ"],
    }
  }
}
```

A command can be given as one string or as a list of arguments.

Besides `start-story` and `stop-story`, there are also `setup`, `start`, `stop`
and `teardown` commands, which are issued at the callbacks of the same names.
`./cb.py describe probe local_shell` lists them all.

### How the Commands Are Run

- **They run in the result directory of their run, not where you launched
  `cb.py`.** A relative path among the arguments is therefore resolved there,
  whereas a relative path to your tool is resolved where you launched
  `cb.py`. Absolute paths avoid the difference, so prefer them for both.
- **They are not passed through a shell.** Each command is split into
  arguments and executed directly, so pipes, redirections, `&&`, variables and
  wildcards reach your tool as literal arguments. Anything of that kind
  belongs in a script, which the command then invokes.
- **They are identical for every run.** Crossbench does not substitute
  anything into them, so the same command line is issued for every repetition
  of every story. If your tool stores its capture under a fixed name (`XYZ`
  above), make sure that a run does not overwrite the one before it; the next
  section shows an easy way.
- **Every command is issued once per run.** That includes `setup` and
  `teardown`: none is issued only once per invocation of Crossbench. Do
  one-off preparation, such as starting a daemon, before launching Crossbench.

### Where the Commands' Output Goes

Crossbench saves only what the commands print. The stdout and stderr of every
command go to the result directory of its run:

```text
results/latest/<browser>/stories/<story>/<repetition>/<cache-temperature>/
  local_shell/
    start_story_run.stdout.txt
    start_story_run.stderr.txt
    stop_story_run.stdout.txt
    stop_story_run.stderr.txt
```

Anything else that your tool produces, such as a capture file, is up to the
tool to manage: Crossbench does not collect these outputs, move them or clean
them up. There are two easy ways of keeping the measurements of different runs
apart: have the stop command print the measurement, which files it with the
story and repetition that it belongs to, or have your tool write the output of
each start into a new directory.

### One Prompt to Expect

Because Web Power repeats its stories, the probe issues this warning when
Crossbench starts:

```text
Probe=local_shell cannot merge data over multiple repetitions=5.
```

It only says that the outputs stay separate for each run, as shown above. By
default, Crossbench asks you to confirm that you want to continue; do so. For
unattended runs, `--env-validation=warn` turns this prompt, and every other
environment prompt, into a logged warning, so check the log for the others.

## Option 2: A Dedicated Probe

A shell probe treats your tool as a black box. Once you want more than that,
such as checking the setup before anything runs, giving each capture a name of
its own, or parsing and reporting the results, write a probe for your tool. At
its core, it still just invokes one command when the story's run phase starts,
and another when it stops. The commands that it invokes from its per-run
callbacks run under the same conditions as a shell probe's: in the result
directory of the run, without a shell, and blocking Crossbench until they
return.

### The Example to Follow: `BitsProbe`

[`BitsProbe`](../../../crossbench/probes/bits/__init__.py) is specific to
Google's hardware, but what it does at each point is a pattern that carries
over to other tools:

| When | What it is for | What `BitsProbe` does there |
| --- | --- | --- |
| Once, before any browser is started (`Probe.setup`) | One-off preparation. If your tool needs a daemon that is shared across all runs, this is a good time to start it. There is no matching callback at the end, so stopping it is up to you. | Starts the background service of Bits unless it is already running, and determines which device to measure. |
| When attached to a browser (`Probe.validate_browser`) | Rejecting a setup that your tool cannot measure, before anything runs. | Refuses browsers that are not on Android. |
| `start_story_setup` | Per-run preparation. It happens before the measured window opens, so it may be slow. | Records the offset between the clocks of host and device, so that traces can be aligned later. |
| `start_story_run` | Starting the measurement. | Starts a collection, and returns once it is sampling. |
| `stop_story_run` | Stopping the measurement, then saving its data, for example by downloading it from the measurement hardware and writing it to files on the host. | Stops the collection, then exports its averages and a trace into the result directory of the run. |
| `teardown` | Telling Crossbench which files the run produced. | Returns the paths of the files in that directory as the result of the probe. |

### Writing Your Probe

A probe consists of two classes: the `Probe`, which holds the configuration
and exists once, and its `ProbeContext`, which is created for each run and
receives the callbacks. Two existing probes show what they look like:

- [`shell.py`](../../../crossbench/probes/shell.py) is the smallest starting
  point. It holds the shell probe of Option 1, which does nothing but run one
  command at each callback.
- [`BitsProbe`](../../../crossbench/probes/bits/__init__.py) is a complete
  power probe, with everything in the table above.

A probe for your tool needs little more than this:

- A `config_parser()` that declares its options, such as the path of your
  tool.
- A `ProbeContext` whose `start_story_run()` invokes your tool's start
  command, and whose `stop_story_run()` invokes its stop command.
- A `teardown()` that returns the paths of the files that the run produced.

### Making the Probe Available

To make your probe available to `--probe`, add its class to
`GENERAL_PURPOSE_PROBES` in
[`crossbench/probes/all.py`](../../../crossbench/probes/all.py), keeping that
list sorted by probe name. Tests in the style of
[`test_bits.py`](../../../tests/crossbench/probes/test_bits.py) are expected
with it. For reporting at the end of a benchmark, override
`Probe.merge_browsers()` and `Probe.log_browsers_result()`;
[`WebPowerProbe`](../../../crossbench/benchmarks/web_power/probe.py) shows how
Web Power turns the per-run Bits averages into its score table.

Unlike a shell probe, this route means carrying code, either contributed to
Crossbench (see [CONTRIBUTING.md](../../../CONTRIBUTING.md)) or kept in a fork
of your own.

## Running Web Power and Scoring the Results

### Running Web Power

- **Switch off Web Power's default measurement** with `--no-probe=perfetto`.
  By default, Web Power records a Perfetto trace on the device. Tracing
  consumes power itself, so it should not run while another instrument is
  measuring. Web Power arranges this automatically for Bits; for your own
  probe, you have to ask.
- **Leave everything else at its default** for score-collecting runs (see
  [Canonical Configuration Runs](README.md#canonical-configuration-runs)).
- **Smoke-test the plumbing first** by adding `--stories=idle-cnn --fast`,
  which runs a single, shortened story once. (Note that this is for smoke
  testing only; the scores produced by a `--fast` run are not valid.)

### Results and Scores

Every run has a result directory of its own:

```text
results/latest/<browser>/stories/<story>/<repetition>/<cache-temperature>/
```

For the first repetition of `idle-cnn`, it ends in
`stories/web-power-idle-cnn/0/0_default/`. Where your tool writes its
measurements is up to your tool; one that is filed in this directory stays
with the story and repetition that it belongs to.

Crossbench does not interpret your tool's output, unless you write a dedicated
probe that does. In particular, the power columns of Web Power's own score
table only show `nan` when your tool does the measuring: turning its output
into scores is up to you. If you want them to be comparable with Web Power's
own, compute them the same way: the average power in milliwatts over the
measured window of each run, and then, for each story, the mean of the
repetitions, discarding the highest and the lowest value when there are five
or more.
