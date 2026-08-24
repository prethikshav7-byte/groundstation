"""
ground_station/experiment/parser.py
═══════════════════════════════════
§11.7 — "Backend parser — required deliverable."

Standalone by requirement: "Lives in the backend as a standalone module,
independent of the telemetry parser (different format, lifecycle, failure
modes). Do not overload the §4 parser with a second mode." So this
imports nothing from codec.py or parser.py, and it takes a file path and
returns a dataset plus a report, "callable and testable with no UI".

THE RULE THAT MATTERS MOST (§11.7)
──────────────────────────────────
"A zero aerosol count is a scientific result; a zero substituted for a
parse failure is a fabricated one, and on a plot the two are
indistinguishable."

So a malformed row is logged with its line number and skipped. Never
zero-filled, never interpolated, never carried forward from the previous
row. The row simply does not exist in the dataset, and the report says
how many did not.

Range violations are treated differently from malformed rows, and the
distinction is deliberate: a negative aerosol count is unphysical but it
is *data the instrument produced*, and it indicates a sensor or logger
fault the operator needs to know about. §11.7 says flag and report rather
than silently drop. Dropping it would hide the fault; keeping it
unflagged would put an impossible value on a science plot. So it is kept
and flagged.
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple


# ─────────────────────────────────────────────────────────────────────────────
#  §11.2 — exactly three columns
# ─────────────────────────────────────────────────────────────────────────────

COLUMNS = ("MISSION_TIME", "ALTITUDE", "AEROSOL_COUNT")
N_COLUMNS = 3

#: §11.7 — negative altitude is tolerated within a small band, because a
#: barometer zeroed at the pad legitimately reads slightly below zero on
#: a cold morning or in a passing pressure change. Beyond this it is a
#: fault worth flagging.
ALTITUDE_TOLERANCE_M = 5.0


class RowProblem(Enum):
    """Why a row was skipped, or why a kept row is suspect."""
    FIELD_COUNT = "row does not have exactly three columns"
    NON_NUMERIC = "non-numeric value in a numeric column"
    NEGATIVE_AEROSOL = "negative aerosol count is unphysical"
    NEGATIVE_ALTITUDE = "altitude below tolerance"
    TIME_REGRESSION = "mission time is not monotonic"

    @property
    def fatal(self) -> bool:
        """Fatal problems skip the row (§11.7 malformed). Non-fatal ones
        keep it and flag it — the operator needs to see the fault."""
        return self in (RowProblem.FIELD_COUNT, RowProblem.NON_NUMERIC)


@dataclass(frozen=True)
class RowIssue:
    line_number: int
    problem: RowProblem
    detail: str
    raw: str


@dataclass
class ParseReport:
    """§11.9 — "Data quality must be visible before conclusions are drawn."""
    path: str = ""
    rows_read: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    issues: List[RowIssue] = field(default_factory=list)
    header_detected: bool = False
    time_gaps: List[Tuple[float, float]] = field(default_factory=list)

    #: Ranges, None when nothing parsed.
    time_range: Optional[Tuple[float, float]] = None
    altitude_range: Optional[Tuple[float, float]] = None
    aerosol_range: Optional[Tuple[float, float]] = None

    @property
    def flagged(self) -> List[RowIssue]:
        """Kept-but-suspect rows (§11.7 range validation)."""
        return [i for i in self.issues if not i.problem.fatal]

    @property
    def rejected(self) -> List[RowIssue]:
        return [i for i in self.issues if i.problem.fatal]

    def summary_lines(self, first_n: int = 5) -> List[str]:
        """§11.9 — rows read/accepted/rejected, first several line
        numbers and reasons, ranges, flagged values, time gaps."""
        out = [
            f"Rows read: {self.rows_read}    accepted: {self.rows_accepted}"
            f"    rejected: {self.rows_rejected}",
        ]
        if self.header_detected:
            out.append("Header row detected and skipped.")
        if self.time_range:
            out.append(f"Mission time: {self.time_range[0]:.2f} – "
                       f"{self.time_range[1]:.2f} s")
        if self.altitude_range:
            out.append(f"Altitude: {self.altitude_range[0]:.1f} – "
                       f"{self.altitude_range[1]:.1f} m AGL")
        if self.aerosol_range:
            out.append(f"Aerosol: {self.aerosol_range[0]:.1f} – "
                       f"{self.aerosol_range[1]:.1f} particles/cm³")

        rejected = self.rejected
        if rejected:
            out.append(f"Rejected rows ({len(rejected)}):")
            for i in rejected[:first_n]:
                out.append(f"   line {i.line_number}: {i.problem.value} — {i.detail}")
            if len(rejected) > first_n:
                out.append(f"   … and {len(rejected) - first_n} more")

        flagged = self.flagged
        if flagged:
            out.append(f"Flagged values ({len(flagged)}) — kept, but indicate "
                       f"a sensor or logger fault:")
            for i in flagged[:first_n]:
                out.append(f"   line {i.line_number}: {i.problem.value} — {i.detail}")
            if len(flagged) > first_n:
                out.append(f"   … and {len(flagged) - first_n} more")

        if self.time_gaps:
            out.append(f"Time gaps ({len(self.time_gaps)}):")
            for a, b in self.time_gaps[:first_n]:
                out.append(f"   {a:.2f} s → {b:.2f} s  ({b - a:.2f} s)")
        return out


@dataclass
class ExperimentDataset:
    """The parsed record. Three parallel lists, in file order."""
    mission_time: List[float] = field(default_factory=list)
    altitude: List[float] = field(default_factory=list)
    aerosol: List[float] = field(default_factory=list)
    report: ParseReport = field(default_factory=ParseReport)

    def __len__(self) -> int:
        return len(self.mission_time)

    @property
    def empty(self) -> bool:
        return not self.mission_time

    # ── §11.11 ascent / descent split ────────────────────────────────────

    def peak_index(self) -> Optional[int]:
        if self.empty:
            return None
        return max(range(len(self.altitude)), key=lambda i: self.altitude[i])

    def split_at_peak(self) -> Tuple[range, range]:
        """§11.11 — "The record crosses each altitude twice, and the two
        legs are different measurements — one continuous line implies a
        single profile that does not exist."

        The peak sample belongs to both legs so the traces meet rather
        than leaving a one-sample hole at the top of the profile.
        """
        peak = self.peak_index()
        if peak is None:
            return range(0), range(0)
        return range(0, peak + 1), range(peak, len(self.altitude))


class ParseError(Exception):
    """Raised when no rows parse (§11.7).

    "Reject the file with a clear message if no rows parse, rather than
    showing an empty graph that reads as 'the experiment recorded
    nothing'." Those two outcomes look identical on a plot and mean
    opposite things.
    """


# ─────────────────────────────────────────────────────────────────────────────
#  Parsing
# ─────────────────────────────────────────────────────────────────────────────

#: A gap larger than this in MISSION_TIME breaks the plotted line (§11.10).
#: Chosen well above the nominal logging interval so ordinary jitter does
#: not fragment the trace.
TIME_GAP_S = 2.0


def parse_experiment_file(path: str,
                          progress: Optional[Callable[[float], None]] = None
                          ) -> ExperimentDataset:
    """§11.7 — path in, dataset plus report out. No UI, no Qt.

    `progress` is called with 0.0–1.0 so a caller can drive a progress
    bar from a background thread; passing None makes this a plain
    synchronous function, which is what the tests use.
    """
    if not os.path.isfile(path):
        raise ParseError(f"No such file: {path}")

    size = os.path.getsize(path)
    if size == 0:
        raise ParseError(f"{os.path.basename(path)} is empty.")

    # §11.7 — tolerate \r\n endings. newline="" hands line endings to the
    # csv module rather than letting universal-newline translation mangle
    # a quoted field.
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    return parse_experiment_text(text, path=path, progress=progress)


def parse_experiment_text(text: str, path: str = "",
                          progress: Optional[Callable[[float], None]] = None
                          ) -> ExperimentDataset:
    """Same as parse_experiment_file but from a string — this is the
    testable core, and what makes the whole module runnable without
    touching a filesystem."""
    ds = ExperimentDataset()
    ds.report.path = path

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    total = max(1, len(rows))

    last_time: Optional[float] = None

    for line_no, raw_row in enumerate(rows, start=1):
        if progress is not None and line_no % 500 == 0:
            progress(line_no / total)

        # §11.7 — tolerate trailing blank lines.
        if not raw_row or all(not c.strip() for c in raw_row):
            continue

        # §11.7 — tolerate a trailing comma, which csv renders as a final
        # empty field. Only one: two trailing empties is a real column
        # count problem, not a formatting quirk.
        row = list(raw_row)
        if len(row) == N_COLUMNS + 1 and not row[-1].strip():
            row = row[:-1]

        raw_text = ",".join(raw_row)

        # §11.7 — tolerate a header row, present or absent.
        if line_no == 1 and _looks_like_header(row):
            ds.report.header_detected = True
            continue

        ds.report.rows_read += 1

        if len(row) != N_COLUMNS:
            _reject(ds, line_no, RowProblem.FIELD_COUNT,
                    f"got {len(row)} columns, expected {N_COLUMNS}", raw_text)
            continue

        try:
            t = float(row[0].strip())
            alt = float(row[1].strip())
            aer = float(row[2].strip())
        except ValueError as e:
            _reject(ds, line_no, RowProblem.NON_NUMERIC, str(e), raw_text)
            continue

        # ── range validation: flag and keep (§11.7) ──────────────────────
        if aer < 0:
            _flag(ds, line_no, RowProblem.NEGATIVE_AEROSOL,
                  f"{aer:.2f} particles/cm³", raw_text)
        if alt < -ALTITUDE_TOLERANCE_M:
            _flag(ds, line_no, RowProblem.NEGATIVE_ALTITUDE,
                  f"{alt:.2f} m (tolerance −{ALTITUDE_TOLERANCE_M:.0f} m)",
                  raw_text)
        if last_time is not None and t < last_time:
            _flag(ds, line_no, RowProblem.TIME_REGRESSION,
                  f"{t:.3f} s follows {last_time:.3f} s", raw_text)
        elif last_time is not None and t - last_time > TIME_GAP_S:
            ds.report.time_gaps.append((last_time, t))

        ds.mission_time.append(t)
        ds.altitude.append(alt)
        ds.aerosol.append(aer)
        ds.report.rows_accepted += 1
        last_time = t

    if progress is not None:
        progress(1.0)

    if ds.empty:
        name = os.path.basename(path) if path else "file"
        raise ParseError(
            f"No usable rows in {name} — {ds.report.rows_read} rows read, "
            f"all rejected. The file may be truncated, may not be an "
            f"experiment log, or may use a different column layout.")

    ds.report.time_range = (min(ds.mission_time), max(ds.mission_time))
    ds.report.altitude_range = (min(ds.altitude), max(ds.altitude))
    ds.report.aerosol_range = (min(ds.aerosol), max(ds.aerosol))
    return ds


def _looks_like_header(row: Sequence[str]) -> bool:
    """A first row whose first cell is not a number is a header.

    Deliberately not a match against the expected column names: a file
    labelled "time,alt,count" is still a header and skipping it is
    right, whereas requiring exact names would push a perfectly good
    file into the FIELD_COUNT path.
    """
    if not row:
        return False
    try:
        float(row[0].strip())
        return False
    except ValueError:
        return True


def _reject(ds: ExperimentDataset, line_no: int, problem: RowProblem,
            detail: str, raw: str) -> None:
    ds.report.rows_rejected += 1
    ds.report.issues.append(RowIssue(line_no, problem, detail, raw))


def _flag(ds: ExperimentDataset, line_no: int, problem: RowProblem,
          detail: str, raw: str) -> None:
    # Note: no rows_rejected increment. The row is kept.
    ds.report.issues.append(RowIssue(line_no, problem, detail, raw))


# ─────────────────────────────────────────────────────────────────────────────
#  §11.12 — export
# ─────────────────────────────────────────────────────────────────────────────

def export_csv(ds: ExperimentDataset, path: str) -> None:
    """Write the parsed dataset (§11.12).

    Writes only accepted rows, with the canonical header. §12.7 keeps
    retrieved files read-only, so this goes to an operator-chosen path
    and never over the source file.
    """
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for t, a, c in zip(ds.mission_time, ds.altitude, ds.aerosol):
            w.writerow([f"{t:.3f}", f"{a:.2f}", f"{c:.2f}"])


def split_on_time_gaps(xs: Sequence[float], ys: Sequence[float],
                       times: Sequence[float],
                       gap_s: float = TIME_GAP_S
                       ) -> Tuple[List[float], List[float]]:
    """Insert NaN at time gaps so the plotted line breaks (§11.10).

    "At a time gap, break the line — no interpolation, and never §7.5's
    ballistic reconciliation, which is meaningless for a science
    profile." A ballistic curve through an aerosol measurement would be
    asserting a concentration nobody measured.
    """
    out_x: List[float] = []
    out_y: List[float] = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        if i and times[i] - times[i - 1] > gap_s:
            out_x.append(float("nan"))
            out_y.append(float("nan"))
        out_x.append(x)
        out_y.append(y)
    return out_x, out_y
