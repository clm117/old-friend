"""启动器：fork 两个子进程，自己不退出（确保服务不被杀）
跑法: python start_both.py
然后另开 terminal 跑 python run_tests.py --suite all --restart
"""
import subprocess, sys, os, time

ROOT = r"d:\ProgramFile\语伴"
PY = r"D:\Program Files (x86)\Python 3.11\python.exe"
LOG_DIR = os.path.join(ROOT, "logs")   # 日志统一落在 logs/，不污染仓库根目录
os.makedirs(LOG_DIR, exist_ok=True)

ai = subprocess.Popen(
    [PY, "-u", "main.py"], cwd=os.path.join(ROOT, "ai-service"),
    stdout=open(os.path.join(LOG_DIR, "ai_out.log"), "w"),
    stderr=open(os.path.join(LOG_DIR, "ai_err.log"), "w"),
)
gw = subprocess.Popen(
    [PY, "-u", "app.py"], cwd=os.path.join(ROOT, "api-gateway"),
    stdout=open(os.path.join(LOG_DIR, "gw_out.log"), "w"),
    stderr=open(os.path.join(LOG_DIR, "gw_err.log"), "w"),
)
print(f"AI pid={ai.pid}  GW pid={gw.pid}")
# 保持父进程活着
try:
    ai.wait()
    gw.wait()
except KeyboardInterrupt:
    ai.terminate(); gw.terminate()
