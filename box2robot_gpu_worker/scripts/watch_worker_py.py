#!/usr/bin/env python3
"""监控 worker.py md5/mtime 变化 — 抓覆盖事件凶手.

启动后无限循环, md5 变化时记录:
- 新 md5
- 时间戳
- 当前 ps 进程列表 (找出谁可能在覆盖)

日志: /root/worker_py_watch.log
"""
import hashlib
import subprocess
import time
from pathlib import Path

WORKER_PY = "/root/autodl-tmp/workspace/box2robot/box2robot_gpu_worker/box2robot_gpu_worker/worker.py"
LOG = "/root/worker_py_watch.log"


def md5_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        return f"ERROR:{e}"


def write_log(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")
        f.flush()


def get_ps():
    try:
        r = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception as e:
        return f"ps err: {e}"


def main():
    init_md5 = md5_file(WORKER_PY)
    write_log(f"=== START_WATCH {time.strftime('%Y-%m-%d %H:%M:%S')} init_md5={init_md5} ===")

    last_md5 = init_md5
    while True:
        try:
            cur = md5_file(WORKER_PY)
            if cur != last_md5:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                write_log(f"\n[CHANGED] {ts} prev={last_md5} new={cur}")
                # ps 看谁在跑 + lsof 看谁打开了 worker.py
                write_log("--- ps -ef (snapshot) ---")
                write_log(get_ps())
                try:
                    r = subprocess.run(["lsof", WORKER_PY], capture_output=True, text=True, timeout=5)
                    write_log(f"--- lsof {WORKER_PY} ---\n{r.stdout}")
                except Exception:
                    pass
                last_md5 = cur
        except Exception as e:
            write_log(f"[ERR] {e}")
        time.sleep(2)


if __name__ == "__main__":
    main()
