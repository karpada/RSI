import sys
from collections import namedtuple, deque
from gc import mem_alloc, mem_free
import network
import utime as time
from machine import Pin, ADC, PWM, reset, freq
from esp32 import mcu_temperature
import ujson
from ntptime import settime
import uasyncio as asyncio
import urequests as requests
from uos import rename, stat
from machine import RTC

# Global variables
VERSION = "0.0.13"  # DO NOT EDIT: This line is automatically updated by the version-bump workflow
MICROPYTHON_TO_TIMESTAMP: int = 946684800  # 2000-1970 --> 3155673600 - 2208988800
TIMESTAMP_2001_01_01: int = (
    978307200  # Monday, This is the date used when ntp is not available
)
TIMESTAMP_2025_01_01: int = (
    1735689600  # after ntp sync, date will be at least 2025-01-01
)
WIFI_SETUP_MODE = False
micropython_to_localtime: int = 0
wlan: network.WLAN = network.WLAN(network.STA_IF)
config: dict = None
valve_status: int = 0
schedule_status: int = 0
heartbeat_pin_id: int = -1
heartbeat_high_is_on: bool = True
schedule_completed_until = []
ad_hoc_irrigation_until = {}


# logging
def get_local_timestamp() -> int:
    return time.time() + micropython_to_localtime


LogLine = namedtuple(
    "LogLine", ["timestamp", "level", "zone_id", "schedule_id", "message"]
)
LOG = deque([], 25)


def log(level: int, zone_id: int, schedule_id: int, message: str) -> None:
    ts = get_local_timestamp()
    print(f"@{ts} z{zone_id} s{schedule_id} {message}")
    if config and level < config["options"]["log"]["level"]:
        return
    LOG.append(LogLine(ts, level, zone_id, schedule_id, message))


def debug(zone_id: int, schedule_id: int, message: str) -> None:
    log(10, zone_id, schedule_id, message)


def info(zone_id: int, schedule_id: int, message: str) -> None:
    log(20, zone_id, schedule_id, message)


def warn(zone_id: int, schedule_id: int, message: str) -> None:
    log(30, zone_id, schedule_id, message)


def error(zone_id: int, schedule_id: int, message: str) -> None:
    log(40, zone_id, schedule_id, message)


# Persistent storage functions
def save_as_json(filename: str, data: dict) -> None:
    info(None, None, f"Saving data to {filename}")
    with open(filename, "w", encoding="utf-8") as f:
        ujson.dump(data, f)


def load_from_json(filename: str) -> dict:
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return ujson.load(f)
    except Exception as e:
        error(None, None, f"Error in load_from_json(): {e}")
        return None


async def connect_wifi() -> None:
    try:
        if not config["options"]["wifi"]["ssid"]:
            return
        network.hostname(config["options"]["wifi"]["hostname"])
        wlan.active(True)
        info(None, None, "Wifi connecting...")
        wlan.connect(
            config["options"]["wifi"]["ssid"], config["options"]["wifi"]["password"]
        )
        for _ in range(15):
            if wlan.isconnected():
                break
            await asyncio.sleep(1)
            print(".", end="")
        if wlan.isconnected():
            info(
                None,
                None,
                f"Connected, ip = {wlan.ifconfig()[0]}, hostname={config['options']['wifi']['hostname']}",
            )
            return
        wlan.active(False)
        warn(None, None, "network connection failed, retrying in 60 seconds")
    except Exception as e:
        wlan.active(False)
        warn(None, None, f"Exception while connecting to wifi: {e}")


async def keep_wifi_connected():
    while True:
        while wlan.isconnected():
            await asyncio.sleep(10)
        await asyncio.sleep(60)
        await connect_wifi()


# Time functions
async def sync_ntp() -> bool:
    try:
        settime()
        info(
            None,
            None,
            f"@{time.time()} NTP synced, UTC time={time.time() + MICROPYTHON_TO_TIMESTAMP} Local time(GMT{config['options']['settings']['timezone_offset']:+})={time.time() + micropython_to_localtime}",
        )
        return True
    except Exception:
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
    sync_conf = config["options"]["fallback_time_sync"]
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
            debug(None, None, f"Temperature log: {temperature_log}")
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
                    - config["options"]["settings"]["timezone_offset"]
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
                    f"RTC before adjustment: {rtc.datetime()}, will assign={basetime}",
                )
                rtc.datetime(basetime)
                warn(
                    None,
                    None,
                    f"it's {hours_till_6_15am} hours till 6:15am, adjusted RTC by {adjustment_hours:+}h ({rtc_hour_gmt} -> {now_hours_gmt}), temperature log: {temperature_log}",
                )
        except Exception as e:
            error(None, None, f"Error in fallback_time_sync: {e}")
    info(None, None, "synced, fallback_time_sync ended")


# Watering control functions
def control_watering(zone_id: int, start: bool) -> None:
    if zone_id < 0 or zone_id >= len(config["zones"]):
        warn(zone_id, None, "invalid zone_id")
        return
    zone = config["zones"][zone_id]
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
        # pulse the pin
        Pin(pin_id, Pin.OUT).value(pin_value)
        time.sleep(0.060)  # is precise timing really needed?
        Pin(pin_id, Pin.IN)
    else:
        # leave the pin in the state
        if start:
            Pin(pin_id, Pin.OUT, value=pin_value)
        else:
            Pin(pin_id, Pin.IN)


async def apply_valves(new_status: int) -> None:
    global valve_status
    if new_status == valve_status:
        return

    debug(
        None, None, f"apply_valves({new_status:08b}), valve_status={valve_status:08b}"
    )
    relay_pin_id = config["options"]["settings"]["relay_pin_id"]
    if relay_pin_id >= 0:
        relay_value = 1 if config["options"]["settings"]["relay_active_is_high"] else 0
        Pin(relay_pin_id, Pin.OUT, value=relay_value)
        await asyncio.sleep(0.250)  # wait for H-Bridges to power up

    for i in range(len(config["zones"])):
        if (valve_status ^ new_status) & (1 << i):
            control_watering(i, bool(new_status & (1 << i)))
            await asyncio.sleep(0.050)  # wait to settle down
    valve_status = new_status

    if relay_pin_id >= 0:
        Pin(relay_pin_id, Pin.IN)


######################
# Irrigation scheduler
######################
async def schedule_irrigation():
    global schedule_status

    await asyncio.sleep(5)
    while True:
        if heartbeat_pin_id > 0:
            Pin(heartbeat_pin_id, Pin.OUT).value(1 if heartbeat_high_is_on else 0)

        local_timestamp = get_local_timestamp()

        valve_desired = 0
        new_schedule_status = 0
        for i, s in enumerate(config["schedules"]):
            # debug(None, i, "checking schedule")
            if local_timestamp < schedule_completed_until[i]:
                continue

            zone_id = s["zone_id"]
            z = config["zones"][zone_id]

            # following checks disabled the schedule until config change
            if not config["options"]["settings"]["enable_irrigation_schedule"]:
                schedule_completed_until[i] = sys.maxsize
                debug(
                    zone_id,
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' disabled because all schedules is disabled",
                )
                continue

            if not s["enabled"]:
                schedule_completed_until[i] = sys.maxsize
                debug(
                    zone_id,
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' disabled because schedule is disabled",
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
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' disabled because duration_sec is zero",
                )
                continue

            if s["expiry"] and local_timestamp > s["expiry"]:
                schedule_completed_until[i] = sys.maxsize
                debug(
                    zone_id,
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' disabled because schedule expired",
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
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' suspended until next start: {schedule_completed_until[i]}",
                )
                continue

            # weekday of current schedule start time, monday is 0, sunday is 6
            weekday = ((local_timestamp + sec_till_start) // 86400 + 2) % 7
            if not s["day_mask"] & (1 << weekday):
                schedule_completed_until[i] = local_timestamp + sec_till_start + 1
                debug(
                    zone_id,
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' suspended until next start {schedule_completed_until[i]} because of day_mask={s['day_mask']:07b} weekday={weekday}",
                )
                continue

            if (
                s["enable_soil_moisture_sensor"]
                and (soil_moisture := get_soil_moisture_milli(s["zone_id"])) is not None
            ):
                # soil_moisture value needs to be taken into account
                if schedule_status & (1 << i):
                    # schedule is active, check if we should stop
                    if soil_moisture >= z["soil_moisture_wet"]:
                        schedule_completed_until[i] = (
                            local_timestamp + sec_till_start + 1
                        )
                        info(
                            zone_id,
                            i,
                            f"Schedule[{i}] zone[{zone_id}]='{z['name']}' stopped and suspended until next start {schedule_completed_until[i]} because soil_moisture={soil_moisture} is wet",
                        )
                        continue
                else:
                    # schedule is about to start, is it dry enough?
                    if soil_moisture >= z["soil_moisture_dry"]:
                        schedule_completed_until[i] = (
                            local_timestamp + sec_till_start + 1
                        )
                        info(
                            zone_id,
                            i,
                            f"Schedule[{i}] zone[{zone_id}]='{z['name']}' won't start and suspended until next start {schedule_completed_until[i]} because soil_moisture={soil_moisture} is not dry enough",
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
            z = config["zones"][zone_id]
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
            for i, zone in enumerate(config["zones"]):
                if zone["master"]:
                    valve_desired |= 1 << i

        for i, s in enumerate(config["schedules"]):
            if (schedule_status ^ new_schedule_status) & (1 << i):
                zone_id = s["zone_id"]
                z = config["zones"][zone_id]
                info(
                    s["zone_id"],
                    i,
                    f"Schedule[{i}] zone[{zone_id}]='{z['name']}' {'started' if new_schedule_status & (1 << i) else 'ended'} for zone {s['zone_id']} ({z['name']})",
                )
        await apply_valves(valve_desired)
        schedule_status = new_schedule_status
        if heartbeat_pin_id > 0:
            Pin(heartbeat_pin_id, Pin.IN)
        await asyncio.sleep(2)


#########################
# Configuration functions
#########################
async def apply_config(new_config: dict) -> None:
    global config
    global schedule_completed_until
    global micropython_to_localtime
    global heartbeat_pin_id
    global LOG

    info(None, None, f"Applying new config = {new_config}")
    normalized_config = {"zones": [], "schedules": [], "options": {}}
    for i, z in enumerate(new_config.get("zones", [])):
        normalized_config["zones"].append(
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
                "adc_pin_id": int(z.get("adc_pin_id", 12)),
                "power_pin_id": int(z.get("power_pin_id", 13)),
            }
        )
    for s in new_config.get("schedules", []):
        normalized_config["schedules"].append(
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
    bo = new_config.get("options", {})
    for key in [
        "wifi",
        "monitoring",
        "soil_moisture_sensor",
        "settings",
        "log",
        "fallback_time_sync",
    ]:
        bo.setdefault(key, {})
    normalized_config["options"] = {
        "wifi": {
            "ssid": str(bo["wifi"].get("ssid", "")),
            "password": str(bo["wifi"].get("password", "")),
            "hostname": str(
                bo["wifi"].get(
                    "hostname",
                    "rsi-" + "".join([f"{b:02x}" for b in wlan.config("mac")[3:6]]),
                )
            ),
        },
        "monitoring": {
            "thingsspeak_apikey": str(bo["monitoring"].get("thingsspeak_apikey", "")),
            "send_interval_sec": int(bo["monitoring"].get("send_interval_sec", 300)),
        },
        "soil_moisture_sensor": {
            "high_is_dry": bool(bo["soil_moisture_sensor"].get("high_is_dry", True)),
            "sample_count": int(bo["soil_moisture_sensor"].get("sample_count", 3)),
        },
        "settings": {
            "enable_irrigation_schedule": bool(
                bo["settings"].get("enable_irrigation_schedule", True)
            ),
            "timezone_offset": float(bo["settings"].get("timezone_offset", -7)),
            "relay_pin_id": int(bo["settings"].get("relay_pin_id", -1)),
            "heartbeat_pin_id": int(
                bo["settings"].get("heartbeat_pin_id", heartbeat_pin_id)
            ),
            "heartbeat_high_is_on": bool(
                bo["settings"].get("heartbeat_high_is_on", heartbeat_high_is_on)
            ),
            "relay_active_is_high": bool(
                bo["settings"].get("relay_active_is_high", False)
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

    # if zones changed, turn off all valves
    if config and config.get("zones", []) != normalized_config["zones"]:
        await apply_valves(0)

    # log(None, None, f"apply_config({new_config})\n    normalized_config={normalized_config}")
    config = normalized_config

    micropython_to_localtime = MICROPYTHON_TO_TIMESTAMP + round(
        config["options"]["settings"]["timezone_offset"] * 3600
    )
    heartbeat_pin_id = config["options"]["settings"]["heartbeat_pin_id"]
    # disable schedules until fallback_time_sync or NTP synchronization is complete
    schedule_completed_until = [TIMESTAMP_2001_01_01] * len(config["schedules"])
    LOG = deque(
        [i for i in LOG if i.level >= config["options"]["log"]["level"]],
        config["options"]["log"]["max_lines"],
    )


def read_soil_moisture_raw(zone_id: int) -> int:
    soil_moisture_config = config["zones"][zone_id]
    if 0 > soil_moisture_config["adc_pin_id"]:
        return None
    if 0 <= soil_moisture_config["power_pin_id"]:
        Pin(soil_moisture_config["power_pin_id"], Pin.OUT).value(1)
        time.sleep_ms(10)  # Use blocking sleep since this function is synchronous
    # https://docs.micropython.org/en/latest/esp32/quickref.html#adc-analog-to-digital-conversion
    adc = ADC(soil_moisture_config["adc_pin_id"], atten=ADC.ATTN_11DB)
    raw_reading = 0
    for i in range(config["options"]["soil_moisture_sensor"]["sample_count"]):
        raw_reading += adc.read_u16()
    raw_reading //= i + 1
    if soil_moisture_config["power_pin_id"] >= 0:
        Pin(soil_moisture_config["power_pin_id"], Pin.IN)
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
        if config["options"]["soil_moisture_sensor"]["high_is_dry"]
        else milli_moist
    )


#############
# HTTP server
#############


async def store_file(reader, length: int, filename: str) -> None:
    try:
        buf = memoryview(bytearray(1024))
        with open("upload.tmp", "wb") as f:
            while length:
                chunk_length = await reader.readinto(buf)
                f.write(buf[:chunk_length])
                length -= chunk_length
        rename("upload.tmp", filename)
    except Exception as e:
        error(None, None, f"Error storing [{filename}]: {e}")
        raise


async def serve_file(filename: str, writer) -> None:
    try:
        buf = memoryview(bytearray(128))
        with open(filename, "rb") as f:
            while length := f.readinto(buf):
                writer.write(buf[:length])
    except Exception as e:
        error(None, None, f"Error serving [{filename}]: {e}")
        raise


async def read_http_headers(reader) -> dict:
    headers = {}
    while True:
        line = await reader.readline()
        if line == b"\r\n" or not line:
            break
        name, value = line.decode().strip().split(": ", 1)
        headers[name.lower()] = value
    return headers


def get_status_message(status_code):
    status_messages = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        500: "Server Error",
    }
    return status_messages.get(status_code, "Unknown")


################
# handle_request
################
async def handle_request(reader, writer):
    content_type = "application/json"
    status_code = 200
    filename = None

    try:
        req = (await reader.readline()).decode().lstrip()
        req_start_time = time.ticks_ms()

        if not req:
            writer.close()
            await writer.wait_closed()
            return
        method, path, _ = req.split(" ", 2)
        path, query_params = path.split("?", 1) if "?" in path else (path, None)
        query_params = (
            dict(
                [
                    param.replace("+", " ").split("=", 1)
                    for param in query_params.split("&")
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
        )  #     headers={headers}")

        reboot = False
        response = "Should not happen"

        if method == "GET" and path == "/":
            filename = "setup.html" if WIFI_SETUP_MODE else "index.html"
            content_type = "text/html"
        elif method == "GET" and path == "/favicon.ico":
            content_type = "image/svg+xml"
            response = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M50 5 C30 5 5 35 5 60 C5 85 25 95 50 95 C75 95 95 85 95 60 C95 35 70 5 50 5Z" fill="#4FC3F7" stroke="#29B6F6" stroke-width="2"/><ellipse cx="30" cy="35" rx="10" ry="15" fill="#81D4FA" transform="rotate(-35 30 35)"/></svg>'
        elif method == "GET" and path == "/config":
            # curl example: curl http://[ESP32_IP]/config
            response = ujson.dumps(config)
        elif method == "POST" and path == "/config":
            body = (
                ujson.loads((await reader.read(content_length)).decode())
                if content_length > 0
                else None
            )
            # restore backup: jq . irrigation-config.json | curl -H "Content-Type: application/json" -X POST --data-binary @- http://192.168.68.ESP/config
            await apply_config(body)
            response = ujson.dumps(config)
            save_as_json("config.json", config)
        elif method == "PUT" and path == "/pause":
            schedule_pause_until = get_local_timestamp() + int(
                query_params.get("duration_sec", 0)
            )
            info(
                None,
                None,
                f"Pausing schedule for {int(query_params.get('duration_sec', 0))} seconds",
            )
            schedule_completed_until[:] = [schedule_pause_until] * len(
                schedule_completed_until
            )
        elif method == "PUT" and path == "/adhoc":
            duration_sec = int(query_params.get("duration_sec", 0))
            zone_id = int(query_params.get("zone_id", -1))
            if 0 <= zone_id < len(config["zones"]) and duration_sec >= 0:
                end_time = get_local_timestamp() + duration_sec
                debug(
                    zone_id,
                    None,
                    f"Ad-hoc irrigation for zone {zone_id} of {duration_sec}s (until {end_time})",
                )
                ad_hoc_irrigation_until[zone_id] = end_time
        elif method == "POST" and path.startswith("/file/"):
            # curl -X POST --data-binary @main.py http://192.168.68.114/file/main.py\?reboot\=1
            info(None, None, f"Updating {path[6:]}")
            await store_file(reader, content_length, path[6:])
            # if '1' == query_params.get('reboot', '0'):
            #     reboot = True
            response = ujson.dumps(
                {
                    "method": method,
                    "filepath": path[6:],
                    "stat": ujson.dumps(stat(path[6:])),
                }
            )
        elif method == "GET" and path.startswith("/file/"):
            filename = path[6:]
            content_type = "text/html"
        elif method == "GET" and path == "/status":
            now = get_local_timestamp()
            response = ujson.dumps(
                {
                    "version": VERSION,
                    "local_timestamp": now,
                    "soil_moisture": {
                        z["name"]: get_soil_moisture_milli(i)
                        for i, z in enumerate(config["zones"])
                        if z["adc_pin_id"] >= 0 and not z["master"]
                    },
                    "machine": sys.implementation._machine,
                    "gc.mem_alloc": mem_alloc(),
                    "gc.mem_free": mem_free(),
                    "valve_status": f"{valve_status:08b}",
                    "schedule_status": f"{schedule_status:08b}",
                    "mcu_temperature": mcu_temperature(),
                    "schedule_completed_until": [
                        max(t - now, -1) for t in schedule_completed_until
                    ],
                    "ad_hoc_irrigation": {
                        f"Zone[{zone_id}]='{config['zones'][zone_id]['name']}'": max(
                            t - now, -1
                        )
                        for zone_id, t in ad_hoc_irrigation_until.items()
                    },
                    "hostname": config["options"]["wifi"]["hostname"],
                    "mac_address": ":".join([f"{b:02x}" for b in wlan.config("mac")]),
                }
            )
        elif method == "GET" and path == "/log":
            now = get_local_timestamp()
            response = ujson.dumps(
                {
                    "local_timestamp": now,
                    "log": [
                        {
                            "timestamp": i.timestamp,
                            "level": i.level,
                            "zone_id": i.zone_id,
                            "schedule_id": i.schedule_id,
                            "message": i.message,
                        }
                        for i in LOG
                    ],
                }
            )
        elif method == "GET" and path == "/logtsv":
            response = "\n".join(
                [
                    f"{i.timestamp}\t{i.level}\t{i.zone_id}\t{i.schedule_id}\t{i.message}"
                    for i in LOG
                ]
            )
        elif method == "PUT" and path == "/reboot":
            response = "OK"
            content_type = "text/html"
            reboot = True
        elif WIFI_SETUP_MODE and method == "GET" and path == "/setup":
            info(None, None, f"Setup: query_params={query_params}")
            save_as_json("config.json", {"options": {"wifi": query_params}})
            response = "OK"
            content_type = "text/html"
            reboot = True
        else:
            response = f"Resource not found: method={method} path={path}"
            status_code = 404

    except Exception as e:
        warn(None, None, f"failed handling request: {e}")
        writer.write(f"HTTP/1.0 500 {get_status_message(500)}\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        raise

    writer.write(
        f"HTTP/1.0 {status_code} {get_status_message(status_code)}\r\nContent-type: {content_type}\r\n\r\n"
    )
    if filename:
        await serve_file(filename, writer)
    else:
        writer.write(response.encode("utf-8"))
    await writer.drain()
    writer.close()
    await writer.wait_closed()
    debug(
        None,
        None,
        f"Handled Request: {method:4} {path:14} in {time.ticks_ms() - req_start_time}ms",
    )

    if reboot:
        info(None, None, "Restarting...")
        await asyncio.sleep(1)
        if heartbeat_pin_id > 0:
            Pin(heartbeat_pin_id, Pin.IN)
        reset()


async def send_metrics():
    while True:
        try:
            metrics = [mcu_temperature(), valve_status] + [
                get_soil_moisture_milli(i)
                for i, z in enumerate(config["zones"])
                if z["adc_pin_id"] >= 0 and not z["master"]
            ]
            # mem_alloc(), mcu_temperature()
            if "thingsspeak_apikey" in config["options"]["monitoring"]:
                params = {
                    "api_key": config["options"]["monitoring"]["thingsspeak_apikey"]
                } | {f"field{i + 1}": m for i, m in enumerate(metrics)}
                requests.get(
                    "http://api.thingspeak.com/update?"
                    + "&".join([f"{k}={v}" for k, v in params.items()]),
                    timeout=10,
                ).close()
        except Exception as e:
            warn(None, None, f"failed sending metrics: {e}")
        finally:
            await asyncio.sleep(config["options"]["monitoring"]["send_interval_sec"])


async def wait_for_wifi_setup(button_pin_id: int, wait_time: int) -> None:
    global WIFI_SETUP_MODE

    for _ in range(round(wait_time * 10)):
        await asyncio.sleep(0.1)
        if 0 == Pin(button_pin_id, Pin.IN, Pin.PULL_UP).value():
            WIFI_SETUP_MODE = True
            break
    if WIFI_SETUP_MODE:
        if heartbeat_pin_id >= 0:
            PWM(Pin(heartbeat_pin_id), freq=5, duty_u16=32768)
        ap = network.WLAN(network.AP_IF)
        ap.active(True)
        ap.config(essid="irrigation-esp32")
        server = await asyncio.start_server(handle_request, "0.0.0.0", 80)
        info(None, None, "Server listening on port 80")
        await server.wait_closed()


async def main():
    global valve_status
    global heartbeat_pin_id
    global heartbeat_high_is_on

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
        f"Starting RSI on [{sys.implementation._machine}] detected as {bootstrap}",
    )
    heartbeat_pin_id = bootstrap.heartbeat_pin_id
    heartbeat_high_is_on = bootstrap.heartbeat_high_is_on

    freq(80_000_000)

    if bootstrap.button_pin_id >= 0:
        await wait_for_wifi_setup(bootstrap.button_pin_id, 1)

    await apply_config(load_from_json("config.json") or {})

    # set valve_status = 0b1111...1 so that the first apply_valves will turn off all valves
    valve_status = (1 << len(config["zones"])) - 1
    info(None, None, "Closing all valves...")
    await apply_valves(0)

    await connect_wifi()
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
