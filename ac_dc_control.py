import sys
import json
import logging
import atexit
import signal
import ctypes
import ctypes.wintypes
from pathlib import Path
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QMessageBox, QDialog,
    QLineEdit, QFormLayout, QDoubleSpinBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

from ats_program.ats_device import ATSDeviceConfigurator

# ===== Config =====
AC_CONFIG = {
    "Device_Type": "CHROMA61815",
    "Setting": {
        "resource_address": "GPIB0::30::INSTR"
    }
}

_BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent

AC_CONFIG_FILE = _BASE_DIR / "ac_config.json"
AC_DEFAULT_CONFIG = {"resource_address": "GPIB0::30::INSTR"}

DC_CONFIG_FILE = _BASE_DIR / "dc_config.json"
DC_DEFAULT_CONFIG = {"resource_address": "GPIB0::14::INSTR"}
# ==================

_console_ctrl_handler_ref = None
CTRL_CLOSE_EVENT = 2


# ── ChromaDCLoad (VISA wrapper) ───────────────────────────────────────────────

class ChromaDCLoad:
    def __init__(self, resource_address: str):
        self.resource_address = resource_address
        self._rm = None
        self._vi = None
        self._visa = None

    def connect(self):
        try:
            self._visa = ctypes.windll.LoadLibrary("visa32.dll")
        except OSError:
            self._visa = ctypes.windll.LoadLibrary("visa64.dll")

        self._visa.viOpenDefaultRM.restype = ctypes.c_int32
        self._visa.viOpen.restype = ctypes.c_int32
        self._visa.viWrite.restype = ctypes.c_int32
        self._visa.viClose.restype = ctypes.c_int32

        rm = ctypes.c_uint32()
        if self._visa.viOpenDefaultRM(ctypes.byref(rm)) < 0:
            raise RuntimeError("Cannot open VISA Resource Manager")
        self._rm = rm

        vi = ctypes.c_uint32()
        status = self._visa.viOpen(
            self._rm, self.resource_address.encode(), 0, 5000, ctypes.byref(vi)
        )
        if status < 0:
            raise RuntimeError(f"Cannot open {self.resource_address} (status={status:#010x})")
        self._vi = vi

    def write(self, cmd: str):
        data = (cmd + "\n").encode()
        ret = ctypes.c_uint32()
        status = self._visa.viWrite(
            self._vi, data, ctypes.c_uint32(len(data)), ctypes.byref(ret)
        )
        if status < 0:
            raise RuntimeError(f"viWrite failed: {cmd!r} (status={status:#010x})")

    def turn_on(self, volt: float, curr: float):
        self.write(f"SOUR:VOLT {volt}")
        self.write(f"SOUR:CURR {curr}")
        self.write("CONF:OUTP ON")

    def turn_off(self):
        self.write("CONF:OUTP OFF")

    def close(self):
        try:
            if self._vi is not None:
                self._visa.viClose(self._vi)
                self._vi = None
            if self._rm is not None:
                self._visa.viClose(self._rm)
                self._rm = None
        except Exception:
            pass


# ── Config helpers ────────────────────────────────────────────────────────────

def load_ac_config() -> dict:
    if AC_CONFIG_FILE.exists():
        try:
            return json.loads(AC_CONFIG_FILE.read_text())
        except Exception:
            pass
    return AC_DEFAULT_CONFIG.copy()


def save_ac_config(cfg: dict):
    AC_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def load_dc_config() -> dict:
    if DC_CONFIG_FILE.exists():
        try:
            return json.loads(DC_CONFIG_FILE.read_text())
        except Exception:
            pass
    return DC_DEFAULT_CONFIG.copy()


def save_dc_config(cfg: dict):
    DC_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Emergency off (both devices) ─────────────────────────────────────────────

def emergency_off(ac_device, dc_device):
    if ac_device:
        try:
            ac_device.turn_off()
            ac_device.stop()
        except Exception:
            pass
    if dc_device:
        try:
            dc_device.turn_off()
            dc_device.close()
        except Exception:
            pass


def register_console_ctrl_handler(ac_device, dc_device):
    global _console_ctrl_handler_ref
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.DWORD)

    def handler(ctrl_type):
        if ctrl_type == CTRL_CLOSE_EVENT:
            emergency_off(ac_device, dc_device)
        return False

    _console_ctrl_handler_ref = handler_type(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_ctrl_handler_ref, True)


# ── DC Settings dialog ────────────────────────────────────────────────────────

class AddressSettingsDialog(QDialog):
    def __init__(self, title_text: str, current_address: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device Settings")
        self.setFixedWidth(360)
        self.setStyleSheet("background-color: #F5F6FA;")

        main_layout = QVBoxLayout()
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet("background-color: #1C2B4B;")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        h_lbl = QLabel(title_text)
        h_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        h_lbl.setStyleSheet("color: #FFFFFF; background: transparent;")
        header_layout.addWidget(h_lbl)
        main_layout.addWidget(header)

        body = QWidget()
        body_layout = QFormLayout(body)
        body_layout.setContentsMargins(20, 20, 20, 12)
        body_layout.setSpacing(10)

        addr_lbl = QLabel("Resource Address:")
        addr_lbl.setFont(QFont("Segoe UI", 9))
        addr_lbl.setStyleSheet("color: #37474F;")

        self.addr_input = QLineEdit(current_address)
        self.addr_input.setFont(QFont("Segoe UI", 10))
        self.addr_input.setFixedHeight(32)
        self.addr_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #CFD8DC;
                border-radius: 3px;
                padding: 0 8px;
                background: white;
                color: #212121;
            }
            QLineEdit:focus { border: 1px solid #1565C0; }
        """)
        body_layout.addRow(addr_lbl, self.addr_input)
        main_layout.addWidget(body)

        btn_widget = QWidget()
        btn_row = QHBoxLayout(btn_widget)
        btn_row.setContentsMargins(20, 4, 20, 16)
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFixedSize(80, 30)
        cancel_btn.setFont(QFont("Segoe UI", 9))
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #ECEFF1;
                color: #546E7A;
                border: 1px solid #CFD8DC;
                border-radius: 3px;
            }
            QPushButton:hover { background-color: #E0E4EA; }
        """)
        cancel_btn.clicked.connect(self.reject)

        ok_btn = QPushButton("Apply")
        ok_btn.setFixedSize(80, 30)
        ok_btn.setFont(QFont("Segoe UI", 9, QFont.Bold))
        ok_btn.setStyleSheet("""
            QPushButton {
                background-color: #1565C0;
                color: white;
                border: none;
                border-radius: 3px;
            }
            QPushButton:hover { background-color: #1976D2; }
        """)
        ok_btn.clicked.connect(self.accept)

        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        main_layout.addWidget(btn_widget)

        self.setLayout(main_layout)

    def get_address(self) -> str:
        return self.addr_input.text().strip()


# ── Main combined window ──────────────────────────────────────────────────────

class ACDCControlWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ac_state = False
        self.dc_state = False
        self.ac_device = None
        self.dc_device = None
        self.ac_phase = 3
        self.ac_config = load_ac_config()
        AC_CONFIG["Setting"]["resource_address"] = self.ac_config["resource_address"]
        self.dc_config = load_dc_config()
        self._init_devices()
        self._init_ui()

    def _init_devices(self):
        try:
            logging.getLogger("ATS.AC_Source")
            self.ac_device = ATSDeviceConfigurator("AC_Source", AC_CONFIG)
            self.ac_device.setup()
        except Exception as e:
            QMessageBox.critical(None, "Device Error", f"Cannot connect to AC Source:\n{e}")
            self.ac_device = None

        try:
            device = ChromaDCLoad(self.dc_config["resource_address"])
            device.connect()
            self.dc_device = device
        except Exception as e:
            QMessageBox.critical(None, "Device Error", f"Cannot connect to DC Load:\n{e}")
            self.dc_device = None

        atexit.register(emergency_off, self.ac_device, self.dc_device)
        signal.signal(signal.SIGTERM,  lambda *_: emergency_off(self.ac_device, self.dc_device))
        signal.signal(signal.SIGINT,   lambda *_: emergency_off(self.ac_device, self.dc_device))
        signal.signal(signal.SIGBREAK, lambda *_: emergency_off(self.ac_device, self.dc_device))
        register_console_ctrl_handler(self.ac_device, self.dc_device)

    def _init_ui(self):
        self.setWindowTitle("AC & DC Control")
        self.setFixedSize(500, 450)

        main_layout = QVBoxLayout()
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # ── Header ──────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(72)
        header.setStyleSheet("background-color: #1C2B4B;")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 12, 20, 12)
        header_layout.setSpacing(3)

        title_lbl = QLabel("AC & DC SOURCE CONTROL")
        title_lbl.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title_lbl.setStyleSheet("color: #FFFFFF; background: transparent;")

        ac_addr = AC_CONFIG["Setting"]["resource_address"]
        dc_addr = self.dc_config.get("resource_address", "")
        self.subtitle_lbl = QLabel(
            f"AC: CHROMA 61815  |  {ac_addr}          "
            f"DC: CHROMA DC LOAD  |  {dc_addr}"
        )
        self.subtitle_lbl.setFont(QFont("Segoe UI", 8))
        self.subtitle_lbl.setStyleSheet("color: #90A4AE; background: transparent;")

        header_layout.addWidget(title_lbl)
        header_layout.addWidget(self.subtitle_lbl)
        main_layout.addWidget(header)

        # ── Body (two panels stacked) ────────────────────────────
        body = QWidget()
        body.setStyleSheet("background-color: #F5F6FA;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        body_layout.addWidget(self._build_ac_panel())

        hdiv = QWidget()
        hdiv.setFixedHeight(1)
        hdiv.setStyleSheet("background-color: #DDE1EA;")
        body_layout.addWidget(hdiv)

        body_layout.addWidget(self._build_dc_panel())
        main_layout.addWidget(body)

        # ── Footer ──────────────────────────────────────────────
        footer = QWidget()
        footer.setFixedHeight(30)
        footer.setStyleSheet("background-color: #EAECF2; border-top: 1px solid #DDE1EA;")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 0, 16, 0)

        ac_status = "Connected" if self.ac_device else "Not Connected"
        ac_color = "#1565C0" if self.ac_device else "#9E9E9E"
        self.ac_device_label = QLabel(f"● AC: {ac_status}")
        self.ac_device_label.setFont(QFont("Segoe UI", 8))
        self.ac_device_label.setStyleSheet(f"color: {ac_color}; background: transparent;")
        footer_layout.addWidget(self.ac_device_label)

        footer_layout.addSpacing(24)

        dc_status = "Connected" if self.dc_device else "Not Connected"
        dc_color = "#1565C0" if self.dc_device else "#9E9E9E"
        self.dc_device_label = QLabel(f"● DC: {dc_status}")
        self.dc_device_label.setFont(QFont("Segoe UI", 8))
        self.dc_device_label.setStyleSheet(f"color: {dc_color}; background: transparent;")
        footer_layout.addWidget(self.dc_device_label)

        footer_layout.addStretch()

        _link_style = """
            QPushButton {
                background-color: transparent;
                color: #78909C;
                border: none;
                text-decoration: underline;
            }
            QPushButton:hover { color: #1565C0; }
        """

        ac_settings_btn = QPushButton("AC Settings")
        ac_settings_btn.setFixedHeight(20)
        ac_settings_btn.setFont(QFont("Segoe UI", 8))
        ac_settings_btn.setStyleSheet(_link_style)
        ac_settings_btn.clicked.connect(self.open_ac_settings)
        footer_layout.addWidget(ac_settings_btn)

        footer_layout.addSpacing(12)

        dc_settings_btn = QPushButton("DC Settings")
        dc_settings_btn.setFixedHeight(20)
        dc_settings_btn.setFont(QFont("Segoe UI", 8))
        dc_settings_btn.setStyleSheet(_link_style)
        dc_settings_btn.clicked.connect(self.open_dc_settings)
        footer_layout.addWidget(dc_settings_btn)

        main_layout.addWidget(footer)
        self.setLayout(main_layout)

    # ── Panel builders ────────────────────────────────────────────────────────

    def _build_ac_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background-color: #F5F6FA;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 18, 24, 14)
        layout.setSpacing(14)

        header_row = QHBoxLayout()
        sec_lbl = QLabel("AC SOURCE")
        sec_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        sec_lbl.setStyleSheet("color: #78909C; background: transparent;")
        self.ac_led = QLabel()
        self.ac_led.setFixedSize(14, 14)
        self.ac_led.setStyleSheet("background-color: #78909C; border-radius: 7px;")
        header_row.addWidget(sec_lbl)
        header_row.addStretch()
        header_row.addWidget(self.ac_led)
        layout.addLayout(header_row)

        # Input card
        input_card = QWidget()
        input_card.setStyleSheet(
            "background-color: #FFFFFF; border: 1px solid #DDE1EA; border-radius: 4px;"
        )
        input_card_layout = QVBoxLayout(input_card)
        input_card_layout.setContentsMargins(16, 12, 16, 12)
        input_card_layout.setSpacing(10)

        spinbox_style = """
            QDoubleSpinBox {
                border: 1px solid #CFD8DC;
                border-radius: 3px;
                padding: 0 6px;
                background: white;
                color: #212121;
            }
            QDoubleSpinBox:focus { border: 1px solid #1565C0; }
        """

        # Row 1: Voltage | Current
        row1 = QHBoxLayout()
        row1.setSpacing(20)

        volt_col = QVBoxLayout()
        volt_col.setSpacing(4)
        volt_lbl = QLabel("VOLTAGE (V)")
        volt_lbl.setFont(QFont("Segoe UI", 8))
        volt_lbl.setStyleSheet("color: #78909C; background: transparent; border: none;")
        self.ac_volt_input = QDoubleSpinBox()
        self.ac_volt_input.setRange(0, 300)
        self.ac_volt_input.setDecimals(1)
        self.ac_volt_input.setValue(230.0)
        self.ac_volt_input.setFixedHeight(36)
        self.ac_volt_input.setFont(QFont("Segoe UI", 12))
        self.ac_volt_input.setStyleSheet(spinbox_style)
        volt_col.addWidget(volt_lbl)
        volt_col.addWidget(self.ac_volt_input)

        vdiv = QWidget()
        vdiv.setFixedWidth(1)
        vdiv.setStyleSheet("background-color: #DDE1EA; border: none;")

        curr_col = QVBoxLayout()
        curr_col.setSpacing(4)
        curr_lbl = QLabel("CURRENT (A)")
        curr_lbl.setFont(QFont("Segoe UI", 8))
        curr_lbl.setStyleSheet("color: #78909C; background: transparent; border: none;")
        self.ac_curr_input = QDoubleSpinBox()
        self.ac_curr_input.setRange(0, 30)
        self.ac_curr_input.setDecimals(1)
        self.ac_curr_input.setValue(30.0)
        self.ac_curr_input.setFixedHeight(36)
        self.ac_curr_input.setFont(QFont("Segoe UI", 12))
        self.ac_curr_input.setStyleSheet(spinbox_style)
        curr_col.addWidget(curr_lbl)
        curr_col.addWidget(self.ac_curr_input)

        row1.addLayout(volt_col)
        row1.addWidget(vdiv)
        row1.addLayout(curr_col)
        input_card_layout.addLayout(row1)

        # Horizontal divider
        hdiv = QWidget()
        hdiv.setFixedHeight(1)
        hdiv.setStyleSheet("background-color: #DDE1EA; border: none;")
        input_card_layout.addWidget(hdiv)

        # Row 2: Phase selection
        phase_row = QHBoxLayout()
        phase_row.setSpacing(8)

        phase_lbl = QLabel("PHASE:")
        phase_lbl.setFont(QFont("Segoe UI", 8))
        phase_lbl.setStyleSheet("color: #78909C; background: transparent; border: none;")
        phase_row.addWidget(phase_lbl)

        self.ac_phase_1_btn = QPushButton("1φ")
        self.ac_phase_1_btn.setFixedSize(48, 28)
        self.ac_phase_1_btn.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.ac_phase_1_btn.setCursor(Qt.PointingHandCursor)
        self.ac_phase_1_btn.clicked.connect(lambda: self._set_ac_phase(1))

        self.ac_phase_3_btn = QPushButton("3φ")
        self.ac_phase_3_btn.setFixedSize(48, 28)
        self.ac_phase_3_btn.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.ac_phase_3_btn.setCursor(Qt.PointingHandCursor)
        self.ac_phase_3_btn.clicked.connect(lambda: self._set_ac_phase(3))

        phase_row.addWidget(self.ac_phase_1_btn)
        phase_row.addWidget(self.ac_phase_3_btn)
        phase_row.addStretch()
        input_card_layout.addLayout(phase_row)

        self._set_ac_phase(3)

        self.ac_toggle_btn = QPushButton("TURN ON")
        self.ac_toggle_btn.setFixedWidth(100)
        self.ac_toggle_btn.setMinimumHeight(80)
        self.ac_toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.ac_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.ac_toggle_btn.setStyleSheet(self._btn_blue())
        self.ac_toggle_btn.clicked.connect(self.toggle_ac)
        ac_content_row = QHBoxLayout()
        ac_content_row.setSpacing(10)
        ac_content_row.addWidget(input_card)
        ac_content_row.addWidget(self.ac_toggle_btn)
        layout.addLayout(ac_content_row)

        return panel

    def _build_dc_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet("background-color: #F5F6FA;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 18, 24, 14)
        layout.setSpacing(14)

        header_row = QHBoxLayout()
        sec_lbl = QLabel("DC LOAD")
        sec_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        sec_lbl.setStyleSheet("color: #78909C; background: transparent;")
        self.dc_led = QLabel()
        self.dc_led.setFixedSize(14, 14)
        self.dc_led.setStyleSheet("background-color: #78909C; border-radius: 7px;")
        header_row.addWidget(sec_lbl)
        header_row.addStretch()
        header_row.addWidget(self.dc_led)
        layout.addLayout(header_row)

        # Input card
        input_card = QWidget()
        input_card.setStyleSheet(
            "background-color: #FFFFFF; border: 1px solid #DDE1EA; border-radius: 4px;"
        )
        input_card_layout = QHBoxLayout(input_card)
        input_card_layout.setContentsMargins(16, 12, 16, 12)
        input_card_layout.setSpacing(20)

        volt_col = QVBoxLayout()
        volt_col.setSpacing(4)
        volt_lbl = QLabel("VOLTAGE (V)")
        volt_lbl.setFont(QFont("Segoe UI", 8))
        volt_lbl.setStyleSheet("color: #78909C; background: transparent; border: none;")
        self.volt_input = QDoubleSpinBox()
        self.volt_input.setRange(0, 80)
        self.volt_input.setDecimals(1)
        self.volt_input.setValue(54.0)
        self.volt_input.setFixedHeight(36)
        self.volt_input.setFont(QFont("Segoe UI", 12))
        self.volt_input.setStyleSheet("""
            QDoubleSpinBox {
                border: 1px solid #CFD8DC;
                border-radius: 3px;
                padding: 0 6px;
                background: white;
                color: #212121;
            }
            QDoubleSpinBox:focus { border: 1px solid #1565C0; }
        """)
        volt_col.addWidget(volt_lbl)
        volt_col.addWidget(self.volt_input)

        vdiv = QWidget()
        vdiv.setFixedWidth(1)
        vdiv.setStyleSheet("background-color: #DDE1EA; border: none;")

        curr_col = QVBoxLayout()
        curr_col.setSpacing(4)
        curr_lbl = QLabel("CURRENT (A)")
        curr_lbl.setFont(QFont("Segoe UI", 8))
        curr_lbl.setStyleSheet("color: #78909C; background: transparent; border: none;")
        self.curr_input = QDoubleSpinBox()
        self.curr_input.setRange(0, 60)
        self.curr_input.setDecimals(1)
        self.curr_input.setValue(2.0)
        self.curr_input.setFixedHeight(36)
        self.curr_input.setFont(QFont("Segoe UI", 12))
        self.curr_input.setStyleSheet("""
            QDoubleSpinBox {
                border: 1px solid #CFD8DC;
                border-radius: 3px;
                padding: 0 6px;
                background: white;
                color: #212121;
            }
            QDoubleSpinBox:focus { border: 1px solid #1565C0; }
        """)
        curr_col.addWidget(curr_lbl)
        curr_col.addWidget(self.curr_input)

        input_card_layout.addLayout(volt_col)
        input_card_layout.addWidget(vdiv)
        input_card_layout.addLayout(curr_col)

        self.dc_toggle_btn = QPushButton("TURN ON")
        self.dc_toggle_btn.setFixedWidth(100)
        self.dc_toggle_btn.setMinimumHeight(80)
        self.dc_toggle_btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.dc_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.dc_toggle_btn.setStyleSheet(self._btn_blue())
        self.dc_toggle_btn.clicked.connect(self.toggle_dc)
        dc_content_row = QHBoxLayout()
        dc_content_row.setSpacing(10)
        dc_content_row.addWidget(input_card)
        dc_content_row.addWidget(self.dc_toggle_btn)
        layout.addLayout(dc_content_row)

        return panel

    # ── Style helpers ─────────────────────────────────────────────────────────

    def _btn_blue(self) -> str:
        return """
            QPushButton {
                background-color: #1565C0;
                color: white;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover { background-color: #1976D2; }
            QPushButton:pressed { background-color: #0D47A1; }
        """

    def _btn_red(self) -> str:
        return """
            QPushButton {
                background-color: #C62828;
                color: white;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover { background-color: #D32F2F; }
            QPushButton:pressed { background-color: #B71C1C; }
        """

    # ── AC toggle ─────────────────────────────────────────────────────────────

    def toggle_ac(self):
        self.ac_state = not self.ac_state
        self._update_ac_ui()
        if self.ac_state:
            self._turn_on_ac()
        else:
            self._turn_off_ac()

    def _update_ac_ui(self):
        if self.ac_state:
            self.ac_led.setStyleSheet("background-color: #43A047; border-radius: 7px;")
            self.ac_toggle_btn.setText("TURN OFF")
            self.ac_toggle_btn.setStyleSheet(self._btn_red())
        else:
            self.ac_led.setStyleSheet("background-color: #78909C; border-radius: 7px;")
            self.ac_toggle_btn.setText("TURN ON")
            self.ac_toggle_btn.setStyleSheet(self._btn_blue())

    def _turn_on_ac(self):
        if self.ac_device:
            try:
                phase_str = "THREE" if self.ac_phase == 3 else "SINGLE"
                self.ac_device.set_phase(phase_str)
                self.ac_device.set_voltage_limit(300)
                self.ac_device.set_current_limit(self.ac_curr_input.value())
                if self.ac_phase == 3:
                    self.ac_device.set_phase_angle_1_2(120)
                    self.ac_device.set_phase_angle_1_3(240)
                self.ac_device.set_voltage_ac(self.ac_volt_input.value())
                self.ac_device.set_frequency(50)
                self.ac_device.turn_on()
            except Exception as e:
                QMessageBox.warning(self, "Error", f"AC Turn ON failed:\n{e}")
                self.ac_state = False
                self._update_ac_ui()

    def _turn_off_ac(self):
        if self.ac_device:
            try:
                self.ac_device.turn_off()
            except Exception as e:
                QMessageBox.warning(self, "Error", f"AC Turn OFF failed:\n{e}")
                self.ac_state = True
                self._update_ac_ui()

    def _set_ac_phase(self, n: int):
        self.ac_phase = n
        selected = """
            QPushButton {
                background-color: #1565C0; color: white;
                border: none; border-radius: 3px;
            }
        """
        unselected = """
            QPushButton {
                background-color: #ECEFF1; color: #546E7A;
                border: 1px solid #CFD8DC; border-radius: 3px;
            }
            QPushButton:hover { background-color: #E0E4EA; }
        """
        self.ac_phase_1_btn.setStyleSheet(selected if n == 1 else unselected)
        self.ac_phase_3_btn.setStyleSheet(selected if n == 3 else unselected)

    # ── DC toggle ─────────────────────────────────────────────────────────────

    def toggle_dc(self):
        self.dc_state = not self.dc_state
        self._update_dc_ui()
        if self.dc_state:
            self._turn_on_dc()
        else:
            self._turn_off_dc()

    def _update_dc_ui(self):
        if self.dc_state:
            self.dc_led.setStyleSheet("background-color: #43A047; border-radius: 7px;")
            self.dc_toggle_btn.setText("TURN OFF")
            self.dc_toggle_btn.setStyleSheet(self._btn_red())
        else:
            self.dc_led.setStyleSheet("background-color: #78909C; border-radius: 7px;")
            self.dc_toggle_btn.setText("TURN ON")
            self.dc_toggle_btn.setStyleSheet(self._btn_blue())

    def _turn_on_dc(self):
        if self.dc_device:
            try:
                self.dc_device.turn_on(self.volt_input.value(), self.curr_input.value())
            except Exception as e:
                QMessageBox.warning(self, "Error", f"DC Turn ON failed:\n{e}")
                self.dc_state = False
                self._update_dc_ui()

    def _turn_off_dc(self):
        if self.dc_device:
            try:
                self.dc_device.turn_off()
            except Exception as e:
                QMessageBox.warning(self, "Error", f"DC Turn OFF failed:\n{e}")
                self.dc_state = True
                self._update_dc_ui()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _update_subtitle(self):
        ac_addr = self.ac_config.get("resource_address", "")
        dc_addr = self.dc_config.get("resource_address", "")
        self.subtitle_lbl.setText(
            f"AC: CHROMA 61815  |  {ac_addr}          "
            f"DC: CHROMA DC LOAD  |  {dc_addr}"
        )

    def open_ac_settings(self):
        dlg = AddressSettingsDialog("AC DEVICE SETTINGS", self.ac_config["resource_address"], self)
        if dlg.exec_() == QDialog.Accepted:
            new_addr = dlg.get_address()
            if new_addr != self.ac_config["resource_address"]:
                self.ac_config["resource_address"] = new_addr
                save_ac_config(self.ac_config)
                self._update_subtitle()
                QMessageBox.information(
                    self, "Settings Saved",
                    f"บันทึกแล้ว: {new_addr}\nรีสตาร์ทโปรแกรมเพื่อเชื่อมต่อใหม่"
                )

    def open_dc_settings(self):
        dlg = AddressSettingsDialog("DC DEVICE SETTINGS", self.dc_config["resource_address"], self)
        if dlg.exec_() == QDialog.Accepted:
            new_addr = dlg.get_address()
            if new_addr != self.dc_config["resource_address"]:
                self.dc_config["resource_address"] = new_addr
                save_dc_config(self.dc_config)
                self._update_subtitle()
                QMessageBox.information(
                    self, "Settings Saved",
                    f"บันทึกแล้ว: {new_addr}\nรีสตาร์ทโปรแกรมเพื่อเชื่อมต่อใหม่"
                )

    def closeEvent(self, event):
        emergency_off(self.ac_device, self.dc_device)
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ACDCControlWindow()
    window.show()
    sys.exit(app.exec_())
