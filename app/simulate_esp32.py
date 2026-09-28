"""模拟一台 ESP32-C3 网关：连 MQTT、发 register/heartbeat、收指令、回 ack。
用法：python3 simulate_esp32.py <MAC小写无冒号> <BIND_KEY> [MQTT_HOST]
示例：python3 simulate_esp32.py aabbcc112233 你的bind_key 127.0.0.1
"""
import sys, json, time, threading, hashlib
import paho.mqtt.client as mqtt

MAC = sys.argv[1]
BIND_KEY = sys.argv[2]
MQTT_HOST = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"
MQTT_PORT = 1883
TOPIC = f"wol/gateway/{MAC}"

def sign(ts, payload):
    return hashlib.sha256(f"{BIND_KEY}{ts}{payload}".encode()).hexdigest()

def on_connect(c, u, f, rc):
    print(f"[MQTT] connected rc={rc}")
    c.subscribe(f"{TOPIC}/cmd", qos=1)
    # 发 register
    reg = {
        "gateway_mac": MAC,
        "local_ip": "192.168.1.100",
        "netmask": "255.255.255.0",
        "gateway_ip": "192.168.1.1",
        "cidr": "192.168.1.0/24",
        "bind_key": BIND_KEY,
    }
    c.publish(f"{TOPIC}/register", json.dumps(reg), qos=1)
    print(f"[ESP] register published")

def on_message(c, u, msg):
    try:
        body = json.loads(msg.payload)
    except Exception:
        return
    ts = body.get("timestamp", "")
    cmd_type = body.get("cmd_type", "")
    payload = body.get("payload", {})
    # 验签
    raw = json.dumps({k: v for k, v in body.items() if k != "sign"},
                     separators=(",", ":"), ensure_ascii=False)
    expected = sign(ts, raw)
    if expected != body.get("sign"):
        print(f"[ESP] !! sign mismatch on {cmd_type}, ignore")
        return
    # 时间戳检查
    if abs(int(time.time()) - int(ts)) > 60:
        print(f"[ESP] !! timestamp expired on {cmd_type}, ignore")
        return

    print(f"[ESP] <- cmd: {cmd_type} payload={json.dumps(payload, ensure_ascii=False)[:200]}")

    # 处理指令
    ok, reply_msg = True, ""
    if cmd_type == "sync_all" or cmd_type == "sync_sub_devices":
        devs = payload.get("sub_devices", [])
        reply_msg = f"synced {len(devs)} devices"
        # 模拟：2 秒后上报子设备在线
        def fake_report():
            time.sleep(2)
            rep = {"devices": [{"mac": d["mac"], "status": "online"} for d in devs]}
            c.publish(f"{TOPIC}/subdevice/report", json.dumps(rep), qos=0)
            print(f"[ESP] -> subdevice/report ({len(devs)} online)")
        threading.Thread(target=fake_report, daemon=True).start()
    elif cmd_type == "update_config":
        reply_msg = "config updated"
    elif cmd_type == "update_wifi":
        reply_msg = "will reboot"
    elif cmd_type == "wake":
        macs = payload.get("macs", [])
        reply_msg = f"sent WOL to {len(macs)} macs"
    elif cmd_type == "scan":
        reply_msg = "scan done"
        # 模拟扫描到 3 台主机
        time.sleep(1)
        hosts = [
            {"ip": "192.168.1.20", "mac": "aabbcc112244"},
            {"ip": "192.168.1.21", "mac": "aabbcc112255"},
            {"ip": "192.168.1.22", "mac": "aabbcc112266"},
        ]
        c.publish(f"{TOPIC}/scan/result", json.dumps({"hosts": hosts}), qos=1)
        print(f"[ESP] -> scan/result (3 hosts)")
    elif cmd_type == "reset":
        reply_msg = "factory reset"

    # 回 ack
    ack = {"cmd_type": cmd_type, "ok": ok, "msg": reply_msg}
    c.publish(f"{TOPIC}/cmd/ack", json.dumps(ack), qos=1)
    print(f"[ESP] -> ack {cmd_type}: {reply_msg}")

client = mqtt.Client(client_id=f"gateway_{MAC}")
client.username_pw_set("wol_gateway", BIND_KEY)
client.on_connect = on_connect
client.on_message = on_message
client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

# 后台心跳
def heartbeat():
    while True:
        client.publish(f"{TOPIC}/heartbeat", json.dumps({"local_ip": "192.168.1.100"}), qos=0)
        print("[ESP] -> heartbeat")
        time.sleep(15)
threading.Thread(target=heartbeat, daemon=True).start()

print(f"[ESP] simulating gateway MAC={MAC}, MQTT={MQTT_HOST}:{MQTT_PORT}")
client.loop_forever()
