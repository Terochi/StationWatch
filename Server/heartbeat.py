import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI


async def monitor_heartbeats():
    offline_pcs = set()
    while True:
        await asyncio.sleep(5)
        current_time = int(time.time())
        threshold = current_time - 15

        try:
            from server import manager, send_ntfy, get_db
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
async def heartbeat_lifespan(_: FastAPI):
    task = asyncio.create_task(monitor_heartbeats())
    yield
    task.cancel()
