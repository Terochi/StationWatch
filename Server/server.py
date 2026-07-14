from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
import sqlite3
import json
import asyncio
import urllib.request
import time
import os


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env.debug', env_file_encoding='utf-8', extra='ignore')

    debug: bool = True
    ntfy_url: str | None = None


settings = ServerSettings()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


class RegistrationForm(BaseModel):
    id: str
    name: str
    owner: str
    category: str = "Other"


class PCUpdateForm(BaseModel):
    name: str
    owner: str


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


with get_db() as db:
    db.execute("""
    CREATE TABLE IF NOT EXISTS pcs (
        id TEXT PRIMARY KEY, name TEXT, owner TEXT, last_seen INTEGER, 
        current_window TEXT, preferred_window TEXT,
        current_exe TEXT, preferred_exe TEXT,
        is_monitoring BOOLEAN DEFAULT 0
    );""")
    db.execute("""
    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY, last_connected_pc TEXT, name TEXT, owner TEXT, 
        category TEXT, manufacturer TEXT, vid TEXT, pid TEXT,
        is_registered BOOLEAN DEFAULT 0, is_monitored BOOLEAN DEFAULT 0,
        is_connected BOOLEAN DEFAULT 1, is_ignored BOOLEAN DEFAULT 0
    );""")
    db.execute("CREATE TABLE IF NOT EXISTS owners (name TEXT PRIMARY KEY);")
    db.commit()

    cursor = db.cursor()

    cursor.execute("PRAGMA table_info(pcs);")
    existing_pc_cols = [row[1] for row in cursor.fetchall()]
    if "current_exe" not in existing_pc_cols:
        db.execute("ALTER TABLE pcs ADD COLUMN current_exe TEXT;")
    if "preferred_exe" not in existing_pc_cols:
        db.execute("ALTER TABLE pcs ADD COLUMN preferred_exe TEXT;")
    if "is_monitoring" not in existing_pc_cols:
        db.execute("ALTER TABLE pcs ADD COLUMN is_monitoring BOOLEAN DEFAULT 0;")

    cursor.execute("PRAGMA table_info(devices);")
    existing_dev_cols = [row[1] for row in cursor.fetchall()]
    if "is_connected" not in existing_dev_cols:
        db.execute("ALTER TABLE devices ADD COLUMN is_connected BOOLEAN DEFAULT 1;")
    if "vid" not in existing_dev_cols:
        db.execute("ALTER TABLE devices ADD COLUMN vid TEXT;")
    if "pid" not in existing_dev_cols:
        db.execute("ALTER TABLE devices ADD COLUMN pid TEXT;")
    if "is_ignored" not in existing_dev_cols:
        db.execute("ALTER TABLE devices ADD COLUMN is_ignored BOOLEAN DEFAULT 0;")
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


manager = ConnectionManager()


async def monitor_heartbeats():
    offline_pcs = set()
    while True:
        await asyncio.sleep(5)
        current_time = int(time.time())
        threshold = current_time - 15

        try:
            with get_db() as db:
                pcs = db.execute("SELECT id, last_seen, name, is_monitoring FROM pcs").fetchall()
                for pc in pcs:
                    pc_id = pc["id"]
                    last_seen = pc["last_seen"]

                    if last_seen and last_seen < threshold:
                        if pc_id not in offline_pcs:
                            offline_pcs.add(pc_id)
                            await manager.broadcast({"type": "pc_updated", "pc_id": pc_id})
                            if pc["is_monitoring"]:
                                send_ntfy(
                                    title=f"Internet Alert: {pc['name'] or pc_id}",
                                    message="PC offline",
                                    priority="high", tags="warning"
                                )
                    else:
                        if pc_id in offline_pcs:
                            offline_pcs.remove(pc_id)
        except Exception as e:
            print(f"Heartbeat monitor error: {e}")


@asynccontextmanager
async def heartbeat_lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_heartbeats())
    yield
    task.cancel()


app.router.lifespan_context = heartbeat_lifespan


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    try:
        with open("index.html", "r", encoding="utf-8") as file:
            return HTMLResponse(content=file.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>index.html not found!</h1><p>Ensure index.html is in the same directory as server.py.</p>",
            status_code=404)


@app.get("/api/dashboard/pcs")
async def get_dashboard_pcs():
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM pcs ORDER BY last_seen DESC").fetchall()]


@app.get("/api/dashboard/devices")
async def get_dashboard_devices():
    with get_db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM devices").fetchall()]


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


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
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


@app.post("/api/devices/register")
async def register_device(form: RegistrationForm):
    with get_db() as db:
        db.execute(
            "UPDATE devices SET name = ?, owner = ?, category = ?, is_registered = 1, is_monitored = 1 WHERE id = ?",
            (form.name, form.owner, form.category, form.id))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
    return {"status": "success"}


@app.delete("/api/devices/{device_id}/pending")
async def dismiss_pending_device(device_id: str):
    with get_db() as db:
        db.execute("DELETE FROM devices WHERE id = ? AND is_registered = 0", (device_id,))
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


@app.post("/api/devices/{device_id}/toggle_ignore")
async def toggle_device_ignore(device_id: str):
    with get_db() as db:
        db.execute("UPDATE devices SET is_ignored = CASE WHEN is_ignored = 1 THEN 0 ELSE 1 END WHERE id = ?",
                   (device_id,))
        db.commit()
    await manager.broadcast({"type": "hardware_changed"})
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
    with get_db() as db:
        db.execute(
            "INSERT INTO pcs (id, last_seen) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (pc_id, payload.get("timestamp")))
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
                db.execute("""
                    INSERT INTO devices (id, last_connected_pc, name, category, manufacturer, vid, pid, is_connected, is_ignored) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0)
                    ON CONFLICT(id) DO UPDATE SET 
                        last_connected_pc=excluded.last_connected_pc, is_connected=1, 
                        category=excluded.category, manufacturer=excluded.manufacturer, 
                        vid=excluded.vid, pid=excluded.pid
                """, (dev["id"], pc_id, dev["name"], dev.get("category", "Other"), dev.get("manufacturer", "Generic"),
                      dev.get("vid"), dev.get("pid")))

            for dev in disconnected:
                db.execute("UPDATE devices SET is_connected = 0 WHERE id = ? AND last_connected_pc = ?",
                           (dev["id"], pc_id))

                pc = db.execute("SELECT is_monitoring, name FROM pcs WHERE id = ?", (pc_id,)).fetchone()
                device_info = db.execute("SELECT name, is_registered, is_ignored FROM devices WHERE id = ?",
                                         (dev["id"],)).fetchone()

                if pc and pc["is_monitoring"] and device_info and device_info["is_registered"] and not device_info[
                    "is_ignored"]:
                    send_ntfy(
                        title=f"Hardware Alert: {pc['name'] or pc_id}",
                        message=f"Monitored device disconnected: {device_info['name']}",
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
