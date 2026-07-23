import threading
import pythoncom
import win32com.client

from connection import send_changed_devices

known_devices = {}
state_lock = threading.Lock()


def get_connected_hardware():
    pythoncom.CoInitialize()
    hardware_dict = {}
    wmi = None
    query_results = None

    try:
        wmi = win32com.client.GetObject("winmgmts:\\\\.\\root\\cimv2")
        query_results = wmi.ExecQuery(
            "SELECT PNPDeviceID, DeviceID, Name, PNPClass, Manufacturer FROM Win32_PnPEntity WHERE PNPDeviceID IS NOT NULL AND ConfigManagerErrorCode = 0"
        )

        for device in query_results:
            pnp_id = device.PNPDeviceID
            dev_id = getattr(device, "DeviceID", None) or pnp_id

            if not pnp_id or "MI_" in pnp_id:
                continue

            data = {
                "name": getattr(device, "Name", None) or "Unknown Device",
                "id": dev_id,
                "class": getattr(device, "PNPClass", None) or "",
                "manufacturer": getattr(device, "Manufacturer", None) or "Generic",
            }

            if "VID_" in pnp_id and "PID_" in pnp_id:
                hardware_dict[dev_id] = data
                continue
            if pnp_id.startswith("DISPLAY\\"):
                hardware_dict[dev_id] = data
                continue
            if pnp_id.startswith("SWD\\MMDEVAPI"):
                hardware_dict[dev_id] = data
                continue

    except Exception as e:
        print(f"[Device Scan Error]: {e}")
    finally:
        del query_results
        del wmi
        pythoncom.CoUninitialize()

    return hardware_dict


def process_hardware_change():
    global known_devices
    current_devices = get_connected_hardware()

    with state_lock:
        current_ids = set(current_devices.keys())
        known_ids = set(known_devices.keys())

        just_connected = current_ids - known_ids
        just_disconnected = known_ids - current_ids

        payload_connected = [current_devices[dev_id] for dev_id in just_connected]
        payload_disconnected = [{"id": dev_id} for dev_id in just_disconnected]

        if payload_connected or payload_disconnected:
            send_changed_devices(payload_connected, payload_disconnected)

        known_devices = current_devices


def init():
    global known_devices
    known_devices = get_connected_hardware()