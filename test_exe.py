# -*- coding: utf-8 -*-
"""端到端测试: 真实运行 dist/WaterReminder.exe, 验证进程存活 / 托盘 / 定时弹窗, 并截图"""

import json
import os
import subprocess
import sys
import time

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "WaterReminder.exe")
SHOTS = os.path.join(os.environ.get("TEMP", "."), "wr_shots_exe")
os.makedirs(SHOTS, exist_ok=True)

CFG_DIR = os.path.join(os.environ.get("APPDATA", "."), "WaterReminder")
CFG = os.path.join(CFG_DIR, "config.json")


def log(msg):
    print("[exe-test]", msg, flush=True)


# --- 备份现有配置 ---
backup = None
if os.path.exists(CFG):
    with open(CFG, "r", encoding="utf-8") as fp:
        backup = fp.read()
    log("backed up existing config")

os.makedirs(CFG_DIR, exist_ok=True)
with open(CFG, "w", encoding="utf-8") as fp:
    json.dump({"interval": 1, "welcomed": False}, fp)
log("config set to interval=1min")

screen_w = 1920
screen_h = 1080

proc = subprocess.Popen([EXE], cwd=HERE)
log("launched pid=%d" % proc.pid)

time.sleep(12)
log("alive@12s = %s (rc=%s)" % (proc.poll() is None, proc.poll()))
shot1 = os.path.join(SHOTS, "tray_area.png")
ImageGrab.grab(bbox=(screen_w - 460, screen_h - 120, screen_w, screen_h)).save(shot1)
log("saved " + shot1)

log("waiting for the 1-minute reminder ...")
time.sleep(66)
log("alive@78s = %s (rc=%s)" % (proc.poll() is None, proc.poll()))
shot2 = os.path.join(SHOTS, "popup_area.png")
ImageGrab.grab(bbox=(screen_w - 420, screen_h - 320, screen_w, screen_h)).save(shot2)
log("saved " + shot2)

# --- 收尾 ---
proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()
log("exe terminated")

if backup is not None:
    with open(CFG, "w", encoding="utf-8") as fp:
        fp.write(backup)
    log("config restored")
else:
    os.remove(CFG)
    log("config removed (was not present before)")
