#!/usr/bin/env python3
"""LingBot VLA worker 集成冒烟测试 — 不跑真训练, 只验证:
  1. trainer 函数能 import (b2r env 主进程能加载)
  2. b2r-vla env preflight 通过
  3. robot_config / train_config yaml 生成
  4. lingbot-vla 自己能解析生成的 yaml (schema 一致)

用法: 在装了 b2r + b2r-vla 的 AutoDL 实例上跑:
  /root/miniconda3/envs/b2r/bin/python /root/smoke_test_lingbot_vla.py
"""
from __future__ import annotations
import os
import sys
import subprocess
import traceback
from pathlib import Path

def step(name):
    print(f"\n=== {name} ===", flush=True)

def fail(msg):
    print(f"FAIL: {msg}", flush=True)
    sys.exit(1)

# -------- [1/4] worker import 链路 --------
step("[1/4] worker import 链路")
try:
    from box2robot_gpu_worker.worker import TrainingWorker
    from box2robot_gpu_worker.lingbot_vla_trainer import (
        train_lingbot_vla, _preflight_check,
        _write_robot_config, _write_train_config, _patch_dataset_codebase_version,
    )
    print(f"  OK: trainer 所有函数可 import")
    print(f"  has TrainingWorker._train_lingbot_vla = {hasattr(TrainingWorker, '_train_lingbot_vla')}")
except Exception as e:
    fail(f"import 失败: {e}\n{traceback.format_exc()}")

# -------- [2/4] preflight (跨 env 验证) --------
step("[2/4] preflight (b2r → b2r-vla env)")
try:
    _preflight_check()
    print("  OK: b2r-vla env import (lingbotvla + torch + lerobot + cuda) 全部通过")
except Exception as e:
    fail(f"preflight 失败: {e}")

# -------- [3/4] yaml 生成 dry-run --------
step("[3/4] robot_config + train_config yaml 生成")
test_dir = Path("/tmp/smoke_test_lingbot")
test_dir.mkdir(parents=True, exist_ok=True)
rc_path = test_dir / "robot_config.yaml"
tc_path = test_dir / "train_config.yaml"
ds_dir = test_dir / "fake_dataset"
out_dir = test_dir / "output"
out_dir.mkdir(exist_ok=True)

_write_robot_config(rc_path, n_servos=6)
_write_train_config(
    tc_path, str(ds_dir), rc_path, out_dir,
    train_steps=10, batch_size=2,
    custom_params={
        "task": "pick up cube",
        "peft_enable": True,
        "lora_rank": 16,
        "norm_type": "bounds_99_woclip",
        "freeze_vision_encoder": False,
    },
    n_servos=6,
)
print(f"  ✓ {rc_path} 生成 ({rc_path.stat().st_size} bytes)")
print(f"  ✓ {tc_path} 生成 ({tc_path.stat().st_size} bytes)")
print(f"\n--- robot_config.yaml ---")
print(rc_path.read_text())
print(f"--- train_config.yaml (top 30 lines) ---")
print("\n".join(tc_path.read_text().splitlines()[:30]))

# -------- [4/4] lingbot-vla 解析生成的 yaml (verify schema) --------
step("[4/4] lingbot-vla 解析 yaml schema")
lvla_py = "/root/miniconda3/envs/b2r-vla/bin/python"
lvla_repo = "/root/autodl-tmp/workspace/box2robot/lingbot-vla"
verify_script = f"""
import sys
sys.path.insert(0, '{lvla_repo}')
from dataclasses import dataclass, field
from lingbotvla.utils.arguments import parse_args, ModelArguments, DataArguments, TrainingArguments
@dataclass
class Args:
    model: ModelArguments = field(default_factory=ModelArguments)
    data: DataArguments = field(default_factory=DataArguments)
    train: TrainingArguments = field(default_factory=TrainingArguments)
sys.argv = ['smoke', '{tc_path}']
args = parse_args(Args)
print('SCHEMA_OK')
print(f'  model.model_path = {{args.model.model_path}}')
print(f'  model.post_training = {{args.model.post_training}}')
print(f'  data.train_path = {{args.data.train_path}}')
print(f'  data.norm_type = {{args.data.norm_type}}')
print(f'  train.max_steps = {{args.train.max_steps}}')
print(f'  train.micro_batch_size = {{args.train.micro_batch_size}}')
print(f'  train.lr = {{args.train.lr}}')
print(f'  train.lr_decay_style = {{args.train.lr_decay_style}}')
print(f'  train.data_parallel_mode = {{args.train.data_parallel_mode}}')
print(f'  train.freeze_vision_encoder = {{args.train.freeze_vision_encoder}}')
"""
result = subprocess.run(
    [lvla_py, "-c", verify_script],
    capture_output=True, text=True, timeout=60,
)
if result.returncode != 0:
    print(f"  stderr: {result.stderr[:1500]}")
    print(f"  stdout: {result.stdout[:1500]}")
    fail(f"lingbot-vla parse_args 返回 {result.returncode}")
print(result.stdout)

print("\n=== SMOKE TEST PASSED ===\n")
