import re
import sys
import uvicorn
from fastapi import FastAPI, WebSocket, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import sqlite3
import json
import asyncio
import urllib.request

from hardware_change import usb_db, extract_vid_pid, parse_pnp_structure, get_clean_device_info
from heartbeat import heartbeat_lifespan


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding='utf-8', extra='ignore')

    debug: bool = True
    ntfy_url: str | None = None
    server_url: str = "localhost:3000"
    heartbeat_interval: int = 5


settings = ServerSettings()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PCUpdateForm(BaseModel):
    name: str
    owner: str


class DeviceTargetForm(BaseModel):
    bus: str
    device_id: str
    instance_id: str


class RegistrationForm(DeviceTargetForm):
    name: str
    owner: str
    category: str = "Other"


class OwnerForm(BaseModel):
    name: str


def send_ntfy(title, message, priority="default", tags="desktop"):
    if not settings.ntfy_url:
        return
    try:
        req = urllib.request.Request(
            settings.ntfy_url,
            data=message.encode('utf-8'),
            headers={"Title": title, "Priority": priority, "Tags": tags}
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        print(f"Ntfy error: {e}")


def get_db():
    conn = sqlite3.connect("monitoring.db")
    conn.row_factory = sqlite3.Row
    return conn


def get_device_group_id(device_id):
    match = re.search(r'(VID_[0-9A-F]{4}&PID_[0-9A-F]{4})', device_id, re.IGNORECASE)
    return match.group(1).upper() if match else device_id


def split_device_id(full_id: str):
    """Reverses the synthesized ID back into bus, device_id, and instance_id."""
    parts = full_id.split('\\', 2)
    bus = parts[0] if len(parts) > 0 else "UNKNOWN"
    device_id = parts[1] if len(parts) > 1 else "UNKNOWN"
    instance_id = parts[2] if len(parts) > 2 else ""
    return bus, device_id, instance_id


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


with get_db() as db:
    db.execute("CREATE TABLE IF NOT EXISTS owners (name TEXT PRIMARY KEY);")
    db.execute("""
    CREATE TABLE IF NOT EXISTS pcs (
        id TEXT PRIMARY KEY, 
        name TEXT, 
        owner TEXT REFERENCES owners (name) ON DELETE SET NULL, 
        last_seen INTEGER, 
        current_window TEXT, 
        preferred_window TEXT,
        current_exe TEXT,
        preferred_exe TEXT,
        is_monitoring BOOLEAN DEFAULT 0
    );""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS devices (
        bus TEXT,
        device_id TEXT, 
        instance_id TEXT, 
        has_serial BOOLEAN NOT NULL,
        last_connected_pc TEXT, 
        owner TEXT REFERENCES owners (name) ON DELETE SET NULL,
        is_registered BOOLEAN DEFAULT 0, 
        is_connected BOOLEAN DEFAULT 1, 
        is_ignored BOOLEAN DEFAULT 0,
        CONSTRAINT id PRIMARY KEY (last_connected_pc, bus, device_id, instance_id)
    );
    """)
    db.execute("CREATE INDEX IF NOT EXISTS physical_id ON devices (bus, device_id, instance_id);")
    db.execute("""
    CREATE TABLE IF NOT EXISTS device_names (
        bus TEXT, 
        device_id TEXT,
        name TEXT, 
        category TEXT, 
        manufacturer TEXT,
        CONSTRAINT id PRIMARY KEY (bus, device_id)
    );""")
    db.commit()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

manager = ConnectionManager()
app.router.lifespan_context = heartbeat_lifespan
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def get_dashboard():
    return FileResponse("static/index.html")


@app.get("/env.js")
def get_env_js():
    js_code = f"""
    window.APP_CONFIG = {{
        SERVER_URL: "{settings.server_url}",
        HEARTBEAT_INTERVAL: {settings.heartbeat_interval}
    }};
    """
    return Response(content=js_code, media_type="application/javascript")


@app.post("/api/pcs/toggle_all_monitoring")
async def toggle_all_monitoring():
    rows = db.execute("SELECT is_monitoring FROM pcs").fetchall()

    if not rows:
        return {"status": "no_pcs"}

    all_enabled = all(row["is_monitoring"] == 1 for row in rows)
    new_target_status = 0 if all_enabled else 1

    db.execute("UPDATE pcs SET is_monitoring = ?", (new_target_status,))
    db.commit()

    await manager.broadcast({"type": "pc_updated"})

    return {"status": "success", "is_monitoring": new_target_status}

@app.get("/api/dashboard/pcs")
async def get_dashboard_pcs():
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM pcs ORDER BY last_seen DESC").fetchall()]


@app.get("/api/dashboard/devices")
async def get_dashboard_devices():
    with get_db() as db:
        query = """
            SELECT 
                d.*, 
                dn.name, dn.category, dn.manufacturer,
                (d.bus || '\\' || d.device_id || CASE WHEN d.instance_id != '' THEN '\\' || d.instance_id ELSE '' END) as id
            FROM devices d
            LEFT JOIN device_names dn ON d.bus = dn.bus AND d.device_id = dn.device_id
        """
        devices = [dict(row) for row in db.execute(query).fetchall()]

        for dev in devices:
            vid, pid = extract_vid_pid(dev["device_id"])
            dev["vid"] = vid
            dev["pid"] = pid

        return devices


@app.post("/api/devices/register")
async def register_device(form: RegistrationForm):
    with get_db() as db:
        db.execute("""
            UPDATE device_names 
            SET name = ?, category = ? 
            WHERE bus = ? AND device_id = ?
        """, (form.name, form.category, form.bus, form.device_id))

        db.execute("""
            UPDATE devices 
            SET owner = ?, is_registered = 1 
            WHERE bus = ? AND device_id = ? AND instance_id = ?
        """, (form.owner, form.bus, form.device_id, form.instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.delete("/api/devices")
async def delete_device(target: DeviceTargetForm):
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE bus = ? AND device_id = ? AND instance_id = ?",
                   (target.bus, target.device_id, target.instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.delete("/api/devices/pending")
async def dismiss_pending_device(target: DeviceTargetForm):
    with get_db() as db:
        db.execute("""
            DELETE FROM devices 
            WHERE bus = ? AND device_id = ? AND instance_id = ? AND is_registered = 0
        """, (target.bus, target.device_id, target.instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.post("/api/devices/toggle_ignore")
async def toggle_device_ignore(target: DeviceTargetForm):
    with get_db() as db:
        db.execute("""
            UPDATE devices SET is_ignored = CASE WHEN is_ignored = 1 THEN 0 ELSE 1 END 
            WHERE bus = ? AND device_id = ? AND instance_id = ?
        """, (target.bus, target.device_id, target.instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.delete("/api/devices/{device_id:path}")
async def delete_device(device_id: str):
    bus, dev_id, instance_id = split_device_id(device_id)
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE bus = ? AND device_id = ? AND instance_id = ?",
                   (bus, dev_id, instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.delete("/api/devices/{device_id:path}/pending")
async def dismiss_pending_device(device_id: str):
    bus, dev_id, instance_id = split_device_id(device_id)
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE bus = ? AND device_id = ? AND instance_id = ? AND is_registered = 0",
                   (bus, dev_id, instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.post("/api/devices/{device_id:path}/toggle_ignore")
async def toggle_device_ignore(device_id: str):
    bus, dev_id, instance_id = split_device_id(device_id)
    with get_db() as db:
        db.execute("""
            UPDATE devices SET is_ignored = CASE WHEN is_ignored = 1 THEN 0 ELSE 1 END 
            WHERE bus = ? AND device_id = ? AND instance_id = ?
        """, (bus, dev_id, instance_id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}

@app.post("/api/pcs/{pc_id}/toggle_monitor")
async def toggle_pc_monitor(pc_id: str):
    with get_db() as db:
        db.execute("UPDATE pcs SET is_monitoring = CASE WHEN is_monitoring = 1 THEN 0 ELSE 1 END WHERE id = ?",
                   (pc_id,))
        db.commit()
    await manager.broadcast({"type": "pc_updated"})
    return {"status": "success"}

@app.post("/api/pcs/{pc_id}/preferred")
async def set_preferred_window(pc_id: str):
    with get_db() as db:
        pc = db.execute("SELECT current_exe FROM pcs WHERE id = ?", (pc_id,)).fetchone()
        if pc and pc["current_exe"]:
            db.execute("UPDATE pcs SET preferred_exe = ? WHERE id = ?", (pc["current_exe"], pc_id))
            db.commit()
    await manager.broadcast({"type": "pc_updated"})
    return {"status": "success"}

@app.get("/api/owners")
async def get_owners():
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM owners ORDER BY name ASC").fetchall()]


@app.get("/api/lookup/usb")
async def lookup_usb(vid: str, pid: str):
    v, p = vid.upper(), pid.upper()
    vendor = usb_db["vendors"].get(v)
    if vendor:
        return {"vendor": vendor["name"], "device": vendor["devices"].get(p, "Unknown Specific Model")}
    return {"vendor": "Unknown Vendor", "device": "Unknown Specific Model"}


@app.post("/api/owners")
async def add_owner(form: OwnerForm):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO owners (name) VALUES (?)", (form.name.strip(),))
        db.commit()
    return {"status": "success"}


@app.delete("/api/owners/{name}")
async def delete_owner(name: str):
    with get_db() as db:
        db.execute("DELETE FROM owners WHERE name = ?", (name,))
        db.commit()
    return {"status": "success"}


@app.put("/api/pcs/{pc_id}")
async def update_pc(pc_id: str, form: PCUpdateForm):
    with get_db() as db:
        db.execute("UPDATE pcs SET name = ?, owner = ? WHERE id = ?", (form.name, form.owner, pc_id))
        db.commit()
    await manager.broadcast({"type": "pc_updated"})
    return {"status": "success"}


@app.delete("/api/pcs/{pc_id}")
async def delete_pc(pc_id: str):
    with get_db() as db:
        db.execute("DELETE FROM pcs WHERE id = ?", (pc_id,))
        db.commit()
    await manager.broadcast({"type": "pc_updated"})
    return {"status": "success"}


window_debounce_tasks = {}


async def debounce_window_check(pc_id, target_exe):
    await asyncio.sleep(2)
    with get_db() as db:
        pc = db.execute("SELECT current_exe, preferred_exe, is_monitoring, name FROM pcs WHERE id = ?",
                        (pc_id,)).fetchone()
        if pc and pc["is_monitoring"]:
            if pc["current_exe"] == target_exe and pc["preferred_exe"] and pc["current_exe"] != pc["preferred_exe"]:
                send_ntfy(
                    title=f"App Alert: {pc['name'] or pc_id}",
                    message=f"Unauthorized app focused: {pc['current_exe']}",
                    priority="high", tags="warning"
                )


@app.post("/api/heartbeat")
async def receive_heartbeat(payload: dict):
    pc_id = payload.get("device_id")
    pc_name = payload.get("device_name")

    with get_db() as db:
        db.execute(
            "INSERT INTO pcs (id, name, last_seen) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (pc_id, pc_name, payload.get("timestamp")))
        db.commit()
    await manager.broadcast({"type": "pc_updated", "pc_id": pc_id})
    return {"status": "ok"}


@app.post("/api/events")
async def receive_event(payload: dict):
    event_type = payload.get("event")
    pc_id = payload.get("device_id")

    with get_db() as db:
        if event_type == "window_changed":
            new_title = payload.get("focused_window")
            new_exe = payload.get("current_exe")
            db.execute("UPDATE pcs SET current_window = ?, current_exe = ? WHERE id = ?", (new_title, new_exe, pc_id))
            db.commit()

            if pc_id in window_debounce_tasks:
                window_debounce_tasks[pc_id].cancel()
            window_debounce_tasks[pc_id] = asyncio.create_task(debounce_window_check(pc_id, new_exe))

            await manager.broadcast({"type": "window_changed", "pc_id": pc_id})

        elif event_type == "hardware_changed":
            connected = payload.get("connected_peripherals", [])
            disconnected = payload.get("disconnected_peripherals", [])

            for dev in connected:

                dev_id = dev.get("id")

                if not dev_id:
                    continue

                info = get_clean_device_info(
                    dev_id,
                    dev.get("name", "Unknown Device"),
                    dev.get("manufacturer", ""),
                    dev.get("class", "")
                )

                db.execute("""
                            INSERT INTO device_names (bus, device_id, name, category, manufacturer) 
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(bus, device_id) DO NOTHING
                        """, (info["Bus"], info["DeviceID"], info["Name"], info["Category"], info["Vendor"]))

                db.execute("""
                            INSERT INTO devices (bus, device_id, instance_id, has_serial, last_connected_pc, is_connected, is_ignored) 
                            VALUES (?, ?, ?, ?, ?, 1, 0)
                            ON CONFLICT(last_connected_pc, bus, device_id, instance_id) DO UPDATE SET 
                                has_serial = excluded.has_serial,
                                is_connected = 1
                        """, (info["Bus"], info["DeviceID"], info["InstanceID"], info["HasTrueSerial"], pc_id))

            for dev in disconnected:
                dev_id = dev.get("id")

                if not dev_id:
                    continue

                bus, device_id, instance_id, _ = parse_pnp_structure(dev_id)

                db.execute("""
                            UPDATE devices 
                            SET is_connected = 0 
                            WHERE bus = ? AND device_id = ? AND instance_id = ? AND last_connected_pc = ?
                        """, (bus, device_id, instance_id, pc_id))
                pc = db.execute("SELECT is_monitoring, name FROM pcs WHERE id = ?", (pc_id,)).fetchone()
                device_info = db.execute("""
                            SELECT dn.name, d.is_registered, d.is_ignored 
                            FROM devices d
                            LEFT JOIN device_names dn ON d.bus = dn.bus AND d.device_id = dn.device_id
                            WHERE d.bus = ? AND d.device_id = ? AND d.instance_id = ? AND d.last_connected_pc = ?
                        """, (bus, device_id, instance_id, pc_id)).fetchone()

                if pc and pc["is_monitoring"] and device_info and device_info["is_registered"] and not device_info[
                    "is_ignored"]:
                    send_ntfy(
                        title=f"Hardware Alert: {pc['name'] or pc_id}",
                        message=f"Monitored device disconnected: {device_info['name'] or 'Unknown Device'}",
                        priority="high", tags="rotating_light"
                    )

            db.commit()
            await manager.broadcast({"type": "hardware_changed", "pc_id": pc_id})

    return {"status": "ok"}


@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except Exception:
        manager.disconnect(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=3000)
