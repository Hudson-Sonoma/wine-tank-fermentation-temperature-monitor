import alarm
import microcontroller
import supervisor
import time

# CircuitPython 8+ runs this file when the board enters safe mode.
# Safe mode does NOT run code.py, so without this the device just sits there
# looking hung. Log the reason, wait a moment (so a genuine hard-fault loop
# doesn't spin at full speed), then restart normally.

reason = supervisor.runtime.safe_mode_reason
print("SAFE MODE:", reason)

# Brownout usually means the battery sagged. Sleep a bit longer so we are not
# resetting into the same low-voltage condition immediately.
delay = 30 if reason == supervisor.SafeModeReason.BROWNOUT else 5

time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + delay)
alarm.light_sleep_until_alarms(time_alarm)

microcontroller.on_next_reset(microcontroller.RunMode.NORMAL)
microcontroller.reset()