import asyncio
import sys

import moteus
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

#Calibration Script Parameters
MOTOR_ID = 1
####Safety limits
MAX_VELOCITY_REV_S = 2.5
MAX_BOARD_TEMP_C = 65.0
MAX_TORQUE_NM = 1.0
MAX_DISPLACEMENT_REV = 1.0  # How far the motor may go from the hold position

####Search range for each gain (servo.pid_position.<name>)
GAIN_LIMITS = {
    "kp": (0.0, 20.0),
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
    askSaveButtons = pyqtSignal()
    finished = pyqtSignal()
    failed = pyqtSignal(str)
    updateGain = pyqtSignal(str, float)
    
    def __init__(self):
        super().__init__()
        self.motor_id = MOTOR_ID
        self.overshoot = OVERSHOOT
        self.save = False
        self.answerEvent = None
        self.loop = None
        self.mainTask = None
        self.stream = None
        self.gains = {}
        
    def run_async_calibration(self):
        # Exceptions must not escape into Qt, and finished must always be emitted
        # so the GUI never stays locked
        try:
            asyncio.run(self._async_main())
        except asyncio.CancelledError:
            print("\nCalibration cancelled")
        except Exception as e:
            print(f"\nCalibration stopped: {e}")
            self.failed.emit(str(e))
        finally:
            self.finished.emit()

    async def _async_main(self):
        self.loop = asyncio.get_running_loop()
        self.mainTask = asyncio.current_task()
        self.answerEvent = asyncio.Event()

        c = moteus.Controller(id=self.motor_id)
        self.stream = moteus.Stream(c)
        
        await self.stream.write_message(b'tel stop')
        await self.stream.flush_read()
        await c.set_stop()
        
        original = {name: await self.get_gain(self.stream, name) for name in GAIN_LIMITS}
        hold_position_rev = (await c.query()).values[moteus.Register.POSITION]
        print(f"Holding at {hold_position_rev:.3f} rev. Original gains: {self.format_gains(original)}")
        
        self.gains = {name: lo for name, (lo, _) in GAIN_LIMITS.items()}
        hold_task = None
        self.save = False

        try:
            for name, value in self.gains.items():
                await self.set_gain(self.stream, name, value)

            hold_task = asyncio.create_task(self.hold_position(c, hold_position_rev))

            for name in TUNE_ORDER:
                await self.runGain(name, hold_task)

            print(f"\nFinal gains: {self.format_gains(self.gains)}")
            await self.ask(self.askSaveButtons, hold_task)

        finally:
            if hold_task is not None:
                hold_task.cancel()  # Stops the motor
                await asyncio.gather(hold_task, return_exceptions=True)

            # Put the original gains back unless the new ones are being kept
            if self.save:
                await self.stream.command(b"conf write")
                print("Saved.")
            else:
                for name, value in original.items():
                    await self.set_gain(self.stream, name, value)
                print(f"Not saved. Restored original gains: {self.format_gains(original)}")
    
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
        name = str(name).encode()
        command_str = b'conf get servo.pid_position.' + name
        reply = float((await stream.command(command_str, allow_any_response=True)).decode())
        return reply

    async def set_gain(self, stream, name, value):  # Added self
        await stream.command(f'conf set servo.pid_position.{name} {value:.6g}'.encode())

    def format_gains(self, gains):  # Added self
        return " ".join(f"{name}={value:.4g}" for name, value in gains.items())
    
    # These run on the GUI thread, so the event has to be set through the worker's loop
    def setOvershoot(self, answer):
        self.overshoot = answer
        self.wakeUp()

    def setSave(self, saveBool):
        self.save = saveBool
        self.wakeUp()

    def wakeUp(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.answerEvent.set)

    def stop(self):
        if self.loop is not None and self.mainTask is not None:
            self.loop.call_soon_threadsafe(self.mainTask.cancel)

    async def ask(self, askSignal, hold_task):
        # Clear before asking so an answer can't be missed
        self.answerEvent.clear()
        askSignal.emit()

        # The hold task keeps running while this waits, so the safety checks stay active
        waiter = asyncio.ensure_future(self.answerEvent.wait())
        await asyncio.wait({waiter, hold_task}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if hold_task.done():
            hold_task.result()  # Raises the safety stop

    # Calibration methods
    async def runGain(self, name, hold_task):
        lo, hi = GAIN_LIMITS[name]
        print(f"\n--- Tuning {name} ---")

        # Keep narrowing the range until the user accepts the current value
        while True:
            self.gains[name] = (lo + hi) / 2
            await self.set_gain(self.stream, name, self.gains[name])
            self.updateGain.emit(name, self.gains[name])
            print(f"Now using: {self.format_gains(self.gains)}")

            await self.ask(self.askOvershootButtons, hold_task)

            if self.overshoot == GOOD:
                print(f"Keeping {name}={self.gains[name]:.4g}")
                break
            if (self.overshoot == OVERSHOOT) == OVERSHOOT_LOWERS_GAIN[name]:
                hi = self.gains[name]
            else:
                lo = self.gains[name]



# 2. Setup Main Window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PID Calibration Tool")

        layout = QVBoxLayout()

        self.kpLabel    = QLabel("Kp: ")
        self.kdLabel    = QLabel("Kd: ")
        self.kiLabel    = QLabel("Ki: ")
        self.statusLabel = QLabel("")
        self.startButton = QPushButton("Begin Calibration...", self)
        
        self.startButton.clicked.connect(self.startCalibration)

        self.overshootYesButton  = QPushButton("Yes Overshoot", self)
        self.overshootNoButton   = QPushButton("Undershoot", self)
        self.overshootGoodButton = QPushButton("Good As Is", self)
        self.saveButton          = QPushButton("Save Calibration", self)
        self.discardButton       = QPushButton("Discard Calibration", self)
        
        # Correctly wire using lambdas (so worker isn't called instantly on startup)
        self.overshootYesButton.clicked.connect(lambda: self.handle_overshoot(OVERSHOOT))
        self.overshootNoButton.clicked.connect(lambda: self.handle_overshoot(UNDERSHOOT))
        self.overshootGoodButton.clicked.connect(lambda: self.handle_overshoot(GOOD))
        self.saveButton.clicked.connect(lambda: self.handle_save(True))
        self.discardButton.clicked.connect(lambda: self.handle_save(False))
        
        self.hideAnswerButtons()
        
        # Add widgets to layout
        layout.addWidget(self.kpLabel)
        layout.addWidget(self.kdLabel)
        layout.addWidget(self.kiLabel)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.startButton)
        layout.addWidget(self.overshootYesButton)
        layout.addWidget(self.overshootNoButton)
        layout.addWidget(self.overshootGoodButton)
        layout.addWidget(self.saveButton)
        layout.addWidget(self.discardButton)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        self.thread = None
        self.worker = None
        self.hadError = False

    def startCalibration(self):
        self.startButton.setEnabled(False)
        self.statusLabel.setText("Calibrating...")
        self.hadError = False

        self.thread = QThread()
        self.worker = Worker()

        self.worker.moveToThread(self.thread)
        
        # Connect thread start to the synchronous runner wrapper
        self.thread.started.connect(self.worker.run_async_calibration)
        
        self.worker.updateGain.connect(self.updateGain)
        self.worker.failed.connect(self.task_failed)
        self.worker.finished.connect(self.task_complete)
        self.worker.askOvershootButtons.connect(self.enableOvershoot)
        self.worker.askSaveButtons.connect(self.enableSave)

        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.clearThread)

        self.thread.start()

    def handle_overshoot(self, val):
        self.hideAnswerButtons()
        if self.worker:
            self.worker.setOvershoot(val)

    def handle_save(self, val):
        self.hideAnswerButtons()
        if self.worker:
            self.worker.setSave(val)

    def hideAnswerButtons(self):
        self.overshootYesButton.hide()
        self.overshootNoButton.hide()
        self.overshootGoodButton.hide()
        self.saveButton.hide()
        self.discardButton.hide()

    def enableOvershoot(self):
        self.overshootYesButton.show()
        self.overshootNoButton.show()
        self.overshootGoodButton.show()

    def enableSave(self):
        self.saveButton.show()
        self.discardButton.show()

    def updateGain(self, name, value):
        label = {"kp": self.kpLabel, "kd": self.kdLabel, "ki": self.kiLabel}[name]
        label.setText(f"{name.capitalize()}: {value:.4f}")

    def task_failed(self, message):
        self.hadError = True
        self.statusLabel.setText(f"Stopped: {message}")

    def task_complete(self):
        self.hideAnswerButtons()
        if not self.hadError:
            self.statusLabel.setText("Calibration finished!")
        self.startButton.setEnabled(True)

    def clearThread(self):
        self.thread = None
        self.worker = None

    def closeEvent(self, event):
        # Stop the motor and restore the gains before the thread is destroyed
        if self.thread is not None:
            self.worker.stop()
            self.thread.wait()
        event.accept()
        
    
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
