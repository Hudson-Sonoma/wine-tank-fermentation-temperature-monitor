from _bleio import adapter
from time import sleep
import analogio
import digitalio
import board
import time
import gc
import alarm
import microcontroller
import supervisor
from watchdog import WatchDogMode
from adafruit_onewire.bus import OneWireBus
from adafruit_ds18x20 import DS18X20

## To INSTALL
## wait for CIRCUITPY drive to appear, then delete code.py.
## Reboot, instal new code.py with thonny, then reboot again.  The new code.py will run.

## TODO: power DS18b20 from D1, not 3.3V rail.  
# This will allow the device to sleep with the sensor powered down.
# and will allow the sensor to be reset.
## TODO: add 100 ohm resistor and two BAT85s to D0 to protect the DS18B20 from ESD and overvoltage.  The DS18B20 is very sensitive to ESD and can be damaged by a static discharge from a human hand.
## TODO: This time, reset after 8 cycles of the packet_ids from 0..255, instead of every 24 hours. 
## TODO: support temperature sensor calibration
## TODO: support settings.toml
## TODO: support resistance NTD sensor.
## TODO: mutliple DS18b20 or RTD on different pins. 
# d0,d1,d2,d3,d4,d5,d6 d7,d8,d9,d10
## TODO: encryption key



# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
DEVICE_NAME = "RedT"      # <= 10 bytes; advertised over BLE
INTERVAL = 60              # seconds between advertisements
REBOOT_AFTER_S = 24 * 3600 # deliberately reset roughly once a day
DEBUG = True               # False = far fewer heap allocations in the loop
MAX_SENSOR_FAILS = 5       # consecutive DS18B20 errors before giving up

# ----------------------------------------------------------------------------
# Watchdog
# ----------------------------------------------------------------------------
w = microcontroller.watchdog
w.timeout = INTERVAL * 2 + 5.0
w.mode = WatchDogMode.RESET


def log(*args):
    if DEBUG:
        print(*args)


def reboot(reason):
    """Clean, deliberate restart."""
    print("Rebooting:", reason)
    try:
        adapter.stop_advertising()
    except Exception:
        pass
    try:
        # make sure we come back up running code.py, not in safe mode
        microcontroller.on_next_reset(microcontroller.RunMode.NORMAL)
    except Exception:
        pass
    time.sleep(0.5)
    microcontroller.reset()


# ----------------------------------------------------------------------------
# Hardware
# ----------------------------------------------------------------------------
charge_rate = digitalio.DigitalInOut(board.CHARGE_RATE)
charge_rate.direction = digitalio.Direction.OUTPUT
charge_rate.value = False  # True = 50mA, False = 100mA

not_charging = digitalio.DigitalInOut(board.CHARGE_STATUS)
not_charging.direction = digitalio.Direction.INPUT

blue_led = digitalio.DigitalInOut(board.LED_BLUE)
blue_led.direction = digitalio.Direction.OUTPUT
blue_led.value = True  # OFF

green_led = digitalio.DigitalInOut(board.LED_GREEN)
green_led.direction = digitalio.Direction.OUTPUT
green_led.value = True  # OFF

red_led = digitalio.DigitalInOut(board.LED_RED)
red_led.direction = digitalio.Direction.OUTPUT
red_led.value = True  # OFF

# Make sure this is off (10k resistor pulls it down)
relay = digitalio.DigitalInOut(board.D1)
relay.direction = digitalio.Direction.INPUT

read_batt_enable = digitalio.DigitalInOut(board.READ_BATT_ENABLE)
read_batt_enable.direction = digitalio.Direction.OUTPUT
read_batt_enable.value = True  # pull low to read battery

vbat = analogio.AnalogIn(board.VBATT)

# ----------------------------------------------------------------------------
# 1-Wire / DS18B20
# ----------------------------------------------------------------------------
ow_bus = OneWireBus(board.D0)
bus_scan = ow_bus.scan()
_tries = 0
while len(bus_scan) == 0:
    print("DS18B20 not found")
    w.feed()
    red_led.value = False
    sleep(0.100)
    red_led.value = True
    time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 2.5)
    alarm.light_sleep_until_alarms(time_alarm)
    _tries += 1
    if _tries > 20:
        reboot("no DS18B20 after 20 scans")
    bus_scan = ow_bus.scan()

ds18_1 = DS18X20(ow_bus, bus_scan[0])
ds18_1.resolution = 12


def get_battery_voltage():
    """Battery voltage in volts."""
    read_batt_enable.value = False   # pull low to read battery
    voltage = vbat.value * (3.3 / 65536) / (510000 / 1510000)
    read_batt_enable.value = True
    return voltage


def get_battery_percentage(voltage_mv):
    if voltage_mv >= 4200:
        return 100
    if voltage_mv < 3300:
        return 0
    return ((voltage_mv - 3300) // 100 + 1) * 10


# ----------------------------------------------------------------------------
# BTHome v2 advertisement, built once and mutated in place (no per-loop allocs)
# ----------------------------------------------------------------------------
class BTHomeAdvertisement:
    def __init__(self, local_name=None):
        name_bytes = bytes(local_name or adapter.name, "utf-8")

        # Service data. Object IDs MUST be in ascending numerical order.
        svc = bytearray([0x00, 0x16, 0xD2, 0xFC, 0x40])
        base = 3  # length of the flags element that precedes the svc data

        self._off_packet = base + len(svc) + 1
        svc.extend([0x00, 0x00])              # 0x00 packet id (uint8)

        self._off_battery = base + len(svc) + 1
        svc.extend([0x01, 0x00])              # 0x01 battery % (uint8)

        self._off_temp = base + len(svc) + 1
        svc.extend([0x02, 0x00, 0x00])        # 0x02 temperature (sint16, 0.01C)

        self._off_volt = base + len(svc) + 1
        svc.extend([0x0C, 0x00, 0x00])        # 0x0C voltage (uint16, 0.001V)

        self._off_charging = base + len(svc) + 1
        svc.extend([0x16, 0x00])              # 0x16 battery charging (bool)

        svc[0] = len(svc) - 1                 # service-data length byte

        adv = bytearray([0x02, 0x01, 0x06])   # flags
        adv.extend(svc)
        adv.extend(bytearray([len(name_bytes) + 1, 0x09]))
        adv.extend(name_bytes)

        if len(adv) > 31:
            raise ValueError("advertisement too long: %d bytes" % len(adv))

        self._adv = adv

    def update(self, t1_c=0.0, voltage_mv=0, packet_id=0, battery_pct=0,
               charging=False):
        a = self._adv
        t = int(round(t1_c * 100))
        a[self._off_temp] = t & 0xFF
        a[self._off_temp + 1] = (t >> 8) & 0xFF

        v = int(voltage_mv)
        a[self._off_volt] = v & 0xFF
        a[self._off_volt + 1] = (v >> 8) & 0xFF

        a[self._off_packet] = packet_id & 0xFF
        a[self._off_battery] = int(battery_pct) & 0xFF
        a[self._off_charging] = 1 if charging else 0
        return a


bthome = BTHomeAdvertisement(DEVICE_NAME)

log("reset reason:", microcontroller.cpu.reset_reason)
try:
    log("safe mode reason:", supervisor.runtime.safe_mode_reason)
except AttributeError:
    pass

# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
STEP = 5 / 18                      # quantize to nearest 0.5 F
BOOT_NS = time.monotonic_ns()      # integer nanoseconds: no float drift
REBOOT_NS = REBOOT_AFTER_S * 1_000_000_000

packet_id = 0
sensor_fails = 0
last_good_c = None

try:
    while True:
        w.feed()

        if time.monotonic_ns() - BOOT_NS >= REBOOT_NS:
            reboot("scheduled 24h restart")

        battery_mv = int(get_battery_voltage() * 1000)
        battery_pct = get_battery_percentage(battery_mv)

        if battery_pct > 50:
            blue_led.value = False          # blue
            sleeptime = 0.350
        elif battery_pct > 20:
            blue_led.value = False           # blue + green = yellow
            green_led.value = False
            sleeptime = 0.250
        else:
            red_led.value = False            # red
            sleeptime = 0.150

        try:
            t1_c = ds18_1.temperature
            last_good_c = t1_c
            sensor_fails = 0
        except Exception as e:
            sensor_fails += 1
            log("DS18B20 read failed (%d):" % sensor_fails, e)
            if sensor_fails >= MAX_SENSOR_FAILS or last_good_c is None:
                reboot("DS18B20 unreadable")
            t1_c = last_good_c

        t1_c_quantized = round(t1_c / STEP) * STEP
        packet_id = (packet_id + 1) & 0xFF

        adv_data = bthome.update(
            t1_c=t1_c_quantized,
            voltage_mv=battery_mv,
            packet_id=packet_id,
            battery_pct=battery_pct,
            charging=not not_charging.value,
        )

        if DEBUG:
            print(DEVICE_NAME, "id", packet_id,
                  "t1_c %.2f" % t1_c,
                  "mv", battery_mv,
                  "pct", battery_pct,
                  "chg", not not_charging.value,
                  "free", gc.mem_free())

        # LEDs off before transmitting: they load the supply enough to cost
        # several dB of RSSI on small cells.
        blue_led.value = True
        green_led.value = True
        red_led.value = True

        adapter.start_advertising(
            adv_data, scan_response=None, connectable=False,
            interval=0.1, tx_power=8,
        )
        sleep(sleeptime)
        adapter.stop_advertising()

        gc.collect()

        time_alarm = alarm.time.TimeAlarm(
            monotonic_time=time.monotonic() + INTERVAL
        )
        alarm.light_sleep_until_alarms(time_alarm)
        del time_alarm

except KeyboardInterrupt:
    print("KeyboardInterrupt - stopping")
    try:
        adapter.stop_advertising()
    except Exception:
        pass
except MemoryError as e:
    gc.collect()
    reboot("MemoryError: %s" % e)
except Exception as e:
    reboot("unhandled exception: %s" % e)
finally:
    print("broadcast stopped.")