"""局部刷新用的 JSON 接口。"""
from fastapi import APIRouter

from ..database import db

router = APIRouter()


@router.get("/api/gateways")
async def api_gateways():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, mac, ip, status, last_heartbeat, bind_message FROM gateway ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/gateway/{gid}/devices")
async def api_devices(gid: int):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, name, mac, ip, status, last_report
               FROM sub_device WHERE gateway_id=? ORDER BY id DESC""",
            (gid,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/gateway/{gid}/scan_results")
async def api_scan_results(gid: int):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, ip, mac, created_at
               FROM scan_result WHERE gateway_id=? ORDER BY id DESC""",
            (gid,),
        ).fetchall()
    return [dict(r) for r in rows]
