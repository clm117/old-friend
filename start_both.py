"""启动器：fork 两个子进程，自己不退出（确保服务不被杀）
跑法: python start_both.py
然后另开 terminal 跑 run_all_tests.py
"""
import subprocess, sys, os, time

ROOT = r"d:\ProgramFile\语伴"
PY = r"D:\Program Files (x86)\Python 3.11\python.exe"

ai = subprocess.Popen(
    [PY, "-u", "main.py"], cwd=os.path.join(ROOT, "ai-service"),
    stdout=open(os.path.join(ROOT, "ai_out.log"), "w"),
    stderr=open(os.path.join(ROOT, "ai_err.log"), "w"),
)
gw = subprocess.Popen(
    [PY, "-u", "app.py"], cwd=os.path.join(ROOT, "api-gateway"),
    stdout=open(os.path.join(ROOT, "gw_out.log"), "w"),
    stderr=open(os.path.join(ROOT, "gw_err.log"), "w"),
)
print(f"AI pid={ai.pid}  GW pid={gw.pid}")
# 保持父进程活着
try:
    ai.wait()
    gw.wait()
except KeyboardInterrupt:
    ai.terminate(); gw.terminate()
