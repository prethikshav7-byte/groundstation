import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtWidgets import QApplication
from ground_station.actuators import BY_ID, Actuator, ArmingCentre, ACTUATORS
from ground_station.models import VehicleID, RocketSafetyState
from ground_station.commands import CommandCentre, CommandStatus
from ground_station.link import LinkSupervisor, SourceState
from ground_station.pages.command import CommandPage, SequenceControlPanel, ActuatorPanel

def test_sequence_controls():
    app = QApplication.instance() or QApplication(sys.argv)

    class MockSerialSource:
        def __init__(self):
            self.state = SourceState.CONNECTED
            self.lines_sent = []
        def send_line(self, line: str):
            self.lines_sent.append(line)

    mock_source = MockSerialSource()
    sup = LinkSupervisor()
    sup.source = mock_source
    centre = CommandCentre(sup.send_command)

    page = CommandPage(centre)
    notices = []
    page.notice.connect(lambda msg: notices.append(msg))
    page.refresh()

    seq = page.sequence_panel
    door_p = page.actuator_panels["ACT_DOOR"]
    deploy_p = page.actuator_panels["ACT_DEPLOY"]
    separate_p = page.actuator_panels["ACT_SEPARATE"]

    # Initial checks
    assert seq._mode_val.text() == "IDLE"
    assert seq._step_val.text() == "0 / 3"
    assert seq._last_val.text() == "—"
    assert seq._status_val.text() == "Ready"

    # Verify buttons are separate objects
    assert seq._btn_lock_all is not seq._btn_reset
    assert seq._btn_lock_all.text() == "LOCK ALL"
    assert seq._btn_reset.text() == "RESET"

    initial_lines_count = len(mock_source.lines_sent)
    initial_records_count = len(centre.records)

    # 1. DOOR TEST (Repeatability: 3 clicks)
    for i in range(1, 4):
        assert door_p._send_btn.isEnabled(), f"Door test button should be enabled before click {i}"
        door_p._send_btn.click()
        assert mock_source.lines_sent[-1] == "TEST1\n"
        assert door_p._send_btn.isEnabled(), f"Door test button should remain enabled after click {i}"
        assert notices[-1] == "Door test command sent"
    assert door_p._last.text() == "TEST1"
    assert seq._last_val.text() == "TEST1"
    assert "Door Test" in seq._step_val.text()

    # 2. CANSAT TEST (Repeatability: 3 clicks)
    for i in range(1, 4):
        assert deploy_p._send_btn.isEnabled(), f"CanSat test button should be enabled before click {i}"
        deploy_p._send_btn.click()
        assert mock_source.lines_sent[-1] == "TEST2\n"
        assert deploy_p._send_btn.isEnabled(), f"CanSat test button should remain enabled after click {i}"
        assert notices[-1] == "CanSat test command sent"
    assert deploy_p._last.text() == "TEST2"
    assert seq._last_val.text() == "TEST2"
    assert "CanSat Test" in seq._step_val.text()

    # 3. SEPARATION TEST (Repeatability: 2 clicks)
    for i in range(1, 3):
        assert separate_p._send_btn.isEnabled(), f"Separation test button should be enabled before click {i}"
        separate_p._send_btn.click()
        assert mock_source.lines_sent[-1] == "TEST3\n"
        assert separate_p._send_btn.isEnabled(), f"Separation test button should remain enabled after click {i}"
        assert notices[-1] == "Separation test command sent"
    assert separate_p._last.text() == "TEST3"
    assert seq._last_val.text() == "TEST3"
    assert "Separation Test" in seq._step_val.text()

    # 4. LOCK ALL (Repeatability: 3 clicks)
    for i in range(1, 4):
        assert seq._btn_lock_all.isEnabled(), f"Lock all button should be enabled before click {i}"
        seq._btn_lock_all.click()
        assert mock_source.lines_sent[-1] == "LOCK:ALL\n"
        assert seq._btn_lock_all.isEnabled(), f"Lock all button should remain enabled after click {i}"
        assert notices[-1] == "Lock All command sent"
    assert seq._mode_val.text() == "LOCKED"
    assert seq._last_val.text() == "LOCK:ALL"
    assert "0 / 3" in seq._step_val.text()
    assert door_p._last.text() == "LOCK:ALL"
    assert deploy_p._last.text() == "LOCK:ALL"
    assert separate_p._last.text() == "LOCK:ALL"

    # 5. RESET (Repeatability: 2 clicks)
    for i in range(1, 3):
        assert seq._btn_reset.isEnabled(), f"Reset button should be enabled before click {i}"
        seq._btn_reset.click()
        assert mock_source.lines_sent[-1] == "RESET\n"
        assert seq._btn_reset.isEnabled(), f"Reset button should remain enabled after click {i}"
        assert notices[-1] == "Reset command sent"
    assert seq._mode_val.text() == "RESET / IDLE"
    assert seq._last_val.text() == "RESET"
    assert "0 / 3" in seq._step_val.text()
    assert door_p._last.text() == "RESET"
    assert deploy_p._last.text() == "RESET"
    assert separate_p._last.text() == "RESET"

    # 6. MANUAL SEQUENCE (Repeatability: 2 clicks)
    for i in range(1, 3):
        assert seq._btn_manual_seq.isEnabled(), f"Manual sequence button should be enabled before click {i}"
        seq._btn_manual_seq.click()
        assert mock_source.lines_sent[-1] == "TEST5\n"
        assert seq._btn_manual_seq.isEnabled(), f"Manual sequence button should remain enabled after click {i}"
        assert notices[-1] == "Manual sequence command sent"
    assert seq._mode_val.text() == "MANUAL"
    assert seq._last_val.text() == "TEST5"
    assert "1 / 3" in seq._step_val.text()
    assert door_p._last.text() == "TEST5"

    # 7. NEXT SEQUENCE STEP (Repeatability: 3 step advances)
    assert seq._btn_next_step.isEnabled()
    seq._btn_next_step.click()
    assert mock_source.lines_sent[-1] == "NEXT\n"
    assert seq._last_val.text() == "NEXT"
    assert "2 / 3" in seq._step_val.text()
    assert deploy_p._last.text() == "NEXT"
    assert notices[-1] == "Next sequence step command sent"

    seq._btn_next_step.click()
    assert mock_source.lines_sent[-1] == "NEXT\n"
    assert "3 / 3" in seq._step_val.text()
    assert separate_p._last.text() == "NEXT"

    seq._btn_next_step.click()
    assert mock_source.lines_sent[-1] == "NEXT\n"
    assert "Complete" in seq._step_val.text()
    assert seq._btn_next_step.isEnabled()

    # 8. FULL SEQUENCE (Repeatability: 2 clicks)
    for i in range(1, 3):
        assert seq._btn_full_seq.isEnabled(), f"Full sequence button should be enabled before click {i}"
        seq._btn_full_seq.click()
        assert mock_source.lines_sent[-1] == "TEST6\n"
        assert seq._btn_full_seq.isEnabled(), f"Full sequence button should remain enabled after click {i}"
        assert notices[-1] == "Full sequence command sent"
    assert seq._mode_val.text() == "FULL SEQUENCE"
    assert seq._last_val.text() == "TEST6"
    assert door_p._last.text() == "TEST6"
    assert deploy_p._last.text() == "TEST6"
    assert separate_p._last.text() == "TEST6"

    # Disconnected source check (no crash)
    sup_none = LinkSupervisor()
    centre_none = CommandCentre(sup_none.send_command)
    page_none = CommandPage(centre_none)
    page_none.sequence_panel._btn_lock_all.click()
    assert page_none.sequence_panel._last_val.text() == "LOCK:ALL"

    # Command Log Verification (Every click = 1 log record)
    total_clicks = 3 + 3 + 2 + 3 + 2 + 2 + 3 + 2 # = 20 clicks
    assert len(mock_source.lines_sent) == total_clicks, f"Expected {total_clicks} lines, got {len(mock_source.lines_sent)}"
    assert len(centre.records) == total_clicks, f"Expected {total_clicks} records, got {len(centre.records)}"

    page.log_panel.refresh()
    assert len(page.log_panel._rendered) == total_clicks, f"Expected {total_clicks} rendered rows, got {len(page.log_panel._rendered)}"

    # Check timeout polling: bench records must NOT time out to TIMED_OUT
    centre.poll_timeouts(now=99999999.0)
    for rec in centre.records:
        assert rec.status == CommandStatus.SENT, f"Record {rec.command} #{rec.sequence} should remain SENT, got {rec.status}"

    print("ALL SEQUENCE AND ACTUATOR REPEATABILITY TESTS PASSED!")

if __name__ == "__main__":
    test_sequence_controls()
