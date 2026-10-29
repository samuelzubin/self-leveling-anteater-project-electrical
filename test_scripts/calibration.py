import asyncio
import moteus

MOTOR_ID = 1

# Safety limits
MAX_VELOCITY_REV_S = 2.5
MAX_BOARD_TEMP_C = 65.0
MAX_TORQUE_NM = 1.0
MAX_DISPLACEMENT_REV = 1.0  # How far the motor may go from the hold position

# Search range for each gain (servo.pid_position.<name>)
GAIN_LIMITS = {
    "kp": (0.0, 20.0),
    "ki": (0.0, 0.2),
    "kd": (0.0, 2.0),
}
# Which way the gain should move when the motor overshoots. Too much p or i
# causes overshoot, while more d damps it.
OVERSHOOT_LOWERS_GAIN = {"kp": True, "ki": True, "kd": False}
TUNE_ORDER = ["kp", "kd", "ki"]

# Answers to the overshoot prompt
OVERSHOOT, UNDERSHOOT, GOOD = 0, 1, 2

HZ = 100
MAX_MISSED_REPLIES = 5


async def main():
    c = moteus.Controller(id=MOTOR_ID)
    stream = moteus.Stream(c)
    await stream.write_message(b'tel stop')
    await stream.flush_read()

    await c.set_stop()

    original = {name: await get_gain(stream, name) for name in GAIN_LIMITS}
    hold_position_rev = (await c.query()).values[moteus.Register.POSITION]
    print(f"Holding at {hold_position_rev:.3f} rev. Original gains: {format_gains(original)}")

    gains = {name: lo for name, (lo, _) in GAIN_LIMITS.items()}
    for name, value in gains.items():
        await set_gain(stream, name, value)

    hold = asyncio.create_task(hold_position(c, hold_position_rev))
    save = False

    try:
        for name in TUNE_ORDER:
            lo, hi = GAIN_LIMITS[name]
            print(f"\n--- Tuning {name} ---")

            # Keep narrowing the range until the user accepts the current value
            while True:
                gains[name] = (lo + hi) / 2
                await set_gain(stream, name, gains[name])
                print(f"Now using: {format_gains(gains)}")

                answer = await ask_overshoot(hold)
                if answer == GOOD:
                    print(f"Keeping {name}={gains[name]:.4g}")
                    break
                if (answer == OVERSHOOT) == OVERSHOOT_LOWERS_GAIN[name]:
                    hi = gains[name]
                else:
                    lo = gains[name]

        print(f"\nFinal gains: {format_gains(gains)}")
        save = await ask_yes_no("Save to the controller's flash? (y/n): ", hold)

    finally:
        hold.cancel()  # Stops the motor
        await asyncio.gather(hold, return_exceptions=True)

        # Put the original gains back unless the new ones are being kept
        if save:
            await stream.command(b"conf write")
            print("Saved.")
        else:
            for name, value in original.items():
                await set_gain(stream, name, value)
            print(f"Not saved. Restored original gains: {format_gains(original)}")


async def hold_position(controller, position):
    missed = 0
    try:
        while True:
            result = await controller.set_position(
                position=position,
                velocity=0.0,
                maximum_torque=MAX_TORQUE_NM,
                watchdog_timeout=0.2,  # Cuts power if CAN comms stall > 200ms
                query=True,
            )

            if result is None:
                missed += 1
                if missed > MAX_MISSED_REPLIES:
                    raise RuntimeError("Lost communication with the controller")
            else:
                missed = 0
                check_limits(result, position)

            await asyncio.sleep(1 / HZ)

    except RuntimeError as e:
        print(f"\nSAFETY STOP: {e}")
        raise
    finally:
        await controller.set_stop()


def check_limits(result, hold_pos):
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


async def get_gain(stream, name):
    print("Setup motor before")
    name = str(name).encode()
    command_str = b'conf get servo.pid_position.' + name
    reply = float((await stream.command(command_str, allow_any_response=True)).decode())
    return reply


async def set_gain(stream, name, value):
    await stream.command(f'conf set servo.pid_position.{name} {value:.6g}'.encode())


def format_gains(gains):
    return " ".join(f"{name}={value:.4g}" for name, value in gains.items())


async def ask_overshoot(hold):
    while True:
        answer = await ask(
            "Pull the motor back and release. Did it overshoot (0), undershoot (1), "
            "or is it good as is (2): ",
            hold,
        )
        if answer in ("0", "1", "2"):
            return int(answer)


async def ask_yes_no(prompt, hold):
    while True:
        answer = (await ask(prompt, hold)).lower()
        if answer in ("y", "n"):
            return answer == "y"


async def ask(prompt, hold):
    # The hold task keeps running while this waits, so the safety checks stay active
    answer = await asyncio.to_thread(input, prompt)
    if hold.done():
        hold.result()
    return answer.strip()


if __name__ == "__main__":
    asyncio.run(main())
