"""FastAPI 入口：装配路由、启动 MQTT、后台心跳看门狗。"""
import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .background import heartbeat_watchdog, ack_watchdog
from .config import settings
from .database import db, init_db
from .mqtt_client import mqtt_client
from .mqtt_handlers import register_handlers
from .routers import devices, gateways, scan
from .routers import api, devices, gateways, scan

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

app = FastAPI(title="WOL Gateway")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(gateways.router)
app.include_router(devices.router)
app.include_router(scan.router)
app.include_router(api.router)


# ---------- 调试页（仅 DEBUG=1 时可用） ----------
@app.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request):
    if not settings.DEBUG:
        return Response("禁止访问", status_code=403)
    return templates.TemplateResponse("debug.html", {
        "request": request,
        "settings": settings
    })



@app.get("/api/debug/mqtt")
async def debug_mqtt():
    if not settings.DEBUG:
        return JSONResponse({"ok": False, "msg": "debug off"}, status_code=403)
    return {
        "connected": mqtt_client.client.is_connected(),
        "last_connect_ts": getattr(mqtt_client, "last_connect_ts", None),
        "last_disconnect_ts": getattr(mqtt_client, "last_disconnect_ts", None),
        "msg_log": list(mqtt_client.msg_log),
    }
    
@app.get("/api/debug/db")
async def api_debug_db():
    if not settings.DEBUG:
        return JSONResponse({"ok": False, "msg": "debug off"}, status_code=403)
    with db() as conn:
        gateways_rows = conn.execute("SELECT id, mac, ip, status, last_heartbeat, bind_message FROM gateway ORDER BY id DESC LIMIT 20").fetchall()
        cmd_rows = conn.execute("SELECT * FROM cmd_log ORDER BY id DESC LIMIT 50").fetchall()
    return {
        "gateways": [dict(r) for r in gateways_rows],
        "cmds": [dict(r) for r in cmd_rows]
    }


@app.on_event("startup")
async def _startup():
    init_db()
    register_handlers()
    mqtt_client.start()
    asyncio.create_task(heartbeat_watchdog())
    asyncio.create_task(ack_watchdog())
    logging.info("server started on :%s (debug=%s)", settings.PORT, settings.DEBUG)


@app.on_event("shutdown")
async def _shutdown():
    mqtt_client.stop()
