"""SQLite 封装：连接、建表、通用查询。"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import settings

_lock = threading.RLock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


# 全局共享连接（FastAPI 多线程 + MQTT 回调线程都会用）
_db = _conn()


@contextmanager
def db():
    with _lock:
        yield _db


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with _lock:
        _db.executescript(
            """
            CREATE TABLE IF NOT EXISTS gateway (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac TEXT UNIQUE NOT NULL,
                bind_key TEXT NOT NULL,
                name TEXT DEFAULT '',
                ip TEXT DEFAULT '',
                netmask TEXT DEFAULT '',
                gateway_ip TEXT DEFAULT '',
                scan_cidr TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unbound',
                ping_interval INTEGER NOT NULL DEFAULT 30,
                heartbeat_timeout INTEGER NOT NULL DEFAULT 30,
                heartbeat_send_interval INTEGER NOT NULL DEFAULT 15,
                wol_broadcast_addr TEXT NOT NULL DEFAULT '255.255.255.255',
                scan_concurrency INTEGER NOT NULL DEFAULT 8,
                wifi_ssid TEXT DEFAULT '',
                wifi_password TEXT DEFAULT '',
                last_heartbeat TEXT DEFAULT '',
                bind_message TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sub_device (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                mac TEXT NOT NULL,
                ip TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'offline',
                last_report TEXT DEFAULT '',
                FOREIGN KEY (gateway_id) REFERENCES gateway(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS scan_result (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                ip TEXT NOT NULL,
                mac TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (gateway_id) REFERENCES gateway(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS cmd_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gateway_id INTEGER NOT NULL,
                cmd_type TEXT NOT NULL,
                payload TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                ack_msg TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                ack_at TEXT DEFAULT ''
            );
            """
        )

from datetime import datetime

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_gateway_by_mac(mac: str):
    """根据 MAC 查询网关记录"""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM gateway WHERE mac = ?",
            (mac,)
        ).fetchone()

def update_gateway_status(mac: str, status: str, ip=None, netmask=None, gateway_ip=None, cidr=None, bind_message=""):
    """更新网关状态、网络信息、绑定提示和最后心跳时间"""
    with db() as conn:
        conn.execute(
            """
            UPDATE gateway
            SET status = ?,
                ip = ?,
                netmask = ?,
                gateway_ip = ?,
                cidr = ?,
                bind_message = ?,
                last_heartbeat = ?
            WHERE mac = ?
            """,
            (
                status,
                ip,
                netmask,
                gateway_ip,
                cidr,
                bind_message,
                now(),
                mac,
            ),
        )

