import requests
import unittest
import os
import sys

# --- Test Configuration ---
# The IP address of the ESP32 device running the RSI software.
# It must be provided as a command-line argument.
if len(sys.argv) < 2:
    print("Error: Device address must be provided as a command-line argument.")
    print("Usage: python3 test_main.py <device_ip_or_hostname>")
    sys.exit(1)

# Get IP from command-line argument, and remove it so unittest doesn't see it
DEVICE_IP = sys.argv.pop(1)
BASE_URL = f"http://{DEVICE_IP}"

class TestRsiApi(unittest.TestCase):
    """
    A test suite for the RSI web API.
    This is a "black-box" test that runs against a live device.
    """

    def test_01_get_status(self):
        """Tests the GET /status endpoint for a valid response."""
        print("Testing GET /status...")
        try:
            response = requests.get(f"{BASE_URL}/status", timeout=5)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            # Check for presence of key fields
            self.assertIn("version", data)
            self.assertIn("local_timestamp", data)
            self.assertIn("valve_status", data)
            self.assertIn("schedule_status", data)
            self.assertIn("hostname", data)
        except requests.exceptions.RequestException as e:
            self.fail(f"Failed to connect to device at {BASE_URL}. Error: {e}")

    def test_02_get_config(self):
        """Tests the GET /config endpoint for a valid response."""
        print("Testing GET /config...")
        try:
            response = requests.get(f"{BASE_URL}/config", timeout=5)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            # Check for the basic structure of the config
            self.assertIn("zones", data)
            self.assertIn("schedules", data)
            self.assertIn("options", data)
        except requests.exceptions.RequestException as e:
            self.fail(f"Failed to connect to device at {BASE_URL}. Error: {e}")

    def test_03_get_log(self):
        """Tests the GET /log endpoint for a valid response."""
        print("Testing GET /log...")
        try:
            response = requests.get(f"{BASE_URL}/log", timeout=5)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("local_timestamp", data)
            self.assertIn("log", data)
            self.assertIsInstance(data["log"], list)
        except requests.exceptions.RequestException as e:
            self.fail(f"Failed to connect to device at {BASE_URL}. Error: {e}")

    def test_04_get_root(self):
        """Tests that GET / serves the correct index.html page."""
        print("Testing GET / content...")
        try:
            # Read the local index.html file
            with open("index.html", "r") as f:
                local_content = f.read()

            # Get the remote content from the root URL
            response = requests.get(f"{BASE_URL}/", timeout=5)
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/html", response.headers["Content-Type"])

            self.assertEqual(local_content, response.text)

        except FileNotFoundError:
            self.fail("Could not find the local index.html file to compare against.")
        except requests.exceptions.RequestException as e:
            self.fail(f"Failed to connect to device at {BASE_URL}. Error: {e}")

    def test_05_get_file_main_py(self):
        """Tests GET /file/main.py and validates its content against the local file."""
        print("Testing GET /file/main.py content...")
        try:
            # Read the local main.py file
            with open("main.py", "r") as f:
                local_content = f.read()

            # Get the remote main.py file content
            file_response = requests.get(f"{BASE_URL}/file/main.py", timeout=5)
            self.assertEqual(file_response.status_code, 200)

            self.assertEqual(local_content, file_response.text)

        except FileNotFoundError:
            self.fail("Could not find the local main.py file to compare against.")
        except requests.exceptions.RequestException as e:
            self.fail(f"Failed to connect to device at {BASE_URL}. Error: {e}")


if __name__ == '__main__':
    print("--- RSI Black-Box Test Suite ---")
    print(f"Target Device: {BASE_URL}")
    print("Please ensure the RSI device is running and connected to the network.\n")

    # It's better to install requests if you don't have it: pip install requests
    try:
        import requests
    except ImportError:
        print("Error: The 'requests' library is not installed.")
        print("Please install it using: pip install requests")
        exit(1)

    unittest.main(verbosity=2)
