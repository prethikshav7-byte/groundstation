"""
ground_station/logs/raw_log.py
══════════════════════════════
§12.1–§12.6 — the raw flight record.

§12.2 is the governing sentence and it is unusually absolute:

    "Logging is never rate-limited, decimated, coalesced, or affected by
    display Pause (§9.4), zoom, or render performance. The log is the
    flight record; the display is a convenience."

So this module deliberately has no throttle, no batching by count, no
sampling, and no reference to any widget. It sits upstream of every
display concern, which is enforced structurally: it is fed from
`LinkSupervisor.raw_line`, which fires on receipt, before the
demultiplexer has even decided whether the line is valid.

WHAT IS AND IS NOT WRITTEN
──────────────────────────
Written verbatim (§12.1): every received line, including malformed ones,
flagged with the reason, with a host-clock receive timestamp prepended.
Malformed lines especially — they are the evidence of what went wrong,
and a log that contains only the lines that parsed cannot explain a
flight where the parsing was the problem.

Never written (§12.3): reconciled segments (§7.5) and ground-derived
velocity (§6.4). Both are ground-side estimates. Putting an estimate in
the raw log makes it indistinguishable from a measurement to anyone
reading the file later, which is the whole reason §1.3 marks derived
values as derived on screen. Derived values that are worth keeping go to
a separate file via `DerivedLog`.

Host clock vs mission time: the prepended timestamp is the host clock at
receipt, not MISSION_TIME. They answer different questions — mission time
says when the vehicle sampled, the host clock says when the ground heard
it, and the difference between them is the only way to measure link
latency or spot a vehicle whose clock has jumped.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, TextIO

from ..models import RejectedLine, VehicleID


#: §12.5 — flush at least once per second, so a crash loses at most ten
#: packets per vehicle at the nominal 10 Hz.
FLUSH_INTERVAL_S = 1.0


def _stamp(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t).isoformat(
        timespec="milliseconds")


class _Sink:
    """One append-only text file with a bounded flush interval.

    Opened in line-buffered append mode. §12.6 requires the files be
    openable while the app is running, which rules out holding an
    exclusive lock and rules out writing through a temp-file-and-rename
    scheme — a reader tailing the log during a flight must see the lines
    that have been flushed.
    """

    def __init__(self, path: str, header: str = ""):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # buffering=1 is line buffering in text mode: each newline reaches
        # the OS immediately, so a tail -f sees lines as they arrive
        # rather than in 8 KB blocks.
        self._fh: Optional[TextIO] = open(path, "a", buffering=1,
                                          encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        self.lines_written = 0
        if header:
            self.write(header)

    def write(self, line: str) -> None:
        if self._fh is None:
            return
        with self._lock:
            self._fh.write(line + "\n")
            self.lines_written += 1
            now = time.monotonic()
            if now - self._last_flush >= FLUSH_INTERVAL_S:
                # §12.5. fsync is deliberately NOT called: flushing to the
                # OS is enough to survive an application crash, which is
                # the failure this guards against, and fsync at 20 lines/s
                # would add a disk round-trip to the receive path for a
                # gain only on a power cut.
                self._fh.flush()
                self._last_flush = now

    def flush(self) -> None:
        if self._fh is None:
            return
        with self._lock:
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
                self._fh = None


class RawLogSet:
    """§12.1 — one file per vehicle, plus one for receiver and source events.

    Three files rather than one interleaved file because §3.8 keeps the
    two vehicle data paths independent all the way down, and because the
    per-vehicle files are what get handed to whoever is debugging one
    vehicle. The source/receiver file is the one that answers "was it the
    cable" (§3.5), and mixing it into the telemetry would bury it.
    """

    def __init__(self, directory: str = "logs",
                 session: Optional[str] = None):
        self.session = session or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.directory = os.path.join(directory, self.session)

        self.vehicles: Dict[VehicleID, _Sink] = {}
        for vid in VehicleID:
            self.vehicles[vid] = _Sink(
                os.path.join(self.directory, f"{vid.value.lower()}_raw.log"),
                header=(f"# {vid.value} raw telemetry — session {self.session}\n"
                        f"# <host_iso_timestamp>\\t<STATUS>\\t<line as received>\n"
                        f"# STATUS is OK, or the rejection reason (§4.5/§4.6).\n"
                        f"# Ground-derived values are NOT in this file (§12.3)."),
            )
        self.system = _Sink(
            os.path.join(self.directory, "receiver_and_source.log"),
            header=(f"# Receiver heartbeat and source events — "
                    f"session {self.session}\n"
                    f"# <host_iso_timestamp>\\t<KIND>\\t<detail>"),
        )
        #: Lines that arrived before their vehicle could be identified.
        self.unattributed = 0

    # ── §12.1 ────────────────────────────────────────────────────────────

    def write_line(self, line: str, received_at: Optional[float] = None,
                   vehicle: Optional[VehicleID] = None,
                   status: str = "OK") -> None:
        """Record one received line verbatim.

        `line` is written exactly as it arrived — not re-encoded from the
        parsed packet. Re-encoding would silently normalise whatever
        formatting quirk the firmware produced, and that quirk is often
        the bug being hunted.
        """
        entry = f"{_stamp(received_at)}\t{status}\t{line}"
        if vehicle is not None:
            self.vehicles[vehicle].write(entry)
        else:
            # A line that failed before its VEHICLE_ID could be read still
            # belongs in the record. It goes to the system log rather than
            # being guessed into a vehicle file, so no vehicle's log
            # contains a line that may not be its own.
            self.unattributed += 1
            self.system.write(entry)

    def write_rejected(self, rejected: RejectedLine) -> None:
        """§4.5 — malformed lines are logged with their reason, not dropped."""
        self.write_line(
            rejected.raw,
            received_at=rejected.received_at,
            vehicle=rejected.vehicle_id,
            status=f"REJECT:{rejected.reason.name}:{rejected.detail}",
        )

    def write_event(self, kind: str, detail: str,
                    at: Optional[float] = None) -> None:
        """Source and receiver events (§3.5, §3.9)."""
        self.system.write(f"{_stamp(at)}\t{kind}\t{detail}")

    # ── lifecycle ────────────────────────────────────────────────────────

    def flush(self) -> None:
        for s in self.vehicles.values():
            s.flush()
        self.system.flush()

    def close(self) -> None:
        for s in self.vehicles.values():
            s.close()
        self.system.close()

    @property
    def line_counts(self) -> Dict[str, int]:
        counts = {v.value: s.lines_written for v, s in self.vehicles.items()}
        counts["system"] = self.system.lines_written
        return counts


# ─────────────────────────────────────────────────────────────────────────────
#  §12.4 — command log
# ─────────────────────────────────────────────────────────────────────────────

class CommandLog:
    """Every command sent and every response received (§12.4).

    Explicitly includes unacknowledged commands and resends. That is the
    point of it: after an anomaly the question is usually "what did we
    send, when, and did it land", and a log that only records successes
    cannot answer the second half.
    """

    def __init__(self, directory: str = "logs",
                 session: Optional[str] = None):
        self.session = session or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._sink = _Sink(
            os.path.join(directory, self.session, "commands.log"),
            header=(f"# Command log — session {self.session}\n"
                    f"# <host_iso_timestamp>\\t<EVENT>\\t<seq>\\t<vehicle>"
                    f"\\t<command>\\t<detail>"),
        )

    def sent(self, sequence: int, vehicle: str, command: str,
             resend_of: Optional[int] = None) -> None:
        detail = f"resend of #{resend_of}" if resend_of is not None else ""
        self._sink.write(
            f"{_stamp()}\tSENT\t{sequence}\t{vehicle}\t{command}\t{detail}")

    def acknowledged(self, sequence: int, vehicle: str, command: str,
                     response: str = "") -> None:
        self._sink.write(
            f"{_stamp()}\tACK\t{sequence}\t{vehicle}\t{command}\t{response}")

    def executed(self, sequence: int, vehicle: str, command: str,
                 response: str = "") -> None:
        self._sink.write(
            f"{_stamp()}\tEXECUTED\t{sequence}\t{vehicle}\t{command}\t{response}")

    def rejected(self, sequence: int, vehicle: str, command: str,
                 reason: str) -> None:
        self._sink.write(
            f"{_stamp()}\tREJECTED\t{sequence}\t{vehicle}\t{command}\t{reason}")

    def timed_out(self, sequence: int, vehicle: str, command: str) -> None:
        self._sink.write(
            f"{_stamp()}\tTIMEOUT\t{sequence}\t{vehicle}\t{command}\t"
            f"no acknowledgment; not retried automatically (§10.7)")

    def receiver_forwarded(self, sequence: int, vehicle: str,
                           command: str) -> None:
        # §10.7 — the receiver forwarding a command says nothing about
        # whether the vehicle heard it. Recorded as a separate, lesser
        # signal so it can never be mistaken for an acknowledgment when
        # the log is read back.
        self._sink.write(
            f"{_stamp()}\tRECEIVER_FORWARDED\t{sequence}\t{vehicle}\t{command}\t"
            f"NOT a vehicle acknowledgment")

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        self._sink.close()


# ─────────────────────────────────────────────────────────────────────────────
#  §12.3 — derived values, kept apart
# ─────────────────────────────────────────────────────────────────────────────

class DerivedLog:
    """Ground-derived values, in their own file (§12.3).

    Separate from the raw log so that nothing in the flight record is a
    ground-side estimate. Optional — §12.3 says derived values go to a
    separate file "if kept", so an operator who does not want them simply
    does not construct this.
    """

    def __init__(self, directory: str = "logs",
                 session: Optional[str] = None):
        self.session = session or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._sink = _Sink(
            os.path.join(directory, self.session, "derived.log"),
            header=("# GROUND-DERIVED VALUES — NOT MEASUREMENTS.\n"
                    "# Velocity/descent rate is differentiated from "
                    "barometric altitude on the ground (§6.4).\n"
                    "# Nothing in this file came off the vehicle.\n"
                    "# <host_iso_timestamp>\\t<vehicle>\\t<mission_time>"
                    "\\t<quantity>\\t<value>"),
        )

    def write(self, vehicle: str, mission_time: float, quantity: str,
              value: Optional[float]) -> None:
        # None is written as an empty field rather than 0, matching §6.4's
        # treatment on the gauge: an unfilled window is not a measurement
        # of zero, and a zero here would be indistinguishable from one.
        text = "" if value is None else f"{value:.4f}"
        self._sink.write(
            f"{_stamp()}\t{vehicle}\t{mission_time:.3f}\t{quantity}\t{text}")

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        self._sink.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Convenience bundle
# ─────────────────────────────────────────────────────────────────────────────

class SessionLogs:
    """All log files for one session, sharing one directory and timestamp."""

    def __init__(self, directory: str = "logs", keep_derived: bool = True):
        self.session = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.raw = RawLogSet(directory, self.session)
        self.commands = CommandLog(directory, self.session)
        self.derived: Optional[DerivedLog] = (
            DerivedLog(directory, self.session) if keep_derived else None)
        self.directory = self.raw.directory

    def flush(self) -> None:
        self.raw.flush()
        self.commands.flush()
        if self.derived is not None:
            self.derived.flush()

    def close(self) -> None:
        self.raw.close()
        self.commands.close()
        if self.derived is not None:
            self.derived.close()
