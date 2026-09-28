"""局域网扫描：下发 scan 指令、展示扫描结果、一键添加。"""
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..database import db, now
from ..mqtt_client import mqtt_client
from ..utils import norm_mac, fmt_mac, paginate

log = logging.getLogger("scan.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/gateway/{gid}/scan", response_class=HTMLResponse)
async def page_scan(request: Request, gid: int, page: int = 1, page_size: int = 20):
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return HTMLResponse("网关不存在", status_code=404)
        total = conn.execute(
            "SELECT COUNT(*) c FROM scan_result WHERE gateway_id=?", (gid,)
        ).fetchone()["c"]
        page, page_size, offset, total_pages = paginate(page, page_size, total)
        rows = conn.execute(
            "SELECT * FROM scan_result WHERE gateway_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (gid, page_size, offset),
        ).fetchall()
    return templates.TemplateResponse(
        "scan.html",
        {
            "request": request,
            "gw": gw,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.post("/gateway/{gid}/scan/trigger")
async def trigger_scan(gid: int):
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if gw["status"] != "online":
            return JSONResponse({"ok": False, "msg": "网关离线，无法扫描"}, status_code=400)
        # 修正publish_cmd参数，删掉gw["bind_key"]
        ret = mqtt_client.publish_cmd(gw["mac"], "scan", {})
        if not ret["ok"]:
            return JSONResponse(ret)
        conn.execute(
            "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at) VALUES (?,?,?,?)",
            (gid, "scan", "trigger", now()),
        )
    # 返回包含cmd_id，前端JS读取
    return JSONResponse(ret)



@router.post("/gateway/{gid}/scan/add")
async def add_from_scan(gid: int, ids: str = Form(...)):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return JSONResponse({"ok": False, "msg": "未选择"})
    added, skipped = 0, []
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        ph = ",".join("?" * len(id_list))
        cur = conn.execute(
            f"SELECT ip, mac FROM scan_result WHERE gateway_id=? AND id IN ({ph})",
            [gid, *id_list],
        )
        for r in cur.fetchall():
            mac = norm_mac(r["mac"])
            # 同网关下 MAC 去重
            dup = conn.execute(
                "SELECT id FROM sub_device WHERE gateway_id=? AND mac=?",
                (gid, mac)
            ).fetchone()
            if dup:
                skipped.append(f"{r['ip']}({mac}) 已存在")
                continue
            conn.execute(
                "INSERT INTO sub_device (gateway_id, name, mac, ip, status) VALUES (?,?,?,?,'offline')",
                (gid, f"PC-{r['ip'][-8:]}", mac, r["ip"]),
            )
            added += 1
        cur2 = conn.execute("SELECT name, mac, ip FROM sub_device WHERE gateway_id=?", (gid,))
        devices = [{"name": r["name"], "mac": norm_mac(r["mac"]), "ip": r["ip"]} for r in cur2.fetchall()]
        if gw["status"] == "online":
            mqtt_client.publish_cmd(gw["mac"], "sync_sub_devices", {"sub_devices": devices})
    return JSONResponse({"ok": True, "added": added, "skipped": skipped})

    
@router.get("/gateway/cmd_status/{cmd_id}")
async def get_cmd_status(cmd_id: str):
    data = mqtt_client.get_cmd_status(cmd_id)
    return JSONResponse(data)

