import sys
from collections import namedtuple, deque
from gc import collect, mem_alloc, mem_free
import network
import utime as time
from machine import RTC, Pin, ADC, PWM, reset, freq
from esp32 import mcu_temperature
import ujson
from ntptime import settime
import uasyncio as asyncio
import urequests as requests
from uos import rename, remove, stat

# Global variables
VERSION = "v1.10.20"  # DO NOT EDIT: This line is automatically updated by the version-bump workflow
MICROPYTHON_TO_TIMESTAMP: int = 946684800  # 2000-1970 --> 3155673600 - 2208988800
TIMESTAMP_2001_01_01: int = (
    978307200  # Monday, This is the date used when ntp is not available
)
TIMESTAMP_2025_01_01: int = (
    1735689600  # after ntp sync, date will be at least 2025-01-01
)
DEFAULT_FREQ = freq()


class GlobalAppState:
    def __init__(self):
        self.WIFI_SETUP_MODE = False
        self.micropython_to_localtime: int = 0
        self.wlan: network.WLAN = network.WLAN(network.STA_IF)
        self.config: dict = None
        self.valve_status: int = 0
        self.schedule_status: int = 0
        self.heartbeat_pin_id: int = -1
        self.heartbeat_high_is_on: bool = True
        self.schedule_completed_until = []
        self.ad_hoc_irrigation_until = {}
        self.LOG = deque([], 25)
        self.rtc_adjustments: int = 0


g = GlobalAppState()


# logging
def get_local_timestamp() -> int:
    return time.time() + g.micropython_to_localtime


g.rtc_adjustments = get_local_timestamp()


def get_uptime_sec() -> int:
    return get_local_timestamp() - g.rtc_adjustments


LogLine = namedtuple(
    "LogLine", ["timestamp", "level", "zone_id", "schedule_id", "message"]
)


def log(level: int, zone_id: int, schedule_id: int, message: str, *args) -> None:
    if g.config and level < g.config["options"]["log"]["level"]:
        return
    if args:
        message = message % args
    ts = get_local_timestamp()
    print(f"@{ts} z{zone_id} s{schedule_id} {message}")
    g.LOG.append(LogLine(ts, level, zone_id, schedule_id, message))


def debug(zone_id: int, schedule_id: int, message: str, *args) -> None:
    log(10, zone_id, schedule_id, message, *args)


def info(zone_id: int, schedule_id: int, message: str, *args) -> None:
    log(20, zone_id, schedule_id, message, *args)


def warn(zone_id: int, schedule_id: int, message: str, *args) -> None:
    log(30, zone_id, schedule_id, message, *args)


def error(zone_id: int, schedule_id: int, message: str, *args) -> None:
    log(40, zone_id, schedule_id, message, *args)


# Persistent storage functions
def save_as_json(filename: str, data: dict) -> None:
    info(None, None, f"Saving data to {filename}")
    with open(f"{filename}.tmp", "w", encoding="utf-8") as f:
        ujson.dump(data, f)
    rename(f"{filename}.tmp", filename)


def load_from_json(filename: str) -> dict:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return ujson.load(f)
    except (OSError, ValueError) as e:
        error(None, None, f"Error in load_from_json(): {e}")
        return None


async def connect_wifi() -> None:
    try:
        if not g.config["options"]["wifi"]["ssid"]:
            return
        network.hostname(g.config["options"]["wifi"]["hostname"])
        g.wlan.active(True)
        g.wlan.config(
            pm=g.wlan.PM_POWERSAVE
            if g.config["options"]["settings"]["enable_power_saving_mode"]
            else g.wlan.PM_PERFORMANCE
        )
        info(None, None, "Wifi connecting...")
        g.wlan.connect(
            g.config["options"]["wifi"]["ssid"], g.config["options"]["wifi"]["password"]
        )
        for _ in range(15):
            if g.wlan.isconnected():
                break
            await asyncio.sleep(1)
            print(".", end="")
        if g.wlan.isconnected():
            info(
                None,
                None,
                f"Connected, ip = {g.wlan.ifconfig()[0]}, hostname={g.config['options']['wifi']['hostname']}",
            )
            return
        g.wlan.active(False)
        warn(None, None, "network connection failed, retrying in 60 seconds")
    except (OSError, ValueError) as e:
        g.wlan.active(False)
        warn(None, None, f"Exception while connecting to wifi: {e}")
    collect()


async def keep_wifi_connected():
    while True:
        while g.wlan.isconnected():
            await asyncio.sleep(10)
        await asyncio.sleep(60)
        await connect_wifi()


# Time functions
async def sync_ntp() -> bool:
    try:
        old_ts = get_local_timestamp()
        settime()
        g.rtc_adjustments += get_local_timestamp() - old_ts
        info(
            None,
            None,
            f"@{time.time()} NTP synced, UTC time={time.time() + MICROPYTHON_TO_TIMESTAMP} Local time(GMT{g.config['options']['settings']['timezone_offset']:+})={get_local_timestamp()}",
        )
        return True
    except OSError:
        warn(
            None,
            None,
            f"@{time.time()} Error syncing time, current UTC timestamp={time.time() + MICROPYTHON_TO_TIMESTAMP}",
        )
        return False


async def periodic_ntp_sync():
    while True:
        while not await sync_ntp():
            await asyncio.sleep(10)  # 10 seconds
        await asyncio.sleep(4 * 24 * 60 * 60)  # resync every 4 days


async def fallback_time_sync():
    await asyncio.sleep(5)
    if get_local_timestamp() > TIMESTAMP_2025_01_01:
        return
    sync_conf = g.config["options"]["fallback_time_sync"]
    warn(
        None,
        None,
        f"no ntp sync, using mcu_temperature as fallback (conf={sync_conf}), starting...",
    )
    temperature_log = [0] * sync_conf["slices_per_day"]
    temperature_log_index = 0
    rtc = RTC()
    while (
        get_local_timestamp() < TIMESTAMP_2025_01_01
        and temperature_log_index < sync_conf["sync_days"] * sync_conf["slices_per_day"]
    ):  # while no ntp sync
        try:
            temperature = 0
            for _ in range(sync_conf["samples_per_slice"]):
                temperature += mcu_temperature() / sync_conf["samples_per_slice"]
                await asyncio.sleep(
                    24
                    * 60
                    * 60
                    / sync_conf["slices_per_day"]
                    / sync_conf["samples_per_slice"]
                )
            temperature_log[temperature_log_index % sync_conf["slices_per_day"]] = (
                1.0
                * temperature_log[temperature_log_index % sync_conf["slices_per_day"]]
                + temperature
            )
            temperature_log_index += 1
            debug(None, None, "Temperature log: %s", temperature_log)
            if temperature_log_index % sync_conf["slices_per_day"] == 0:
                # assuming that minimum temperature is at 6:15am
                hours_till_6_15am = (
                    24
                    * min((v, i) for i, v in enumerate(temperature_log))[1]
                    / sync_conf["slices_per_day"]
                )
                now_hours_gmt = (
                    48
                    + 6.25
                    - hours_till_6_15am
                    - g.config["options"]["settings"]["timezone_offset"]
                ) % 24
                basetime = (
                    list(rtc.datetime())
                    if TIMESTAMP_2001_01_01 < get_local_timestamp()
                    else [2001, 1, 1, 0, 12, 0, 0, 0]
                )  # Monday noon
                rtc_hour_gmt = basetime[4] + basetime[5] / 60 + basetime[6] / 3600
                adjustment_hours = (12 + now_hours_gmt - rtc_hour_gmt) % 24 - 12
                if (
                    abs(adjustment_hours) < 24 / sync_conf["slices_per_day"] * 1.5
                ):  # avoid jitter by requiring at least 2 slices
                    info(
                        None,
                        None,
                        f"skipping RTC adjustment, because adjustment_hours={adjustment_hours} is too small, temperature log: {temperature_log}",
                    )
                    continue
                basetime[4:7] = (0, 0, round(now_hours_gmt * 3600))
                debug(
                    None,
                    None,
                    "RTC before adjustment: %s, will assign=%s",
                    rtc.datetime(),
                    basetime,
                )
                old_ts = get_local_timestamp()
                rtc.datetime(basetime)
                g.rtc_adjustments += get_local_timestamp() - old_ts
                warn(
                    None,
                    None,
                    f"it's {hours_till_6_15am} hours till 6:15am, adjusted RTC by {adjustment_hours:+}h ({rtc_hour_gmt} -> {now_hours_gmt}), temperature log: {temperature_log}",
                )
        except (OSError, ValueError, TypeError) as e:
            error(None, None, f"Error in fallback_time_sync: {e}")
    info(None, None, "synced, fallback_time_sync ended")


# Watering control functions
def control_watering(zone_id: int, start: bool) -> None:
    if zone_id < 0 or zone_id >= len(g.config["zones"]):
        warn(zone_id, None, "invalid zone_id")
        return
    zone = g.config["zones"][zone_id]
    pin_id = zone["on_pin"] if start else zone["off_pin"]
    if pin_id < 0:
        warn(
            zone_id,
            None,
            f"Zone[{zone_id}]='{zone['name']}' (off_pin={zone['off_pin']}, on_pin={zone['on_pin']}) will NOP on {'open' if start else 'close'} because pin_id < 0",
        )
        return
    pin_value = 1 if zone["active_is_high"] else 0
    pulse_mode = zone["on_pin"] != zone["off_pin"]
    debug(
        zone_id,
        None,
        f"Zone[{zone_id}]='{zone['name']}' (off_pin={zone['off_pin']}, on_pin={zone['on_pin']}) will be set {'OPEN' if start else 'CLOSE'} using{' pulse' if pulse_mode else ''} pin_id({pin_id}).value({pin_value})",
    )
    if pulse_mode:
        other_pin_id = zone["off_pin"] if start else zone["on_pin"]
        if other_pin_id >= 0:
            Pin(other_pin_id, Pin.OUT, value=1 - pin_value)
        # pulse the pin
        Pin(pin_id, Pin.OUT, value=pin_value)
        time.sleep(0.060)  # is precise timing really needed?
        Pin(pin_id, Pin.OUT, value=1 - pin_value)
    else:
        # leave the pin in the state
        if start:
            Pin(pin_id, Pin.OUT, value=pin_value)
        else:
            Pin(pin_id, Pin.OUT, value=1 - pin_value)


async def apply_valves(new_status: int) -> None:
    if new_status == g.valve_status:
        return

    debug(
        None,
        None,
        f"apply_valves({new_status:08b}), g.valve_status={g.valve_status:08b}",
    )
    relay_pin_id = g.config["options"]["settings"]["relay_pin_id"]
    if relay_pin_id >= 0:
        relay_value = (
            1 if g.config["options"]["settings"]["relay_active_is_high"] else 0
        )
        Pin(relay_pin_id, Pin.OUT, value=relay_value)
        await asyncio.sleep(0.250)  # wait for H-Bridges to power up

    for i in range(len(g.config["zones"])):
        if (g.valve_status ^ new_status) & (1 << i):
            control_watering(i, bool(new_status & (1 << i)))
            await asyncio.sleep(0.050)  # wait to settle down
    g.valve_status = new_status

    if relay_pin_id >= 0:
        Pin(relay_pin_id, Pin.OUT, value=1 - relay_value)


######################
# Irrigation scheduler
######################
def compute_desired_valves(
    config,
    local_timestamp,
    schedule_completed_until,
    ad_hoc_irrigation_until,
    schedule_status,
    valve_status,
    get_soil_moisture_fn,
):
    valve_desired = 0
    new_schedule_status = 0
    zones = config["zones"]
    for i, s in enumerate(config["schedules"]):
        # debug(None, i, "checking schedule")
        if local_timestamp < schedule_completed_until[i]:
            continue

        zone_id = s["zone_id"]
        z = zones[zone_id]

        # following checks disabled the schedule until config change
        if not config["options"]["settings"]["enable_irrigation_schedule"]:
            schedule_completed_until[i] = sys.maxsize
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' disabled because all schedules is disabled",
                i,
                zone_id,
                z["name"],
            )
            continue

        if not s["enabled"]:
            schedule_completed_until[i] = sys.maxsize
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' disabled because schedule is disabled",
                i,
                zone_id,
                z["name"],
            )
            continue

        duration_sec = s["duration_sec"]
        if 0 <= z["irrigation_factor_override"]:
            duration_sec *= z["irrigation_factor_override"]
        duration_sec = min(round(duration_sec), 86400)
        if duration_sec <= 0:
            schedule_completed_until[i] = sys.maxsize
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' disabled because duration_sec is zero",
                i,
                zone_id,
                z["name"],
            )
            continue

        if s["expiry"] and local_timestamp > s["expiry"]:
            schedule_completed_until[i] = sys.maxsize
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' disabled because schedule expired",
                i,
                zone_id,
                z["name"],
            )
            continue

        sec_till_start = (86400 + s["start_sec"] - local_timestamp % 86400) % 86400
        sec_till_end = (sec_till_start + duration_sec) % 86400

        if sec_till_start < sec_till_end:
            # we are outside the schedule window = (start, end], skip till sec_till_start==86399
            schedule_completed_until[i] = local_timestamp + sec_till_start + 1
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' suspended until next start: %s",
                i,
                zone_id,
                z["name"],
                schedule_completed_until[i],
            )
            continue

        # weekday of current schedule start time, monday is 0, sunday is 6
        weekday = ((local_timestamp + sec_till_start) // 86400 + 2) % 7
        if not s["day_mask"] & (1 << weekday):
            schedule_completed_until[i] = local_timestamp + sec_till_start + 1
            debug(
                zone_id,
                i,
                "Schedule[%s] zone[%s]='%s' suspended until next start %s because of day_mask=0x%02x weekday=%s",
                i,
                zone_id,
                z["name"],
                schedule_completed_until[i],
                s["day_mask"],
                weekday,
            )
            continue

        if (
            s["enable_soil_moisture_sensor"]
            and (soil_moisture := get_soil_moisture_fn(s["zone_id"])) is not None
        ):
            # soil_moisture value needs to be taken into account
            if schedule_status & (1 << i):
                # schedule is active, check if we should stop
                if soil_moisture >= z["soil_moisture_wet"]:
                    schedule_completed_until[i] = local_timestamp + sec_till_start + 1
                    info(
                        zone_id,
                        i,
                        "Schedule[%s] zone[%s]='%s' stopped and suspended until next start %s because soil_moisture=%s is wet",
                        i,
                        zone_id,
                        z["name"],
                        schedule_completed_until[i],
                        soil_moisture,
                    )
                    continue
            else:
                # schedule is about to start, is it dry enough?
                if soil_moisture >= z["soil_moisture_dry"]:
                    schedule_completed_until[i] = local_timestamp + sec_till_start + 1
                    info(
                        zone_id,
                        i,
                        "Schedule[%s] zone[%s]='%s' won't start and suspended until next start %s because soil_moisture=%s is not dry enough",
                        i,
                        zone_id,
                        z["name"],
                        schedule_completed_until[i],
                        soil_moisture,
                    )
                    continue

        # schedule status unaffected by interval duty cycle (avoiding log spam)
        new_schedule_status |= 1 << i

        if s["interval_duration_sec"] > 0:
            if (86400 - sec_till_start) % s["interval_duration_sec"] >= s[
                "interval_on_sec"
            ]:
                # we are outside the fogger window
                continue

        # we should irrigate, set the valve status
        valve_desired |= 1 << s["zone_id"]
        # debug(zone_id, i, f"valve_desired={valve_desired:08b} for schedule={s}")

    # check if we have ad-hoc irrigation
    for zone_id, end_time in list(ad_hoc_irrigation_until.items()):
        z = zones[zone_id]
        if end_time > local_timestamp:
            valve_desired |= 1 << zone_id
            if not valve_status & (1 << zone_id):
                info(
                    zone_id,
                    None,
                    f"Ad-hoc irrigation in zone[{zone_id}]='{z['name']}' (ends in {end_time - local_timestamp}s) is starting",
                )
        else:
            info(
                zone_id,
                None,
                f"Ad-hoc irrigation in zone[{zone_id}]='{z['name']}' has ended",
            )
            del ad_hoc_irrigation_until[zone_id]

    # debug(None, None, f"valve_desired={valve_desired:08b}")
    if valve_desired > 0:
        for i, zone in enumerate(zones):
            if zone["master"]:
                valve_desired |= 1 << i

    return valve_desired, new_schedule_status


async def schedule_irrigation():
    await asyncio.sleep(5)
    while True:
        if g.heartbeat_pin_id > 0:
            Pin(g.heartbeat_pin_id, Pin.OUT, value=1 if g.heartbeat_high_is_on else 0)

        local_timestamp = get_local_timestamp()
        valve_desired, new_schedule_status = compute_desired_valves(
            g.config,
            local_timestamp,
            g.schedule_completed_until,
            g.ad_hoc_irrigation_until,
            g.schedule_status,
            g.valve_status,
            get_soil_moisture_milli,
        )

        for i, s in enumerate(g.config["schedules"]):
            if (g.schedule_status ^ new_schedule_status) & (1 << i):
                zone_id = s["zone_id"]
                z = g.config["zones"][zone_id]
                info(
                    s["zone_id"],
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' {'started' if new_schedule_status & (1 << i) else 'ended'} for zone {s['zone_id']} ({z['name']})",
                )
        await apply_valves(valve_desired)
        g.schedule_status = new_schedule_status
        if g.heartbeat_pin_id > 0:
            Pin(g.heartbeat_pin_id, Pin.OUT, value=0 if g.heartbeat_high_is_on else 1)
        collect()
        await asyncio.sleep(2)


#########################
# Configuration functions
#########################


CONFIG_FILENAME = "rsi-config.json"


def migrate_config_if_needed() -> None:
    try:
        stat(CONFIG_FILENAME)
    except OSError:
        try:
            rename("config.json", CONFIG_FILENAME)
            info(None, None, f"Renamed config.json to {CONFIG_FILENAME}")
        except OSError:
            pass  # old config does not exist, nothing to do


def normalize_config(
    raw: dict,
    default_hostname: str,
    default_heartbeat_pin_id: int,
    default_heartbeat_high_is_on: bool,
) -> dict:
    normalized = {"zones": [], "schedules": [], "options": {}}
    for i, z in enumerate(raw.get("zones", [])):
        normalized["zones"].append(
            {
                "name": str(z.get("name", f"zone-{i}")),
                "master": bool(z.get("master", False)),
                "active_is_high": bool(z.get("active_is_high", True)),
                "on_pin": int(z.get("on_pin", -1)),
                "off_pin": int(z.get("off_pin", -1)),
                # SoilMoistureSensor
                "irrigation_factor_override": float(
                    z.get("irrigation_factor_override", -1)
                ),
                "soil_moisture_dry": int(z.get("soil_moisture_dry", 300)),
                "soil_moisture_wet": int(z.get("soil_moisture_wet", 700)),
                "adc_pin_id": int(z.get("adc_pin_id", -1)),
                "power_pin_id": int(z.get("power_pin_id", -1)),
            }
        )
    for s in raw.get("schedules", []):
        normalized["schedules"].append(
            {
                "enabled": bool(s.get("enabled", True)),
                "zone_id": int(s["zone_id"]),
                "start_sec": int(s["start_sec"]),
                "duration_sec": int(s["duration_sec"]),
                "enable_soil_moisture_sensor": bool(
                    s.get("enable_soil_moisture_sensor", True)
                ),
                "day_mask": int(s.get("day_mask", 0b1111111)),
                "interval_duration_sec": max(int(s.get("interval_duration_sec", 0)), 0),
                "interval_on_sec": max(int(s.get("interval_on_sec", 10)), 0),
                "expiry": int(s.get("expiry", 0)),
            }
        )
    bo = raw.get("options", {})
    for key in [
        "wifi",
        "monitoring",
        "soil_moisture_sensor",
        "settings",
        "log",
        "fallback_time_sync",
    ]:
        bo.setdefault(key, {})
    normalized["options"] = {
        "wifi": {
            "ssid": str(bo["wifi"].get("ssid", "")),
            "password": str(bo["wifi"].get("password", "")),
            "hostname": str(bo["wifi"].get("hostname", default_hostname)),
        },
        "monitoring": {
            "thingsspeak_apikey": str(bo["monitoring"].get("thingsspeak_apikey", "")),
            "send_interval_sec": int(bo["monitoring"].get("send_interval_sec", 300)),
        },
        "soil_moisture_sensor": {
            "high_is_dry": bool(bo["soil_moisture_sensor"].get("high_is_dry", True)),
            "sample_count": max(
                int(bo["soil_moisture_sensor"].get("sample_count", 3)), 1
            ),
        },
        "settings": {
            "enable_irrigation_schedule": bool(
                bo["settings"].get("enable_irrigation_schedule", True)
            ),
            "timezone_offset": float(bo["settings"].get("timezone_offset", -7)),
            "relay_pin_id": int(bo["settings"].get("relay_pin_id", -1)),
            "heartbeat_pin_id": int(
                bo["settings"].get("heartbeat_pin_id", default_heartbeat_pin_id)
            ),
            "heartbeat_high_is_on": bool(
                bo["settings"].get("heartbeat_high_is_on", default_heartbeat_high_is_on)
            ),
            "relay_active_is_high": bool(
                bo["settings"].get("relay_active_is_high", False)
            ),
            "enable_power_saving_mode": bool(
                bo["settings"].get("enable_power_saving_mode", False)
            ),
        },
        "log": {
            "level": int(bo["log"].get("level", 20)),
            "max_lines": int(bo["log"].get("max_lines", 50)),
        },
        "fallback_time_sync": {
            "sync_days": int(bo["fallback_time_sync"].get("sync_days", 1)),
            "slices_per_day": int(bo["fallback_time_sync"].get("slices_per_day", 48)),
            "samples_per_slice": int(
                bo["fallback_time_sync"].get("samples_per_slice", 15)
            ),
        },
    }
    return normalized


async def apply_config(new_config: dict) -> None:
    info(None, None, f"Applying new config = {new_config}")
    default_hostname = "rsi-" + "".join([f"{b:02x}" for b in g.wlan.config("mac")[3:6]])
    normalized_config = normalize_config(
        new_config, default_hostname, g.heartbeat_pin_id, g.heartbeat_high_is_on
    )

    # if zones changed, turn off all valves
    if g.config and g.config.get("zones", []) != normalized_config["zones"]:
        await apply_valves(0)

    # log(None, None, f"apply_config({new_config})\n    normalized_config={normalized_config}")
    g.config = normalized_config

    old_ts = get_local_timestamp()
    g.micropython_to_localtime = MICROPYTHON_TO_TIMESTAMP + round(
        g.config["options"]["settings"]["timezone_offset"] * 3600
    )
    g.rtc_adjustments += get_local_timestamp() - old_ts
    g.heartbeat_pin_id = g.config["options"]["settings"]["heartbeat_pin_id"]
    g.heartbeat_high_is_on = g.config["options"]["settings"]["heartbeat_high_is_on"]
    # disable schedules until fallback_time_sync or NTP synchronization is complete
    g.schedule_completed_until = [TIMESTAMP_2001_01_01] * len(g.config["schedules"])
    g.LOG = deque(
        [i for i in g.LOG if i.level >= g.config["options"]["log"]["level"]],
        g.config["options"]["log"]["max_lines"],
    )
    freq(
        80_000_000
        if g.config["options"]["settings"]["enable_power_saving_mode"]
        else DEFAULT_FREQ
    )
    if g.wlan.active():
        g.wlan.config(
            pm=g.wlan.PM_POWERSAVE
            if g.config["options"]["settings"]["enable_power_saving_mode"]
            else g.wlan.PM_PERFORMANCE
        )


def read_soil_moisture_raw(zone_id: int) -> int:
    soil_moisture_config = g.config["zones"][zone_id]
    if 0 > soil_moisture_config["adc_pin_id"]:
        return None
    if 0 <= soil_moisture_config["power_pin_id"]:
        Pin(soil_moisture_config["power_pin_id"], Pin.OUT, value=1)
        time.sleep_ms(10)  # Use blocking sleep since this function is synchronous
    # https://docs.micropython.org/en/latest/esp32/quickref.html#adc-analog-to-digital-conversion
    adc = ADC(soil_moisture_config["adc_pin_id"], atten=ADC.ATTN_11DB)
    raw_reading = 0
    sample_count = g.config["options"]["soil_moisture_sensor"]["sample_count"]
    for i in range(sample_count):
        raw_reading += adc.read_u16()
    raw_reading //= sample_count
    if soil_moisture_config["power_pin_id"] >= 0:
        Pin(soil_moisture_config["power_pin_id"], Pin.OUT, value=0)
    return raw_reading


def get_soil_moisture_milli(zone_id: int, raw_reading: int = None) -> int:
    if raw_reading is None:
        raw_reading = read_soil_moisture_raw(zone_id)
    if raw_reading is None:
        return None
    # raw range of [1..65534] is linearly mapped onto [1..999], 0->0, 65535->1000
    milli_moist = int((65.3 + raw_reading) // 65.6)
    return (
        1000 - milli_moist
        if g.config["options"]["soil_moisture_sensor"]["high_is_dry"]
        else milli_moist
    )


#############
# HTTP server
#############


async def store_file(reader, length: int, filename: str) -> None:
    tmp_filename = f"upload-{filename}.tmp"
    received = 0
    try:
        buf = memoryview(bytearray(512))
        with open(tmp_filename, "wb") as f:
            while received < length:
                chunk_length = reader.readinto(buf)
                # Check if it's a coroutine (awaitable generator) by looking for 'send'
                if hasattr(chunk_length, "send"):
                    chunk_length = await chunk_length
                if chunk_length == 0:
                    break
                f.write(buf[:chunk_length])
                received += chunk_length
        if received != length:
            raise OSError(
                f"Incomplete upload for {filename}: expected {length} bytes, received {received} bytes"
            )
        rename(tmp_filename, filename)
    except (OSError, ValueError) as e:
        error(None, None, f"Error storing [{filename}]: {e}")
        try:
            remove(tmp_filename)
        except OSError:
            pass
        raise


async def store_url(url: str, filename: str) -> None:
    response = requests.get(url)
    try:
        if response.status_code != 200:
            raise RuntimeError(f"HTTP Error {response.status_code}")

        headers = getattr(response, "headers", {})
        length = int(headers.get("content-length", headers.get("Content-Length")))
        await store_file(response.raw, length, filename)
    finally:
        response.close()


async def serve_file(filename: str, writer) -> None:
    try:
        buf = memoryview(bytearray(512))
        with open(filename, "rb") as f:
            while (length := f.readinto(buf)) > 0:
                writer.write(buf[:length])
                await writer.drain()
    except OSError as e:
        error(None, None, f"Error serving [{filename}]: {e}")
        raise


async def read_http_headers(reader) -> dict:
    headers = {}
    while True:
        line = await reader.readline()
        if line == b"\r\n" or not line:
            break
        name, value = line.decode().strip().split(":", 1)
        headers[name.lower()] = value.strip()
    return headers


def get_status_message(status_code):
    status_messages = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        500: "Server Error",
    }
    return status_messages.get(status_code, "Unknown")


#######################
# HTTP Response Helpers
#######################


async def send_response(writer, content_type, response, status_code=200):
    encoded = response.encode("utf-8") if response else b""
    writer.write(
        f"HTTP/1.0 {status_code} {get_status_message(status_code)}\r\nContent-type: {content_type}\r\nContent-Length: {len(encoded)}\r\n\r\n".encode(
            "utf-8"
        )
    )
    if encoded:
        writer.write(encoded)
    await writer.drain()


async def send_json(writer, data, status_code=200):
    await send_response(writer, "application/json", ujson.dumps(data), status_code)


async def send_file(writer, content_type, filename, status_code=200):
    file_size = stat(filename)[6]
    writer.write(
        f"HTTP/1.0 {status_code} {get_status_message(status_code)}\r\nContent-type: {content_type}\r\nContent-Length: {file_size}\r\n\r\n".encode(
            "utf-8"
        )
    )
    await serve_file(filename, writer)


#######################
# HTTP Route Handlers
#######################


async def handle_get_root(writer, **kwargs):
    filename = "setup.html" if g.WIFI_SETUP_MODE else "index.html"
    await send_file(writer, "text/html", filename)


async def handle_get_favicon(writer, **kwargs):
    response = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M50 5 C30 5 5 35 5 60 C5 85 25 95 50 95 C75 95 95 85 95 60 C95 35 70 5 50 5Z" fill="#4FC3F7" stroke="#29B6F6" stroke-width="2"/><ellipse cx="30" cy="35" rx="10" ry="15" fill="#81D4FA" transform="rotate(-35 30 35)"/></svg>'
    await send_response(writer, "image/svg+xml", response)


async def handle_get_config(writer, **kwargs):
    await send_json(writer, g.config)


async def handle_post_config(reader, content_length, writer, **kwargs):
    body = (
        ujson.loads((await reader.read(content_length)).decode())
        if content_length > 0
        else None
    )
    if not body:
        await send_response(writer, "text/html", "Empty config body", status_code=400)
        return
    await apply_config(body)
    save_as_json(CONFIG_FILENAME, g.config)
    await send_json(writer, g.config)


async def handle_put_pause(writer, query_params, **kwargs):
    duration_sec = int(query_params.get("duration_sec", 0))
    schedule_pause_until = get_local_timestamp() + duration_sec
    info(None, None, f"Pausing schedule for {duration_sec} seconds")
    g.schedule_completed_until[:] = [schedule_pause_until] * len(
        g.schedule_completed_until
    )
    await send_json(writer, {"status": "ok"})


async def handle_put_adhoc(writer, query_params, **kwargs):
    duration_sec = int(query_params.get("duration_sec", 0))
    zone_id = int(query_params.get("zone_id", -1))
    if 0 <= zone_id < len(g.config["zones"]) and duration_sec >= 0:
        end_time = get_local_timestamp() + duration_sec
        debug(
            zone_id,
            None,
            f"Ad-hoc irrigation for zone {zone_id} of {duration_sec}s (until {end_time})",
        )
        g.ad_hoc_irrigation_until[zone_id] = end_time
    await send_json(writer, {"status": "ok"})


async def handle_post_file(
    reader, content_length, writer, path, query_params, **kwargs
):
    filename = path[6:]
    info(None, None, f"Updating {filename}")
    await store_file(reader, content_length, filename)
    response = {
        "filepath": filename,
        "stat": ujson.dumps(stat(filename)),
    }
    await send_json(writer, response)


async def handle_get_file(writer, path, **kwargs):
    filename = path[6:]
    await send_file(writer, "text/html", filename)  # Content-type is a simplification


async def handle_get_status(writer, **kwargs):
    now = get_local_timestamp()
    response = {
        "version": VERSION,
        "local_timestamp": now,
        "soil_moisture": {
            z["name"]: get_soil_moisture_milli(i)
            for i, z in enumerate(g.config["zones"])
            if z["adc_pin_id"] >= 0 and not z["master"]
        },
        "machine": sys.implementation._machine,
        "gc.mem_alloc": mem_alloc(),
        "gc.mem_free": mem_free(),
        "valve_status": f"{g.valve_status:08b}",
        "schedule_status": f"{g.schedule_status:08b}",
        "mcu_temperature": mcu_temperature(),
        "uptime": get_uptime_sec(),
        "schedule_completed_until": [
            max(t - now, -1) for t in g.schedule_completed_until
        ],
        "ad_hoc_irrigation": {
            f"Zone[{zone_id}]='{g.config['zones'][zone_id]['name']}'": max(t - now, -1)
            for zone_id, t in g.ad_hoc_irrigation_until.items()
        },
        "hostname": g.config["options"]["wifi"]["hostname"],
        "mac_address": ":".join([f"{b:02x}" for b in g.wlan.config("mac")]),
    }
    await send_json(writer, response)


async def handle_get_log(writer, **kwargs):
    now = get_local_timestamp()
    response = {
        "local_timestamp": now,
        "log": [
            {
                "timestamp": i.timestamp,
                "level": i.level,
                "zone_id": i.zone_id,
                "schedule_id": i.schedule_id,
                "message": i.message,
            }
            for i in g.LOG
        ],
    }
    await send_json(writer, response)


async def handle_get_logtsv(writer, **kwargs):
    response = "\n".join(
        [
            f"{i.timestamp}\t{i.level}\t{i.zone_id}\t{i.schedule_id}\t{i.message}"
            for i in g.LOG
        ]
    )
    await send_response(writer, "text/tab-separated-values", response)


async def handle_put_reboot(writer, **kwargs):
    await send_response(writer, "text/html", "OK")
    return True  # Signal for reboot


async def handle_update_by_tag(writer, query_params, **kwargs):
    tag = query_params.get("tag")
    if not tag:
        await send_response(
            writer, "text/html", "Error: 'tag' parameter is missing.", status_code=400
        )
        return

    info(None, None, f"Received update request for tag: {tag}")
    try:
        with open("update_tag.txt", "w") as f:
            f.write(tag)
        await send_response(writer, "text/html", "OK")
        return True
    except OSError as e:
        error(None, None, f"Failed to save update tag: {e}")
        await send_response(
            writer, "text/html", f"Error saving update tag: {e}", status_code=500
        )


async def handle_not_found(writer, method, path, **kwargs):
    response = f"Resource not found: method={method} path={path}"
    await send_response(writer, "text/html", response, status_code=404)


################
# handle_request
################

ROUTES = {
    ("GET", "/"): handle_get_root,
    ("GET", "/favicon.ico"): handle_get_favicon,
    ("GET", "/config"): handle_get_config,
    ("POST", "/config"): handle_post_config,
    ("PUT", "/pause"): handle_put_pause,
    ("PUT", "/adhoc"): handle_put_adhoc,
    ("GET", "/status"): handle_get_status,
    ("GET", "/log"): handle_get_log,
    ("GET", "/logtsv"): handle_get_logtsv,
    ("PUT", "/reboot"): handle_put_reboot,
    ("PUT", "/update"): handle_update_by_tag,
}


async def handle_request(reader, writer):
    reboot = False
    req_start_time = time.ticks_ms()
    method, path = "unknown", "unknown"  # for logging in case of early failure

    try:
        req = (await reader.readline()).decode().lstrip()
        if not req:
            return

        method, path, _ = req.split(" ", 2)
        path, query_params = path.split("?", 1) if "?" in path else (path, None)
        query_params = (
            dict(
                [
                    p.replace("+", " ").split("=", 1) if "=" in p else (p, "")
                    for p in query_params.split("&")
                ]
            )
            if query_params
            else {}
        )

        headers = await read_http_headers(reader)
        content_length = int(headers.get("content-length", "0"))

        debug(
            None,
            None,
            f"Processing request: {method:4} {path:14} query_params={query_params}, (content_length={content_length})",
        )

        handler = ROUTES.get((method, path))
        handler_kwargs = {
            "reader": reader,
            "writer": writer,
            "query_params": query_params,
            "content_length": content_length,
            "path": path,
            "method": method,
            "headers": headers,
        }

        if handler:
            reboot = await handler(**handler_kwargs)
        elif method == "POST" and path.startswith("/file/"):
            await handle_post_file(**handler_kwargs)
        elif method == "GET" and path.startswith("/file/"):
            await handle_get_file(**handler_kwargs)
        else:
            await handle_not_found(**handler_kwargs)

    except Exception as e:
        warn(None, None, f"failed handling request: {e}")
        try:
            await send_response(writer, "text/html", "Server Error", status_code=500)
        except OSError as e2:
            warn(None, None, f"failed sending error response: {e2}")
    finally:
        writer.close()
        await writer.wait_closed()

    debug(
        None,
        None,
        f"Handled Request: {method:4} {path:14} in {time.ticks_ms() - req_start_time}ms",
    )
    collect()

    if reboot:
        info(None, None, "Restarting...")
        await asyncio.sleep(1)
        if g.heartbeat_pin_id > 0:
            Pin(g.heartbeat_pin_id, Pin.OUT, value=0 if g.heartbeat_high_is_on else 1)
        reset()


async def send_metrics():
    while True:
        try:
            metrics = [mcu_temperature(), g.valve_status] + [
                get_soil_moisture_milli(i)
                for i, z in enumerate(g.config["zones"])
                if z["adc_pin_id"] >= 0 and not z["master"]
            ]
            # mem_alloc(), mcu_temperature()
            if g.config["options"]["monitoring"]["thingsspeak_apikey"]:
                params = {
                    "api_key": g.config["options"]["monitoring"]["thingsspeak_apikey"]
                } | {f"field{i + 1}": m for i, m in enumerate(metrics)}
                r = requests.get(
                    "http://api.thingspeak.com/update?"
                    + "&".join([f"{k}={v}" for k, v in params.items()]),
                    timeout=10,
                )
                r.text
                r.close()
        except OSError as e:
            warn(None, None, f"failed sending metrics: {e}")
        finally:
            await asyncio.sleep(g.config["options"]["monitoring"]["send_interval_sec"])
        collect()


async def run_setup_mode_if_needed(button_pin_id: int, wait_time: int) -> None:
    try:
        stat(CONFIG_FILENAME)
    except OSError:
        g.WIFI_SETUP_MODE = True

    if not g.WIFI_SETUP_MODE and button_pin_id >= 0:
        for _ in range(round(wait_time * 10)):
            await asyncio.sleep(0.1)
            if 0 == Pin(button_pin_id, Pin.IN, Pin.PULL_UP).value():
                g.WIFI_SETUP_MODE = True
                break

    if g.WIFI_SETUP_MODE:
        if g.heartbeat_pin_id >= 0:
            PWM(Pin(g.heartbeat_pin_id), freq=5, duty_u16=32768)
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid="irrigation-esp32")
        server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
        info(None, None, "Server listening on port 80")
        await server.wait_closed()


async def process_ota_update():
    github_repo_owner = "karpada"
    github_repo_name = "RSI"
    try:
        stat("update_tag.txt")
    except OSError:
        return

    try:
        info(None, None, "Starting OTA update...")
        with open("update_tag.txt", "r") as f:
            tag = f.read().strip()
        info(None, None, f"Processing OTA update for tag: {tag}")
        remove("update_tag.txt")

        files_to_update = ["index.html", "setup.html", "main.py"]
        success = True
        for filename in files_to_update:
            try:
                info(None, None, f"Attempting to update {filename} for tag {tag}")
                raw_url = f"https://raw.githubusercontent.com/{github_repo_owner}/{github_repo_name}/{tag}/{filename}"
                collect()

                await store_url(raw_url, f"{filename}.ota")
                info(None, None, f"Successfully downloaded {filename}")
            except (OSError, RuntimeError) as e:
                error(None, None, f"Failed to download {filename}: {e}")
                success = False
                break
        if success:
            for filename in files_to_update:
                rename(f"{filename}.ota", filename)
            info(None, None, f"Update to tag '{tag}' successful. Rebooting...")
        else:
            error(None, None, f"Update to tag '{tag}' failed for some files.")
    finally:
        await asyncio.sleep(1)
        reset()


async def main():
    if sys.maxsize >> 30 == 0:
        warn(None, None, ">>> We have less than 31 bits :(")

    BoardBootstrap = namedtuple(
        "BoardBootstrap",
        ["name", "button_pin_id", "heartbeat_pin_id", "heartbeat_high_is_on"],
    )
    for bootstrap in [
        BoardBootstrap("ESP32S3", 0, 44, True),  # blue
        BoardBootstrap("ESP8266", -1, 2, True),
        BoardBootstrap("S2_MINI", 0, 15, True),
        BoardBootstrap("ESP32S2", 0, 15, True),
        BoardBootstrap("ESP32C3", 9, 8, False),
        BoardBootstrap("UNKNOWN", -1, -1, True),
    ]:
        if bootstrap.name in sys.implementation._machine:
            break
    info(
        None,
        None,
        f"Starting RSI {VERSION} on [{sys.implementation._machine}] detected as {bootstrap}",
    )
    g.heartbeat_pin_id = bootstrap.heartbeat_pin_id
    g.heartbeat_high_is_on = bootstrap.heartbeat_high_is_on

    migrate_config_if_needed()

    await run_setup_mode_if_needed(bootstrap.button_pin_id, 1)

    await apply_config(load_from_json(CONFIG_FILENAME) or {})

    # set valve_status = 0b1111...1 so that the first apply_valves will turn off all valves
    g.valve_status = (1 << len(g.config["zones"])) - 1
    info(None, None, "Closing all valves...")
    await apply_valves(0)

    await connect_wifi()

    await process_ota_update()

    asyncio.create_task(keep_wifi_connected())
    asyncio.create_task(periodic_ntp_sync())
    asyncio.create_task(send_metrics())
    asyncio.create_task(schedule_irrigation())
    asyncio.create_task(fallback_time_sync())

    server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
    info(None, None, "Server listening on port 80")
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
