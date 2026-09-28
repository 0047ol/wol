import asyncio
import logging
from datetime import datetime, timedelta

from .config import settings
from .database import db

log = logging.getLogger("background")


def _parse(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


async def heartbeat_watchdog():
    """网关心跳超时巡检，超时标记离线"""
    while True:
        try:
            with db() as conn:
                cur = conn.execute(
                    "SELECT id, status, last_heartbeat, heartbeat_timeout FROM gateway"
                )
                now_dt = datetime.now()
                for row in cur.fetchall():
                    if row["status"] == "unbound":
                        continue
                    last = _parse(row["last_heartbeat"])
                    if not last:
                        continue
                    if now_dt - last > timedelta(seconds=row["heartbeat_timeout"]):
                        if row["status"] != "offline":
                            conn.execute(
                                "UPDATE gateway SET status='offline' WHERE id=?",
                                (row["id"],),
                            )
                            log.info("gateway id=%s marked offline", row["id"])
        except Exception:
            log.exception("heartbeat_watchdog error")
        await asyncio.sleep(settings.HB_SCAN_INTERVAL)


async def ack_watchdog():
    """定时清理超时 ACK，释放任务锁"""
    from .mqtt_client import mqtt_client

    while True:
        try:
            mqtt_client.check_ack_timeout()
        except Exception:
            log.exception("ack_watchdog error")
        await asyncio.sleep(0.5)
