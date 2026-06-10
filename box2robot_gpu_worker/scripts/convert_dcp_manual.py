#!/usr/bin/env python3
"""Manual dcp → safetensors converter — 给已训练好的 lingbot_vla job 补转换.

用法 (在 803 上跑, b2r env):
  /root/miniconda3/envs/b2r/bin/python /root/convert_dcp_manual.py \\
      /autodl-fs/data/box2robot-outputs/pool-default/<job_id>/model/output \\
      /autodl-fs/data/box2robot-outputs/pool-default/<job_id>/model/deploy_model

输出: deploy_dir 含 model.safetensors + config.json + lingbotvla_cli.yaml + norm_stats.json
"""
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} <output_dir> <deploy_dir>", file=sys.stderr)
    sys.exit(1)

output_dir = Path(sys.argv[1])
deploy_dir = Path(sys.argv[2])

print(f"[CONVERT] output_dir={output_dir}", flush=True)
print(f"[CONVERT] deploy_dir={deploy_dir}", flush=True)

# 走 b2r env 主进程, 用 trainer 里的转换函数 (它内部会 subprocess 调 b2r-vla env)
sys.path.insert(0, "/root/autodl-tmp/workspace/box2robot/box2robot_gpu_worker")
from box2robot_gpu_worker.lingbot_vla_trainer import _convert_dcp_to_safetensors

_convert_dcp_to_safetensors(output_dir, deploy_dir)
print("[CONVERT] DONE", flush=True)
