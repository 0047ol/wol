import json
import time
import logging
from collections import deque
from typing import Dict, Any, Callable
import paho.mqtt.client as mqtt
from datetime import datetime  # 新增
from .database import get_gateway_by_mac, update_gateway_status, now

log = logging.getLogger(__name__)


class MQTTClient:
    def __init__(self, broker_host: str, broker_port: int, server_username: str, server_password: str):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.server_username = server_username
        self.server_password = server_password
        self.client = mqtt.Client(client_id="wol_server")
        self.client.username_pw_set(self.server_username, self.server_password)

        # 事件处理器，兼容 .on() 注册方式
        self._event_handlers: Dict[str, Callable] = {}

        # 消息日志（debug面板最多100条）
        self.msg_log = deque(maxlen=100)
        # 等待ack的指令
        self.pending_acks: Dict[str, Dict[str, Any]] = dict()
        # 指令历史日志
        self.cmd_log: Dict[str, Dict[str, Any]] = dict()
        # 网关任务互斥锁：key=gateway_mac, value=set(cmd_type)
        self.task_locks: Dict[str, set] = dict()

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.last_connect_ts = None
        self.last_disconnect_ts = None

    # 保留原有on方法，兼容mqtt_handlers.py
    def on(self, event_name: str, handler: Callable):
        self._event_handlers[event_name] = handler

    def start(self):
        self.client.connect(self.broker_host, self.broker_port, keepalive=30)
        self.client.loop_start()
        log.info("MQTT client started, connecting to %s:%s", self.broker_host, self.broker_port)

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected successfully")
            # 订阅所有网关上行主题
            self.client.subscribe("wol/gateway/+/register", qos=1)
            self.client.subscribe("wol/gateway/+/heartbeat", qos=0)
            self.client.subscribe("wol/gateway/+/cmd/ack", qos=1)
            self.client.subscribe("wol/gateway/+/subdevice/report", qos=0)
            self.client.subscribe("wol/gateway/+/scan/result", qos=1)
            self.connected_flag = True
            self.last_connect_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            self.connected_flag = False
            log.error("MQTT connect failed rc=%d", rc)
            
    def _on_disconnect(self, client, userdata, rc):
        self.connected_flag = False
        self.last_disconnect_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.warning(f"MQTT disconnected rc={rc}") # logger → log


    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            self.msg_log.appendleft({
                "dir": "IN",
                "ts": now(),
                "topic": msg.topic,
                "payload": payload[:500],
            })
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("invalid json on %s: %s", msg.topic, payload[:200])
                return

            parts = msg.topic.split("/")
            if len(parts) <4 or parts[0]!="wol" or parts[1]!="gateway":
                return
            event = "/".join(parts[3:])
            # 触发事件，调用mqtt_handlers注册的handler
            if event in self._event_handlers:
                self._event_handlers[event](msg.topic, data)
        except Exception:
            log.exception("handle message error topic=%s", msg.topic)

    def get_gateway_bindkey(self, mac: str):
        gw = get_gateway_by_mac(mac)
        if not gw:
            return None
        return gw["bind_key"]


    def publish_cmd(self, gateway_mac: str, cmd_type: str, payload: dict, timeout_sec=8):
        # 任务互斥锁校验
        if gateway_mac not in self.task_locks:
            self.task_locks[gateway_mac] = set()
        if cmd_type in self.task_locks[gateway_mac]:
            return {"ok": False, "msg": "任务正在执行，请稍后重试", "cmd_id": None}
        self.task_locks[gateway_mac].add(cmd_type)

        bind_key = self.get_gateway_bindkey(gateway_mac)
        if not bind_key:
            self.task_locks[gateway_mac].discard(cmd_type)
            return {"ok": False, "msg": "网关不存在，无bind_key", "cmd_id": None}

        import hashlib
        ts = str(int(time.time()))
        payload = payload or {}

        # 关键：签名输入 = 整个 body（去掉 sign）序列化，和模拟器保持一致
        body = {"cmd_type": cmd_type, "payload": payload, "timestamp": ts}
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        sign_src = f"{bind_key}{ts}{body_str}"
        sign = hashlib.sha256(sign_src.encode("utf-8")).hexdigest()
        body["sign"] = sign

        topic = f"wol/gateway/{gateway_mac}/cmd"
        self.client.publish(topic, json.dumps(body, ensure_ascii=False), qos=1)

        cmd_id = f"{gateway_mac}_{cmd_type}_{ts}"
        self.pending_acks[cmd_id] = {
            "gateway_mac": gateway_mac,
            "cmd_type": cmd_type,
            "send_ts": int(ts),
            "timeout_ts": int(ts) + timeout_sec
        }
        self.cmd_log[cmd_id] = {"status": "pending", "msg": "等待设备应答"}
        return {"ok": True, "msg": "指令已下发", "cmd_id": cmd_id}


    # 后台看门狗，扫描超时ack，释放锁并标记timeout
    def check_ack_timeout(self):
        now_ts = int(time.time())
        for cmd_id in list(self.pending_acks.keys()):
            item = self.pending_acks[cmd_id]
            if now_ts > item["timeout_ts"]:
                gm = item["gateway_mac"]
                ct = item["cmd_type"]
                if gm in self.task_locks:
                    self.task_locks[gm].discard(ct)
                self.cmd_log[cmd_id] = {"status": "timeout", "msg": "未收到设备应答，指令超时"}
                del self.pending_acks[cmd_id]

    def get_cmd_status(self, cmd_id: str):
        if cmd_id in self.pending_acks:
            return {"status": "pending", "msg": "等待应答"}
        log_item = self.cmd_log.get(cmd_id)
        if not log_item:
            return {"status": "unknown", "msg": "无此指令记录"}
        return log_item

    # 处理ack回调，释放锁，更新状态
    def handle_cmd_ack_callback(self, topic, data):
        mac = topic.split("/")[2]
        cmd_type = data.get("cmd_type")
        found_cid = None
        for cid, item in self.pending_acks.items():
            if item["gateway_mac"] == mac and item["cmd_type"] == cmd_type:
                found_cid = cid
                break
        if found_cid:
            item = self.pending_acks.pop(found_cid)
            gm = item["gateway_mac"]
            ct = item["cmd_type"]
            if gm in self.task_locks:
                self.task_locks[gm].discard(ct)
            ok = data.get("ok", False)
            self.cmd_log[found_cid] = {
                "status": "ok" if ok else "fail",
                "msg": data.get("msg", "")
            }

# 全局单例，main.py使用
mqtt_client = MQTTClient(
    broker_host="127.0.0.1",
    broker_port=1883,
    server_username="",
    server_password=""
)
