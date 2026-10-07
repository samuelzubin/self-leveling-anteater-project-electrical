import asyncio
import math
import sys

import moteus

MOTOR_ID = 1
MAX_TORQUE_NM = 1.0
MAX_TEMP = 65
MAX_VELOCITY = 12.0

async def send_and_read(controller: moteus.Controller, torque_nm: float):
    output = await controller.set_position(
        position=math.nan,
        velocity=0.0,
        kp_scale=0.0,
        kd_scale=0.0,
        ilimit_scale=0.0,
        feedforward_torque=torque_nm,
        maximum_torque=MAX_TORQUE_NM,
        watchdog_timeout=0.2,
        query=True,
    )

    if not output:
        raise RuntimeError("Cant connect to motor")

    # Read data
    voltage        = output.values.get(moteus.Register.VOLTAGE, 0.0)
    current        = output.values.get(moteus.Register.Q_CURRENT, 0.0)
    torque         = output.values.get(moteus.Register.TORQUE, 0.0)
    vel            = output.values.get(moteus.Register.VELOCITY)
    temp           = output.values.get(moteus.Register.TEMPERATURE)

    if vel is None or temp is None:
        raise RuntimeError("Can't read velocity or temperature")

    # Power
    rad_s  = vel * 2.0 * math.pi
    p_mech = torque * rad_s

    # Safety
    if abs(vel) > MAX_VELOCITY:
        raise RuntimeError("Velocity is too high")
    if temp > MAX_TEMP:
        raise RuntimeError("Temperature is too high")

    # Print output
    print(
        f"Cmd: {torque_nm:+.2f} Nm | "
            f"Meas Tq: {torque:+.2f} Nm | "
            f"Volt: {voltage:4.1f} V | "
            f"Curr: {current:5.2f} A | "
            f"P_mech: {p_mech:5.1f} W | "
            f"Vel: {vel:+.2f} rev/s | "
            f"Temp: {temp:.1f} °C",
        end="\r",
        flush=True,
    )


async def main():
    try:
        user_input = input("Enter target torque (Nm): ")
        target_torque = float(user_input)
    except ValueError:
        print("Enter a valid torque")
        return

    if abs(target_torque) > MAX_TORQUE_NM:
        print("Target torque is too high")
        return

    try:
        controller = moteus.Controller(id=MOTOR_ID)
        await controller.set_stop()
    except Exception as e:
        print(f"Can't connect to motor: {e}", file=sys.stderr)
        return

    hz = 100
    ramp_steps = hz
    hold_steps = int(3.0 * hz)

    try:
        print(f"\n Increasing torque to {target_torque} Nm")
        for i in range(ramp_steps):
            t_cmd = (i / ramp_steps) * target_torque
            await send_and_read(controller, t_cmd)
            await asyncio.sleep(1 / hz)

        print(f"\n Holding {target_torque} Nm")
        for _ in range(hold_steps):
            await send_and_read(controller, target_torque)
            await asyncio.sleep(1 / hz)

        print("\n\nTest completed")

    except KeyboardInterrupt:
        print("Test was interrupted", file=sys.stderr)
    except Exception as e:
        print(f"{e}", file=sys.stderr)
    finally:
        await controller.set_stop()
        print("Motor stopped")


if __name__ == "__main__":
    asyncio.run(main())
