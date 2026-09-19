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

import math
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
    raw_str = raw.strip()
    # Try numeric code first
    try:
        code = int(float(raw_str))
        code_map = {
            0: FlightState.BOOT,
            1: FlightState.PRE_LAUNCH,
            2: FlightState.BOOST,
            3: FlightState.COAST,
            4: FlightState.APOGEE,
            5: FlightState.DESCENT,
            6: FlightState.LANDING,
            7: FlightState.RECOVERY,
        }
        if code in code_map:
            return code_map[code]
    except ValueError:
        pass

    # Try name string
    key = raw_str.upper().replace("-", "_").replace(" ", "_")
    if key == "PRELAUNCH":
        return FlightState.PRE_LAUNCH
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
    raw_str = raw.strip()
    try:
        code = int(float(raw_str))
        if code == 1:
            return VehicleID.ROCKET
        elif code == 2:
            return VehicleID.CANSAT
    except ValueError:
        pass

    key = raw_str.upper()
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


def _to_scaled_float(scale: float):
    """Decode fields that may be transmitted as scaled integers or direct floats.
    If no decimal point is present in the integer string, divides by scale factor.
    """
    def converter(raw: str) -> float:
        raw_str = raw.strip()
        try:
            if "." in raw_str:
                return float(raw_str)
            else:
                return float(int(raw_str)) / scale
        except ValueError:
            try:
                return float(raw_str)
            except ValueError:
                raise CodecError(RejectReason.NON_NUMERIC, f"{raw!r} is not a number")
    return converter


def _to_int(raw: str) -> int:
    try:
        return int(float(raw))   # tolerate "12.0" from a float-formatting MCU
    except ValueError:
        raise CodecError(RejectReason.NON_NUMERIC, f"{raw!r} is not an integer")


def _to_team_id(raw: str) -> Any:
    raw_str = raw.strip()
    try:
        return int(float(raw_str))
    except ValueError:
        return raw_str


def _to_str(raw: str) -> str:
    return raw.strip()


def _to_accelerometer(raw: str) -> Any:
    raw_str = raw.strip()
    try:
        return float(raw_str)
    except ValueError:
        return raw_str


@dataclass(frozen=True)
class FieldDef:
    name: str                    # canonical key, matches TelemetryPacket
    convert: Callable[[str], Any]
    unit: str = ""
    note: str = ""
    fmt: str = "{:.2f}"


#: 19-field live rocket telemetry specification ($T + 17 data fields + checksum = 19 fields)
FIELD_SPEC_19: Tuple[FieldDef, ...] = (
    FieldDef("team_id",           _to_team_id,                "",      "fixed format",                      "{}"),
    FieldDef("mission_time",      _to_float,                  "s",     "time from boot",                    "{:.2f}"),
    FieldDef("packet_count",      _to_int,                    "",      "packets sent during telemetry",     "{}"),
    FieldDef("altitude",          _to_scaled_float(10.0),     "m",     "0.1m resolution, corrected",        "{:.2f}"),
    FieldDef("pressure",          _to_float,                  "Pa",    "1Pa resolution",                    "{:.1f}"),
    FieldDef("temperature",       _to_scaled_float(10.0),     "°C",    "0.1C resolution",                   "{:.2f}"),
    FieldDef("battery_voltage",   _to_scaled_float(100.0),    "V",     "0.01V resolution",                  "{:.2f}"),
    FieldDef("gnss_time",         _to_str,                    "",      "seconds UTC",                       "{}"),
    FieldDef("gnss_latitude",     _to_scaled_float(10000.0),  "deg",   "0.0001 deg resolution",             "{:.6f}"),
    FieldDef("gnss_longitude",    _to_scaled_float(10000.0),  "deg",   "0.0001 deg resolution",             "{:.6f}"),
    FieldDef("gnss_altitude",     _to_scaled_float(10.0),     "m",     "0.1m resolution",                   "{:.2f}"),
    FieldDef("gnss_satellites",   _to_int,                    "",      "number of satellites",              "{}"),
    FieldDef("accelerometer",     _to_accelerometer,          "m/s²",  "m/s2 resolution",                   "{:.2f}"),
    FieldDef("gyro_spin_rate",    _to_float,                  "deg/s", "deg/s spin rate",                   "{:.2f}"),
    FieldDef("state",             _to_state,                  "",      "0..7 flight software state",        "{}"),
    FieldDef("velocity",          _to_float,                  "m/s",   "m/s velocity",                      "{:.2f}"),
    FieldDef("vehicle_id",        _to_vehicle,                "",      "1-ROCKET, 2-CANSAT",                "{}"),
)

#: Legacy 21-field wire order ($T + 20 payload fields + checksum = 21 fields)
FIELD_SPEC_21: Tuple[FieldDef, ...] = (
    FieldDef("team_id",         _to_team_id, "",      "constant",                          "{}"),
    FieldDef("vehicle_id",      _to_vehicle, "",      "routing key, §3.2",                 "{}"),
    FieldDef("mission_time",    _to_float,   "s",     "monotonic",                         "{:.2f}"),
    FieldDef("packet_count",    _to_int,     "",      "per-vehicle, never reset",          "{}"),
    FieldDef("state",           _to_state,   "",      "one of the 8 in §5.1",              "{}"),
    FieldDef("altitude",        _to_float,   "m",     "AGL, zeroed at the pad",            "{:.2f}"),
    FieldDef("pressure",        _to_float,   "Pa",    "",                                  "{:.1f}"),
    FieldDef("temperature",     _to_float,   "°C",    "",                                  "{:.2f}"),
    FieldDef("battery_voltage", _to_float,   "V",     "",                                  "{:.2f}"),
    FieldDef("gnss_time",       _to_str,     "",      "hh:mm:ss.ss UTC",                   "{}"),
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

FIELD_SPEC = FIELD_SPEC_19
N_FIELDS = len(FIELD_SPEC_19)           # 17 payload fields
N_WIRE_FIELDS = N_FIELDS + 1            # + checksum = 19 fields total with prefix $T

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
        """Inverse of decode."""


def _decode_pkt_frame(line: str) -> DecodedFrame:
    """Decode authoritative PHOENIX dB.V1 13-field KEY=VALUE telemetry packet."""
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) != 13:
        raise CodecError(
            RejectReason.FIELD_COUNT,
            f"expected exactly 13 fields for PKT frame, got {len(parts)}",
        )

    required_keys = {"PKT", "P", "T", "ALT", "AX", "AY", "AZ", "LAT", "LON", "GALT", "SAT", "FIX", "SIM"}
    parsed: Dict[str, float] = {}
    for part in parts:
        k, sep, v = part.partition("=")
        if not sep:
            raise CodecError(
                RejectReason.FIELD_COUNT,
                f"malformed field without '=' delimiter: {part!r}",
            )
        k_clean = k.strip().upper()
        v_clean = v.strip()
        try:
            parsed[k_clean] = float(v_clean)
        except ValueError:
            raise CodecError(RejectReason.NON_NUMERIC, f"field {k_clean}: {v_clean!r} is not numeric")

    missing_keys = required_keys - set(parsed.keys())
    if missing_keys:
        raise CodecError(
            RejectReason.FIELD_COUNT,
            f"missing required telemetry fields: {sorted(missing_keys)}",
        )

    pkt_count = int(parsed["PKT"])
    pressure_val = parsed["P"]
    ax, ay, az = parsed["AX"], parsed["AY"], parsed["AZ"]
    accel_mag = math.sqrt(ax**2 + ay**2 + az**2)

    values: Dict[str, Any] = {
        "team_id": "PHOENIX dB.V1",
        "vehicle_id": VehicleID.ROCKET,
        "vehicle_code": 1,
        "mission_time": float(pkt_count),
        "packet_count": pkt_count,
        "state": FlightState.BOOT,
        "flight_state_code": 0,
        "altitude": parsed["ALT"],
        "pressure": pressure_val,
        "temperature": parsed["T"],
        "battery_voltage": 0.0,
        "gnss_time": "",
        "gnss_latitude": parsed["LAT"],
        "gnss_longitude": parsed["LON"],
        "gnss_altitude": parsed["GALT"],
        "gnss_satellites": int(parsed["SAT"]),
        "accel_x": ax,
        "accel_y": ay,
        "accel_z": az,
        "accelerometer": accel_mag,
        "gyro_x": 0.0,
        "gyro_y": 0.0,
        "gyro_z": 0.0,
        "gyro_spin_rate": 0.0,
        "velocity": None,
        "raw_accelerometer": None,
        "checksum": "",
    }
    extras = {
        "gnss_fix": str(int(parsed["FIX"])),
        "sim": str(int(parsed["SIM"])),
        "SIM": str(int(parsed["SIM"])),
    }
    return DecodedFrame(values=values, extras=extras, unknown_extras=())


class CsvCodec(PacketCodec):
    """Line-oriented ASCII CSV for live telemetry packets."""

    name = "CSV"

    def decode(self, frame: str) -> DecodedFrame:
        line = frame.strip()
        if line.startswith("PKT="):
            return _decode_pkt_frame(line)

        if not line.startswith(PREFIX_TELEMETRY + ","):
            raise CodecError(RejectReason.FIELD_COUNT,
                             f"missing {PREFIX_TELEMETRY} prefix")

        parts = [p.strip() for p in line.split(",")]

        # Distinguish 21-field legacy format vs 19-field designated format
        # 19 fields: $T + 17 payload + checksum = 19 fields
        # 21 fields: $T + 20 payload + checksum = 21 fields
        is_21 = len(parts) >= 21 and (parts[2].upper() in ("ROCKET", "CANSAT") or parts[1].upper() in ("ROCKET", "CANSAT"))

        if is_21:
            spec_list = FIELD_SPEC_21
            n_fields = len(FIELD_SPEC_21)
            payload_fields = parts[1:1 + n_fields]
            checksum_field = parts[1 + n_fields].strip() if len(parts) > 1 + n_fields else ""
            extra_fields = parts[2 + n_fields:]
        else:
            if len(parts) < 19:
                raise CodecError(
                    RejectReason.FIELD_COUNT,
                    f"expected exactly 19 fields, got {len(parts)}",
                )
            if len(parts) > 19 and not any("=" in p for p in parts[19:]):
                raise CodecError(
                    RejectReason.FIELD_COUNT,
                    f"expected exactly 19 fields, got {len(parts)}",
                )
            spec_list = FIELD_SPEC_19
            n_fields = len(FIELD_SPEC_19)
            payload_fields = parts[1:1 + n_fields]
            checksum_field = parts[1 + n_fields].strip() if len(parts) > 1 + n_fields else ""
            extra_fields = parts[2 + n_fields:]

        # ── verify checksum before converting ────────────────────────────
        covered = PREFIX_TELEMETRY + "," + ",".join(payload_fields)
        expected = xor_checksum(covered)
        if checksum_field:
            ck_match = (checksum_field.upper() == expected)
            if not ck_match:
                try:
                    ck_match = (int(checksum_field) == int(expected, 16))
                except ValueError:
                    pass
            if not ck_match:
                raise CodecError(
                    RejectReason.CHECKSUM,
                    f"got {checksum_field!r}, computed {expected}",
                )

        values: Dict[str, Any] = {}
        for spec, raw in zip(spec_list, payload_fields):
            try:
                values[spec.name] = spec.convert(raw)
            except CodecError as e:
                raise CodecError(e.reason, f"field {spec.name}: {e.detail}")

        # Set codes and defaults
        if "state" in values:
            state_val = values["state"]
            if isinstance(state_val, FlightState):
                values["flight_state_code"] = state_val.ordinal
        if "vehicle_id" in values:
            vid_val = values["vehicle_id"]
            if isinstance(vid_val, VehicleID):
                values["vehicle_code"] = 1 if vid_val is VehicleID.ROCKET else 2

        if not is_21:
            accel_val = values.get("accelerometer")
            if isinstance(accel_val, (int, float)):
                values["accel_z"] = float(accel_val)
                values["accelerometer"] = float(accel_val)
                values["raw_accelerometer"] = None
            elif isinstance(accel_val, str):
                values["raw_accelerometer"] = accel_val
                values["accelerometer"] = None
            else:
                values["accelerometer"] = None
                values["raw_accelerometer"] = None
            values.setdefault("accel_x", 0.0)
            values.setdefault("accel_y", 0.0)
            values.setdefault("accel_z", 0.0)
            values.setdefault("gyro_x", 0.0)
            values.setdefault("gyro_y", 0.0)
            values.setdefault("gyro_z", values.get("gyro_spin_rate", 0.0))
        else:
            values.setdefault("gyro_spin_rate", values.get("gyro_z", 0.0))
            values.setdefault("velocity", None)
            values.setdefault("raw_accelerometer", None)
            values.setdefault("accelerometer", None)

        values["checksum"] = checksum_field
        extras, unknown = _parse_extras(extra_fields)
        return DecodedFrame(values=values, extras=extras, unknown_extras=unknown)

    def encode(self, values: Dict[str, Any]) -> str:
        out: List[str] = []
        is_19 = "gyro_spin_rate" in values or "velocity" in values or "accelerometer" in values or ("accel_x" not in values)
        spec_list = FIELD_SPEC_19 if is_19 else FIELD_SPEC_21
        for spec in spec_list:
            v = values.get(spec.name)
            if v is None:
                if spec.name == "accelerometer":
                    v = values.get("accel_z", 0.0)
                elif spec.name == "gyro_spin_rate":
                    v = values.get("gyro_z", 0.0)
                elif spec.name == "velocity":
                    v = 0.0
                elif spec.name == "vehicle_id":
                    v = values.get("vehicle_id", VehicleID.ROCKET)
                elif spec.name == "state":
                    v = values.get("state", FlightState.BOOT)
                else:
                    v = 0
            if isinstance(v, (VehicleID, FlightState)):
                out.append(v.value)
            elif isinstance(v, float):
                out.append(spec.fmt.format(v))
            else:
                out.append(str(v))
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
