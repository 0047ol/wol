"""子设备管理 API + 页面。"""
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ..database import db, now
from ..mqtt_client import mqtt_client
from ..utils import norm_mac, fmt_mac, paginate

log = logging.getLogger("device.router")
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _push_sync(conn, gateway_id: int):
    """把最新子设备列表下发给在线网关。"""
    row = conn.execute("SELECT * FROM gateway WHERE id=?", (gateway_id,)).fetchone()
    if not row or row["status"] != "online":
        return False
    cur = conn.execute("SELECT name, mac, ip FROM sub_device WHERE gateway_id=?", (gateway_id,))
    devices = [{"name": r["name"], "mac": norm_mac(r["mac"]), "ip": r["ip"]} for r in cur.fetchall()]
    mqtt_client.publish_cmd(row["mac"], "sync_sub_devices", {"sub_devices": devices})
    conn.execute(
        "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at) VALUES (?,?,?,?)",
        (gateway_id, "sync_sub_devices", f"count={len(devices)}", now()),
    )
    return True


# ---------- 页面：子设备列表（按网关） ----------
@router.get("/gateway/{gid}/devices", response_class=HTMLResponse)
async def page_devices(request: Request, gid: int, page: int = 1, page_size: int = 20, q: str = ""):
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return HTMLResponse("网关不存在", status_code=404)
        where, args = "WHERE gateway_id=?", [gid]
        if q:
            where += " AND (name LIKE ? OR mac LIKE ? OR ip LIKE ?)"
            like = f"%{q}%"
            args += [like, like, like]
        total = conn.execute(f"SELECT COUNT(*) c FROM sub_device {where}", args).fetchone()["c"]
        page, page_size, offset, total_pages = paginate(page, page_size, total)
        rows = conn.execute(
            f"SELECT * FROM sub_device {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, page_size, offset],
        ).fetchall()
    return templates.TemplateResponse(
        "devices.html",
        {
            "request": request,
            "gw": gw,
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "total": total,
            "q": q,
        },
    )


# ---------- 批量新增（每行：name,mac,ip） ----------
@router.post("/gateway/{gid}/devices/batch_add")
async def batch_add(gid: int, lines: str = Form(...)):
    added, skipped = 0, []
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        for raw in lines.strip().splitlines():
            parts = [p.strip() for p in raw.replace("，", ",").split(",")]
            if len(parts) < 2:
                skipped.append(raw); continue
            name, mac = parts[0], norm_mac(parts[1])
            ip = parts[2] if len(parts) > 2 else ""
            if not mac or len(mac) != 12:
                skipped.append(raw); continue
            # 同网关下 MAC 去重
            dup = conn.execute(
                "SELECT id FROM sub_device WHERE gateway_id=? AND mac=?",
                (gid, mac)
            ).fetchone()
            if dup:
                skipped.append(f"{raw}（MAC已存在）")
                continue
            try:
                conn.execute(
                    "INSERT INTO sub_device (gateway_id, name, mac, ip, status) VALUES (?,?,?,?,'offline')",
                    (gid, name, mac, ip),
                )
                added += 1
            except Exception:
                skipped.append(raw)
        pushed = _push_sync(conn, gid)
    return JSONResponse({"ok": True, "added": added, "skipped": skipped, "pushed_to_gateway": pushed})



# ---------- 批量删除 ----------
@router.post("/gateway/{gid}/devices/batch_delete")
async def batch_delete(gid: int, ids: str = Form(...)):
    """ids: 逗号分隔的子设备 id"""
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return JSONResponse({"ok": False, "msg": "未选择设备"})
    with db() as conn:
        ph = ",".join("?" * len(id_list))
        conn.execute(f"DELETE FROM sub_device WHERE gateway_id=? AND id IN ({ph})", [gid, *id_list])
        pushed = _push_sync(conn, gid)
    return JSONResponse({"ok": True, "deleted": len(id_list), "pushed_to_gateway": pushed})


# ---------- 批量唤醒 ----------
@router.post("/gateway/{gid}/devices/wake")
async def wake(gid: int, ids: str = Form(...)):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        return JSONResponse({"ok": False, "msg": "未选择设备"})
    with db() as conn:
        gw = conn.execute("SELECT * FROM gateway WHERE id=?", (gid,)).fetchone()
        if not gw:
            return JSONResponse({"ok": False, "msg": "网关不存在"}, status_code=404)
        if gw["status"] != "online":
            return JSONResponse({"ok": False, "msg": "网关离线，无法下发唤醒指令"}, status_code=400)
        ph = ",".join("?" * len(id_list))
        cur = conn.execute(
            f"SELECT mac FROM sub_device WHERE gateway_id=? AND id IN ({ph})",
            [gid, *id_list],
        )
        macs = [norm_mac(r["mac"]) for r in cur.fetchall()]
        mqtt_client.publish_cmd(gw["mac"], "wake", {"macs": macs})
        conn.execute(
            "INSERT INTO cmd_log (gateway_id, cmd_type, payload, created_at) VALUES (?,?,?,?)",
            (gid, "wake", json.dumps(macs), now()),
        )
    return JSONResponse({"ok": True, "msg": f"已向 {len(macs)} 台设备发送 WOL 魔术包"})
