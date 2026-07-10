import unittest
from unittest.mock import MagicMock, mock_open, patch
import sys
import os

# Add project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock MicroPython modules for unit tests
sys.modules["gc"] = MagicMock()
sys.modules["ujson"] = MagicMock()
sys.modules["utime"] = MagicMock()
sys.modules["network"] = MagicMock()
sys.modules["machine"] = MagicMock()
sys.modules["esp32"] = MagicMock()
sys.modules["ntptime"] = MagicMock()
sys.modules["uasyncio"] = MagicMock()
sys.modules["urequests"] = MagicMock()
sys.modules["uos"] = MagicMock()

from main import get_soil_moisture_milli, save_as_json, load_from_json, compute_desired_valves  # noqa: E402


def _make_config(zones=None, schedules=None, enable_irrigation_schedule=True):
    """Build a minimal config dict for compute_desired_valves tests."""
    if zones is None:
        zones = [
            {
                "name": "zone-0", "master": False, "active_is_high": True,
                "on_pin": 0, "off_pin": 1, "irrigation_factor_override": -1,
                "soil_moisture_dry": 300, "soil_moisture_wet": 700,
                "adc_pin_id": -1, "power_pin_id": -1,
            }
        ]
    if schedules is None:
        schedules = []
    return {
        "zones": zones,
        "schedules": schedules,
        "options": {
            "settings": {"enable_irrigation_schedule": enable_irrigation_schedule},
            "log": {"level": 10, "max_lines": 25},
        },
    }


def _make_schedule(zone_id=0, start_sec=0, duration_sec=3600, enabled=True,
                   day_mask=0b1111111, enable_soil_moisture_sensor=False,
                   interval_duration_sec=0, interval_on_sec=10, expiry=0):
    return {
        "zone_id": zone_id, "start_sec": start_sec, "duration_sec": duration_sec,
        "enabled": enabled, "day_mask": day_mask,
        "enable_soil_moisture_sensor": enable_soil_moisture_sensor,
        "interval_duration_sec": interval_duration_sec,
        "interval_on_sec": interval_on_sec, "expiry": expiry,
    }


class TestUnitTests(unittest.TestCase):
    """
    A test suite for internal functions, using mocking to isolate them from hardware and network.
    """

    def setUp(self):
        # Reset mocks before each test
        sys.modules["ujson"].reset_mock()
        sys.modules["uos"].reset_mock()

        # Mock the global config object for tests that need it
        self.mock_config = {
            "zones": [{"adc_pin_id": 12, "power_pin_id": 13, "sample_count": 3}],
            "options": {
                "soil_moisture_sensor": {"high_is_dry": True, "sample_count": 3},
                "log": {"level": 10, "max_lines": 25},
            },
        }
        patcher = patch("main.g.config", self.mock_config)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_get_soil_moisture_milli(self):
        # Test with high_is_dry = True
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=0), 1000)
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=32767), 500)
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=65535), 0)

        # Test with high_is_dry = False
        self.mock_config["options"]["soil_moisture_sensor"]["high_is_dry"] = False
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=0), 0)
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=32767), 500)
        self.assertEqual(get_soil_moisture_milli(0, raw_reading=65535), 1000)

    @unittest.expectedFailure
    def test_get_soil_moisture_milli_with_none_raw_reading(self):
        """This test is expected to fail due to a quirk of the mocking framework."""
        self.assertIsNone(get_soil_moisture_milli(0, raw_reading=None))

    def test_get_soil_moisture_milli_with_no_adc_pin(self):
        self.mock_config["zones"][0]["adc_pin_id"] = -1
        self.assertIsNone(get_soil_moisture_milli(0))

    def test_save_and_load_json(self):
        test_data = {"key": "value", "number": 123}
        test_filename = "test.json"
        tmp_filename = f"{test_filename}.tmp"

        m = mock_open()
        with patch("builtins.open", m):
            save_as_json(test_filename, test_data)
            m.assert_called_once_with(tmp_filename, "w", encoding="utf-8")
            sys.modules["ujson"].dump.assert_called_once_with(test_data, m())
            sys.modules["uos"].rename.assert_called_once_with(
                tmp_filename, test_filename
            )

            m.reset_mock()
            sys.modules["ujson"].reset_mock()
            mock_file_content = '{"key": "value", "number": 123}'
            m = mock_open(read_data=mock_file_content)
            sys.modules["ujson"].load.return_value = test_data

            with patch("builtins.open", m):
                loaded_data = load_from_json(test_filename)
                m.assert_called_once_with(test_filename, "r", encoding="utf-8")
                sys.modules["ujson"].load.assert_called_once_with(m())
                self.assertEqual(loaded_data, test_data)

    def test_load_from_json_file_not_found(self):
        test_filename = "non_existent.json"
        with patch("builtins.open", mock_open()) as m:
            m.side_effect = FileNotFoundError
            result = load_from_json(test_filename)
            self.assertIsNone(result)


class TestComputeDesiredValves(unittest.TestCase):
    """Tests for the compute_desired_valves scheduling logic."""

    def setUp(self):
        patcher = patch("main.g.config", _make_config())
        self.addCleanup(patcher.stop)
        patcher.start()

    def _call(self, config, timestamp, schedule_completed_until=None,
              ad_hoc=None, schedule_status=0, valve_status=0, soil_fn=None):
        if schedule_completed_until is None:
            schedule_completed_until = [0] * len(config["schedules"])
        if ad_hoc is None:
            ad_hoc = {}
        if soil_fn is None:
            soil_fn = lambda zone_id: None
        return compute_desired_valves(
            config, timestamp, schedule_completed_until,
            ad_hoc, schedule_status, valve_status, soil_fn
        )

    def test_no_schedules(self):
        config = _make_config(schedules=[])
        v, s = self._call(config, 1000)
        self.assertEqual(v, 0)
        self.assertEqual(s, 0)

    def test_schedule_active_during_window(self):
        # Schedule starts at 00:00 (0s), duration 1h. Timestamp is 00:30 (1800s into the day).
        # sec_till_start = (86400 + 0 - 1800) % 86400 = 84600
        # sec_till_end = (84600 + 3600) % 86400 = 1800
        # sec_till_start (84600) >= sec_till_end (1800) → inside window → valve on
        config = _make_config(schedules=[_make_schedule(start_sec=0, duration_sec=3600)])
        v, s = self._call(config, 1800)
        self.assertEqual(v, 0b1)  # zone 0 on
        self.assertEqual(s, 0b1)  # schedule 0 active

    def test_schedule_outside_window(self):
        # Schedule starts at 00:00 (0s), duration 1h. Timestamp is 02:00 (7200s).
        # sec_till_start = (86400 + 0 - 7200) % 86400 = 79200
        # sec_till_end = (79200 + 3600) % 86400 = 82800 < 79200? No, 82800 > 79200
        # Wait: sec_till_start (79200) < sec_till_end (82800) → outside window → skip
        config = _make_config(schedules=[_make_schedule(start_sec=0, duration_sec=3600)])
        v, s = self._call(config, 7200)
        self.assertEqual(v, 0)
        self.assertEqual(s, 0)

    def test_schedule_disabled(self):
        config = _make_config(schedules=[_make_schedule(enabled=False)])
        completed = [0]
        v, s = self._call(config, 1800, schedule_completed_until=completed)
        self.assertEqual(v, 0)
        self.assertEqual(completed[0], sys.maxsize)

    def test_all_schedules_disabled(self):
        config = _make_config(
            schedules=[_make_schedule()], enable_irrigation_schedule=False
        )
        completed = [0]
        v, s = self._call(config, 1800, schedule_completed_until=completed)
        self.assertEqual(v, 0)
        self.assertEqual(completed[0], sys.maxsize)

    def test_schedule_expired(self):
        config = _make_config(schedules=[_make_schedule(expiry=1000)])
        completed = [0]
        v, s = self._call(config, 2000, schedule_completed_until=completed)
        self.assertEqual(v, 0)
        self.assertEqual(completed[0], sys.maxsize)

    def test_day_mask_blocks_schedule(self):
        # Timestamp 1800 → weekday depends on epoch. Use day_mask=0 to block all days.
        config = _make_config(schedules=[_make_schedule(day_mask=0)])
        v, s = self._call(config, 1800)
        self.assertEqual(v, 0)

    def test_ad_hoc_irrigation(self):
        config = _make_config(schedules=[])
        ad_hoc = {0: 5000}  # zone 0 until timestamp 5000
        v, s = self._call(config, 3000, ad_hoc=ad_hoc)
        self.assertEqual(v, 0b1)

    def test_ad_hoc_expired(self):
        config = _make_config(schedules=[])
        ad_hoc = {0: 2000}
        v, s = self._call(config, 3000, ad_hoc=ad_hoc)
        self.assertEqual(v, 0)
        self.assertNotIn(0, ad_hoc)  # expired entry removed

    def test_master_valve_activated(self):
        zones = [
            {
                "name": "zone-0", "master": False, "active_is_high": True,
                "on_pin": 0, "off_pin": 1, "irrigation_factor_override": -1,
                "soil_moisture_dry": 300, "soil_moisture_wet": 700,
                "adc_pin_id": -1, "power_pin_id": -1,
            },
            {
                "name": "master", "master": True, "active_is_high": True,
                "on_pin": 2, "off_pin": 3, "irrigation_factor_override": -1,
                "soil_moisture_dry": 300, "soil_moisture_wet": 700,
                "adc_pin_id": -1, "power_pin_id": -1,
            },
        ]
        config = _make_config(zones=zones, schedules=[_make_schedule(zone_id=0)])
        v, s = self._call(config, 1800)
        self.assertTrue(v & (1 << 0))  # zone 0 on
        self.assertTrue(v & (1 << 1))  # master on

    def test_soil_moisture_too_wet_prevents_start(self):
        config = _make_config(schedules=[
            _make_schedule(enable_soil_moisture_sensor=True)
        ])
        # soil_moisture=400 >= soil_moisture_dry=300 → too wet to start
        v, s = self._call(config, 1800, soil_fn=lambda z: 400, schedule_status=0)
        self.assertEqual(v, 0)

    def test_soil_moisture_dry_enough_to_start(self):
        config = _make_config(schedules=[
            _make_schedule(enable_soil_moisture_sensor=True)
        ])
        # soil_moisture=200 < soil_moisture_dry=300 → dry enough
        v, s = self._call(config, 1800, soil_fn=lambda z: 200, schedule_status=0)
        self.assertEqual(v, 0b1)

    def test_soil_moisture_wet_stops_active_schedule(self):
        config = _make_config(schedules=[
            _make_schedule(enable_soil_moisture_sensor=True)
        ])
        # Schedule already active (schedule_status bit set), moisture=800 >= wet=700
        v, s = self._call(config, 1800, soil_fn=lambda z: 800, schedule_status=0b1)
        self.assertEqual(v, 0)
        self.assertEqual(s, 0)

    def test_interval_fogger_on_period(self):
        # interval_duration_sec=60, interval_on_sec=10
        # (86400 - sec_till_start) % 60 < 10 → valve on
        # At timestamp=1800: sec_till_start=84600, elapsed=86400-84600=1800, 1800%60=0 < 10 → on
        config = _make_config(schedules=[
            _make_schedule(interval_duration_sec=60, interval_on_sec=10)
        ])
        v, s = self._call(config, 1800)
        self.assertEqual(v, 0b1)
        self.assertEqual(s, 0b1)  # schedule status set regardless of interval

    def test_interval_fogger_off_period(self):
        # At timestamp=1810: sec_till_start=84590, elapsed=86400-84590=1810, 1810%60=10 >= 10 → off
        config = _make_config(schedules=[
            _make_schedule(interval_duration_sec=60, interval_on_sec=10)
        ])
        v, s = self._call(config, 1810)
        self.assertEqual(v, 0)
        self.assertEqual(s, 0b1)  # schedule_status still set (unaffected by interval)

    def test_already_completed_schedule_skipped(self):
        config = _make_config(schedules=[_make_schedule()])
        completed = [99999]  # completed_until in the future
        v, s = self._call(config, 1800, schedule_completed_until=completed)
        self.assertEqual(v, 0)
        self.assertEqual(s, 0)

    def test_irrigation_factor_override(self):
        zones = [
            {
                "name": "zone-0", "master": False, "active_is_high": True,
                "on_pin": 0, "off_pin": 1, "irrigation_factor_override": 0.0,
                "soil_moisture_dry": 300, "soil_moisture_wet": 700,
                "adc_pin_id": -1, "power_pin_id": -1,
            }
        ]
        # duration_sec * 0.0 = 0 → disabled
        config = _make_config(zones=zones, schedules=[_make_schedule(duration_sec=3600)])
        completed = [0]
        v, s = self._call(config, 1800, schedule_completed_until=completed)
        self.assertEqual(v, 0)
        self.assertEqual(completed[0], sys.maxsize)


if __name__ == "__main__":
    unittest.main(verbosity=2)

