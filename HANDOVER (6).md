# Phase 1 handover — §1–5 data layer

Scope built: models, wire codec, demultiplexer, per-vehicle streams, state
timeline, derived velocity, link layer. Ground station app only, per the
scope answer. CSV primary with the codec swappable (§3.11 option 1 kept
open, §4.2 honoured).

Everything below `link.py` is free of Qt and pyserial. `test_phase1.py`
runs 40 checks against §3–§7.5.2 with no hardware and no display —
`python3 test_phase1.py`.

---

## 1. Blocking firmware mismatch — the state set

The rocket `.ino` state machine ends at `IMPACT`. §5.1 requires eight
states ending `LANDING → RECOVERY`, and `IMPACT` is not among them.

`IMPACT` is **rejected** as an unknown state string rather than quietly
aliased to `LANDING`. Aliasing was the tempting option and is the wrong
one: §1.5 exists to stop the data being reinterpreted anywhere between
the sensor and the screen, and a ground-side rename is exactly that. The
rejection surfaces in the malformed counter, which is the visible signal
that firmware and spec disagree.

Consequence while the firmware is unchanged: **the rocket's packets are
rejected wholesale once it leaves DESCENT**, and `RECOVERY` never
arrives, so the §10.5/§11.5 file-transfer guards can never open. This
needs a firmware change before a real flight, not a ground-side patch.

## 2. Interface decisions the spec left open

These are implemented as stated and must be given to the firmware owners
verbatim — both sides have to agree exactly or nothing decodes.

**Line prefixes** (§3.12 requires reserved prefixes, names them nowhere):

| Prefix | Line type |
|---|---|
| `$T` | telemetry (§4.1) |
| `$H` | receiver heartbeat (§3.4) |
| `$F` | file-transfer frame (§11.5) |
| `$C` | uplink command (§10.5) |

**Checksum coverage** (§4.1 field 21). Taken literally as "XOR of
preceding bytes": every byte from the first character of the line up to,
but not including, the comma before the checksum. Includes the `$T`
prefix; excludes the checksum itself; **excludes receiver-appended
trailing fields**.

That last exclusion is required, not incidental. §4.3 wants the vehicle's
checksum verified end to end and §1.5/§3.4 forbid the receiver touching
vehicle data. If the receiver appended RSSI inside the checksummed region
it would have to recompute, which masks exactly the LoRa-hop corruption
the check exists to catch.

**Trailing-field format** (§4.7). `KEY=VALUE` preferred — e.g.
`RSSI=-97.5,SNR=8.2`. Bare positional values are accepted and assigned
RSSI then SNR in order, because that is the likely first implementation,
but please ask for `KEY=VALUE`: positional extras break the moment anyone
adds a third field.

**Command framing.** `$C,<TARGET>,<SEQ>,<COMMAND>,<CHECKSUM>`, addressed
to a vehicle, never to a radio (§3.4, §10.8). The sequence number is not
decoration — without it an ack for the first attempt is indistinguishable
from an ack for a resend, which is the precise ambiguity that fires
`ACT_SEPARATE` twice (§10.7).

## 3. §3.11 throughput — measured, not estimated

A representative encoded packet is **144 bytes** including newline, 163
with RSSI/SNR appended. At 10 Hz that is **13.0 kbps per channel** against
roughly 5.5 kbps at SF7/125 kHz — **2.4× over budget**, confirming the
spec's own ~2× figure.

CSV is in place as decided, and `PacketCodec` is the swap point: implement
`decode`/`encode` against `FIELD_SPEC` and nothing downstream changes.
The decision is still live, and it is cheaper to take before the firmware
CSV writer is finished than after.

## 4. Firmware work this layer assumes exists

Flagged per §3.4's own note. None of it is ground-station work.

- **Ground receiver Teensy** (all five of §3.4): forward unmodified,
  append per-channel RSSI/SNR after field 21, route uplink by target
  vehicle, emit `$H` heartbeat at ≥1 Hz, buffer to serialise near-
  simultaneous packets. The app already parses and acts on all five.
- **Both vehicles**: 21-field packet per §4.1 — note this **removes**
  velocity from the wire (now ground-derived, §6.4) and **adds** GNSS,
  accel, and gyro fields the current firmware does not send.
- **Vehicle flash persistence** (§10.3) for the refresh continuity check.
  `StateTimeline.arm_refresh()` takes the worst-case error as an argument
  rather than hardcoding 100 ms, so if §10.3.1's advice is taken and the
  time tick drops to 1 Hz, pass `1.0` and the operator sees the figure
  that is actually true of the firmware flying.

## 5. Deliberate deviation from §3.10

§3.10 reads as "parse on the reader thread, queue the results". Built
instead: reader thread does serial I/O only and queues raw lines; the
demultiplexer runs on the UI thread as the queue drains.

`TelemetryDemux` owns counters, packet-count continuity, loss tiering and
the timeline — all mutable, all read by the dashboards. Parsing on the
reader thread puts that state on one thread and its readers on another,
which is the shared mutable state §13.5 rules out, and would need a lock
on every counter read.

The requirement §3.10 exists to protect is its own last line — "serial
I/O never blocks the render thread" — and that holds: the blocking
`readline()` is on the reader thread, and decoding a 144-byte line costs
microseconds against the ≥40 lines/s requirement. Revisit only if the
format becomes an expensive binary decode.

## 6. Judgement calls worth a second opinion

- **Loss thresholds in packets, not seconds.** §7.5.2 gives both; packets
  is what is actually measured, and the second figures are only correct
  at 10 Hz — they would be wrong the moment §3.11 option 3 drops the
  downlink to 3–5 Hz. Configurable via `LossThresholds`.
- **Derived velocity returns `None`, never `0.0`, while its window
  fills.** A zero descent rate on the pad and an unpopulated window look
  identical on a gauge and only one is a measurement. The gauge must
  render `None` as "—", not as a number.
- **Least-squares slope over the §6.4 window**, not last-minus-first —
  the endpoint difference uses the same window but discards the samples
  between, keeping most of the noise the window was meant to suppress.
- **The window is dropped across a GAP or OUTAGE** (`on_gap()`). A slope
  fitted across a two-second hole is not a velocity, and under a
  parachute it would read as a plausible number rather than an obvious
  error.
- **Inferred states (§5.6) are timestamped at the later endpoint.** The
  state has no received time; this is the honest bound, and the
  `INFERRED` marking tells the reader not to treat it as measured.
- **Continuity-break latches.** Once a post-refresh check fails, §10.3
  says every subsequent timestamp is untrustworthy, so it does not clear
  on the next good packet.

## 7. What is not built yet

Phases 2–4, in the order agreed: §2 launcher and §6–9 dashboards and
graph engine; §10 Command and §11 Experiment; §12 logging and §13.6
simulator.

Note that **none of the old `flight_sim.py` survives** — §13.6 needs a
simulator that emits multiplexed 10 Hz for both vehicles through the same
parser (§2.2), plus heartbeat loss, malformed packets, all three loss
tiers, out-of-order packets, an APOGEE lost entirely, refresh dropouts,
unacknowledged commands and a mid-transfer failure. `PacketCodec.encode`
exists for exactly that: the simulator emits wire lines, so it exercises
the real parser's failure paths rather than bypassing them.

---

## 8. Running it today

The GUI cannot launch yet — `app.py`, `widgets.py` and `telemetry_plot.py`
still import `RocketTelemetry` / `RocketState` / `CanSatState`, which §4.1
and §5.1 collapsed into `TelemetryPacket` and one 8-member `FlightState`.
They are rewritten in Phase 2.

Two things do run:

```bash
python3 test_phase1.py                      # 40 checks, no deps
python3 link_monitor.py --demo              # synthetic flight, no deps
python3 link_monitor.py --demo --faults -v  # + loss, corruption, duplicates
python3 link_monitor.py --list              # serial devices + stable ids
python3 link_monitor.py --port /dev/ttyACM0 # live, against the receiver
```

`link_monitor.py` runs the real §4 parser with no Qt, which is what the
Qt-free boundary was for. Point it at the receiver as soon as the
firmware emits anything: it names the failing field and the reason, which
is a great deal easier to work against than a graph that silently shows
nothing. Note it also verifies the four interface decisions in §2 above
are agreed — if the prefixes or the checksum coverage differ, every line
comes back `FIELD_COUNT` or `CHECKSUM` immediately.

`--demo` is a stand-in, not the §13.6 simulator. It covers four failure
modes; §13.6 needs considerably more and arrives in Phase 4.

---

# Phase 2 handover — §2, §6–§9, §7 graph engine

14 new files. `test_phase2.py` adds 60 checks (axes, trajectory,
reconciliation, decimation) that run with no Qt and no pyqtgraph:

```bash
python3 test_phase2.py
python3 main.py            # mode-selection screen (§2.1)
```

Live mode needs `PyQt6`, `pyqtgraph` and `pyserial`. Simulator Mode is
selectable but says plainly that it arrives in Phase 4 rather than
opening an empty window.

## 9. §8's altitude and velocity bases are not simultaneously satisfiable

Found while testing `FlightProfile` against the 1 km target.

§8 gives a 1 km apogee and, separately, ~150–200 m/s at burnout. Under
the gravity-only piecewise model §7.5.5 mandates, a vehicle at 180 m/s
coasts v²/2g ≈ 1,650 m *after* burnout and reaches ~1,900 m — nearly
double the target. The missing ~900 m is aerodynamic drag, which a
constant-acceleration model does not represent.

Resolved by making apogee the input and deriving burnout velocity from
it (~127 m/s), because apogee is the mission requirement and is what
§8's 0–1,300 m altitude extent is built on.

**What this means operationally:** the §7.5.5 ballistic arc drawn across
an outage is also drag-free, so it overestimates altitude mid-span. Over
a 2 s outage (the §7.5.2 threshold) the error is about a metre. Over a
20 s outage during high-speed coast it could be tens of metres. The arc
is dotted and never logged (§7.5.8), so it is presented as an estimate —
but if long outages prove common on the real link, adding a quadratic
drag term to `trajectory.py` is the fix, and it applies to both the
reconstruction and the Phase 4 simulator automatically because §7.5.5
makes them share the module.

## 10. Phase 2 judgement calls

- **`reconcile()` refuses to fabricate a peak.** If the fitted apex falls
  outside the outage window, the endpoints describe a monotonic segment
  and a parabola would invent an apogee that did not occur. A straight
  line is substituted and `Arc.exact` goes False with the reason
  attached. Same for a powered segment with no entry velocity — two
  points do not determine it, so it does not guess.
- **Min/max decimation, not stride sampling.** At full-mission zoom one
  pixel column spans hundreds of samples, so stride sampling drops a
  40 ms burnout spike with ~99% probability and the operator sees a clean
  trace. Min/max guarantees both extremes of every column survive.
- **Pause holds samples and replays them** rather than dropping them.
  Dropping would surface as a GAP or OUTAGE and draw a reconciliation
  segment across data the app actually received — inventing an estimate
  to cover a hole of its own making, which §9.4 forbids.
- **Gauges render `None` as an em dash, never 0.00.** A derived velocity
  whose window has not filled is not a measurement of zero.
- **Scroll is swallowed, not ignored.** §7.2 forbids wheel zoom; the
  event is accepted so it also cannot bubble to a parent scroll area and
  move the page under the cursor.
- **One derived-velocity estimator per vehicle.** The vehicle page owns
  it and returns the value; Overview reuses it. Two estimators would hold
  different windows and could show different numbers for the same
  instant.
- **One entry point, mode chosen at runtime.** The old main.py/main_sim.py
  split is how the previous build ended up calling a `step()` signature
  the simulator did not have — the two paths were only ever exercised
  separately.

## 11. Deleted

`telemetry_plot.py`, `widgets.py`, the old `app.py`, `main_sim.py`,
`flight_sim.py`, `serial_reader.py`. `theme.py` survives unchanged and is
now at `ground_station/theme.py`.

## 12. Not yet verified

PyQt6 and pyqtgraph are not installed in the environment these files were
written in, so everything below `link.py`, `plot.py` and the widgets is
tested and everything in them is only statically checked — imports
resolved, syntax parsed, relative imports verified against real names.
Expect the first live run to need small fixes in layout and in the
pyqtgraph API surface specifically. The §7 rules those widgets implement
are unit-tested where they could be separated out, which is why
`reconcile.py`, `decimate.py`, `trajectory.py` and `axes.py` are separate
modules rather than methods on the graph widget.

---

# Phase 3 handover — §10 Command, §11 Experiment

`test_phase3.py` adds 104 checks (guards, ack lifecycle, mode
confirmation, experiment parsing, transfer protocol). All three suites:

```bash
python3 test_phase1.py && python3 test_phase2.py && python3 test_phase3.py
```

Both dashboards are now enabled in the sidebar.

## 13. Provenance note

`commands.py`, `actuators.py`, `experiment/parser.py`,
`experiment/transfer.py` and `test_phase3.py` were already present in the
workspace at the start of this phase and were not written in the session
that produced this note. They were reviewed and verified rather than
rewritten — the 104 checks pass and the modules match §10 and §11
closely. Newly written here: `pages/command.py`,
`experiment/profile_plot.py`, `pages/experiment.py`,
`sample_experiment.csv`, and the `app.py` / `link.py` changes below.

## 14. Two integration bugs found while wiring the UI

**Duplicate sequence counters (the serious one).** `CommandCentre` owns
the §10.7 acknowledgment lifecycle and assigns a sequence per command.
`LinkSupervisor.send_command` was assigning its *own* independent
sequence to the wire frame. Every command would therefore have gone out
carrying a number the ack matcher had never issued, so every command
would have sat permanently unacknowledged — and §10.7 requires an
unacknowledged command look visibly different from a successful one, so
the operator would have seen every single command fail while the vehicle
replied correctly to all of them. The predictable response is to resend,
and for `ACT_SEPARATE` that fires the mechanism twice, which is the exact
failure §10.7 exists to prevent.

Fixed by making `send_command` accept the sequence from `CommandCentre`.
The local counter remains only as a fallback for callers that do not
track acks.

**Progress scale mismatch.** `parse_experiment_file` reports 0.0–1.0; the
UI progress bar expects 0–100. Scaled in the worker rather than changing
the parser, which is deliberately free of UI assumptions.

Also corrected: `split_on_time_gaps(xs, ys, times)` was being called with
the arguments in the wrong order.

## 15. `sample_experiment.csv`

§13.6 requires a sample CSV shipping with malformed rows, out-of-range
values and a time gap. Generated from the same 1 km profile as
`trajectory.py`, 467 rows, and it exercises every §11.7 path:

- 3 rejected: 2-column row, non-numeric altitude, 4-column row
- 3 flagged but kept: negative aerosol, negative altitude, non-monotonic
  mission time
- 1 time gap: 40.6 s → 47.0 s, which §11.10 renders as a line break
- header row, CRLF endings, a trailing comma and trailing blank lines,
  all tolerated

The ascent and descent legs deliberately differ (descent reads ~18%
higher), so the §11.11 split shows two visibly distinct traces rather
than one line retraced — which is what makes the split worth having.

## 16. Phase 3 judgement calls

- **Actuator "actual" shows `not reported`, never the commanded value.**
  §10.1 forbids displaying commanded as actual, and the fallback is most
  tempting exactly when it is most dangerous — when no echo has come
  back. A commanded/actual disagreement raises a visible warning, since
  that is what a jammed mechanism looks like from the ground.
- **±180° buttons are disabled off-home rather than clamped**, per
  §10.1.1's reasoning that a control which is nearly always impossible
  trains operators to ignore clamp warnings.
- **CanSat actuator panels are disabled, not hidden.** The operator can
  see the controls exist and why they are unavailable, rather than
  wondering whether the page failed to load.
- **The Command page's QTimer only refreshes.** It drives the arming
  countdown and ack ages. It never calls `send()` — §1.4's "no command is
  ever sent automatically" is checkable by grepping this file for
  `send`, and every hit is inside a `clicked` handler.
- **The retrieval half degrades to an explained disabled panel** while
  the analysis half stays fully live, per §11.3. An operator with a file
  on disk never touches retrieval.

## 17. Still not verified

Same caveat as Phase 2, now covering `pages/command.py`,
`pages/experiment.py` and `experiment/profile_plot.py`: PyQt6 and
pyqtgraph are not installed here, so those three are statically checked
only. Everything they call is unit-tested.

Phase 4 remains: §12 logging and the §13.6 simulator.

---

# Phase 4 handover — §12 logging, §13.6 simulator

Final phase. `test_phase4.py` adds 62 checks; all four suites pass:

```bash
for t in 1 2 3 4; do python3 test_phase$t.py; done
python3 main.py --sim          # simulator mode, no hardware
python3 main.py                # mode-selection screen
```

`--log-dir` sets where session logs go (default `logs/`).

## 18. §13.6 is fully covered, and verified rather than asserted

`test_phase4.py` runs the entire simulated mission through the **real**
parser and checks each §13.6 item actually reaches the app:

| §13.6 requirement | How it is verified |
|---|---|
| 10 Hz both vehicles, one source | >500 packets accepted per vehicle |
| receiver heartbeats + loss | heartbeat count, then a scheduled silence |
| malformed packets | FIELD_COUNT and NON_NUMERIC both observed |
| all three loss tiers | DROPOUT, GAP, OUTAGE all observed |
| per-vehicle RSSI variation | fade window measurably lower than baseline |
| out-of-order packets | DUPLICATE / TIME_REGRESSION observed |
| APOGEE lost entirely | timeline marks it `inferred, not received` |
| refresh dropouts | scheduled distinctly from a plain outage |
| unacknowledged commands | armed, fires once, never auto-retried |
| transfer failing midway | armed in the schedule; §11.5 resume tested in Phase 3 |
| sample experiment CSV | `sample_experiment.csv`, Phase 3 |

**Faults are scheduled, not random.** Two reasons. A reproducible failure
can be debugged — "it sometimes draws the wrong arc" is not actionable.
And §13.6 names specific scenarios: at 1% random loss an eight-packet
APOGEE window survives ~92% of the time, so a random simulator would
almost never produce the one case it is required to produce.

The engine is Qt-free (`simulator/engine.py`); `simulator/source.py` is a
thin wrapper that turns engine output into signals. That split is why
§13.6 is testable at all.

## 19. The simulator cannot drift from live mode

Verified statically: `SimulatedSource` exposes every signal and method
`LinkSupervisor` does, and `app.py` uses nothing outside that set. So
`main.py` picks one object and contains **no branch on mode** below that
line. If the interfaces ever diverge, simulator mode stops exercising the
code that runs in flight and the simulator quietly becomes a demo.

Likewise the simulator imports `FlightProfile` from
`graphs/trajectory.py` rather than modelling its own flight, per §7.5.5.
Without that, the reconciliation arc would be validated against different
physics than the flight it reconstructs — invisible in exactly the test
built to catch it.

## 20. A bug the §13.6 work caught

`non_numeric()` was corrupting a line that already carried the receiver's
appended `RSSI`/`SNR`, so it recomputed the checksum across the appended
fields. The packet then failed as CHECKSUM rather than NON_NUMERIC — the
non-numeric rejection path was never once exercised while the test
appeared to cover it.

Fixed by corrupting the **vehicle's** line before the receiver appends
anything, which is also the real order of events (§3.4 appends after
field 21; §4.3 makes the checksum the vehicle's, covering only its own
fields). This is the second time in this project that a fault-injection
bug made a rejection path silently untested — the first was the duplicate
branch in `link_monitor.py`. Worth remembering that fault injectors need
tests as much as the code they exercise.

## 21. §12 logging notes

- **Logging is wired to `raw_line`, not to the packet signals.**
  `raw_line` fires on receipt, ahead of the demultiplexer, so malformed
  lines reach the log even though they never become packets. Wiring to
  the packet signals would produce a log containing only the lines that
  parsed — useless for a flight where parsing was the problem.
- **Logs open before the source does**, so the first arriving line is
  already being recorded.
- **Lines are written verbatim, never re-encoded from the parsed
  packet.** Re-encoding would normalise away whatever firmware quirk
  produced them, which is usually the thing being hunted.
- **Host clock, not MISSION_TIME.** They answer different questions, and
  the difference between them is the only way to measure link latency or
  spot a vehicle whose clock jumped.
- **Line-buffered, flushed at 1 Hz, no `fsync`.** §12.5 guards against an
  application crash, which flushing to the OS already covers; `fsync` at
  20 lines/s would add a disk round-trip to the receive path for a gain
  only on a power cut. §12.6's "openable while running" rules out
  exclusive locks and temp-file-and-rename.
- **Unattributed lines go to the system log**, never guessed into a
  vehicle file, so no vehicle's log contains a line that may not be its.
- **§12.3 is structural**: derived velocity has its own file, with a
  header stating nothing in it came off the vehicle. `None` is written as
  an empty field, never 0 — matching §6.4's treatment on the gauge.

## 22. Project status

All four phases delivered. Outstanding items are unchanged and none are
ground-station work:

1. **Firmware state set** (§1 of this handover) — still the blocker. The
   rocket `.ino` ends at `IMPACT`; §5.1 needs `LANDING` and `RECOVERY`,
   and §11.5's guards cannot open without `RECOVERY`.
2. **Ground receiver firmware** — all five §3.4 duties. The app already
   parses and acts on every one.
3. **§3.11 wire format** — 2.4× over budget at 10 Hz, measured. CSV is in
   place and `PacketCodec` is the swap point.
4. **§8's drag-free inconsistency** (§9 above) — decide whether long
   outages are common enough to warrant a drag term in `trajectory.py`.

Still unverified: the six PyQt/pyqtgraph UI modules, which are statically
checked only because neither library is installed in the build
environment. Everything they call is unit-tested. Expect small layout and
pyqtgraph API fixes on the first live run.
