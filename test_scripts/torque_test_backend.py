import asyncio
import math
from dataclasses import dataclass
from typing import Optional

import moteus
from PyQt6.QtCore import QObject, QThread, pyqtSignal

MAX_TORQUE_NM = 1.0
MAX_VELOCITY_REV_S = 12.0
MAX_TEMP_C = 65.0
DEFAULT_HZ = 100

@dataclass
class TelemetryData:
    cmd_torque: float
    meas_torque: float
    voltage: float
    current: float
    p_elec: float
    p_mech: float
    angular_vel: float  # rev/s
    temperature: float


class _TorqueWorker(QObject):
    """Runs the moteus control loop on its own thread. Not used directly by a GUI."""

    telemetry = pyqtSignal(object)  # TelemetryData
    fault = pyqtSignal(str)
    connected = pyqtSignal()
    connection_failed = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, motor_id: int, hz: int):
        super().__init__()
        self._motor_id = motor_id
        self._hz = hz
        self._target_torque = 0.0
        self._running = False

    def set_target_torque(self, torque: float):
        self._target_torque = torque

    def request_stop(self):
        self._running = False

    def run(self):
        asyncio.run(self._control_loop())

    async def _control_loop(self):
        try:
            controller = moteus.Controller(id=self._motor_id)
            await controller.set_stop()
        except Exception as e:
            self.connection_failed.emit(str(e))
            return

        self.connected.emit()
        self._running = True
        period_s = 1.0 / self._hz

        try:
            while self._running:
                await self._send_and_read(controller)
                await asyncio.sleep(period_s)
        except Exception as e:
            self.fault.emit(str(e))
        finally:
            self._running = False
            await controller.set_stop()
            self.stopped.emit()

    async def _send_and_read(self, controller: moteus.Controller):
        torque = self._target_torque

        result = await controller.set_position(
            position=math.nan,
            velocity=0.0,
            kp_scale=0.0,
            kd_scale=0.0,
            ilimit_scale=0.0,
            feedforward_torque=torque,
            maximum_torque=MAX_TORQUE_NM,
            watchdog_timeout=0.2,
            query=True,
        )

        if not result:
            raise RuntimeError("No communication response from moteus controller.")

        voltage     = result.values.get(moteus.Register.VOLTAGE, 0.0)
        current     = result.values.get(moteus.Register.Q_CURRENT, 0.0)
        meas_torque = result.values.get(moteus.Register.TORQUE, 0.0)
        angular_vel = result.values.get(moteus.Register.VELOCITY)
        temp_c      = result.values.get(moteus.Register.TEMPERATURE)

        # Stop if values cannot be read
        if angular_vel is None or temp_c is None:
            raise RuntimeError("Cannot read velocity/temperature")

        # Stop if over limits
        if abs(angular_vel) > MAX_VELOCITY_REV_S:
            raise RuntimeError(f"Max velocity exceeded: {angular_vel:.2f} rev/s")
        if temp_c > MAX_TEMP_C:
            raise RuntimeError(f"Max temperature exceeded: {temp_c:.1f} °C")

        rad_s = angular_vel * 2.0 * math.pi
        p_elec = voltage * abs(current)
        p_mech = meas_torque * rad_s

        self.telemetry.emit(TelemetryData(
            cmd_torque=torque,
            meas_torque=meas_torque,
            voltage=voltage,
            current=current,
            p_elec=p_elec,
            p_mech=p_mech,
            angular_vel=angular_vel,
            temperature=temp_c,
        ))


class MoteusTorqueController(QObject):
    """
    PyQt6-facing controller for live moteus torque control.

    Owns a background QThread running the moteus control loop so the GUI thread
    is never blocked by asyncio/CAN I/O. Connect to the signals below, then call
    start() / set_target_torque() / stop() from the GUI thread.
    """

    telemetry = pyqtSignal(object)  # TelemetryData
    fault = pyqtSignal(str)
    connected = pyqtSignal()
    connection_failed = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, motor_id: int = 1, hz: int = DEFAULT_HZ, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._thread = QThread()
        self._worker = _TorqueWorker(motor_id, hz)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.telemetry.connect(self.telemetry)
        self._worker.connected.connect(self.connected)
        self._worker.fault.connect(self.fault)
        self._worker.connection_failed.connect(self.connection_failed)
        self._worker.connection_failed.connect(self._cleanup_thread)
        self._worker.stopped.connect(self.stopped)
        self._worker.stopped.connect(self._cleanup_thread)

    def start(self):
        if not self._thread.isRunning():
            self._thread.start()

    def set_target_torque(self, torque: float):
        if abs(torque) > MAX_TORQUE_NM:
            raise ValueError(f"Set torque {torque} Nm exceeds MAX_TORQUE_NM ({MAX_TORQUE_NM} Nm).")
        self._worker.set_target_torque(torque)

    def stop(self):
        self._worker.request_stop()

    def _cleanup_thread(self, *_):
        self._thread.quit()
        self._thread.wait()


if __name__ == "__main__":
    import sys

    from PyQt6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)
    controller = MoteusTorqueController(motor_id=1)

    def on_telemetry(data: TelemetryData):
        print(
            f"Cmd: {data.cmd_torque:+.2f} Nm | Meas: {data.meas_torque:+.2f} Nm | "
            f"Volt: {data.voltage:4.1f} V | Curr: {data.current:5.2f} A | "
            f"Temp: {data.temperature:.1f} C",
            end="\r",
        )

    def on_fault(msg: str):
        print(f"\nFault: {msg}")

    def on_connection_failed(msg: str):
        print(f"Failed to connect: {msg}")
        app.quit()

    def on_stopped():
        print("\nMotor controller stopped")
        app.quit()

    controller.telemetry.connect(on_telemetry)
    controller.fault.connect(on_fault)
    controller.connection_failed.connect(on_connection_failed)
    controller.stopped.connect(on_stopped)
    controller.connected.connect(lambda: controller.set_target_torque(0.2))

    controller.start()
    QTimer.singleShot(5000, controller.stop)

    sys.exit(app.exec())
