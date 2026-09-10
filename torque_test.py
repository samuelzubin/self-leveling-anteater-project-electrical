import asyncio
import math
import moteus

MAX_VELOCITY_REV_S = 10.0
MAX_BOARD_TEMP_C = 65.0
MAX_TORQUE_NM = 0.5

async def main():
    c = moteus.Controller(id=1)
    await c.set_stop()

    target_torque = 0.2
    hold_sec = 5.0
    hz = 100

    try:
        # Ramp up torque
        for i in range(hz):
            torque = (i / hz) * target_torque
            await send_torque(c, torque)
            await asyncio.sleep(1 / hz)

        # Hold target torque
        for _ in range(int(hold_sec * hz)):
            await send_torque(c, target_torque)
            await asyncio.sleep(1 / hz)

    finally:
        await c.set_stop()


async def send_torque(controller, torque):
    result = await controller.set_position(
        position=math.nan,
        velocity=0.0,
        kp_scale=0.0,
        kd_scale=0.0,
        ilimit_scale=0.0,
        feedforward_torque=torque,
        maximum_torque=MAX_TORQUE_NM,
        watchdog_timeout=0.2,  # Cuts power if CAN comms stall > 200ms
        query=True,
    )

    vel = abs(result.values.get(moteus.Register.VELOCITY, 0.0))
    temp = result.values.get(moteus.Register.TEMPERATURE, 0.0)

    if vel > MAX_VELOCITY_REV_S:
        raise RuntimeError(f"Over-speed cutoff triggered: {vel:.2f} rev/s")
    if temp > MAX_BOARD_TEMP_C:
        raise RuntimeError(f"Thermal cutoff triggered: {temp:.1f} °C")


if __name__ == "__main__":
    asyncio.run(main())
