# -*- coding: utf-8 -*-
"""
语伴 - 自动化测试统一执行入口

一键完成：环境自检 → 服务自动拉起（缺失时）→ 执行测试套件 → 汇总报告

用法：
  python run_tests.py                # 深度套件（API 分支/临界值/安全攻击面 + UI 全界面）
  python run_tests.py --suite all    # 深度套件 + 基础回归
  python run_tests.py --suite base   # 仅基础回归
  python run_tests.py --no-start     # 不自动拉起服务（要求服务已在运行）
  python run_tests.py --keep         # 测完保留本脚本拉起的服务

环境变量（均可覆盖）：
  YUBAN_PY               带 playwright 的 Python 解释器
  YUBAN_SERVICE_PY       带 uvicorn/fastapi 的服务端 Python 解释器
  YUBAN_API_BASE         API 网关地址，默认 http://127.0.0.1:8005
  YUBAN_UI_BASE          前端地址，默认 http://localhost:8899
  YUBAN_AI_BASE          AI 服务地址，默认 http://127.0.0.1:8084

输出：控制台汇总 + test_outputs/<suite>.log + test_report.json
退出码：0 全部通过；1 存在失败；2 环境准备失败
"""
import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "test_outputs"
API_BASE = os.environ.get("YUBAN_API_BASE", "http://127.0.0.1:8005")
UI_BASE = os.environ.get("YUBAN_UI_BASE", "http://localhost:8899")
AI_BASE = os.environ.get("YUBAN_AI_BASE", "http://127.0.0.1:8084")

DEFAULT_PLAYWRIGHT_PY = r"C:\Users\Administrator\python-sdk\python3.13.2\python.exe"
DEFAULT_SERVICE_PY = r"E:\python\python.exe"
# 网关 JWT 密钥：优先读环境变量；未设置则每次运行生成随机值并注入服务与测试子进程。
# 不在仓库中硬编码真实密钥（公开仓库会导致凭证伪造风险）。
DEFAULT_JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)

SUITES = {
    "deep": {
        "script": ROOT / "ui_test_deep.py",
        "report": ROOT / "ui_test_deep_report.json",
        "desc": "深度套件：API 分支/数据类型/状态/临界值/安全攻击面 + UI 全界面",
    },
    "base": {
        "script": ROOT / "ui_test_suite.py",
        "report": ROOT / "ui_test_report.json",
        "desc": "基础回归：登录 + 7 个标签页按钮/表单/边界值",
    },
}

STARTED = []  # 本脚本拉起的服务进程
LOG_FILES = []


def log(msg):
    print(f"[runner] {msg}", flush=True)


def port_of(url):
    p = urlparse(url)
    return p.hostname or "127.0.0.1", p.port or 80


def load_dotenv(path=ROOT / ".env"):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def py_probe(py, code, timeout=90):
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def find_python(env_key, probe_code, candidates):
    cands = [os.environ.get(env_key)] + candidates
    for c in cands:
        if not c:
            continue
        exe = c if os.path.isabs(c) else c
        if os.path.isabs(exe) and not Path(exe).exists():
            continue
        if py_probe(exe, probe_code):
            return exe
    return None


def wait_port(host, port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (urllib.error.URLError, OSError):
        return False


def wait_http(url, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if http_ok(url):
            return True
        time.sleep(1)
    return False


def start_service(name, args, cwd, env, log_path):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    f = open(log_path, "w", encoding="utf-8")
    LOG_FILES.append(f)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, cwd=str(cwd), env=env,
                            stdout=f, stderr=subprocess.STDOUT, **kwargs)
    STARTED.append((name, proc))
    log(f"已启动 {name}（pid={proc.pid}，日志 {log_path}）")
    return proc


def stop_service(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=8)
    except Exception:
        pass


def child_env(base_env):
    env = os.environ.copy()
    env.update(base_env)
    return env


# ============================================================
# 服务定义
# ============================================================
def service_specs(service_py):
    dotenv = load_dotenv()
    gateway_env = {
        # 测试期打开鉴权，并放宽限流避免 429 干扰用例
        "AUTH_REQUIRED": "true",
        "JWT_SECRET": os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET),
        "LOGIN_RATE_LIMIT_PER_MINUTE": "300",
        "RATE_LIMIT_PER_MINUTE": "600",
        "EXPENSIVE_RATE_LIMIT_PER_MINUTE": "120",
        "AI_SERVICE_URL": AI_BASE,
        "AI_SERVICE_WS_URL": AI_BASE.replace("http", "ws", 1),
        "AI_INTERNAL_TOKEN": os.environ.get("AI_INTERNAL_TOKEN",
                                            os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)),
    }
    ai_env = {
        "TTS_PROVIDER": os.environ.get("TTS_PROVIDER", dotenv.get("TTS_PROVIDER", "cloud")),
        "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", dotenv.get("LLM_PROVIDER", "qwen")),
        "LLM_MODEL": os.environ.get("LLM_MODEL", dotenv.get("LLM_MODEL", "qwen-plus")),
        "LLM_API_KEY": os.environ.get("LLM_API_KEY", dotenv.get("LLM_API_KEY", "")),
        "AI_INTERNAL_TOKEN": os.environ.get("AI_INTERNAL_TOKEN",
                                            os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)),
        # 测试数据隔离：不污染 ai-service/data 中的真实角色与会话
        "YUBAN_DATA_DIR": str(LOG_DIR / "test_data"),
        "YUBAN_STATE_FILE": str(LOG_DIR / "test_data" / "state.json"),
        "REDIS_URL": "",
    }
    host_ui, port_ui = port_of(UI_BASE)
    host_api, port_api = port_of(API_BASE)
    host_ai, port_ai = port_of(AI_BASE)
    return [
        # 先启动 AI 服务：网关 /health 会探测上游，AI 未就绪时网关会返回 503
        {
            "name": "ai-service",
            "host": host_ai, "port": port_ai,
            "args": [service_py, "main.py"], "cwd": ROOT / "ai-service",
            "env": ai_env, "log": LOG_DIR / "service_ai.log",
            # 用 /health 判存活；/ready 会因云依赖（额度/模型）不可用而 503，属预期降级
            "health": AI_BASE.rstrip("/") + "/health",
            # torch/模型冷启动可达 60s+，就绪等待单独放宽，避免卡边界误判
            "ready_timeout": int(os.environ.get("YUBAN_AI_READY_SECONDS", "240")),
        },
        {
            "name": "api-gateway",
            "host": host_api, "port": port_api,
            "args": [service_py, "app.py"], "cwd": ROOT / "api-gateway",
            "env": gateway_env, "log": LOG_DIR / "service_gateway.log",
            # 网关自身存活即可启动测试；/health 的上游探测由用例单独验证
            "health": API_BASE.rstrip("/") + "/",
        },
        {
            "name": "frontend-static",
            "host": host_ui, "port": port_ui,
            "args": [service_py, "-m", "http.server", str(port_ui), "--directory", str(ROOT / "frontend")],
            "cwd": ROOT, "env": {}, "log": LOG_DIR / "service_frontend.log",
            "health": UI_BASE.rstrip("/") + "/",
        },
    ]


def pids_on_port(port):
    """Windows：从 netstat 解析监听指定端口的 PID（用于 --restart）"""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore").stdout
    except Exception:
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            if parts[4].isdigit():
                pids.add(int(parts[4]))
    return pids


def stop_existing_services(specs):
    """--restart 时停掉占用测试端口的旧进程，避免复用旧代码/旧限流桶"""
    for spec in specs:
        pids = pids_on_port(spec["port"])
        if not pids:
            continue
        for pid in pids:
            log(f"停止旧进程 {spec['name']} pid={pid}")
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            except Exception:
                pass
    time.sleep(2)


def prepare_services(service_py, allow_start=True):
    ok = True
    for spec in service_specs(service_py):
        if wait_port(spec["host"], spec["port"], timeout=1):
            log(f"{spec['name']} 已在 {spec['host']}:{spec['port']} 运行，复用")
            continue
        if not allow_start:
            log(f"[环境] {spec['name']} 未运行且未开启自动拉起（--no-start）")
            ok = False
            continue
        start_service(spec["name"], spec["args"], spec["cwd"], child_env(spec["env"]), spec["log"])
        ready_timeout = spec.get("ready_timeout", 60)
        if not wait_port(spec["host"], spec["port"], timeout=ready_timeout):
            log(f"[环境] {spec['name']} 端口 {spec['port']} 未就绪（等待 {ready_timeout}s），请查看 {spec['log']}")
            ok = False
            continue
        if not wait_http(spec["health"], timeout=max(120, ready_timeout)):
            log(f"[环境] {spec['name']} 健康检查失败：{spec['health']}，请查看 {spec['log']}")
            ok = False
    return ok


# ============================================================
# 套件执行与报告
# ============================================================
def run_suite(key, test_py, service_py):
    spec = SUITES[key]
    script = spec["script"]
    if not script.exists():
        log(f"[套件] 找不到 {script}")
        return {"suite": key, "desc": spec["desc"], "ok": False, "stdout": "", "report": None}

    report_path = spec["report"]
    if report_path.exists():
        report_path.unlink()

    env = child_env({"YUBAN_API_BASE": API_BASE, "YUBAN_UI_BASE": UI_BASE,
                     "JWT_SECRET": os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET),
                     "YUBAN_SERVICE_PY": service_py,
                     "PYTHONIOENCODING": "utf-8"})
    log(f"[套件] 运行 {key}: {script.name}")
    t0 = time.time()
    proc = subprocess.run([test_py, str(script)], cwd=str(ROOT), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    out = proc.stdout or ""
    elapsed = round(time.time() - t0, 1)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{key}_run.log"
    log_path.write_text(out, encoding="utf-8")
    print(out, flush=True)

    report = None
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"[套件] 报告解析失败：{e}")

    summary = (report or {}).get("summary", {})
    log(f"[套件] {key} 结束：rc={proc.returncode} 耗时={elapsed}s "
        f"通过={summary.get('passed')} 失败={summary.get('failed')}")
    return {
        "suite": key, "desc": spec["desc"], "ok": proc.returncode == 0 and bool(report),
        "returncode": proc.returncode, "elapsed_s": elapsed,
        "summary": summary, "report": report, "log": str(log_path),
    }


def norm_results(report):
    """把不同套件的用例结果归一化为 {id, case, status}"""
    items = []
    for r in (report or {}).get("results", []):
        items.append({
            "id": r.get("id") or r.get("name") or "",
            "area": r.get("area", ""),
            "case": r.get("case") or r.get("name") or "",
            "status": r.get("status", "UNKNOWN"),
            "detail": r.get("detail", ""),
        })
    return items


def write_report(runs, env_info):
    suites = {}
    failures = []
    total = passed = failed = blocked = 0
    for run in runs:
        items = norm_results(run["report"])
        suites[run["suite"]] = {
            "desc": run["desc"],
            "elapsed_s": run["elapsed_s"],
            "summary": run["summary"] or {"total": len(items)},
            "results": items,
            "log": run["log"],
        }
        for it in items:
            total += 1
            if it["status"] == "PASS":
                passed += 1
            elif it["status"] == "BLOCKED":
                blocked += 1
            else:
                failed += 1
                failures.append({"suite": run["suite"], **it})

    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "environment": env_info,
        "summary": {"total": total, "passed": passed, "failed": failed, "blocked": blocked},
        "suites": suites,
        "failures": failures,
    }
    out = ROOT / "test_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, out


def main():
    ap = argparse.ArgumentParser(description="语伴自动化测试统一执行入口")
    ap.add_argument("--suite", choices=["deep", "base", "all"], default="deep",
                    help="要执行的套件（默认 deep）")
    ap.add_argument("--no-start", action="store_true", help="不自动拉起服务")
    ap.add_argument("--keep", action="store_true", help="测完保留本脚本拉起的服务")
    ap.add_argument("--restart", action="store_true",
                    help="先停止占用测试端口的旧服务，确保加载最新代码（默认复用已运行服务）")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    selected = ["deep", "base"] if args.suite == "all" else [args.suite]

    test_py = find_python("YUBAN_PY",
                          "import playwright.sync_api",
                          [sys.executable, DEFAULT_PLAYWRIGHT_PY, "python", "py"])
    if not test_py:
        log("未找到带 playwright 的 Python 解释器。请先 pip install playwright && playwright install chromium，"
            "或用 YUBAN_PY 指定解释器路径。")
        return 2
    log(f"测试解释器：{test_py}")

    service_py = find_python("YUBAN_SERVICE_PY",
                             "import uvicorn, fastapi, requests, jwt",
                             [DEFAULT_SERVICE_PY, sys.executable, "python"])
    if not service_py:
        log("未找到带 uvicorn/fastapi 的 Python 解释器，无法自动拉起服务。"
            "可用 --no-start 并对已运行的服务测试，或用 YUBAN_SERVICE_PY 指定。")
        if not args.no_start:
            return 2
        service_py = sys.executable
    log(f"服务解释器：{service_py}")

    runs = []
    try:
        if args.restart and not args.no_start:
            stop_existing_services(service_specs(service_py))
        if not prepare_services(service_py, allow_start=not args.no_start):
            log("环境未就绪，终止测试。")
            return 2

        for key in selected:
            runs.append(run_suite(key, test_py, service_py))
    finally:
        if STARTED and not args.keep:
            for name, proc in STARTED:
                stop_service(proc)
                log(f"已停止 {name}")
        for f in LOG_FILES:
            try:
                f.close()
            except Exception:
                pass

    report, out = write_report(runs, {
        "api_base": API_BASE, "ui_base": UI_BASE, "ai_base": AI_BASE,
        "test_python": test_py, "service_python": service_py,
        "services_started": [n for n, _ in STARTED],
    })

    s = report["summary"]
    print("\n" + "=" * 72)
    print(f"自动化测试汇总：总计 {s['total']}  通过 {s['passed']}  失败 {s['failed']}  阻塞 {s['blocked']}")
    for key, info in report["suites"].items():
        sub = info["summary"]
        print(f"  - {key}: 通过 {sub.get('passed', 0)} / 失败 {sub.get('failed', 0)}（{info['elapsed_s']}s）")
    if report["failures"]:
        print("-" * 72)
        print("失败用例：")
        for f in report["failures"]:
            print(f"  [{f['suite']}] {f['id']} {f['case']} | {f['detail']}")
    print("=" * 72)
    print(f"报告：{out}")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
