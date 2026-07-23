import ctypes
import ctypes.wintypes
import socket
import win32api
import win32con
import win32gui
import win32process

import helper, connection, devices

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WM_DEVICECHANGE = 0x0219
DEVICE_NOTIFY_ALL_INTERFACE_CLASSES = 0x00000004


class DEV_BROADCAST_DEVICEINTERFACE(ctypes.Structure):
    _fields_ = [
        ("dbcc_size", ctypes.wintypes.DWORD),
        ("dbcc_devicetype", ctypes.wintypes.DWORD),
        ("dbcc_reserved", ctypes.wintypes.DWORD),
        ("dbcc_classguid", ctypes.c_byte * 16),
        ("dbcc_name", ctypes.c_wchar * 1)
    ]


def get_hostname() -> str:
    return socket.gethostname()


def get_machine_guid() -> str:
    try:
        key = win32api.RegOpenKeyEx(
            win32con.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            win32con.KEY_READ | win32con.KEY_WOW64_64KEY
        )
        value, _ = win32api.RegQueryValueEx(key, "MachineGuid")
        win32api.RegCloseKey(key)
        return value
    except Exception as e:
        helper.exit_with_error(f"Error retrieving MachineGuid: {e}")


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
        except Exception:
            pass

        helper.start_thread(connection._send_win_change, (window_title, exe_path))
    except Exception:
        pass


def register_device_notifications(hwnd):
    filter_header = DEV_BROADCAST_DEVICEINTERFACE()
    filter_header.dbcc_size = ctypes.sizeof(DEV_BROADCAST_DEVICEINTERFACE)
    filter_header.dbcc_devicetype = 0x00000005

    user32.RegisterDeviceNotificationW(
        hwnd,
        ctypes.byref(filter_header),
        DEVICE_NOTIFY_ALL_INTERFACE_CLASSES
    )


def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DEVICECHANGE:
        helper.start_thread(devices.process_hardware_change)
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


user32 = ctypes.windll.user32
WinEventProcType = ctypes.WINFUNCTYPE(
    None, ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.HWND,
    ctypes.wintypes.LONG, ctypes.wintypes.LONG, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD
)
global_hook_proc = WinEventProcType(callback_window_changed)


def start_message_pump():
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.lpszClassName = "HardwareListener"
    wc.hInstance = win32api.GetModuleHandle(None)
    try:
        win32gui.RegisterClass(wc)
    except Exception:
        pass

    hwnd = win32gui.CreateWindow("HardwareListener", "HardwareListenerWindow", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)

    register_device_notifications(hwnd)

    try:
        win32gui.PumpMessages()
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")


def init():
    user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, global_hook_proc, 0, 0,
                           WINEVENT_OUTOFCONTEXT)