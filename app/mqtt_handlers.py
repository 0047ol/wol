"""MQTT 上行消息处理器：register / heartbeat / subdevice/report / scan/result / cmd/ack"""
import json
import logging
from datetime import datetime

from .database import db, now
from .mqtt_client import mqtt_client
from .utils import norm_mac, fmt_mac

log = logging.getLogger("mqtt.handler")


def _get_gateway_by_mac(cur, mac: str):
    cur.execute("SELECT * FROM gateway WHERE mac = ?", (mac,))
    return cur.fetchone()


def handle_register(topic: str, data: dict):
    """ESP32 注册上报：校验 MAC+密钥，绑定/上线；下行全套业务配置。"""
    mac = norm_mac(data.get("gateway_mac") or topic.split("/")[2])
    bind_key = data.get("bind_key", "")
    local_ip = data.get("local_ip", "")
    netmask = data.get("netmask", "")
    gateway_ip = data.get("gateway_ip", "")
    cidr = data.get("cidr", "")

    with db() as conn:
        row = _get_gateway_by_mac(conn.cursor(), mac)
        if not row:
            log.warning("register rejected: mac %s not found", mac)
            return
        if row["bind_key"] != bind_key:
            conn.execute(
                "UPDATE gateway SET bind_message = ? WHERE id = ?",
                ("绑定失败：密钥不匹配", row["id"]),
            )
            log.warning("register rejected: bind_key mismatch for %s", mac)
            return

        # 绑定成功
        conn.execute(
            """UPDATE gateway
               SET ip=?, netmask=?, gateway_ip=?, status='online',
                   last_heartbeat=?, bind_message=''
               WHERE id=?""",
            (local_ip, netmask, gateway_ip, now(), row["id"]),
        )
        # 如果用户没改过 scan_cidr，用 ESP32 上报的 cidr 填充默认
        if cidr and not row["scan_cidr"]:
            conn.execute("UPDATE gateway SET scan_cidr=? WHERE id=?", (cidr, row["id"]))

        # 拉取该网关全量子设备
        cur = conn.execute("SELECT name, mac, ip FROM sub_device WHERE gateway_id=?", (row["id"],))
        devices = [{"name": r["name"], "mac": norm_mac(r["mac"]), "ip": r["ip"]} for r in cur.fetchall()]

        config_payload = {
            "ping_interval": row["ping_interval"],
            "heartbeat_send_interval": row["heartbeat_send_interval"],
            "wol_broadcast_addr": row["wol_broadcast_addr"],
            "scan_cidr": cidr or row["scan_cidr"],
            "scan_concurrency": row["scan_concurrency"],
            "sub_devices": devices,
        }

    # 下发 sync_sub_devices + update_config（合并成一个 sync_all 指令）
    mqtt_client.publish_cmd(mac, bind_key, "sync_all", config_payload)
    log.info("gateway %s bound, config pushed", fmt_mac(mac))


def handle_heartbeat(topic: str, data: dict):
    mac = norm_mac(topic.split("/")[2])
    ip = data.get("local_ip", "")
    with db() as conn:
        conn.execute(
            "UPDATE gateway SET ip=?, status='online', last_heartbeat=? WHERE mac=? AND status != 'unbound'",
            (ip, now(), mac),
        )


def handle_subdevice_report(topic: str, data: dict):
    """批量上报子设备状态：[{mac, status}]"""
    mac = norm_mac(topic.split("/")[2])
    devices = data.get("devices", [])
    with db() as conn:
        row = _get_gateway_by_mac(conn.cursor(), mac)
        if not row:
            return
        for d in devices:
            dmac = norm_mac(d.get("mac", ""))
            if not dmac:
                continue
            conn.execute(
                """UPDATE sub_device
                   SET status=?, last_report=?
                   WHERE gateway_id=? AND mac=?""",
                (d.get("status", "offline"), now(), row["id"], dmac),
            )


def handle_scan_result(topic: str, data: dict):
    mac = norm_mac(topic.split("/")[2])
    hosts = data.get("hosts", [])
    with db() as conn:
        row = _get_gateway_by_mac(conn.cursor(), mac)
        if not row:
            return
        conn.execute("DELETE FROM scan_result WHERE gateway_id=?", (row["id"],))
        for h in hosts:
            ip = h.get("ip", "")
            hmac = norm_mac(h.get("mac", ""))
            if not ip or not hmac:
                continue
            conn.execute(
                "INSERT INTO scan_result (gateway_id, ip, mac, created_at) VALUES (?,?,?,?)",
                (row["id"], ip, hmac, now()),
            )


def handle_cmd_ack(topic: str, data: dict):
    mqtt_client.handle_cmd_ack_callback(topic, data)
    """ESP32 指令执行应答：{cmd_type, ok, msg}"""
    mac = norm_mac(topic.split("/")[2])
    cmd_type = data.get("cmd_type", "")
    ok = data.get("ok", False)
    msg = data.get("msg", "")
    with db() as conn:
        row = _get_gateway_by_mac(conn.cursor(), mac)
        if not row:
            return
        # 找最近一条同类型未 ack 的指令
        cur = conn.execute(
            """SELECT id FROM cmd_log
               WHERE gateway_id=? AND cmd_type=? AND status='sent'
               ORDER BY id DESC LIMIT 1""",
            (row["id"], cmd_type),
        )
        log_row = cur.fetchone()
        if log_row:
            conn.execute(
                "UPDATE cmd_log SET status=?, ack_msg=?, ack_at=? WHERE id=?",
                ("ok" if ok else "failed", msg, now(), log_row["id"]),
            )


def register_handlers():
    mqtt_client.on("register", handle_register)
    mqtt_client.on("heartbeat", handle_heartbeat)
    mqtt_client.on("subdevice/report", handle_subdevice_report)
    mqtt_client.on("scan/result", handle_scan_result)
    mqtt_client.on("cmd/ack", handle_cmd_ack)
