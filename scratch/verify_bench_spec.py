import sys
from ground_station.commands import (
    COMMANDS, BENCH_COMMANDS, CommandCentre, CommandRecord, CommandStatus,
)
from ground_station.models import VehicleID
from ground_station.actuators import ACTUATORS, Actuator
from ground_station.pages.command import ActuatorPanel, SequenceControlPanel, CommandLogPanel, CommandPage
from PyQt6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

def test_exact_commands_and_newlines():
    sent_lines = []
    
    def fake_transmit(target, wire_text, seq):
        sent_lines.append(wire_text)
        
    centre = CommandCentre(fake_transmit)
    page = CommandPage(centre)
    
    # 1. Rocket Door 30, 60, 90, Lock
    door_panel = page.actuator_panels["ACT_DOOR"]
    door_panel._angle_buttons[30].click()
    assert sent_lines[-1] == "DOOR:30"
    assert door_panel._commanded.text() == "30°"
    
    door_panel._angle_buttons[60].click()
    assert sent_lines[-1] == "DOOR:60"
    assert door_panel._commanded.text() == "60°"
    
    door_panel._angle_buttons[90].click()
    assert sent_lines[-1] == "DOOR:90"
    assert door_panel._commanded.text() == "90°"
    
    door_panel._lock_btn.click()
    assert sent_lines[-1] == "LOCK:DOOR"
    assert door_panel._commanded.text() == "0°"
    
    # Repeatability test
    door_panel._angle_buttons[30].click()
    assert sent_lines[-1] == "DOOR:30"
    
    # 2. CanSat 30, 60, 90, Lock
    deploy_panel = page.actuator_panels["ACT_DEPLOY"]
    deploy_panel._angle_buttons[30].click()
    assert sent_lines[-1] == "CANSAT:30"
    assert deploy_panel._commanded.text() == "30°"
    
    deploy_panel._angle_buttons[60].click()
    assert sent_lines[-1] == "CANSAT:60"
    assert deploy_panel._commanded.text() == "60°"
    
    deploy_panel._angle_buttons[90].click()
    assert sent_lines[-1] == "CANSAT:90"
    assert deploy_panel._commanded.text() == "90°"
    
    deploy_panel._lock_btn.click()
    assert sent_lines[-1] == "LOCK:CANSAT"
    assert deploy_panel._commanded.text() == "0°"
    
    # 3. Canister Separation 30, 60, 90, Lock
    sep_panel = page.actuator_panels["ACT_SEPARATE"]
    sep_panel._angle_buttons[30].click()
    assert sent_lines[-1] == "SEPARATION:30"
    assert sep_panel._commanded.text() == "30°"
    
    sep_panel._angle_buttons[60].click()
    assert sent_lines[-1] == "SEPARATION:60"
    assert sep_panel._commanded.text() == "60°"
    
    sep_panel._angle_buttons[90].click()
    assert sent_lines[-1] == "SEPARATION:90"
    assert sep_panel._commanded.text() == "90°"
    
    sep_panel._lock_btn.click()
    assert sent_lines[-1] == "LOCK:SEPARATION"
    assert sep_panel._commanded.text() == "0°"
    
    # 4. Sequence panel buttons
    seq = page.sequence_panel
    seq._btn_lock_all.click()
    assert sent_lines[-1] == "LOCK:ALL"
    
    seq._btn_reset.click()
    assert sent_lines[-1] == "RESET"
    
    seq._btn_manual_seq.click()
    assert sent_lines[-1] == "TEST5"
    
    seq._btn_next_step.click()
    assert sent_lines[-1] == "NEXT"
    
    seq._btn_full_seq.click()
    assert sent_lines[-1] == "TEST6"
    
    # Test Actuator Send default test commands (TEST1, TEST2, TEST3)
    door_panel._last_command_sent = ""
    door_panel.actuator.commanded = None
    door_panel._send_btn.click()
    assert sent_lines[-1] == "TEST1"
    
    deploy_panel._last_command_sent = ""
    deploy_panel.actuator.commanded = None
    deploy_panel._send_btn.click()
    assert sent_lines[-1] == "TEST2"
    
    sep_panel._last_command_sent = ""
    sep_panel.actuator.commanded = None
    sep_panel._send_btn.click()
    assert sent_lines[-1] == "TEST3"
    
    # 5. Check Command Log formatting
    page.log_panel.refresh()
    for seq_num, widget in page.log_panel._rendered.items():
        rec = centre._by_sequence[seq_num]
        if rec.command in BENCH_COMMANDS:
            assert widget._text.text() == rec.command
            assert "ROCKET" not in widget._text.text()
            assert widget._status.text() == "SENT"
            
    # 6. Check wire newline formatting via LinkSupervisor
    from ground_station.link import LinkSupervisor
    link = LinkSupervisor()
    class DummySource:
        def __init__(self):
            self.lines = []
        def send_line(self, line):
            self.lines.append(line)
    dummy = DummySource()
    link.source = dummy
    
    all_cmds = [
        "DOOR:30", "DOOR:60", "DOOR:90", "LOCK:DOOR",
        "CANSAT:30", "CANSAT:60", "CANSAT:90", "LOCK:CANSAT",
        "SEPARATION:30", "SEPARATION:60", "SEPARATION:90", "LOCK:SEPARATION",
        "LOCK:ALL", "TEST1", "TEST2", "TEST3", "TEST4", "TEST5", "NEXT", "TEST6", "RESET"
    ]
    for c in all_cmds:
        link.send_command(VehicleID.ROCKET, c)
        assert dummy.lines[-1] == c + "\n"
        
    print("ALL SPECIFICATION CHECKS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_exact_commands_and_newlines()
