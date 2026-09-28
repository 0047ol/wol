"""全局配置：从环境变量读取，给一套本地默认值。"""
import os


def _get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # HTTP
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8088"))

    # 调试模式：环境变量 DEBUG=1 开启
    # - uvicorn 自动 reload
    # - 日志级别 DEBUG
    # - /debug 页面展示 MQTT 状态、最近指令、原始报文
    DEBUG = _get_bool("DEBUG", True)

    # MQTT
    MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
    MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
    MQTT_USERNAME = os.getenv("MQTT_USERNAME", "wol_server")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")  # 服务端连 broker 的密码，按需填写

    # 主题前缀
    MQTT_TOPIC_PREFIX = "wol/gateway"

    # 数据库
    DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "wol.db"))

    # 签名时间窗（秒）
    SIGN_TOLERANCE = 60

    # 心跳超时扫描间隔（秒）
    HB_SCAN_INTERVAL = 10

    # 指令 ack 超时（秒）
    CMD_ACK_TIMEOUT = 8

    # 分页默认值
    DEFAULT_PAGE_SIZE = 20
    MAX_PAGE_SIZE = 100


settings = Settings()
