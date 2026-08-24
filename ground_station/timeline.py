"""
ground_station/timeline.py
══════════════════════════
Per-vehicle state timeline (§5.5) — "the primary post-flight debugging
artefact".

Three rules live here and nowhere else:

  §5.2  A backward transition is displayed with an anomaly flag. It is
        never suppressed and never corrected. The app does not enforce
        forward-only transitions — a vehicle reporting a regression is
        reporting real information about its flight software, and hiding
        it would mask a genuine fault.

  §5.6  If the observed state jumps over one or more states, the skipped
        states are recorded as "inferred, not received", visually
        distinct from an observed transition. This is the ONLY place in
        the app that draws a state conclusion. It is retrospective,
        display-only, and never feeds a command (§1.3).

  §5.7  The first state after a REFRESH_PROCESSOR is marked "restored
        from flash", carrying the §10.3 worst-case time error so the
        operator knows the timeline's precision from that point on.

§1.3 is the boundary being defended here: FLIGHT_SOFTWARE_STATE is
received and mirrored, never computed on the ground. Inference under §5.6
is an explicitly bounded exception — it annotates history, it does not
produce a current state, and nothing reads it back as authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import FlightState, StateEntry, StateObservation


@dataclass
class ContinuityCheck:
    """§10.3 — result of the post-refresh continuity verification."""
    ok: bool
    detail: str = ""


class StateTimeline:
    """One vehicle's observed state history."""

    def __init__(self) -> None:
        self.entries: List[StateEntry] = []
        self._current: Optional[FlightState] = None

        # §10.3 refresh handshake
        self._expect_restore: bool = False
        self._pre_refresh_state: Optional[FlightState] = None
        self._pre_refresh_time: Optional[float] = None
        self._restore_uncertainty: Optional[float] = None

        #: Set True and left True if a post-refresh continuity check ever
        #: fails. From that point every timestamp in the log is suspect
        #: (§10.3), so this latches rather than clearing on the next good
        #: packet.
        self.continuity_broken: bool = False

    @property
    def current(self) -> Optional[FlightState]:
        return self._current

    # ── §10.3 refresh handshake ──────────────────────────────────────────

    def arm_refresh(self, worst_case_error_s: float = 0.1) -> None:
        """Call immediately before sending REFRESH_PROCESSOR.

        `worst_case_error_s` is one flash-persistence interval — 100 ms at
        the firmware's 10 Hz (§10.3). Passed in rather than hardcoded
        because §10.3.1 recommends dropping to a 1 Hz time tick to avoid
        wearing out the flash sector, which would make it 1 s. The
        operator must be shown whichever figure is true of the firmware
        actually flying.
        """
        self._expect_restore = True
        self._pre_refresh_state = self._current
        self._pre_refresh_time = (self.entries[-1].mission_time
                                  if self.entries else None)
        self._restore_uncertainty = worst_case_error_s

    def _check_continuity(self, state: FlightState,
                          mission_time: float) -> ContinuityCheck:
        """§10.3 — mission time must not go backwards and state must be
        the same or later. A regression means the flash restore failed."""
        problems = []
        if (self._pre_refresh_time is not None
                and mission_time < self._pre_refresh_time):
            problems.append(
                f"mission time went backwards across the refresh: "
                f"{mission_time:.2f} s < {self._pre_refresh_time:.2f} s"
            )
        if (self._pre_refresh_state is not None
                and state.ordinal < self._pre_refresh_state.ordinal):
            problems.append(
                f"state regressed across the refresh: {state.value} < "
                f"{self._pre_refresh_state.value}"
            )
        if problems:
            return ContinuityCheck(ok=False, detail="; ".join(problems))
        return ContinuityCheck(ok=True)

    # ── ingest ───────────────────────────────────────────────────────────

    def observe(self, state: FlightState, mission_time: float
                ) -> List[StateEntry]:
        """Record one packet's reported state.

        Returns the entries appended by this call — empty when the state
        is unchanged, one for a normal transition, and more when §5.6
        inference fills a jump. Returning them lets the caller log exactly
        what it displayed without re-diffing the timeline.
        """
        appended: List[StateEntry] = []

        # NOTE the ordering: the refresh check comes BEFORE the
        # unchanged-state early return, and must stay there. A successful
        # flash restore normally comes back in the *same* state it left
        # (§10.3 — the whole point is that a refresh does not drop the
        # state machine to BOOT), so the common case is state == current.
        # Returning early on that would skip both the §5.7 marking and the
        # §10.3 continuity check, i.e. the app would silently fail to
        # verify the one thing the refresh needed verifying.
        if self._expect_restore:
            self._expect_restore = False
            check = self._check_continuity(state, mission_time)
            if not check.ok:
                self.continuity_broken = True
            entry = StateEntry(
                state=state,
                mission_time=mission_time,
                observation=StateObservation.RESTORED,
                backward=(self._current is not None
                          and state.ordinal < self._current.ordinal),
                time_uncertainty=self._restore_uncertainty,
            )
            self.entries.append(entry)
            appended.append(entry)
            self._current = state
            return appended

        if state == self._current:
            return []

        previous = self._current

        # ── §5.2 backward transition ─────────────────────────────────────
        if previous is not None and state.ordinal < previous.ordinal:
            entry = StateEntry(state=state, mission_time=mission_time,
                               observation=StateObservation.OBSERVED,
                               backward=True)
            self.entries.append(entry)
            appended.append(entry)
            self._current = state
            return appended

        # ── §5.6 inferred intermediate states ────────────────────────────
        # A short-lived state such as APOGEE can be missed entirely if
        # every packet carrying it is lost. The jump itself is the
        # evidence it occurred.
        if previous is not None:
            for skipped_ordinal in range(previous.ordinal + 1, state.ordinal):
                skipped = _by_ordinal(skipped_ordinal)
                entry = StateEntry(
                    state=skipped,
                    # The inferred state has no received timestamp. Using
                    # the *later* endpoint would misplace it after the
                    # event; the honest bound is the first moment it could
                    # have occurred, and the INFERRED marking tells the
                    # reader not to treat it as measured.
                    mission_time=mission_time,
                    observation=StateObservation.INFERRED,
                )
                self.entries.append(entry)
                appended.append(entry)

        entry = StateEntry(state=state, mission_time=mission_time,
                           observation=StateObservation.OBSERVED)
        self.entries.append(entry)
        appended.append(entry)
        self._current = state
        return appended

    # ── queries ──────────────────────────────────────────────────────────

    def first_observed(self, state: FlightState) -> Optional[float]:
        for e in self.entries:
            if e.state is state:
                return e.mission_time
        return None

    @property
    def anomalies(self) -> List[StateEntry]:
        """Entries the operator needs to look at: backward transitions
        (§5.2) and anything not directly observed (§5.6, §5.7)."""
        return [e for e in self.entries
                if e.backward or e.observation is not StateObservation.OBSERVED]

    def state_at(self, mission_time: float) -> Optional[FlightState]:
        """State in effect at a given mission time.

        Used by §7.5.6's reconcile(state_before, state_after, ...) to pick
        the segment shape either side of an outage.
        """
        found = None
        for e in self.entries:
            if e.mission_time <= mission_time:
                found = e.state
            else:
                break
        return found


def _by_ordinal(ordinal: int) -> FlightState:
    for s in FlightState:
        if s.ordinal == ordinal:
            return s
    raise ValueError(f"no state with ordinal {ordinal}")
