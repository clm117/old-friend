"""CI 冒烟测试：不依赖 GPU/模型/上游 AI 服务。

覆盖：
- /health 在上游不可达时返回 503（不再假健康）
- /ready 就绪探针返回 503 且给出原因
- 无凭证访问受保护接口返回 401，且携带 CORS 头
- 正确凭证登录、/api/auth/me 可达
- 伪造 token 被拒绝
- CORS 预检允许 DELETE
- /metrics 暴露 Prometheus 指标

运行：python tests/ci_smoke.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api-gateway"))

TMP = tempfile.mkdtemp(prefix="yuban-ci-")
USERS_FILE = Path(TMP) / "users.json"
os.environ.update({
    "AUTH_REQUIRED": "true",
    "JWT_SECRET": "ci-only-secret-0123456789abcdef0123456789abcdef",
    "AUTH_USERS_FILE": str(USERS_FILE),
    # 指向不可用端口，验证健康检查的降级行为
    "AI_SERVICE_URL": "http://127.0.0.1:9",
    "AI_SERVICE_WS_URL": "ws://127.0.0.1:9",
    "LOGIN_RATE_LIMIT_PER_MINUTE": "50",
    "CORS_ALLOW_ORIGINS": "http://localhost:8080",
    "GATEWAY_REDIS_URL": "",
})

from auth_store import hash_password  # noqa: E402

USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
USERS_FILE.write_text(json.dumps({"ciuser": hash_password("CiTest@123456")}), encoding="utf-8")


def main() -> int:
    from fastapi.testclient import TestClient

    import app as gateway  # noqa: E402  导入后 app 不会自行启动 uvicorn

    client = TestClient(gateway.app, raise_server_exceptions=False)
    failures = []

    def check(name, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    r = client.get("/health")
    body = r.json()
    check("/health 上游不可达 → 503", r.status_code == 503 and body.get("status") == "degraded",
          f"status={r.status_code}")
    check("/health 暴露鉴权要求", body.get("auth_required") is True)

    r = client.get("/ready")
    body = r.json()
    check("/ready 未就绪 → 503", r.status_code == 503 and "ai_service_not_ready" in body.get("problems", []),
          f"status={r.status_code}")

    r = client.get("/api/characters", headers={"Origin": "http://localhost:8080"})
    check("无凭证 → 401", r.status_code == 401, f"status={r.status_code}")
    check("401 携带 CORS 头", r.headers.get("access-control-allow-origin") == "http://localhost:8080",
          r.headers.get("access-control-allow-origin", "<none>"))

    r = client.post("/api/auth/login", json={"username": "ciuser", "password": "CiTest@123456"})
    token = (r.json() or {}).get("token", "")
    check("正确凭证登录 → 200 + token", r.status_code == 200 and bool(token), f"status={r.status_code}")

    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("/api/auth/me → 200", r.status_code == 200, f"status={r.status_code}")

    r = client.get("/api/characters", headers={"Authorization": "Bearer forged.token.value"})
    check("伪造 token → 401", r.status_code == 401, f"status={r.status_code}")

    r = client.options("/api/characters", headers={
        "Origin": "http://localhost:8080",
        "Access-Control-Request-Method": "DELETE",
    })
    allow = r.headers.get("access-control-allow-methods", "")
    check("CORS 预检允许 DELETE", r.status_code == 200 and "DELETE" in allow, allow)

    r = client.get("/metrics")
    check("/metrics 可抓取", r.status_code == 200 and "gateway_requests_total" in r.text,
          f"status={r.status_code}")

    print(f"\nCI 冒烟：{len(failures)} 项失败")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
