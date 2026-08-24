"""
ground_station/experiment/transfer.py
═════════════════════════════════════
§11.5 — chunked, CRC'd, acknowledged, resumable file transfer over a
direct USB connection to the recovered CanSat.

Pure protocol state machine: no Qt, no serial. The page drives it and
hands it frames. That keeps the resume logic — the part with real edge
cases — testable without a rocket.

THE FAILURE THIS IS BUILT AROUND (§11.5)
────────────────────────────────────────
"A silently truncated file that parses cleanly is the worst failure here,
because the resulting profile looks entirely plausible."

That is the reason for the whole-file checksum at the end rather than
trusting per-chunk CRCs alone. Per-chunk CRC catches corruption inside a
chunk; it cannot catch a transfer that simply stopped early, because
every chunk that did arrive was individually perfect. Only a length and
whole-file check catches the truncation, and a truncated aerosol profile
is exactly the kind of wrong answer that survives review.

RESUME (§11.5)
──────────────
"an interrupted transfer resumes from the last good chunk.
Restart-from-zero is a poor default on a cable that has just been through
a rocket flight." Resume state is therefore held per (filename, size) and
survives a disconnect within the session.
"""
from __future__ import annotations

import binascii
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

#: §3.12 — file-transfer frames carry their own reserved prefix so they
#: cannot be confused with telemetry or heartbeat lines.
PREFIX_FILE = "$F"

#: Frame kinds within the $F namespace.
KIND_LIST = "LIST"
KIND_META = "META"
KIND_CHUNK = "CHUNK"
KIND_DONE = "DONE"
KIND_ERROR = "ERROR"


class TransferState(Enum):
    IDLE = "idle"
    LISTING = "listing"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class RemoteFile:
    """One entry from LIST_FILES (§11.5)."""
    name: str
    size: int
    recorded: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}   {self.size / 1024:.0f} KB   {self.recorded}".strip()


@dataclass
class TransferProgress:
    """§11.5 — percentage, bytes, elapsed and estimated remaining."""
    received: int = 0
    total: int = 0
    chunks_ok: int = 0
    chunks_retried: int = 0
    elapsed_s: float = 0.0

    @property
    def fraction(self) -> float:
        return 0.0 if self.total <= 0 else min(1.0, self.received / self.total)

    @property
    def percent(self) -> float:
        return 100.0 * self.fraction

    def eta_s(self) -> Optional[float]:
        if self.elapsed_s <= 0 or self.received <= 0 or self.total <= 0:
            return None
        rate = self.received / self.elapsed_s
        if rate <= 0:
            return None
        return max(0.0, (self.total - self.received) / rate)


def crc32(data: bytes) -> str:
    return f"{binascii.crc32(data) & 0xFFFFFFFF:08X}"


class FileTransfer:
    """Receiver side of the §11.5 protocol.

    Frames in via `feed()`; commands out via the injected `send`
    callable. Nothing here opens a port.
    """

    #: A chunk not acknowledged after this many attempts fails the
    #: transfer rather than looping. §1.4's no-autonomy rule is about
    #: telecommand, not about a retry inside a transfer the operator
    #: explicitly started — but an unbounded retry would still hang the
    #: dashboard with no way out, so it is bounded and reported.
    MAX_CHUNK_RETRIES = 3

    def __init__(self, send: Callable[[str], None]):
        self._send = send
        self.state = TransferState.IDLE
        self.files: List[RemoteFile] = []
        self.progress = TransferProgress()
        self.error: str = ""

        self._name: str = ""
        self._expected_size: int = 0
        self._expected_crc: str = ""
        self._chunks: Dict[int, bytes] = {}
        self._next_seq: int = 0
        self._total_chunks: int = 0
        self._retries: int = 0

        #: Resume points, keyed by filename. Survives a disconnect within
        #: the session (§11.5).
        self._resume: Dict[str, Tuple[int, Dict[int, bytes]]] = {}

    # ── listing ──────────────────────────────────────────────────────────

    def request_list(self) -> None:
        self.state = TransferState.LISTING
        self.files = []
        self._send("LIST_FILES")

    # ── transfer ─────────────────────────────────────────────────────────

    def request_file(self, name: str) -> None:
        self._name = name
        self.error = ""
        self._retries = 0

        # §11.5 — resume from the last good chunk rather than restarting.
        resumed = self._resume.get(name)
        if resumed is not None:
            self._next_seq, self._chunks = resumed[0], dict(resumed[1])
        else:
            self._next_seq, self._chunks = 0, {}

        self.state = TransferState.TRANSFERRING
        self._send(f"REQUEST_FILE {name} FROM {self._next_seq}")

    def abort(self) -> None:
        """§11.5 — ABORT_TRANSFER. Keeps what arrived so a later attempt
        can resume; the operator aborting is not the operator discarding."""
        if self.state is TransferState.TRANSFERRING:
            self._resume[self._name] = (self._next_seq, dict(self._chunks))
        self.state = TransferState.ABORTED
        self._send("ABORT_TRANSFER")

    def connection_lost(self) -> None:
        """Same bookkeeping as abort, without sending anything."""
        if self.state is TransferState.TRANSFERRING:
            self._resume[self._name] = (self._next_seq, dict(self._chunks))
            self.state = TransferState.FAILED
            self.error = "Connection lost — resume available"

    # ── frames in ────────────────────────────────────────────────────────

    def feed(self, frame: str) -> None:
        """One $F line from the CanSat."""
        if not frame.startswith(PREFIX_FILE + ","):
            return
        parts = frame[len(PREFIX_FILE) + 1:].split(",", 1)
        if not parts:
            return
        kind = parts[0].strip().upper()
        body = parts[1] if len(parts) > 1 else ""

        if kind == KIND_LIST:
            self._on_list(body)
        elif kind == KIND_META:
            self._on_meta(body)
        elif kind == KIND_CHUNK:
            self._on_chunk(body)
        elif kind == KIND_DONE:
            self._on_done(body)
        elif kind == KIND_ERROR:
            self.state = TransferState.FAILED
            self.error = body.strip() or "vehicle reported an error"

    def _on_list(self, body: str) -> None:
        # $F,LIST,<name>,<size>,<recorded>
        bits = [b.strip() for b in body.split(",")]
        if len(bits) < 2:
            return
        try:
            size = int(bits[1])
        except ValueError:
            return
        self.files.append(RemoteFile(bits[0], size,
                                     bits[2] if len(bits) > 2 else ""))

    def _on_meta(self, body: str) -> None:
        # $F,META,<name>,<size>,<total_chunks>,<whole_file_crc>
        bits = [b.strip() for b in body.split(",")]
        if len(bits) < 4:
            self.state = TransferState.FAILED
            self.error = "malformed transfer metadata"
            return
        try:
            self._expected_size = int(bits[1])
            self._total_chunks = int(bits[2])
        except ValueError:
            self.state = TransferState.FAILED
            self.error = "malformed transfer metadata"
            return
        self._expected_crc = bits[3].upper()
        self.progress = TransferProgress(
            received=sum(len(c) for c in self._chunks.values()),
            total=self._expected_size,
            chunks_ok=len(self._chunks),
        )

    def _on_chunk(self, body: str) -> None:
        # $F,CHUNK,<seq>,<crc>,<hex payload>
        bits = body.split(",", 2)
        if len(bits) < 3:
            self._nak(self._next_seq, "malformed chunk frame")
            return
        seq_text, crc_text, payload_text = bits
        try:
            seq = int(seq_text.strip())
            payload = binascii.unhexlify(payload_text.strip())
        except (ValueError, binascii.Error):
            self._nak(self._next_seq, "undecodable chunk payload")
            return

        expected_crc = crc_text.strip().upper()
        if crc32(payload) != expected_crc:
            self._nak(seq, "chunk CRC mismatch")
            return

        if seq != self._next_seq:
            # Out of order. Ask again for the one actually needed rather
            # than accepting it into a hole — a chunk store with holes is
            # how a truncated file ends up looking complete.
            self._send(f"RESEND_CHUNK {self._next_seq}")
            return

        self._chunks[seq] = payload
        self._next_seq += 1
        self._retries = 0
        self.progress.received += len(payload)
        self.progress.chunks_ok += 1
        self._send(f"ACK_CHUNK {seq}")

    def _nak(self, seq: int, reason: str) -> None:
        self._retries += 1
        self.progress.chunks_retried += 1
        if self._retries > self.MAX_CHUNK_RETRIES:
            self.state = TransferState.FAILED
            self.error = f"chunk {seq} failed after {self.MAX_CHUNK_RETRIES} retries: {reason}"
            self._resume[self._name] = (self._next_seq, dict(self._chunks))
            return
        self._send(f"RESEND_CHUNK {seq}")

    def _on_done(self, body: str) -> None:
        self.state = TransferState.VERIFYING
        data = self.assembled()

        # §11.5 — verify a whole-file checksum before accepting the file.
        if len(data) != self._expected_size:
            self.state = TransferState.FAILED
            self.error = (
                f"Transfer truncated: {len(data)} bytes received, "
                f"{self._expected_size} expected. Not accepted — a "
                f"truncated file would parse cleanly and produce a "
                f"plausible-looking profile.")
            self._resume[self._name] = (self._next_seq, dict(self._chunks))
            return

        if self._expected_crc and crc32(data) != self._expected_crc:
            self.state = TransferState.FAILED
            self.error = ("Whole-file checksum mismatch — file not accepted.")
            self._resume[self._name] = (self._next_seq, dict(self._chunks))
            return

        self.state = TransferState.COMPLETE
        self._resume.pop(self._name, None)

    # ── output ───────────────────────────────────────────────────────────

    def assembled(self) -> bytes:
        return b"".join(self._chunks[i] for i in sorted(self._chunks))

    def save(self, directory: str, flight_date: str = "",
             vehicle: str = "CANSAT") -> str:
        """§11.5 — write to a persistent directory with flight date and
        vehicle in the filename."""
        if self.state is not TransferState.COMPLETE:
            raise RuntimeError("Refusing to save an unverified transfer")
        os.makedirs(directory, exist_ok=True)
        stem, ext = os.path.splitext(self._name)
        parts = [p for p in (vehicle, flight_date, stem) if p]
        out = os.path.join(directory, "_".join(parts) + (ext or ".csv"))
        with open(out, "wb") as fh:
            fh.write(self.assembled())
        return out

    @property
    def can_resume(self) -> bool:
        return bool(self._resume.get(self._name))


# ─────────────────────────────────────────────────────────────────────────────
#  §11.6 — retrieval notification
# ─────────────────────────────────────────────────────────────────────────────

class RetrievalNotifier:
    """§11.6 — one non-blocking notification per file per session.

    "a notification that reappears gets dismissed reflexively, including
    on the flight that mattered." So a file that has been announced once
    is not announced again, whether the operator retrieved it or
    dismissed it.

    Retrieved files are tracked by name + size + checksum so that
    re-retrieval is an explicit choice rather than silently suppressed —
    the operator can always ask again via the manual button.
    """

    def __init__(self) -> None:
        self._announced: set[Tuple[str, int]] = set()
        self._retrieved: Dict[Tuple[str, int], str] = {}

    def should_announce(self, f: RemoteFile) -> bool:
        key = (f.name, f.size)
        if key in self._announced or key in self._retrieved:
            return False
        self._announced.add(key)
        return True

    def note_retrieved(self, f: RemoteFile, checksum: str) -> None:
        self._retrieved[(f.name, f.size)] = checksum

    def already_retrieved(self, f: RemoteFile) -> bool:
        return (f.name, f.size) in self._retrieved

    def message(self, f: RemoteFile) -> str:
        return (f"Experiment data available on CanSat "
                f"({f.size / 1024:.0f} KB"
                f"{', recorded ' + f.recorded if f.recorded else ''}). "
                f"Retrieve now?")
