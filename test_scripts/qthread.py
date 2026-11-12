# Importing packages
import sys
import time
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QLabel
from PyQt6.QtCore import QObject, QThread, pyqtSignal

import asyncio
import moteus

#Calibration Script Parameters
MOTOR_ID = 1
####Safety limits
MAX_VELOCITY_REV_S = 2.5
MAX_BOARD_TEMP_C = 65.0
MAX_TORQUE_NM = 1.0
MAX_DISPLACEMENT_REV = 1.0  # How far the motor may go from the hold position

####Search range for each gain (servo.pid_position.<name>)
GAIN_LIMITS = {
    "kp": (0.0, 50.0),
    "ki": (0.0, 0.2),
    "kd": (0.0, 2.0),
}
####Which way the gain should move when the motor overshoots. Too much p or i
####causes overshoot, while more d damps it.
OVERSHOOT_LOWERS_GAIN = {"kp": True, "ki": True, "kd": False}
TUNE_ORDER = ["kp", "kd", "ki"]

####Answers to the overshoot prompt
OVERSHOOT, UNDERSHOOT, GOOD = 0, 1, 2

####Need to define
HZ = 100
MAX_MISSED_REPLIES = 5


class Worker(QObject):
    # Define signals to communicate with the main thread
    progress = pyqtSignal(int)
    askOvershootButtons = pyqtSignal()
    finished = pyqtSignal()
    updateKp = pyqtSignal(float)
    
    def __init__(self):
        super().__init__()
        self.motor_id = MOTOR_ID
        self.overshoot = False
        self.save = False
        self.overshootEvent = asyncio.Event()
        self.stream = None
        self.gains = {}
        
    def run_async_calibration(self):
        asyncio.run(self._async_main())

    async def _async_main(self):
        c = moteus.Controller(id=self.motor_id)
        self.stream = moteus.Stream(c)
        
        await self.stream.write_message(b'tel stop')
        await self.stream.flush_read()
        await c.set_stop()
        
        original = {name: await self.get_gain(self.stream, name) for name in GAIN_LIMITS}
        hold_position_rev = (await c.query()).values[moteus.Register.POSITION]
        print(f"Holding at {hold_position_rev:.3f} rev. Original gains: {self.format_gains(original)}")
        
        self.gains = {name: lo for name, (lo, _) in GAIN_LIMITS.items()}
        for name, value in self.gains.items():
            await self.set_gain(self.stream, name, value)
            
        hold_task = asyncio.create_task(self.hold_position(c, hold_position_rev))
        await self.set_gain(self.stream, name, 10)
        await self.runKp()
        
        hold_task.cancel()
        self.finished.emit()
    
    # Helper functions
    async def hold_position(self, controller, position):
        missed = 0
        try:
            while True:
                result = await controller.set_position(
                    position=position,
                    velocity=0.0,
                    maximum_torque=MAX_TORQUE_NM,
                    watchdog_timeout=0.2,
                    query=True,
                )

                if result is None:
                    missed += 1
                    if missed > MAX_MISSED_REPLIES:
                        raise RuntimeError("Lost communication with the controller")
                else:
                    missed = 0
                    self.check_limits(result, position)

                await asyncio.sleep(1 / HZ)

        except RuntimeError as e:
            print(f"\nSAFETY STOP: {e}")
            raise
        finally:
            await controller.set_stop()

    def check_limits(self, result, hold_pos):  # Added self
        mode = result.values.get(moteus.Register.MODE, moteus.Mode.POSITION)
        vel = abs(result.values.get(moteus.Register.VELOCITY, 0.0))
        pos = result.values.get(moteus.Register.POSITION, hold_pos)
        temp = result.values.get(moteus.Register.TEMPERATURE, 0.0)

        if mode in (moteus.Mode.FAULT, moteus.Mode.TIMEOUT):
            fault = result.values.get(moteus.Register.FAULT, 0)
            raise RuntimeError(f"Controller fault: mode={mode} code={fault}")
        if vel > MAX_VELOCITY_REV_S:
            raise RuntimeError(f"Max speed exceeded: {vel:.2f} rev/s")
        if abs(pos - hold_pos) > MAX_DISPLACEMENT_REV:
            raise RuntimeError(f"Motor went past {pos - hold_pos:.2f} rev from hold position")
        if temp > MAX_BOARD_TEMP_C:
            raise RuntimeError(f"Board is at {temp:.1f} °C")

    async def get_gain(self, stream, name):  # Added self
        print("Setup motor before")
        name = str(name).encode()
        command_str = b'conf get servo.pid_position.' + name
        reply = float((await stream.command(command_str, allow_any_response=True)).decode())
        return reply

    async def set_gain(self, stream, name, value):  # Added self
        await stream.command(f'conf set servo.pid_position.{name} {value:.6g}'.encode())

    def format_gains(self, gains):  # Added self
        return " ".join(f"{name}={value:.4g}" for name, value in gains.items())
    
    def setOvershoot(self, overshootBool):
        self.overshoot = overshootBool
        self.overshootEvent.set()

    def setSave(self, saveBool):
        self.save = saveBool
        
    # Calibration methods
    async def runKp(self):
        name = "kp"
        lo, hi = GAIN_LIMITS[name]
        while True:
            self.gains[name] = (lo + hi) / 2
            await self.set_gain(self.stream, name, self.gains[name])
            
            self.overshootEvent.clear()
            self.askOvershootButtons.emit()
            await self.overshootEvent.wait()
            
            if (self.overshoot == True) == OVERSHOOT_LOWERS_GAIN[name]:
                    hi = self.gains[name]
            else:
                    lo = self.gains[name]
            print("Updated")
            await self.get_gain(self.stream, name)
            self.updateKp.emit(self.gains[name])
        self.finished.emit()



# 2. Setup Main Window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PID Calibration Tool")

        layout = QVBoxLayout()

        self.kpLabel    = QLabel("Kp: ")
        self.kdLabel    = QLabel("Kd: ")
        self.kiLabel    = QLabel("Ki: ")
        self.kpButton   = QPushButton("Begin kp Calibration...", self)
        self.kdButton   = QPushButton("Begin ki Calibration...", self)
        self.kiButton   = QPushButton("Begin kd Calibration...", self)
        
        self.kpButton.clicked.connect(self.startKpCalibration)

        self.overshootYesButton = QPushButton("Yes Overshoot", self)
        self.overshootNoButton  = QPushButton("No Overshoot", self)
        self.saveButton         = QPushButton("Save Calibration", self)
        
        # Correctly wire using lambdas (so worker isn't called instantly on startup)
        self.overshootYesButton.clicked.connect(lambda: self.handle_overshoot(True))
        self.overshootNoButton.clicked.connect(lambda: self.handle_overshoot(False))
        
        self.saveButton.setCheckable(True)
        
        self.overshootYesButton.hide()
        self.overshootNoButton.hide()
        
        # Add widgets to layout
        layout.addWidget(self.kpLabel)
        layout.addWidget(self.kdLabel)
        layout.addWidget(self.kiLabel)
        layout.addWidget(self.kpButton)
        layout.addWidget(self.kdButton)
        layout.addWidget(self.kiButton)
        layout.addWidget(self.overshootYesButton)
        layout.addWidget(self.overshootNoButton)
        layout.addWidget(self.saveButton)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        self.thread = None
        self.worker = None

    def startKpCalibration(self):
        self.kpButton.setEnabled(False)
        self.kdButton.setEnabled(False)
        self.kiButton.setEnabled(False)

        self.thread = QThread()
        self.worker = Worker()

        self.worker.moveToThread(self.thread)
        
        # Connect thread start to the synchronous runner wrapper
        self.thread.started.connect(self.worker.run_async_calibration)
        
        self.worker.updateKp.connect(self.updateKp)
        self.worker.finished.connect(self.task_complete)
        self.worker.askOvershootButtons.connect(self.enableOvershoot)

        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)

        self.thread.start()

    def handle_overshoot(self, val):
        if self.worker:
            self.worker.setOvershoot(val)
        self.overshootYesButton.hide()
        self.overshootNoButton.hide()

    def enableOvershoot(self):
        self.overshootYesButton.show()
        self.overshootNoButton.show()

    def updateKp(self, value):
        self.kpLabel.setText(f"Kp: {value:.4f}")

    def task_complete(self):
        self.kpLabel.setText("Kp calibration finished!")
        self.kpButton.setEnabled(True)
        self.kdButton.setEnabled(True)
        self.kiButton.setEnabled(True)
        
    
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())