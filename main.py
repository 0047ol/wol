from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List
import paho.mqtt.client as mqtt
import uuid
import json
import os

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# MQTT配置
MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
TRIGGER_TOPIC = "wol/trigger"  # 下发：消息体是MAC地址
STATUS_TOPIC = "wol/status"    # 上报：消息体格式 {"mac":"xx:xx:xx:xx:xx:xx","status":"online/offline"}

# 本地存储文件
DEVICES_FILE = "devices.json"

def load_devices():
    """从本地文件加载设备列表"""
    if os.path.exists(DEVICES_FILE):
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # 首次启动，预置一台示例设备
    return [
        {
            "id": str(uuid.uuid4()),
            "name": "台式机",
            "mac": "AA:BB:CC:DD:EE:FF",
            "status": "offline"
        }
    ]

def save_devices():
    """保存设备列表到本地文件"""
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(devices, f, ensure_ascii=False, indent=2)

# 启动时加载设备
devices = load_devices()

class DeviceAdd(BaseModel):
    name: str
    mac: str

class BulkWake(BaseModel):
    device_ids: List[str]

def on_message(client, userdata, msg):
    global devices
    if msg.topic == STATUS_TOPIC:
        try:
            data = json.loads(msg.payload.decode())
            target_mac = data["mac"].upper()
            target_status = data["status"]
            for d in devices:
                if d["mac"].upper() == target_mac:
                    d["status"] = target_status
                    save_devices()
                    print(f"设备 {d['name']}({target_mac}) 状态更新：{target_status}")
                    break
        except Exception as e:
            print(f"状态消息解析失败：{e}, 原始内容：{msg.payload.decode()}")

mqttc = mqtt.Client()
mqttc.on_message = on_message
mqttc.connect(MQTT_BROKER, MQTT_PORT, 60)
mqttc.subscribe(STATUS_TOPIC)
mqttc.loop_start()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/devices")
def get_devices():
    return {"devices": devices}

@app.post("/api/devices")
def add_device(device: DeviceAdd):
    new_device = {
        "id": str(uuid.uuid4()),
        "name": device.name,
        "mac": device.mac.upper(),
        "status": "offline"
    }
    devices.append(new_device)
    save_devices()
    return {"success": True, "device": new_device}

@app.delete("/api/devices/{device_id}")
def delete_device(device_id: str):
    global devices
    devices = [d for d in devices if d["id"] != device_id]
    save_devices()
    return {"success": True}

@app.post("/api/devices/{device_id}/wake")
def wake_single(device_id: str):
    target = next((d for d in devices if d["id"] == device_id), None)
    if not target:
        return {"success": False, "msg": "设备不存在"}
    mqttc.publish(TRIGGER_TOPIC, target["mac"])
    print(f"唤醒单台：{target['name']} - {target['mac']}")
    return {"success": True, "msg": f"已下发唤醒指令到 {target['name']}"}

@app.post("/api/devices/bulk-wake")
def wake_bulk(req: BulkWake):
    woken_count = 0
    for device_id in req.device_ids:
        target = next((d for d in devices if d["id"] == device_id), None)
        if target:
            mqttc.publish(TRIGGER_TOPIC, target["mac"])
            woken_count += 1
    print(f"批量唤醒 {woken_count} 台")
    return {"success": True, "msg": f"已下发 {woken_count} 台设备的唤醒指令"}

# 批量删除
class BulkDelete(BaseModel):
    device_ids: List[str]

@app.post("/api/devices/bulk-delete")
def delete_bulk(req: BulkDelete):
    global devices
    del_ids = set(req.device_ids)
    devices = [d for d in devices if d["id"] not in del_ids]
    save_devices()
    return {"success": True, "msg": f"已删除 {len(del_ids)} 台设备"}

# 局域网扫描接口（通过MQTT通知ESP32扫描局域网）
@app.post("/api/scan")
def scan_lan():
    mqttc.publish("wol/scan", "start")
    print("已下发局域网扫描指令到ESP32")
    return {"success": True, "msg": "扫描指令已下发，等待ESP32上报结果..."}

# 兼容旧接口
@app.get("/wake")
def old_wake():
    if devices:
        mqttc.publish(TRIGGER_TOPIC, devices[0]["mac"])
    return {"msg": "唤醒指令已下发"}

@app.get("/status")
def old_status():
    return {"pc_status": devices[0]["status"] if devices else "offline"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8088, debug=True)
