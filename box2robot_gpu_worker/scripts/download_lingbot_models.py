#!/usr/bin/env python3
"""下载 LingBot VLA 训练所需 base 模型到 autodl-fs 共享盘.

为什么放 autodl-fs:
  - autodl-fs 是 region 内跨实例持久化的共享文件系统 (NFS)
  - 同 region 任何 AutoDL 实例的 HF_HOME 指向这里 → 不用每个实例重复下载
  - lingbot-vla-4b (~8GB) + Qwen2.5-VL-3B-Instruct (~7GB) 一次下完, 跨实例复用

用法:
  /root/miniconda3/envs/b2r-vla/bin/python /root/download_lingbot_models.py

环境要求:
  - b2r-vla env (huggingface_hub 已装)
  - autodl-fs 已挂载 (/root/autodl-fs/data/)
  - 网络: 走 hf-mirror + 学术加速

日志:
  - stdout 实时打 progress (run with nohup ... > /root/download_lvla.log 2>&1 &)
"""
from __future__ import annotations
import os
import sys
import time

# 0. Daemonize (双 fork, 脱离 SSH session) — 用 --daemon 标志触发
#    SSH paramiko 关 channel 时杀所有子进程, 普通 nohup/setsid/( & ) 都被收割.
#    双 fork + setsid 让进程被 PID 1 (init) 接管, 不再属于 SSH 进程树.
if "--daemon" in sys.argv:
    # 第一次 fork
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    # 第二次 fork (脱离 controlling terminal)
    if os.fork() > 0:
        sys.exit(0)
    # 重定向 stdio 到 log 文件
    sys.stdin = open(os.devnull, "r")
    log_path = "/root/download_lvla.log"
    log_fd = open(log_path, "a", buffering=1)
    os.dup2(log_fd.fileno(), sys.stdout.fileno())
    os.dup2(log_fd.fileno(), sys.stderr.fileno())
    # 写 PID 文件
    with open("/root/download_lvla.pid", "w") as f:
        f.write(str(os.getpid()))

# 1. HF env 必须在 import huggingface_hub 之前设
os.environ["HF_HOME"] = "/root/autodl-fs/data/box2robot-base-models"
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_XET"] = "1"      # 国内 xet/CAS 鉴权失败, 强制走传统 LFS
# 强制禁用 hf_transfer (装了的话 huggingface_hub 自动用, 但跟 hf-mirror + AutoDL 学术加速 proxy 不兼容必失败).
# pop 不够, 必须显式 = "0" 才能 override huggingface_hub 的 auto-detect.
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"

os.makedirs(os.environ["HF_HOME"], exist_ok=True)

# 2. 模型清单 (按依赖顺序: 先 Qwen base 后 lingbot 4B)
MODELS = [
    ("Qwen/Qwen2.5-VL-3B-Instruct", "~7GB", "VLM backbone (lingbot-vla 强依赖)"),
    ("robbyant/lingbot-vla-4b",     "~8GB", "LingBot VLA 4B 主模型权重"),
]

print(f"[INIT] HF_HOME={os.environ['HF_HOME']}", flush=True)
print(f"[INIT] HF_ENDPOINT={os.environ['HF_ENDPOINT']}", flush=True)

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[FAIL] huggingface_hub 未装. 这个脚本必须在 b2r-vla env 里跑:", flush=True)
    print("  /root/miniconda3/envs/b2r-vla/bin/python /root/download_lingbot_models.py", flush=True)
    sys.exit(1)

# 3. 逐个下载 (失败 retry 2 次)
total_start = time.time()
for idx, (repo_id, size, desc) in enumerate(MODELS, 1):
    print(f"\n[{idx}/{len(MODELS)}] {repo_id} ({size}) — {desc}", flush=True)
    print(f"  开始时间: {time.strftime('%H:%M:%S')}", flush=True)
    t0 = time.time()
    for attempt in range(3):
        try:
            local_path = snapshot_download(
                repo_id=repo_id,
                cache_dir=None,         # 默认走 HF_HOME
                resume_download=True,   # 断点续传
                max_workers=4,
            )
            elapsed = time.time() - t0
            print(f"  ✓ 完成: {local_path} (耗时 {elapsed/60:.1f} 分钟)", flush=True)
            break
        except Exception as e:
            print(f"  ⚠ attempt {attempt+1}/3 失败: {type(e).__name__}: {str(e)[:200]}", flush=True)
            if attempt == 2:
                print(f"  ✗ 重试 3 次仍失败, 中止", flush=True)
                sys.exit(2)
            print(f"  等待 30s 后重试...", flush=True)
            time.sleep(30)

# 4. 列结果 + 占用空间
print(f"\n[DONE] 总耗时 {(time.time()-total_start)/60:.1f} 分钟", flush=True)
import subprocess
result = subprocess.run(
    ["du", "-sh", os.environ["HF_HOME"]],
    capture_output=True, text=True,
)
print(f"[SIZE] {result.stdout.strip()}", flush=True)
print("[SIZE] (跨实例复用: 同 region 的 AutoDL 实例 HF_HOME 指向这里即可)", flush=True)
