"""
ground_station/parser.py
════════════════════════
Demultiplexing (§3.2), per-vehicle stream state (§4.4), duplicate and
regression rejection (§4.6), loss tiering (§7.5.2), and link quality
(§3.7).

Qt-free and hardware-free on purpose — every rule in here is testable
without opening a port or a window.

DESIGN NOTE — why loss tiering lives here and not in the graph widget:
§7.5.2 classifies loss by *consecutive packets missing*, which is a
property of PACKET_COUNT continuity, not of anything a plot knows. The
graph is handed a tier and renders it (§7.5.3–7.5.9); it does not derive
one. That also means the tier counts in the health strip (§6.5) and the
tier a graph draws can never disagree, because there is one computation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Dict, List, Optional, Tuple
from collections import deque
import time

from ..codec import (
    CodecError, CsvCodec, DecodedFrame, PacketCodec,
    PREFIX_FILE, PREFIX_HEARTBEAT, PREFIX_TELEMETRY, decode_heartbeat,
)
from ..models import (
    ReceiverHeartbeat, RejectReason, RejectedLine, TelemetryPacket,
    VehicleID, VehicleLinkState, VEHICLE_LOST_AFTER_S, VEHICLE_STALE_AFTER_S,
)


# ─────────────────────────────────────────────────────────────────────────────
#  §7.5.2 — loss tiers
# ─────────────────────────────────────────────────────────────────────────────

class LossTier(Enum):
    NONE = "none"           # contiguous, nothing missing
    DROPOUT = "dropout"     # 1–4 missing: draw straight through, no annotation
    GAP = "gap"             # 5–19 missing: break the line, no interpolation
    OUTAGE = "outage"       # ≥20 missing: full reconciliation (§7.5.3+)


@dataclass
class LossThresholds:
    """§7.5.2 — "thresholds are configurable; the right values depend on
    the link quality actually achieved (§3.11)". Defaults are the spec's.

    Expressed in packets rather than seconds because packet count is the
    quantity actually measured; the spec's second figures are just the
    packet counts at the nominal 10 Hz, and would be wrong the moment
    §3.11 option 3 drops the downlink to 3–5 Hz.
    """
    dropout_max: int = 4        # 1..4 → DROPOUT
    gap_max: int = 19           # 5..19 → GAP, ≥20 → OUTAGE

    def classify(self, missing: int) -> LossTier:
        if missing <= 0:
            return LossTier.NONE
        if missing <= self.dropout_max:
            return LossTier.DROPOUT
        if missing <= self.gap_max:
            return LossTier.GAP
        return LossTier.OUTAGE


# ─────────────────────────────────────────────────────────────────────────────
#  Counters
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StreamCounters:
    """Everything the health strip (§6.5) and rejected-packet display
    (§4.5) need. Monotonic for the whole mission — RESET_COUNTERS was
    removed (§10.5)."""
    accepted: int = 0
    #: §4.5 reasons, kept apart from §4.6 reasons — see RejectReason.is_malformed.
    malformed: int = 0
    checksum_failed: int = 0
    duplicates: int = 0
    time_regressions: int = 0
    #: §7.5.2 tier counts.
    dropouts: int = 0
    gaps: int = 0
    outages: int = 0
    #: Total packets the vehicle says it sent but we never saw.
    missing_total: int = 0

    @property
    def rejected_total(self) -> int:
        return (self.malformed + self.checksum_failed
                + self.duplicates + self.time_regressions)


@dataclass(frozen=True)
class IngestResult:
    """What happened to one line. `packet` is None iff it was rejected."""
    packet: Optional[TelemetryPacket]
    rejected: Optional[RejectedLine] = None
    tier: LossTier = LossTier.NONE
    missing: int = 0

    @property
    def accepted(self) -> bool:
        return self.packet is not None


# ─────────────────────────────────────────────────────────────────────────────
#  Per-vehicle stream
# ─────────────────────────────────────────────────────────────────────────────

class VehicleStream:
    """All mutable state for one vehicle (§4.4, §13.5).

    Two instances share nothing. A fault on one channel cannot disturb the
    other's display, counters, or logging (§3.8).
    """

    #: §3.7 — window over which packet-success percentage is computed.
    #: 200 packets is 20 s at 10 Hz: long enough to be stable, short
    #: enough that a link degrading during descent shows up while the
    #: operator can still act on it.
    SUCCESS_WINDOW = 200

    def __init__(self, vehicle_id: VehicleID,
                 thresholds: Optional[LossThresholds] = None):
        self.vehicle_id = vehicle_id
        self.thresholds = thresholds or LossThresholds()
        self.counters = StreamCounters()

        self.last_packet: Optional[TelemetryPacket] = None
        self._last_packet_count: Optional[int] = None
        self._last_mission_time: Optional[float] = None
        self._last_received_at: float = 0.0
        self._first_received_at: Optional[float] = None

        # Rolling window of (expected, received) for §3.7.
        self._window: Deque[Tuple[int, int]] = deque(maxlen=self.SUCCESS_WINDOW)

        # §4.7 — unknown trailing field keys already reported, so they are
        # logged once per session rather than once per packet.
        self._reported_unknown_extras: set[str] = set()

        # §10.3 — set by the Command dashboard immediately before a
        # REFRESH_PROCESSOR so the next packet can be continuity-checked
        # and marked "restored from flash" (§5.7).
        self.awaiting_refresh: bool = False
        self.pre_refresh_mission_time: Optional[float] = None

    # ── ingest ───────────────────────────────────────────────────────────

    def ingest(self, packet: TelemetryPacket) -> IngestResult:
        """Apply §4.6 and §7.5.2 to one already-decoded packet."""
        # §4.6 — duplicates and regressions, counted separately from
        # malformed. Checked before anything else touches stream state.
        if self._last_packet_count is not None:
            if packet.packet_count <= self._last_packet_count:
                self.counters.duplicates += 1
                return IngestResult(
                    packet=None,
                    rejected=RejectedLine(
                        reason=RejectReason.DUPLICATE,
                        detail=(f"packet_count {packet.packet_count} ≤ last "
                                f"accepted {self._last_packet_count}"),
                        raw=packet.raw,
                        received_at=packet.received_at,
                        vehicle_id=self.vehicle_id,
                    ),
                )

        if self._last_mission_time is not None:
            if packet.mission_time < self._last_mission_time:
                self.counters.time_regressions += 1
                return IngestResult(
                    packet=None,
                    rejected=RejectedLine(
                        reason=RejectReason.TIME_REGRESSION,
                        detail=(f"mission_time {packet.mission_time:.3f} < last "
                                f"accepted {self._last_mission_time:.3f}"),
                        raw=packet.raw,
                        received_at=packet.received_at,
                        vehicle_id=self.vehicle_id,
                    ),
                )

        # ── §7.5.2 loss tiering ──────────────────────────────────────────
        if self._last_packet_count is None:
            missing = 0          # first packet of the session
        else:
            missing = packet.packet_count - self._last_packet_count - 1

        tier = self.thresholds.classify(missing)
        if tier is LossTier.DROPOUT:
            self.counters.dropouts += 1
        elif tier is LossTier.GAP:
            self.counters.gaps += 1
        elif tier is LossTier.OUTAGE:
            self.counters.outages += 1
        self.counters.missing_total += max(0, missing)

        self._window.append((missing + 1, 1))
        self._last_packet_count = packet.packet_count
        self._last_mission_time = packet.mission_time
        self._last_received_at = packet.received_at
        self.last_packet = packet
        self.counters.accepted += 1

        return IngestResult(packet=packet, tier=tier, missing=missing)

    def note_rejection(self, rejected: RejectedLine) -> None:
        """Count a line that failed before it became a packet (§4.5)."""
        if rejected.reason is RejectReason.CHECKSUM:
            self.counters.checksum_failed += 1
        elif rejected.reason.is_malformed:
            self.counters.malformed += 1

    def should_report_unknown_extra(self, key: str) -> bool:
        """§4.7 — True the first time this session, False after."""
        if key in self._reported_unknown_extras:
            return False
        self._reported_unknown_extras.add(key)
        return True

    # ── §3.7 link quality ────────────────────────────────────────────────

    @property
    def packet_success(self) -> Optional[float]:
        """Rolling success percentage from PACKET_COUNT continuity.

        None until there is enough history to mean anything — showing
        "100%" off a single packet is worse than showing nothing, because
        it is read as a healthy link.
        """
        if len(self._window) < 2:
            return None
        expected = sum(e for e, _ in self._window)
        received = sum(r for _, r in self._window)
        if expected <= 0:
            return None
        return 100.0 * received / expected

    # ── §3.6 link state ──────────────────────────────────────────────────

    def link_state(self, now: Optional[float] = None) -> VehicleLinkState:
        """RECEIVING / STALE / LOST from wall-clock silence.

        Wall clock, not mission time: mission time only advances when a
        packet arrives, so a mission-time test could never detect silence.
        """
        if self.last_packet is None:
            return VehicleLinkState.LOST
        now = time.monotonic() if now is None else now
        silent = now - self._last_received_at
        if silent > VEHICLE_LOST_AFTER_S:
            return VehicleLinkState.LOST
        if silent > VEHICLE_STALE_AFTER_S:
            return VehicleLinkState.STALE
        return VehicleLinkState.RECEIVING

    @property
    def seconds_since_packet(self) -> Optional[float]:
        if self.last_packet is None:
            return None
        return time.monotonic() - self._last_received_at


# ─────────────────────────────────────────────────────────────────────────────
#  Demultiplexer
# ─────────────────────────────────────────────────────────────────────────────

class TelemetryDemux:
    """Turns raw lines from the single USB source into per-vehicle results.

    §3.2: routing is by VEHICLE_ID and only by VEHICLE_ID. There is no
    port-based routing path here and none should be added — with one
    multiplexed source it is not merely the preferred method, it is the
    only possible one.
    """

    def __init__(self, codec: Optional[PacketCodec] = None,
                 thresholds: Optional[LossThresholds] = None):
        self.codec = codec or CsvCodec()
        self.streams: Dict[VehicleID, VehicleStream] = {
            v: VehicleStream(v, thresholds) for v in VehicleID
        }

        self.last_heartbeat: Optional[ReceiverHeartbeat] = None
        self._last_heartbeat_at: float = 0.0

        self.ignored_lines: int = 0
        self.on_file_frame: Optional[Callable[[str], None]] = None
        self.notices: List[str] = []
        self._latest_rssi: Optional[float] = None
        self._latest_snr: Optional[float] = None

    def feed(self, line: str, received_at: Optional[float] = None
             ) -> Optional[IngestResult]:
        """Process one line. Returns an IngestResult for telemetry lines
        (accepted or rejected), None for heartbeats, file frames, and
        lines belonging to another parser."""
        received_at = time.monotonic() if received_at is None else received_at
        text = line.strip()
        if not text:
            return None

        if text.startswith(PREFIX_HEARTBEAT + ","):
            self._feed_heartbeat(text, received_at)
            return None

        if text.startswith(PREFIX_FILE + ","):
            if self.on_file_frame is not None:
                self.on_file_frame(text)
            return None

        # Parse ground station receiver diagnostic RSSI / SNR lines
        if text.upper().startswith("RSSI") and ":" in text:
            self._parse_rssi_line(text)
            return None
        if text.upper().startswith("SNR") and ":" in text:
            self._parse_snr_line(text)
            return None

        if not (text.startswith(PREFIX_TELEMETRY + ",") or text.startswith("PKT=")):
            self.ignored_lines += 1
            return None

        return self._feed_telemetry(text, received_at)

    def _parse_rssi_line(self, text: str) -> None:
        try:
            val_part = text.split(":", 1)[1].strip()
            num_str = val_part.replace("dBm", "").replace("dbm", "").strip()
            self._latest_rssi = float(num_str)
        except Exception:
            pass

    def _parse_snr_line(self, text: str) -> None:
        try:
            val_part = text.split(":", 1)[1].strip()
            num_str = val_part.replace("dB", "").replace("db", "").strip()
            self._latest_snr = float(num_str)
        except Exception:
            pass

    # ── internals ────────────────────────────────────────────────────────

    def _feed_telemetry(self, text: str, received_at: float) -> IngestResult:
        try:
            decoded: DecodedFrame = self.codec.decode(text)
        except CodecError as e:
            # The line failed before we could trust its VEHICLE_ID, so it
            # cannot be attributed to a stream. Attempt a best-effort read
            # of field 2 purely so the counter lands on the right vehicle;
            # if that is not credible either, the rejection is unattributed
            # and counted globally.
            vehicle = self._sniff_vehicle(text)
            rejected = RejectedLine(reason=e.reason, detail=e.detail,
                                    raw=text, received_at=received_at,
                                    vehicle_id=vehicle)
            if vehicle is not None:
                self.streams[vehicle].note_rejection(rejected)
            return IngestResult(packet=None, rejected=rejected)

        vehicle: VehicleID = decoded.values["vehicle_id"]
        stream = self.streams[vehicle]

        for key in decoded.unknown_extras:
            if stream.should_report_unknown_extra(key):
                self.notices.append(
                    f"{vehicle.value}: unrecognised trailing field {key!r} "
                    f"(§4.7 — accepted and ignored, reported once per session)"
                )

        extras = dict(decoded.extras)
        if self._latest_rssi is not None:
            extras.setdefault("rssi", f"{self._latest_rssi:.1f}")
        if self._latest_snr is not None:
            extras.setdefault("snr", f"{self._latest_snr:.2f}")

        if text.startswith("PKT="):
            if stream._first_received_at is None:
                stream._first_received_at = received_at
            elapsed = round(received_at - stream._first_received_at, 3)
            decoded.values["mission_time"] = elapsed

        packet = TelemetryPacket(
            **decoded.values,
            extras=extras,
            received_at=received_at,
            raw=text,
        )
        return stream.ingest(packet)

    def _sniff_vehicle(self, text: str) -> Optional[VehicleID]:
        """Best-effort VEHICLE_ID read from a line that failed to decode."""
        if text.startswith("PKT="):
            return VehicleID.ROCKET
        parts = [p.strip() for p in text.split(",")]
        # For 19-field packet ($T at index 0), vehicle is at index 17.
        # For 21-field packet ($T at index 0), vehicle is at index 2.
        for idx in (17, 2):
            if idx < len(parts):
                token = parts[idx].upper()
                if token in ("1", "ROCKET"):
                    return VehicleID.ROCKET
                elif token in ("2", "CANSAT"):
                    return VehicleID.CANSAT
        return None

    def _feed_heartbeat(self, text: str, received_at: float) -> None:
        try:
            uptime, counts = decode_heartbeat(text)
        except CodecError:
            # A corrupted heartbeat is not evidence the receiver is alive.
            # Dropping it silently lets the §3.5 timeout fire, which is the
            # correct outcome.
            return
        self.last_heartbeat = ReceiverHeartbeat(
            uptime=uptime, counts=counts, received_at=received_at, raw=text,
        )
        self._last_heartbeat_at = received_at

    # ── §3.5 receiver health ─────────────────────────────────────────────

    def heartbeat_age(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_heartbeat is None:
            return None
        now = time.monotonic() if now is None else now
        return now - self._last_heartbeat_at

    def all_vehicles_silent(self, now: Optional[float] = None) -> bool:
        """§3.5 — used to suppress the "two independent vehicle failures"
        reading. Simultaneous loss of both vehicles is far more likely to
        be one USB cable than a catastrophic flight event, and the app
        must never present it as the latter."""
        return all(s.link_state(now) is VehicleLinkState.LOST
                   for s in self.streams.values())

    def drain_notices(self) -> List[str]:
        out = self.notices[:]
        self.notices.clear()
        return out
