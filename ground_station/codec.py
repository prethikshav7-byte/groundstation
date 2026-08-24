"""
ground_station/codec.py
═══════════════════════
The wire layer, and *only* the wire layer.

§4.2 requires the field set and the wire encoding to be separable, so that
taking §3.11 option 1 (packed binary) later is a codec swap rather than a
rewrite of everything downstream. That split is the whole point of this
module:

    FIELD_SPEC        the logical field set — names, types, units. Shared
                      by every codec. This is what §4.1 actually defines.
    PacketCodec       abstract encoding. decode() turns one frame into a
                      {field_name: typed_value} dict plus extras.
    CsvCodec          the encoding in use today (§3.11 answer: CSV now,
                      swappable later).

Everything downstream of decode_line() works on TelemetryPacket and has no
idea whether the bytes were CSV or binary. Nothing outside this module may
assume CSV.

⚠️ INTERFACE DECISIONS MADE HERE — these must be handed to the firmware
owners, because the spec does not pin them down and both sides have to
agree exactly:

 1. Line prefixes (§3.12 requires reserved prefixes but does not name
    them):  $T telemetry, $H receiver heartbeat, $F file-transfer frame.

 2. Checksum coverage (§4.1 field 21, §4.3). Defined literally as "XOR of
    preceding bytes": every byte of the line from the first character up
    to, but not including, the comma that precedes the checksum field.
    That includes the $T prefix and excludes the checksum itself.

    Critically it ALSO excludes any receiver-appended trailing fields,
    which is required rather than incidental: §4.3 wants the vehicle's
    checksum verified end to end, and §1.5/§3.4 forbid the receiver
    touching vehicle data. A receiver that appended RSSI *inside* the
    checksummed region would force a recompute and mask exactly the
    LoRa-hop corruption the check exists to catch.

 3. Trailing-field format (§4.7). Preferred is self-describing
    "KEY=VALUE" (e.g. RSSI=-97.5). Bare positional values are also
    accepted and assigned RSSI then SNR in order, because that is the
    likely first firmware attempt, but KEY=VALUE should be requested —
    positional extras cannot survive anyone adding a third field.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .models import FlightState, RejectReason, VehicleID


# ─────────────────────────────────────────────────────────────────────────────
#  Line prefixes (§3.12)
# ─────────────────────────────────────────────────────────────────────────────

PREFIX_TELEMETRY = "$T"
PREFIX_HEARTBEAT = "$H"
PREFIX_FILE = "$F"

#: Every prefix this app knows. A parser ignores the others' lines rather
#: than logging them as malformed (§3.12).
KNOWN_PREFIXES = (PREFIX_TELEMETRY, PREFIX_HEARTBEAT, PREFIX_FILE)


# ─────────────────────────────────────────────────────────────────────────────
#  Errors
# ─────────────────────────────────────────────────────────────────────────────

class CodecError(Exception):
    """Frame could not be decoded. Carries the §4.5 reason so the caller
    can count it correctly without re-deriving why."""

    def __init__(self, reason: RejectReason, detail: str):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


# ─────────────────────────────────────────────────────────────────────────────
#  §4.1 — the logical field set, independent of encoding
# ─────────────────────────────────────────────────────────────────────────────

def _to_state(raw: str) -> FlightState:
    key = raw.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return FlightState[key]
    except KeyError:
        raise CodecError(
            RejectReason.UNKNOWN_STATE,
            # Naming IMPACT explicitly here because it is the one wrong
            # value we already know the current firmware emits.
            f"{raw!r} is not one of the 8 states in §5.1"
            + (" (firmware still emits the removed IMPACT state)"
               if key == "IMPACT" else ""),
        )


def _to_vehicle(raw: str) -> VehicleID:
    key = raw.strip().upper()
    try:
        return VehicleID[key]
    except KeyError:
        raise CodecError(RejectReason.UNKNOWN_VEHICLE,
                         f"{raw!r} is not ROCKET or CANSAT")


def _to_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise CodecError(RejectReason.NON_NUMERIC, f"{raw!r} is not a number")


def _to_int(raw: str) -> int:
    try:
        return int(float(raw))   # tolerate "12.0" from a float-formatting MCU
    except ValueError:
        raise CodecError(RejectReason.NON_NUMERIC, f"{raw!r} is not an integer")


def _to_str(raw: str) -> str:
    return raw.strip()


@dataclass(frozen=True)
class FieldDef:
    name: str                    # canonical key, matches TelemetryPacket
    convert: Callable[[str], Any]
    unit: str = ""
    note: str = ""
    #: Fixed-point format used when encoding (simulator, §13.6). Explicit
    #: per field rather than a general float format, because a general one
    #: emits scientific notation for large values — "9.9e+04" for pressure
    #: parses fine here but is a poor thing to ask firmware to produce, and
    #: the simulator has to emit lines that look like real ones (§2.2).
    fmt: str = "{:.2f}"


#: §4.1 fields 1–20, in wire order. Field 21 (CHECKSUM) is not listed
#: because it is a property of the *encoding*, not of the data — a binary
#: codec would carry a CRC instead and nothing downstream would notice.
FIELD_SPEC: Tuple[FieldDef, ...] = (
    FieldDef("team_id",         _to_int,     "",      "constant",                          "{}"),
    FieldDef("vehicle_id",      _to_vehicle, "",      "routing key, §3.2",                 "{}"),
    FieldDef("mission_time",    _to_float,   "s",     "monotonic",                         "{:.2f}"),
    FieldDef("packet_count",    _to_int,     "",      "per-vehicle, never reset",          "{}"),
    FieldDef("state",           _to_state,   "",      "one of the 8 in §5.1",              "{}"),
    FieldDef("altitude",        _to_float,   "m",     "AGL, zeroed at the pad",            "{:.2f}"),
    FieldDef("pressure",        _to_float,   "Pa",    "",                                  "{:.1f}"),
    FieldDef("temperature",     _to_float,   "°C",    "",                                  "{:.2f}"),
    FieldDef("battery_voltage", _to_float,   "V",     "",                                  "{:.2f}"),
    FieldDef("gnss_time",       _to_str,     "",      "hh:mm:ss.ss UTC",                   "{}"),
    # 6 dp ≈ 0.11 m of latitude — finer than the GNSS fix itself, and
    # enough that a recovery walk-in is not limited by the wire format.
    FieldDef("gnss_latitude",   _to_float,   "deg",   "signed decimal",                    "{:.6f}"),
    FieldDef("gnss_longitude",  _to_float,   "deg",   "signed decimal",                    "{:.6f}"),
    FieldDef("gnss_altitude",   _to_float,   "m",     "MSL — different datum from field 6", "{:.2f}"),
    FieldDef("gnss_satellites", _to_int,     "",      "",                                  "{}"),
    FieldDef("accel_x",         _to_float,   "g",     "",                                  "{:.3f}"),
    FieldDef("accel_y",         _to_float,   "g",     "",                                  "{:.3f}"),
    FieldDef("accel_z",         _to_float,   "g",     "",                                  "{:.3f}"),
    FieldDef("gyro_x",          _to_float,   "deg/s", "",                                  "{:.2f}"),
    FieldDef("gyro_y",          _to_float,   "deg/s", "",                                  "{:.2f}"),
    FieldDef("gyro_z",          _to_float,   "deg/s", "",                                  "{:.2f}"),
)

N_FIELDS = len(FIELD_SPEC)              # 20 payload fields
N_WIRE_FIELDS = N_FIELDS + 1            # + checksum = 21 (§4.1)

#: Trailing extras the receiver may append (§3.4, §4.7), in the order a
#: positional (non KEY=VALUE) implementation would most likely use them.
POSITIONAL_EXTRAS = ("RSSI", "SNR")


@dataclass(frozen=True)
class DecodedFrame:
    """Codec output: typed field values plus any trailing extras. Encoding
    is fully resolved by this point."""
    values: Dict[str, Any]
    extras: Dict[str, str]
    #: Extra keys that are not in POSITIONAL_EXTRAS and were not otherwise
    #: recognised — logged once per session by the caller, not per packet
    #: (§4.7).
    unknown_extras: Tuple[str, ...] = ()


# ─────────────────────────────────────────────────────────────────────────────
#  Checksum (§4.1 field 21, §4.3)
# ─────────────────────────────────────────────────────────────────────────────

def xor_checksum(payload: str) -> str:
    """XOR of every byte of `payload`, as two uppercase hex digits.

    `payload` is the whole line up to but not including the comma before
    the checksum field — see the interface note at the top of this file.
    The vehicle computes this; the app verifies it; the receiver must not
    touch it (§4.3).
    """
    x = 0
    for byte in payload.encode("utf-8"):
        x ^= byte
    return f"{x:02X}"


# ─────────────────────────────────────────────────────────────────────────────
#  Codec interface
# ─────────────────────────────────────────────────────────────────────────────

class PacketCodec(ABC):
    """One wire encoding of FIELD_SPEC.

    Implement a BinaryCodec against this when §3.11 option 1 is taken; the
    parser, dashboards, graphs, and logs need no change.
    """

    #: Human-readable name, shown in the connection panel so the operator
    #: can see which encoding the app is speaking.
    name: str = "abstract"

    @abstractmethod
    def decode(self, frame: str) -> DecodedFrame:
        """Decode one telemetry frame. Raises CodecError on any failure."""

    @abstractmethod
    def encode(self, values: Dict[str, Any]) -> str:
        """Inverse of decode. Needed by the simulator (§13.6), which must
        emit through the same format the parser consumes (§2.2) rather
        than constructing TelemetryPacket objects directly — otherwise the
        simulator cannot exercise the parser's own failure paths."""


class CsvCodec(PacketCodec):
    """Line-oriented ASCII CSV — the format in use today (§4.1)."""

    name = "CSV (21 fields)"

    def decode(self, frame: str) -> DecodedFrame:
        line = frame.strip()
        if line.startswith(PREFIX_TELEMETRY + ","):
            body = line[len(PREFIX_TELEMETRY) + 1:]
        else:
            raise CodecError(RejectReason.FIELD_COUNT,
                             f"missing {PREFIX_TELEMETRY} prefix")

        parts = body.split(",")

        # §4.7 — accept extra fields beyond 21 rather than rejecting.
        if len(parts) < N_WIRE_FIELDS:
            raise CodecError(
                RejectReason.FIELD_COUNT,
                f"expected at least {N_WIRE_FIELDS} fields, got {len(parts)}",
            )

        payload_fields = parts[:N_FIELDS]
        checksum_field = parts[N_FIELDS].strip()
        extra_fields = parts[N_WIRE_FIELDS:]

        # ── verify before converting ─────────────────────────────────────
        # Order matters: a corrupted line can trivially also be
        # non-numeric, and reporting it as NON_NUMERIC would send whoever
        # reads the log hunting a firmware formatting bug that isn't
        # there. Checksum failure is the more specific diagnosis, so it
        # wins.
        covered = PREFIX_TELEMETRY + "," + ",".join(payload_fields)
        expected = xor_checksum(covered)
        if checksum_field.upper() != expected:
            raise CodecError(
                RejectReason.CHECKSUM,
                f"got {checksum_field!r}, computed {expected}",
            )

        values: Dict[str, Any] = {}
        for spec, raw in zip(FIELD_SPEC, payload_fields):
            try:
                values[spec.name] = spec.convert(raw)
            except CodecError as e:
                # Re-raise with the field name attached — "not a number"
                # on its own is useless when there are 20 candidates.
                raise CodecError(e.reason, f"field {spec.name}: {e.detail}")

        extras, unknown = _parse_extras(extra_fields)
        return DecodedFrame(values=values, extras=extras, unknown_extras=unknown)

    def encode(self, values: Dict[str, Any]) -> str:
        out: List[str] = []
        for spec in FIELD_SPEC:
            v = values[spec.name]
            if isinstance(v, (VehicleID, FlightState)):
                out.append(v.value)
            else:
                out.append(spec.fmt.format(v))
        covered = PREFIX_TELEMETRY + "," + ",".join(out)
        return covered + "," + xor_checksum(covered)


def _parse_extras(fields: Sequence[str]) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """§4.7 — trailing fields, KEY=VALUE preferred, positional tolerated."""
    extras: Dict[str, str] = {}
    unknown: List[str] = []
    positional_i = 0

    for raw in fields:
        token = raw.strip()
        if not token:
            continue
        if "=" in token:
            key, _, val = token.partition("=")
            key = key.strip().upper()
            extras[key] = val.strip()
            if key not in POSITIONAL_EXTRAS:
                unknown.append(key)
        else:
            if positional_i < len(POSITIONAL_EXTRAS):
                extras[POSITIONAL_EXTRAS[positional_i]] = token
                positional_i += 1
            else:
                unknown.append(token)

    return extras, tuple(unknown)


# ─────────────────────────────────────────────────────────────────────────────
#  Heartbeat frames (§3.4)
# ─────────────────────────────────────────────────────────────────────────────
#  Format:  $H,<uptime_s>,<rocket_count>,<cansat_count>,<checksum>
#
#  Checksummed on the same rule as telemetry so a corrupted heartbeat
#  cannot fabricate a healthy receiver.

def decode_heartbeat(frame: str) -> Tuple[float, Dict[VehicleID, int]]:
    line = frame.strip()
    if not line.startswith(PREFIX_HEARTBEAT + ","):
        raise CodecError(RejectReason.FIELD_COUNT, "not a heartbeat frame")

    parts = line[len(PREFIX_HEARTBEAT) + 1:].split(",")
    if len(parts) < 4:
        raise CodecError(RejectReason.FIELD_COUNT,
                         f"expected 4 heartbeat fields, got {len(parts)}")

    covered = PREFIX_HEARTBEAT + "," + ",".join(parts[:3])
    expected = xor_checksum(covered)
    if parts[3].strip().upper() != expected:
        raise CodecError(RejectReason.CHECKSUM,
                         f"got {parts[3]!r}, computed {expected}")

    return _to_float(parts[0]), {
        VehicleID.ROCKET: _to_int(parts[1]),
        VehicleID.CANSAT: _to_int(parts[2]),
    }


def encode_heartbeat(uptime: float, rocket_count: int, cansat_count: int) -> str:
    covered = f"{PREFIX_HEARTBEAT},{uptime:.2f},{rocket_count},{cansat_count}"
    return covered + "," + xor_checksum(covered)


# ─────────────────────────────────────────────────────────────────────────────
#  Uplink command framing (§10.5, §10.7)
# ─────────────────────────────────────────────────────────────────────────────
#  Commands are addressed to a vehicle; the receiver picks the LoRa
#  channel (§3.4, §10.8). The app never selects a channel, so the frame
#  carries a target, never a radio.

PREFIX_COMMAND = "$C"


def frame_command(target: VehicleID, command: str, sequence: int) -> str:
    """Build one checksummed uplink line.

    `sequence` lets the app match an acknowledgment to the exact command
    that produced it — required by §10.7, and required to make operator
    resends safe: without it, an ack for the first attempt is
    indistinguishable from an ack for the resend, which is precisely the
    ambiguity that makes an ACT_SEPARATE fire twice.
    """
    covered = f"{PREFIX_COMMAND},{target.value},{sequence},{command}"
    return covered + "," + xor_checksum(covered)
