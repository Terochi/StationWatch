import time
import requests

from agent import settings, DEVICE_ID, DEVICE_NAME

session = requests.Session()

SERVER_URL = settings.server_url + "api/events"
HEARTBEAT_URL = settings.server_url + "api/heartbeat"


def send_payload(url, data):
    try:
        data["device_id"] = DEVICE_ID
        data["timestamp"] = int(time.time())
        session.post(url, json=data, timeout=3)
    except requests.exceptions.RequestException:
        pass


def _send_win_change(title, exe):
    send_payload(SERVER_URL, {
        "event": "window_changed",
        "focused_window": title,
        "current_exe": exe
    })


def send_changed_devices(payload_connected, payload_disconnected):
    send_payload(SERVER_URL, {
        "event": "hardware_changed", "connected_peripherals": payload_connected,
        "disconnected_peripherals": payload_disconnected
    })


def heartbeat_loop():
    while True:
        send_payload(HEARTBEAT_URL, {"status": "alive", "device_name": DEVICE_NAME})
        time.sleep(settings.heartbeat_interval)
