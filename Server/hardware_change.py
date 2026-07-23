import os
import urllib.request

import re

EISA_MAP = {
    "DEL": "Dell",
    "SAM": "Samsung",
    "GSM": "LG",
    "LGD": "LG",
    "LPL": "LG",
    "AOC": "AOC",
    "ACR": "Acer",
    "API": "Acer",
    "ACI": "Asus",
    "ASU": "Asus",
    "BNQ": "BenQ",
    "HPQ": "HP",
    "HWP": "HP",
    "HPN": "HP",
    "LEN": "Lenovo",
    "PHL": "Philips",
    "SNY": "Sony",
    "VSC": "ViewSonic",
    "NEC": "NEC",
    "MSI": "MSI"
}

GENERIC_NAMES = {
    "usb input device", "usb composite device", "generic usb hub",
    "usb mass storage device", "generic pnp monitor", "generic non-pnp monitor",
    "high definition audio device", "usb audio device", "default monitor"
}


def parse_pnp_structure(pnp_id):
    """
    Extracts structural elements (Bus, DeviceID, InstanceID)
    out of the raw PNPDeviceID according to system-layer rules.
    """
    if not pnp_id:
        return "UNKNOWN", "UNKNOWN", ""

    pnp_id_upper = pnp_id.upper()

    if pnp_id_upper.startswith("SWD\\MMDEVAPI"):
        bus = "SOUND"
        device_id = pnp_id.split('\\')[-1]
        instance_id = ""
        has_true_serial = False
        return bus, device_id, instance_id, has_true_serial

    parts = pnp_id.split('\\')
    bus = parts[0] if len(parts) > 0 else "UNKNOWN"
    device_id = parts[1] if len(parts) > 1 else "UNKNOWN"
    instance_id = "\\".join(parts[2:]) if len(parts) > 2 else ""
    has_true_serial = '&' not in instance_id and '.' not in instance_id

    return bus, device_id, instance_id, has_true_serial


def prettify_monitor_vendor(device_id_part):
    """Parses a monitor's device segment to turn EISA blocks into real vendor titles."""
    prefix = device_id_part[:3].upper()
    return EISA_MAP.get(prefix, prefix)


GENERIC_VENDORS = {
    "standard", "microsoft", "generic", "(standard", "intel", "advanced micro devices"
}


def extract_vid_pid(device_id):
    """Extracts VID/PID for USB, or VEN/DEV for PCI/Bluetooth."""
    if not device_id:
        return None, None

    vid_match = re.search(r'(?:VID|VEN)_([0-9A-F]{4})', device_id, re.IGNORECASE)
    pid_match = re.search(r'(?:PID|DEV)_([0-9A-F]{4})', device_id, re.IGNORECASE)

    vid = vid_match.group(1).upper() if vid_match else None
    pid = pid_match.group(1).upper() if pid_match else None
    return vid, pid


def clean_manufacturer(mfg):
    """Filters out generic OS-level driver manufacturers."""
    if not mfg:
        return "Unknown"

    mfg_lower = mfg.lower()
    for gen_v in GENERIC_VENDORS:
        if mfg_lower.startswith(gen_v) or gen_v in mfg_lower:
            return "Unknown"

    return mfg.strip()


def determine_category(pnp_class, name):
    """Determines category using PNPClass first, then keyword fallback."""
    pnp_class = str(pnp_class).lower() if pnp_class else ""
    name = str(name).lower() if name else ""

    if pnp_class == "mouse": return "Mouse"
    if pnp_class == "keyboard": return "Keyboard"
    if pnp_class == "monitor": return "Monitor"
    if pnp_class == "bluetooth": return "Bluetooth"
    if pnp_class in ["image", "camera"]: return "Camera"

    if pnp_class in ["audioendpoint", "media"]:
        if any(x in name for x in ["speaker", "realtek"]): return "Speakers"
        if any(x in name for x in ["mic", "microphone"]): return "Microphone"
        return "Headphones"

    if any(x in name for x in ["gamepad", "joystick", "controller", "xbox", "dualshock", "dualsense"]):
        return "Controller"

    if pnp_class in ["diskdrive", "usb"] and "storage" in name:
        return "Storage"

    return "Other"


usb_db = {"vendors": {}}


def init_usb_db():
    if not os.path.exists("usb.ids"):
        try:
            print("Downloading USB IDs database...")
            urllib.request.urlretrieve("http://www.linux-usb.org/usb.ids", "usb.ids")
        except Exception as e:
            print(f"Notice: Could not download USB IDs ({e})")
            return
    try:
        current_vid = None
        with open("usb.ids", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip(): continue
                if not line.startswith("\t"):
                    vid = line[:4].upper()
                    usb_db["vendors"][vid] = {"name": line[5:].strip(), "devices": {}}
                    current_vid = vid
                elif line.startswith("\t") and not line.startswith("\t\t") and current_vid:
                    pid = line[1:5].upper()
                    usb_db["vendors"][current_vid]["devices"][pid] = line[6:].strip()
    except Exception:
        pass


init_usb_db()


def get_clean_device_info(id, name, manufacturer, cls):
    bus, device_id, instance_id, has_true_serial = parse_pnp_structure(id)
    vid, pid = extract_vid_pid(id)

    vendor = clean_manufacturer(manufacturer)
    name = name
    category = determine_category(cls, name)

    if bus == "DISPLAY":
        vendor = prettify_monitor_vendor(device_id)
        if vendor in ["Generic", "Unknown"]:
            vendor = "Generic Monitor"

    db_vendor = None
    db_name = None
    if vid and vid in usb_db.get("vendors", {}):
        db_vendor = usb_db["vendors"][vid].get("name")
        if pid and pid in usb_db["vendors"][vid].get("devices", {}):
            db_name = usb_db["vendors"][vid]["devices"][pid]

    if db_vendor:
        vendor = db_vendor

    if name.lower() in GENERIC_NAMES:
        if db_name:
            name = db_name
        elif vendor not in ["Unknown", "Generic"]:
            name = f"{vendor} {name}"

    if vendor not in ["Unknown", "Generic"] and name.lower().startswith(vendor.lower()):
        name = name[len(vendor):].strip()

    return {
        'Name': name,
        'Vendor': vendor,
        'Category': category,
        'Bus': bus,
        'DeviceID': device_id,
        'InstanceID': instance_id,
        'HasTrueSerial': has_true_serial
    }


def change(pc_id, connected, disconnected):
    from server import get_db
    with get_db() as db:
        for dev in connected:
            id = getattr(dev, "id", '')
            if id is None: continue

            name = getattr(dev, "name", 'Unknown Device')
            manufacturer = getattr(dev, "manufacturer", '')
            cls = getattr(dev, "class", '')
            info = get_clean_device_info(id, name, manufacturer, cls)

            db.execute("""
                INSERT INTO device_names (bus, device_id, name, category, manufacturer) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET 
                    name=excluded.name, category=excluded.category, manufacturer=excluded.manufacturer;
            """, (info["Bus"], info["DeviceID"], info["Name"], info["Category"], info["Vendor"]))

            db.execute("""
                INSERT INTO devices (bus, device_id, instance_id, has_serial, last_connected_pc, is_connected, is_ignored) 
                VALUES (?, ?, ?, ?, ?, 1, 0)
                ON CONFLICT(physical_id) DO UPDATE SET 
                    has_serial=excluded.has_serial, last_connected_pc=excluded.last_connected_pc, is_connected=1;
            """, (info["Bus"], info["DeviceID"], info["InstanceID"], info["HasTrueSerial"], pc_id))
            db.commit()

        for dev in disconnected:
            id = getattr(dev, "id", '')
            if id is None: continue
