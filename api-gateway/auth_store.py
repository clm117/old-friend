"""
语伴 网关账号存储（仅用标准库，无第三方依赖）

账号文件路径：环境变量 AUTH_USERS_FILE，默认与本模块同目录的 users.json。
文件格式（明文 JSON，仅存口令派生值，不含原始口令）：

{
  "alice": {
    "salt": "<base64>",
    "hash": "<base64>",
    "iterations": 210000,
    "created_at": 1712345678
  }
}

口令派生：PBKDF2-HMAC-SHA256 + 每账号独立随机盐；校验使用 hmac.compare_digest
常量时间比较，避免按比较耗时侧信道推断口令。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

DEFAULT_ITERATIONS = 210_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@\-]{1,64}$")


def valid_username(name: str) -> bool:
    """账号名字符白名单，避免脏数据进入账号表与日志"""
    return bool(name) and bool(_USERNAME_RE.match(name))


def users_file_path() -> Path:
    """账号文件路径：优先 AUTH_USERS_FILE，否则回退到本模块同目录"""
    env = os.environ.get("AUTH_USERS_FILE", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "users.json"


def load_users(path: Path | None = None) -> dict:
    """读取账号表；文件缺失或损坏时返回空表（由调用方决定是否拒绝启动）"""
    p = path or users_file_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_users(users: dict, path: Path | None = None) -> Path:
    """写回账号表：先写临时文件再原子替换，避免写一半损坏"""
    p = path or users_file_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)  # 尽量收紧权限（Windows 上可能无效，忽略）
    except OSError:
        pass
    return p


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> dict:
    """生成口令记录（含随机盐与迭代次数，便于后续平滑提升强度）"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(dk).decode("ascii"),
        "iterations": iterations,
        "created_at": int(time.time()),
    }


def verify_password(password: str, record: dict) -> bool:
    """常量时间校验；记录结构异常一律视为不通过"""
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record.get("iterations", DEFAULT_ITERATIONS))
    except (KeyError, TypeError, ValueError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# 账号不存在时用于耗时对齐的固定盐（抗账号名枚举）
_DUMMY_SALT = b"\x00" * 16


def dummy_verify(password: str) -> None:
    """对不存在的账号执行等价的密钥派生，使失败耗时与真实校验接近"""
    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _DUMMY_SALT, DEFAULT_ITERATIONS)
