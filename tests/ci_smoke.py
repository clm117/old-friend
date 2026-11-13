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
import re
import subprocess
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

    r = client.get("/api/children", headers={"Origin": "http://localhost:8080"})
    check("无凭证 → 401", r.status_code == 401, f"status={r.status_code}")
    check("401 携带 CORS 头", r.headers.get("access-control-allow-origin") == "http://localhost:8080",
          r.headers.get("access-control-allow-origin", "<none>"))

    r = client.post("/api/auth/login", json={"username": "ciuser", "password": "CiTest@123456"})
    token = (r.json() or {}).get("token", "")
    check("正确凭证登录 → 200 + token", r.status_code == 200 and bool(token), f"status={r.status_code}")

    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    check("/api/auth/me → 200", r.status_code == 200, f"status={r.status_code}")

    r = client.get("/api/children", headers={"Authorization": "Bearer forged.token.value"})
    check("伪造 token → 401", r.status_code == 401, f"status={r.status_code}")

    r = client.options("/api/children", headers={
        "Origin": "http://localhost:8080",
        "Access-Control-Request-Method": "DELETE",
    })
    allow = r.headers.get("access-control-allow-methods", "")
    check("CORS 预检允许 DELETE", r.status_code == 200 and "DELETE" in allow, allow)

    r = client.get("/metrics")
    check("/metrics 可抓取", r.status_code == 200 and "gateway_requests_total" in r.text,
          f"status={r.status_code}")

    check_compose(check)
    check_startup_guards(check)

    print(f"\nCI 冒烟：{len(failures)} 项失败")
    return 1 if failures else 0


def check_compose(check):
    """Compose 静态可执行性：无 Docker 时校验结构、依赖门控与端口一致性。"""
    path = ROOT / "docker-compose.yml"
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        check("Compose YAML 可解析", False, f"{type(e).__name__}: {e}")
        return
    services = (doc or {}).get("services") or {}
    core = {"nginx", "auth-init", "api-gateway", "ai-service"}
    check("Compose YAML 可解析且核心服务齐全", core <= set(services), sorted(services))

    missing_dep, bad_condition = [], []
    for name, svc in services.items():
        dep = svc.get("depends_on") or {}
        pairs = dep.items() if isinstance(dep, dict) else [(d, {}) for d in dep]
        for target, opts in pairs:
            if target not in services:
                missing_dep.append(f"{name}->{target}")
            cond = (opts or {}).get("condition")
            if cond and cond not in ("service_started", "service_healthy",
                                     "service_completed_successfully"):
                bad_condition.append(f"{name}->{target}:{cond}")
    gw_dep = services.get("api-gateway", {}).get("depends_on") or {}
    gate_ok = (isinstance(gw_dep, dict)
               and gw_dep.get("auth-init", {}).get("condition") == "service_completed_successfully"
               and gw_dep.get("ai-service", {}).get("condition") == "service_healthy")
    check("依赖目标存在、门控条件合法", not missing_dep and not bad_condition and gate_ok,
          {"missing": missing_dep, "bad_condition": bad_condition, "gateway": gw_dep})

    hc_missing = [n for n in ("api-gateway", "ai-service")
                  if not (services.get(n) or {}).get("healthcheck")]
    hosts = {}
    for name, svc in services.items():
        for entry in svc.get("ports") or []:
            text = re.sub(r"\$\{[^}]*:-([^}]+)\}", r"\1", str(entry))
            parts = text.split(":")
            if len(parts) < 2:
                continue
            host = parts[-2]
            if host.isdigit():
                hosts.setdefault(host, []).append(name)
    conflicts = {p: ns for p, ns in hosts.items() if len(ns) > 1}
    gw_env = (services.get("api-gateway") or {}).get("environment") or {}
    ai_expose = (services.get("ai-service") or {}).get("expose") or []
    routing_ok = str(gw_env.get("AI_SERVICE_URL", "")).endswith(":8084") and "8084" in map(str, ai_expose)
    check("健康检查齐全、端口无冲突、网关指向 AI 暴露端口",
          not hc_missing and not conflicts and routing_ok,
          {"healthcheck_missing": hc_missing, "ports": hosts, "conflicts": conflicts,
           "ai_url": gw_env.get("AI_SERVICE_URL"), "ai_expose": ai_expose})


def _import_gateway(extra_env):
    env = os.environ.copy()
    env.update(extra_env)
    return subprocess.run([sys.executable, "-c", "import app"], cwd=str(ROOT / "api-gateway"),
                          env=env, capture_output=True, text=True, timeout=90)


def check_startup_guards(check):
    """启动门控：不安全配置必须在导入阶段拒绝启动。"""
    empty = Path(TMP) / "users_empty.json"
    empty.write_text("{}", encoding="utf-8")
    strong = "ci-strong-secret-0123456789abcdef0123456789abcdef"
    r1 = _import_gateway({"AUTH_REQUIRED": "true", "JWT_SECRET": strong,
                          "AUTH_USERS_FILE": str(empty)})
    check("空账号表 + AUTH_REQUIRED → 拒绝启动",
          r1.returncode != 0 and "账号表为空" in (r1.stderr or ""),
          {"rc": r1.returncode, "stderr": (r1.stderr or "")[-160:]})
    r2 = _import_gateway({"AUTH_REQUIRED": "true", "JWT_SECRET": "secret",
                          "AUTH_USERS_FILE": str(USERS_FILE)})
    check("默认 JWT_SECRET + AUTH_REQUIRED → 拒绝启动",
          r2.returncode != 0 and "JWT_SECRET 未配置" in (r2.stderr or ""),
          {"rc": r2.returncode, "stderr": (r2.stderr or "")[-160:]})


if __name__ == "__main__":
    raise SystemExit(main())
