# LoadLine 2 WebAPI Benchmark

LoadLine 2 WebAPI is a modification of the [LoadLine 2 benchmark](README.md)
that doesn't use any Chrome- or Android-specific features, relying instead on
pure WebAPI. This makes it possible to run the benchmark on e.g. iOS devices,
but the setup is a little bit more complicated.

*** note
Note: LoadLine 2 WebAPI scores are not directly comparable to LoadLine 2 scores!
When comparing devices, make sure you run the same modification of LoadLine on
all of them.
***

## Network setup

Page loading speed heavily depends on the network latency, so making the
network connection as predictable as possible is crucial to getting
consistent results. We recommend using Ethernet connection to the device,
because it provides stable network latency. While it's possible to run the
benchmark via WiFi, the noise level will likely be so high that you won't be
able to make any meaningful conclusions. The following instructions assume you
use Ethernet connection.

## Prerequisites (one-off setup)

* If running on iOS, first follow the
  [Safari on iOS](../../../README.md#safari-on-ios) instructions.
* Follow the
  [WPR over Ethernet Prerequisites](../../../README.md#prerequisites)
  instructions.

## Running the benchmark

* Follow the
  [Starting WPR over Ethernet](../../../README.md#starting-the-network)
  instructions.
* Run `./cb.py loadline2-webapi-phone --browser <browser>`, where browser can
  be one of `android:chrome` or `ios:safari`. This command also requires root on
  the host. Alternatively, you can run the tablet version with
  `./cb.py loadline2-webapi-tablet --browser <browser>`.
