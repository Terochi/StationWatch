import time
import socket
import threading
import requests
import ctypes
import ctypes.wintypes
import win32gui
import win32api
import win32con
import win32process
import win32com.client
import pythoncom
import re
import os
from dotenv import load_dotenv
load_dotenv()
# TODO: https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/

DEBUG = os.getenv("DEBUG", False)
SERVER_HOSTNAME = "localhost" if DEBUG else os.getenv("SERVER_HOSTNAME", "localhost")

SERVER_URL_RAW = f"http://{SERVER_HOSTNAME}:3000/"
SERVER_URL = SERVER_URL_RAW + "api/events"
HEARTBEAT_URL = SERVER_URL_RAW + "api/heartbeat"
HEARTBEAT_INTERVAL = 5
DEVICE_ID = socket.gethostname()

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WM_DEVICECHANGE = 0x0219

known_devices = {}
state_lock = threading.Lock()

user32 = ctypes.windll.user32
WinEventProcType = ctypes.WINFUNCTYPE(
    None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
    ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD
)

def _send_win_change(title, exe):
    send_payload(SERVER_URL, {
        "event": "window_changed", 
        "focused_window": title,
        "current_exe": exe
    })

def callback_window_changed(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        window_title = buff.value or "Unknown Window"

        exe_path = "Unknown Location"
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
            exe_path = win32process.GetModuleFileNameEx(handle, 0)
        except Exception: pass

        threading.Thread(target=_send_win_change, args=(window_title, exe_path), daemon=True).start()
    except Exception: pass

global_hook_proc = WinEventProcType(callback_window_changed)

def send_payload(url, data):
    try:
        data["device_id"] = DEVICE_ID
        data["timestamp"] = int(time.time())
        requests.post(url, json=data, timeout=3)
    except requests.exceptions.RequestException: pass

def heartbeat_loop():
    while True:
        send_payload(HEARTBEAT_URL, {"status": "alive"})
        time.sleep(HEARTBEAT_INTERVAL)

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
    hardware_dict = {}
    try:
        wmi = win32com.client.GetObject("winmgmts:")
        devices = wmi.ExecQuery("SELECT Name, DeviceID, PNPClass, Manufacturer FROM Win32_PnPEntity WHERE ConfigManagerErrorCode = 0")
        allowed_classes = {"AudioEndpoint", "Media", "USBDevice", "USB", "Bluetooth", "Monitor", "Keyboard", "Mouse", "HIDClass", "DiskDrive", "WPD"}

        for device in devices:
            if device.PNPClass in allowed_classes and device.Name:
                category = determine_category(device.PNPClass, device.Name)
                manufacturer = clean_manufacturer(device.Manufacturer)
                vid, pid = extract_vid_pid(device.DeviceID)
                hardware_dict[device.DeviceID] = {
                    "name": device.Name, "id": device.DeviceID, "category": category, "manufacturer": manufacturer, "vid": vid, "pid": pid
                }
    except Exception: pass
    return hardware_dict

def process_hardware_change():
    global known_devices
    pythoncom.CoInitialize()
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
                send_payload(SERVER_URL, {
                    "event": "hardware_changed", "connected_peripherals": payload_connected, "disconnected_peripherals": payload_disconnected
                })
            known_devices = current_devices
    finally: pythoncom.CoUninitialize()

def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DEVICECHANGE:
        threading.Thread(target=process_hardware_change, daemon=True).start()
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

def start_message_pump():
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.lpszClassName = "HardwareListener"
    wc.hInstance = win32api.GetModuleHandle(None)
    try: win32gui.RegisterClass(wc)
    except Exception: pass
    hwnd = win32gui.CreateWindow("HardwareListener", "HardwareListenerWindow", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
    try:
        while True:
            win32gui.PumpWaitingMessages()
            time.sleep(0.5)
    except KeyboardInterrupt: print("\nShutting down gracefully...")

def main():
    global known_devices
    print(f"Starting Agent for {DEVICE_ID}...")
    print("Initializing hardware baseline silently...")
    pythoncom.CoInitialize()
    known_devices = get_connected_hardware() 
    pythoncom.CoUninitialize()
    
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    
    user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, global_hook_proc, 0, 0, WINEVENT_OUTOFCONTEXT)
    print("Ready. Only physical connect/disconnect events will stream to your dashboard.\n")
    start_message_pump()

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        input("Press Enter to exit...")