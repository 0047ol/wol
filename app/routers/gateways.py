"""网关管理 API + 页面路由。"""
import logging
from typing import Optional

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..database import db, now
from ..mqtt_client import mqtt_client
from ..utils import gen_bind_key, norm_mac, fmt_mac, paginate

log = logging.getLogger("gateway.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ---------- 页面 ----------
@router.get("/", response_class=HTMLResponse)
async def page_gateways(request: Request, page: int = 1, page_size: int = 20, q: str = ""):
    with db() as conn:
        where, args = "WHERE 1=1", []
        if q:
            where += " AND (mac LIKE ? OR ip LIKE ? OR name LIKE ?)"
            like = f"%{q}%"
            args = [like, like, like]
        total = conn.execute(f"SELECT COUNT(*) c FROM gateway {where}", args).fetchone()["c"]
        page, page_size, offset, total_pages = paginate(page, page_size, total)
        rows = conn.execute(
            f"SELECT * FROM gateway {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, page_size, offset],
        ).fetchall()
    return templates.TemplateResponse(
        "gateways.html",
        {
            "request": request,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total": total,
            "q": q,
        },
    )


# ---------- 新增网关 ----------
@router.post("/gateway/create")
async def create_gateway(name: str = Form(""), mac: str = Form("")):
    mac = norm_mac(mac)
    if not mac:
        # MAC 由 ESP32 上报时自动匹配；允许先不填 MAC 纯预生成密钥
        # 但为了让 ESP32 首次上报能匹配，这里要求必须填 MAC（从 ESP32 外壳/路由器查到）
        return JSONResponse({"ok": False, "msg": "请填写 ESP32 MAC 地址"}, status_code=400)
    bind_key = gen_bind_key()
    with db() as conn:
        try:
            conn.execute(
                """INSERT INTO gateway (mac, bind_key, name, status, created_at)
                   VALUES (?,?,?, 'unbound', ?)""",
                (mac, bind_key, name, now()),
            )
        except Exception as e:
            return JSONResponse({"ok": False, "msg": f"MAC 已存在或写入失败: {e}"}, status_code=400)
    return JSONResponse({"ok": True, "bind_key": bind_key, "mac": fmt_mac(mac)})


# ---------- 编辑网关配置 ----------
@router.post("/gateway/{gid}/update")
async def update_gateway(
    gid: int,
    name: str = Form(""),
    ping_interval: int = Form(30),
    heartbeat_timeout: int = Form(30),
    heartbeat_send_interval: int = Form(15),
    wol_broadcast_addr: str = Form("255.255.255.255"),
    scan_cidr: str = Form(""),
    scan_concurrency: int = Form(8),
    wifi_ssid: str = Form(""),
    wifi_password: str = Form(""),
):
    with db() as conn:
        row = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)

        # 业务配置更新
        conn.execute(
            """UPDATE gateway SET name=?, ping_interval=?, heartbeat_timeout=?,
                   heartbeat_send_interval=?, wol_broadcast_addr=?, scan_cidr=?,
                   scan_concurrency=? WHERE id=?""",
            (name, ping_interval, heartbeat_timeout, heartbeat_send_interval,
             wol_broadcast_addr, scan_cidr, scan_concurrency, gid),
        )

        # WiFi 变更：下发 update_wifi（如果填了）
        pushed = []
        if wifi_ssid and wifi_ssid != row["wifi_ssid"] or (wifi_password and wifi_password != row["wifi_password"]):
            conn.execute("UPDATE gateway SET wifi_ssid=?, wifi_password=? WHERE id=?",
                         (wifi_ssid, wifi_password, gid))
            if row["status"] == "online":
                mqtt_client.publish_cmd(row["mac"], "update_wifi",
                                        {"ssid": wifi_ssid, "password": wifi_password})
                pushed.append("wifi")

        # 业务配置下发
        if row["status"] == "online":
            mqtt_client.publish_cmd(row["mac"], "update_config", {
                "ping_interval": ping_interval,
                "heartbeat_send_interval": heartbeat_send_interval,
                "wol_broadcast_addr": wol_broadcast_addr,
                "scan_cidr": scan_cidr,
                "scan_concurrency": scan_concurrency,
            })
            pushed.append("config")

        conn.execute(
            "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at) VALUES (?,?,?,?)",
            (gid, "update_config", f"pushed={','.join(pushed)}", now()),
        )
    return JSONResponse({"ok": True, "pushed": pushed})


# ---------- 解绑/重置 ----------
@router.post("/gateway/{gid}/unbind")
async def unbind_gateway(gid: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)

        # 在线则先发 reset；离线则直接删
        if row["status"] == "online":
            mqtt_client.publish_cmd(row["mac"], row["bind_key"], "reset", {})
            conn.execute(
                "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at) VALUES (?,?,?,?)",
                (gid, "reset", "unbind", now()),
            )
            # 不立即删除；等 ack。这里直接删也可以——按需求"解绑就立即删除"
            # 按你最终决定：立即删除数据库记录（级联删子设备）
            conn.execute("DELETE FROM gateway WHERE id=?", (gid,))
            return JSONResponse({"ok": True, "msg": "已下发重置指令并删除网关记录"})
        else:
            conn.execute("DELETE FROM gateway WHERE id=?", (gid,))
            return JSONResponse({"ok": True, "msg": "网关离线，已直接删除记录"})


# ---------- 重新获取 bind_key（不修改） ----------
@router.get("/gateway/{gid}/key")
async def get_key(gid: int):
    with db() as conn:
        row = conn.execute("SELECT mac, bind_key FROM gateway WHERE id=?", (gid,)).fetchone()
        if not row:
            return JSONResponse({"ok": False}, status_code=404)
        return JSONResponse({"ok": True, "mac": fmt_mac(row["mac"]), "bind_key": row["bind_key"]})
        
# app/routers/gateways.py
@router.get("/gateway/cmd_status/{cmd_id}")
async def get_cmd_status(cmd_id:str):
    s = mqtt_client.get_cmd_status(cmd_id)
    return s

