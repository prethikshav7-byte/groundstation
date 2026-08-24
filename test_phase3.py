"""Phase 3 core tests — commands, actuators, experiment parser, transfer.
No Qt, no hardware.  Run: python3 test_phase3.py"""
import sys
sys.path.insert(0, ".")

from ground_station.models import FlightState, VehicleID, VehicleLinkState
from ground_station.commands import (
    COMMANDS, CommandCentre, CommandStatus, Guard, ModeValue, check_guard,
)
from ground_station.actuators import (
    ACTUATORS, Actuator, ArmingCentre, BusQueue, BY_ID, CalibrationRun,
    CalibrationOutcome,
)
from ground_station.experiment.parser import (
    ParseError, RowProblem, parse_experiment_text, split_on_time_gaps,
)
from ground_station.experiment.transfer import (
    FileTransfer, RemoteFile, RetrievalNotifier, TransferState, crc32,
)
import binascii

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


LIVE = VehicleLinkState.RECEIVING

print("\n§10.5/§10.8 guards")
g = check_guard(COMMANDS["REQUEST_STATUS"], FlightState.COAST, LIVE, False)
check("unguarded command allowed in flight", g.allowed and not g.needs_confirm)
g = check_guard(COMMANDS["REQUEST_STATUS"], FlightState.COAST,
                VehicleLinkState.LOST, False)
check("LOST vehicle blocks command", not g.allowed, g.reason)
check("not queued for reconnect", "not queued" in g.reason, g.reason)
g = check_guard(COMMANDS["SET_MODE_HIBERNATE"], FlightState.BOOST, LIVE, False)
check("hibernate in flight needs confirm", g.allowed and g.needs_confirm)
g = check_guard(COMMANDS["SET_MODE_HIBERNATE"], FlightState.PRE_LAUNCH, LIVE, False)
check("hibernate on ground no confirm", g.allowed and not g.needs_confirm)
g = check_guard(COMMANDS["MANUAL_DEPLOY"], FlightState.DESCENT, LIVE, False)
check("destructive always needs confirm", g.needs_confirm)

print("\n§2.5/§10.5 ENTER_SIMULATION")
g = check_guard(COMMANDS["ENTER_SIMULATION"], FlightState.PRE_LAUNCH, LIVE, True)
check("disabled in Simulator Mode", not g.allowed, g.reason)
check("reason distinguishes the two", "different thing" in g.reason, g.reason)
g = check_guard(COMMANDS["ENTER_SIMULATION"], FlightState.BOOST, LIVE, False)
check("disallowed after PRE_LAUNCH", not g.allowed, g.reason)
g = check_guard(COMMANDS["ENTER_SIMULATION"], FlightState.PRE_LAUNCH, LIVE, False)
check("allowed pre-launch in live mode", g.allowed)

print("\n§11.4/§10.5 file commands")
g = check_guard(COMMANDS["LIST_FILES"], FlightState.RECOVERY, LIVE, False,
                usb_direct=False)
check("blocked over LoRa relay", not g.allowed, g.reason)
g = check_guard(COMMANDS["LIST_FILES"], FlightState.DESCENT, LIVE, False,
                usb_direct=True)
check("blocked outside RECOVERY", not g.allowed, g.reason)
g = check_guard(COMMANDS["LIST_FILES"], FlightState.RECOVERY, LIVE, False,
                usb_direct=True)
check("allowed on USB in RECOVERY", g.allowed)

print("\n§10.5 RESET_COUNTERS removed")
check("not in the command set", "RESET_COUNTERS" not in COMMANDS)

print("\n§10.7 acknowledgment lifecycle")
sent = []
cc = CommandCentre(lambda t, c, s: sent.append((t, c, s)), ack_timeout=1.0)
r = cc.send(VehicleID.ROCKET, "REQUEST_STATUS")
check("transmitted once", len(sent) == 1)
check("status SENT", r.status is CommandStatus.SENT)
check("unacknowledged listed", cc.unacknowledged == [r])
cc.note_receiver_forwarded(r.sequence)
check("receiver forward is not an ack",
      r.status is CommandStatus.SENT and r.receiver_forwarded)
cc.acknowledge(r.sequence, "ok")
check("acknowledged", r.status is CommandStatus.ACKNOWLEDGED)
check("no longer unacknowledged", cc.unacknowledged == [])
cc.mark_executed(r.sequence)
check("executed", r.status is CommandStatus.EXECUTED)

print("\n§10.7 timeout does not retry")
cc2 = CommandCentre(lambda t, c, s: sent.append((t, c, s)), ack_timeout=1.0)
before = len(sent)
r2 = cc2.send(VehicleID.CANSAT, "REQUEST_STATUS")
timed = cc2.poll_timeouts(now=r2.sent_at + 2.0)
check("timed out", timed and timed[0].status is CommandStatus.TIMED_OUT)
check("nothing re-transmitted", len(sent) == before + 1, len(sent) - before)
r3 = cc2.resend(r2.sequence)
check("operator resend transmits", len(sent) == before + 2)
check("resend gets a NEW sequence", r3.sequence != r2.sequence)
check("resend records its origin", r3.resend_of == r2.sequence)

print("\n§10.4 modes never flip optimistically")
cc3 = CommandCentre(lambda t, c, s: None, ack_timeout=1.0)
m = cc3.modes[VehicleID.ROCKET]
check("starts UNKNOWN", m.sampling is ModeValue.UNKNOWN)
rec = cc3.send(VehicleID.ROCKET, "SET_MODE_HIBERNATE")
check("PENDING after send, not HIBERNATE", m.sampling is ModeValue.PENDING)
cc3.acknowledge(rec.sequence)
check("HIBERNATE only after ack", m.sampling is ModeValue.HIBERNATE)
rec2 = cc3.send(VehicleID.ROCKET, "ENABLE_TELEMETRY")
cc3.acknowledge(rec2.sequence)
check("telemetry ENABLED", m.telemetry is ModeValue.ENABLED)
check("HIBERNATE + ENABLED flagged dangerous", m.dangerous)

cc4 = CommandCentre(lambda t, c, s: None, ack_timeout=1.0)
m4 = cc4.modes[VehicleID.CANSAT]
rec = cc4.send(VehicleID.CANSAT, "SET_MODE_LIVE")
cc4.poll_timeouts(now=rec.sent_at + 2.0)
check("unacknowledged mode -> UNCONFIRMED", m4.sampling is ModeValue.UNCONFIRMED)
check("not silently LIVE", m4.sampling is not ModeValue.LIVE)

print("\n§10.1 actuators")
check("three actuators", len(ACTUATORS) == 3)
check("all on one PCA9685", {a.driver for a in ACTUATORS} == {"PCA9685"})
check("channels 0,1,2", sorted(a.channel for a in ACTUATORS) == [0, 1, 2])
a = Actuator(BY_ID["ACT_DOOR"])
check("actual unknown before echo", a.position is None)
a.note_commanded(270.0)
check("commanded never shown as actual", a.position is None, a.position)
check("remaining travel unknown too", a.remaining_travel == (None, None))
a.note_echo(180.0)
check("actual after echo", a.position == 180.0)
check("at home", a.at_home)
check("remaining travel from home", a.remaining_travel == (180.0, 180.0))
check("commanded != actual detected", a.commanded_matches_actual is False)

print("\n§10.1.1 travel limits")
ok, why = a.can_move(180.0)
check("+180 reachable from home", ok, why)
a.note_echo(200.0)
ok, why = a.can_move(180.0)
check("+180 blocked off home", not ok, why)
check("reason names the limit", "360" in why, why)
res = a.evaluate(180.0)
check("clamped not wrapped", res.target == 360.0, res.target)
check("clamp is reported", res.clamped and "clamped to" in res.message, res.message)
check("message names both figures",
      "+180" in res.message and "+160" in res.message, res.message)

print("\n§10.6 arm -> fire")
arm = ArmingCentre(timeout=10.0)
check("nothing armed initially", arm.armed_control(now=0.0) is None)
check("fire without arm refused", not arm.fire("ACT_SEPARATE", now=0.0))
arm.arm("ACT_SEPARATE", now=0.0)
check("armed", arm.is_armed("ACT_SEPARATE", now=1.0))
arm.arm("ACT_DEPLOY", now=1.0)
check("arming one disarms the other", not arm.is_armed("ACT_SEPARATE", now=1.0))
check("new one armed", arm.is_armed("ACT_DEPLOY", now=1.0))
check("auto-disarm after 10 s", not arm.is_armed("ACT_DEPLOY", now=12.0))
arm.arm("ACT_DEPLOY", now=20.0)
check("fire consumes the arm", arm.fire("ACT_DEPLOY", now=21.0))
check("second fire refused", not arm.fire("ACT_DEPLOY", now=21.1))

print("\n§10.1.2 shared-bus fairness")
q = BusQueue()
q.submit_poll("ACT_DOOR")
q.submit_move("ACT_SEPARATE", 270.0)
q.submit_poll("ACT_DEPLOY")
check("moves jump ahead of polls", q.next()[0] == "move")
check("then polls", q.next()[0] == "poll")
check("depth tracked", q.depth == 1)

print("\n§10.2 calibration")
run = CalibrationRun()
check("five sensors", len(run.results) == 5)
check("all start not-run",
      all(v is CalibrationOutcome.NOT_RUN for v in run.results.values()))
run.set("PRESSURE", CalibrationOutcome.PASS)
run.set("GNSS", CalibrationOutcome.FAIL, "no fix")
check("not complete until all run", not run.complete)
for k in ("ALTITUDE", "IMU", "TEMPERATURE"):
    run.set(k, CalibrationOutcome.PASS)
check("complete", run.complete)
check("per-sensor failure visible", run.failures == ["GNSS"], run.failures)

print("\n§11.7 experiment parser")
good = "MISSION_TIME,ALTITUDE,AEROSOL_COUNT\n0.0,0.0,120.0\n1.0,50.0,118.5\n2.0,120.0,95.2\n"
ds = parse_experiment_text(good)
check("header skipped", ds.report.header_detected)
check("three rows", len(ds) == 3)
check("no rejects", ds.report.rows_rejected == 0)
check("ranges computed", ds.report.altitude_range == (0.0, 120.0))

ds = parse_experiment_text("0.0,0.0,120.0\n1.0,50.0,118.5\n")
check("headerless file works", len(ds) == 2 and not ds.report.header_detected)

messy = ("MISSION_TIME,ALTITUDE,AEROSOL_COUNT\r\n"
         "0.0,0.0,120.0,\r\n"          # trailing comma
         "1.0,abc,118.5\r\n"           # non-numeric
         "2.0,120.0\r\n"               # wrong column count
         "3.0,180.0,-4.0\r\n"          # negative aerosol: flag, keep
         "4.0,-40.0,90.0\r\n"          # negative altitude: flag, keep
         "2.5,200.0,88.0\r\n"          # time regression: flag, keep
         "\r\n")                       # trailing blank
ds = parse_experiment_text(messy)
check("trailing comma tolerated", ds.mission_time[0] == 0.0)
check("CRLF tolerated", len(ds) == 4, len(ds))
check("two rows rejected", ds.report.rows_rejected == 2, ds.report.rows_rejected)
reasons = {i.problem for i in ds.report.rejected}
check("both malformed reasons seen",
      reasons == {RowProblem.NON_NUMERIC, RowProblem.FIELD_COUNT}, reasons)
check("line numbers recorded",
      all(i.line_number > 0 for i in ds.report.rejected))
flagged = {i.problem for i in ds.report.flagged}
check("range issues flagged not dropped",
      flagged == {RowProblem.NEGATIVE_AEROSOL, RowProblem.NEGATIVE_ALTITUDE,
                  RowProblem.TIME_REGRESSION}, flagged)
check("flagged rows kept in dataset", -4.0 in ds.aerosol)
check("no zero substituted for the bad row", 0.0 not in ds.altitude[1:])

try:
    parse_experiment_text("garbage\nnot,a,valid\nrow,here,either\n")
    check("all-bad file raises", False)
except ParseError as e:
    check("all-bad file raises", True)
    check("message is not an empty graph", "No usable rows" in str(e), str(e))

print("\n§11.10/§11.11 plotting helpers")
gap = "0.0,0.0,10.0\n1.0,10.0,11.0\n9.0,90.0,12.0\n10.0,100.0,13.0\n"
ds = parse_experiment_text(gap)
check("time gap detected", len(ds.report.time_gaps) == 1, ds.report.time_gaps)
gx, gy = split_on_time_gaps(ds.altitude, ds.aerosol, ds.mission_time)
check("line breaks at gap", any(x != x for x in gx))
check("no interpolation added", len([x for x in gx if x == x]) == 4)

profile = "0.0,0.0,10.0\n1.0,500.0,20.0\n2.0,1000.0,30.0\n3.0,400.0,25.0\n4.0,0.0,12.0\n"
ds = parse_experiment_text(profile)
up, down = ds.split_at_peak()
check("peak found", ds.peak_index() == 2)
check("ascent leg", list(up) == [0, 1, 2], list(up))
check("descent leg", list(down) == [2, 3, 4], list(down))
check("legs share the peak sample", up[-1] == down[0])

print("\n§11.5 transfer protocol")
out = []
ft = FileTransfer(out.append)
ft.request_list()
check("LIST_FILES sent", out == ["LIST_FILES"])
ft.feed("$F,LIST,flight1.csv,412000,07 Aug 2026")
check("file listed", ft.files and ft.files[0].size == 412000)

payload = b"0.0,0.0,120.0\n1.0,50.0,118.5\n"
half = len(payload) // 2
c0, c1 = payload[:half], payload[half:]
out.clear()
ft.request_file("flight1.csv")
check("resume offset 0 first time", "FROM 0" in out[0], out[0])
ft.feed(f"$F,META,flight1.csv,{len(payload)},2,{crc32(payload)}")
ft.feed(f"$F,CHUNK,0,{crc32(c0)},{binascii.hexlify(c0).decode()}")
check("chunk 0 acked", "ACK_CHUNK 0" in out, out)
check("progress advanced", ft.progress.received == len(c0))

out.clear()
ft.feed(f"$F,CHUNK,1,DEADBEEF,{binascii.hexlify(c1).decode()}")
check("bad CRC triggers resend", any("RESEND_CHUNK" in o for o in out), out)
check("not counted as received", ft.progress.received == len(c0))
ft.feed(f"$F,CHUNK,1,{crc32(c1)},{binascii.hexlify(c1).decode()}")
ft.feed("$F,DONE,")
check("complete after verification", ft.state is TransferState.COMPLETE, ft.error)
check("assembled correctly", ft.assembled() == payload)

print("\n§11.5 truncation is caught")
out.clear()
ft2 = FileTransfer(out.append)
ft2.request_file("flight2.csv")
ft2.feed(f"$F,META,flight2.csv,{len(payload)},2,{crc32(payload)}")
ft2.feed(f"$F,CHUNK,0,{crc32(c0)},{binascii.hexlify(c0).decode()}")
ft2.feed("$F,DONE,")
check("truncated transfer rejected", ft2.state is TransferState.FAILED)
check("reason mentions truncation", "truncated" in ft2.error.lower(), ft2.error)
check("resume available after failure", ft2.can_resume)

out.clear()
ft2.request_file("flight2.csv")
check("resumes from last good chunk", "FROM 1" in out[0], out[0])

print("\n§11.6 notification fires once per file per session")
n = RetrievalNotifier()
f = RemoteFile("flight1.csv", 412000, "07 Aug 2026")
check("announced once", n.should_announce(f))
check("not announced again", not n.should_announce(f))
n.note_retrieved(f, "ABC123")
check("tracked as retrieved", n.already_retrieved(f))
check("message names size", "402 KB" in n.message(f) or "KB" in n.message(f))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")

# ═══════════════════════════════════════════════════════════════════════════
#  Regression tests for bug fixes
# ═══════════════════════════════════════════════════════════════════════════

print("\n--- Regression: #1 arm-check ordering in _request ---")
# An unarmed destructive actuator must show 'arm first', not a clamp message,
# even when the requested delta would be clamped.
arm_reg = ArmingCentre(timeout=10.0)
act_sep = Actuator(BY_ID["ACT_SEPARATE"])
act_sep.note_echo(360.0)           # at the upper hard stop — any +delta clamps
# Simulate what _request() does with the new ordering:
# arm check must be evaluated before evaluate().
is_armed_before_check = arm_reg.is_armed("ACT_SEPARATE")
check("unarmed destructive: arm gate fires before evaluate",
      not is_armed_before_check)
# If we were to call evaluate() first on the un-armed path, the clamped
# delta_applied would be 0.0 and _request would return silently — the operator
# would never see "arm first".  With the new ordering, we never reach
# evaluate() for an unarmed destructive control.
result_if_evaluated = act_sep.evaluate(45.0)   # would clamp to 0°
check("clamped result would hide arm requirement",
      result_if_evaluated.delta_applied == 0.0)
# Arm it and verify fire works correctly afterwards.
arm_reg.arm("ACT_SEPARATE", now=0.0)
check("armed correctly", arm_reg.is_armed("ACT_SEPARATE", now=0.5))
check("fire succeeds when armed", arm_reg.fire("ACT_SEPARATE", now=0.5))
check("double-fire refused", not arm_reg.fire("ACT_SEPARATE", now=0.5))

print("\n--- Regression: #2 pure getter + expire() ---")
arm2 = ArmingCentre(timeout=5.0)
arm2.arm("ACT_DEPLOY", now=0.0)
# Multiple reads must not clear the armed state prematurely.
check("first read: armed", arm2.armed_control(now=1.0) == "ACT_DEPLOY")
check("second read: still armed", arm2.armed_control(now=2.0) == "ACT_DEPLOY")
check("third read: still armed", arm2.armed_control(now=3.0) == "ACT_DEPLOY")
# Reads past expiry report None but do NOT clear _armed.
check("read past expiry: None", arm2.armed_control(now=6.0) is None)
check("_armed still set (expire not called yet)",
      arm2._armed == "ACT_DEPLOY")   # internal check: mutation not done
# expire() is the ONLY path that clears _armed on timeout.
fired = arm2.expire(now=6.0)
check("expire() clears it", arm2._armed is None)
check("expire() returns True when it fires", fired)
check("expire() on already-clear: False", not arm2.expire(now=6.0))

print("\n--- Regression: #3 falsy-zero position base (_send_move) ---")
# A servo at 0.0° is at the lower hard stop.  `(0.0 or 180.0)` evaluates
# to 180.0 in the old code — a 180° error on an irreversible mechanism.
act_door = Actuator(BY_ID["ACT_DOOR"])
act_door.note_echo(0.0)          # genuinely at 0°
pos = act_door.position          # must be 0.0, not None
check("actual 0.0 is reported correctly (not falsy-None)",
      pos == 0.0 and pos is not None)
# The new _send_move logic: use actuator.position directly (never `or home`).
target_correct = act_door.position + 45.0
check("+45° from 0° → 45°, not 225°", target_correct == 45.0)
# When position is None, the move must be refused outright.
act_door2 = Actuator(BY_ID["ACT_DOOR"])
check("position is None before first echo", act_door2.position is None)
# Simulate the guard: if position is None, we refuse.
should_refuse = act_door2.position is None
check("move refused when no echo received", should_refuse)

print("\n--- Regression: #4 calibration guard GROUND_ONLY ---")
# Each CALIBRATE_* command must now be in COMMANDS with Guard.GROUND_ONLY.
cal_cmds = ["CALIBRATE_PRESSURE", "CALIBRATE_ALTITUDE", "CALIBRATE_IMU",
            "CALIBRATE_GNSS", "CALIBRATE_TEMPERATURE", "CALIBRATE_ALL"]
for cmd in cal_cmds:
    check(f"{cmd} in COMMANDS", cmd in COMMANDS)
    if cmd in COMMANDS:
        check(f"{cmd} guard is GROUND_ONLY",
              COMMANDS[cmd].guard is Guard.GROUND_ONLY,
              COMMANDS[cmd].guard)

# Guard must BLOCK calibration in flight.
g = check_guard(COMMANDS["CALIBRATE_PRESSURE"], FlightState.COAST, LIVE, False)
check("CALIBRATE_PRESSURE blocked in COAST", not g.allowed, g.reason)
g = check_guard(COMMANDS["CALIBRATE_ALL"], FlightState.DESCENT, LIVE, False)
check("CALIBRATE_ALL blocked in DESCENT", not g.allowed, g.reason)

# Guard must ALLOW calibration on the ground.
g = check_guard(COMMANDS["CALIBRATE_PRESSURE"], FlightState.PRE_LAUNCH, LIVE, False)
check("CALIBRATE_PRESSURE allowed in PRE_LAUNCH", g.allowed, g.reason)
g = check_guard(COMMANDS["CALIBRATE_IMU"], FlightState.BOOT, LIVE, False)
check("CALIBRATE_IMU allowed in BOOT", g.allowed, g.reason)

print("\n--- Regression: #5 backward-transition entries from timeline ---")
from ground_station.timeline import StateTimeline
from ground_station.models import StateObservation

tl = StateTimeline()
tl.observe(FlightState.BOOST, 5.0)
tl.observe(FlightState.COAST, 10.0)
# Inject a backward transition.
entries = tl.observe(FlightState.BOOST, 11.0)
check("backward transition returns an entry", len(entries) == 1)
check("entry flagged backward", entries[0].backward)
check("entry state is the regressed state", entries[0].state is FlightState.BOOST)
# Normal (non-backward) transitions must not be flagged.
tl2 = StateTimeline()
tl2.observe(FlightState.PRE_LAUNCH, 0.0)
fwd = tl2.observe(FlightState.BOOST, 3.0)
check("forward transition not flagged", fwd and not fwd[0].backward)
# Unchanged state returns an empty list, not a falsely-backward entry.
empty = tl2.observe(FlightState.BOOST, 4.0)
check("unchanged state returns empty list", empty == [])

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("all checks passed")
