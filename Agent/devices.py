import re
import threading
import pythoncom
import win32com
import win32com.client

from Agent.connection import send_changed_devices

known_devices = {}
state_lock = threading.Lock()


def get_device_group_id(device_id):
    match = re.search(r'(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})', device_id, re.IGNORECASE)
    return match.group(1).upper() if match else device_id


def extract_vid_pid(device_id):
    vid_match = re.search(r'VID_([0-9A-F]{4})', device_id, re.IGNORECASE)
    pid_match = re.search(r'PID_([0-9A-F]{4})', device_id, re.IGNORECASE)
    vid = vid_match.group(1).upper() if vid_match else None
    pid = pid_match.group(1).upper() if pid_match else None
    return vid, pid


def determine_category(pnp_class, name):
    pnp_class = str(pnp_class).lower()
    name = str(name).lower()
    if "gamepad" in name or "joystick" in name or "controller" in name or "xbox" in name: return "Controller"
    if "mouse" in name or pnp_class == "mouse": return "Mouse"
    if "keyboard" in name or pnp_class == "keyboard": return "Keyboard"
    if pnp_class == "monitor" or "display" in pnp_class: return "Monitor"
    if pnp_class in ["audioendpoint", "media"] or "headphone" in name or "headset" in name or "audio" in name:
        if "speaker" in name or "realtek" in name: return "Speakers"
        return "Headphones"
    return "Other"


def clean_manufacturer(mfg):
    if not mfg or "standard" in mfg.lower() or mfg.startswith("@") or "microsoft" in mfg.lower(): return "Generic"
    return mfg.strip()


def get_connected_hardware():
    pythoncom.CoInitialize()
    hardware_dict = {}
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        devices = wmi.ExecQuery(
            "SELECT Name, DeviceID, PNPClass, Manufacturer FROM Win32_PnPEntity WHERE ConfigManagerErrorCode = 0")
        allowed_classes = {"AudioEndpoint", "Media", "USBDevice", "USB", "Bluetooth", "Monitor", "Keyboard", "Mouse",
                           "HIDClass", "DiskDrive", "WPD"}

        for device in devices:
            if device.PNPClass in allowed_classes and device.Name:
                category = determine_category(device.PNPClass, device.Name)
                manufacturer = clean_manufacturer(device.Manufacturer)
                vid, pid = extract_vid_pid(device.DeviceID)
                hardware_dict[device.DeviceID] = {
                    "name": device.Name, "id": device.DeviceID, "category": category, "manufacturer": manufacturer,
                    "vid": vid, "pid": pid
                }
    except Exception:
        pass
    finally:
        pythoncom.CoUninitialize()
    return hardware_dict


def process_hardware_change():
    global known_devices
    try:
        current_devices = get_connected_hardware()
        with state_lock:
            current_ids = set(current_devices.keys())
            known_ids = set(known_devices.keys())

            just_connected = current_ids - known_ids
            just_disconnected = known_ids - current_ids

            payload_connected = []
            payload_disconnected = []

            if just_connected:
                grouped_connected = {}
                for dev_id in just_connected:
                    g_id = get_device_group_id(dev_id)
                    if g_id not in grouped_connected: grouped_connected[g_id] = []
                    grouped_connected[g_id].append(current_devices[dev_id])

                for group_id, devices in grouped_connected.items():
                    payload_connected.append({
                        "id": group_id, "name": devices[0]['name'], "category": devices[0]['category'],
                        "manufacturer": devices[0]['manufacturer'], "vid": devices[0]['vid'], "pid": devices[0]['pid']
                    })

            if just_disconnected:
                grouped_disconnected = set()
                for dev_id in just_disconnected:
                    grouped_disconnected.add(get_device_group_id(dev_id))

                for g_id in grouped_disconnected:
                    payload_disconnected.append({"id": g_id})

            if payload_connected or payload_disconnected:
                send_changed_devices(payload_connected, payload_disconnected)
            known_devices = current_devices
    finally:
        pythoncom.CoUninitialize()


def init():
    global known_devices
    known_devices = get_connected_hardware()
