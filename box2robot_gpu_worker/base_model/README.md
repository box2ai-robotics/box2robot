# base_model — VLA 基础模型缓存

Worker 启动 (`b2r-gpu`) 时自动把 `HF_HOME` 设到这个目录，
HuggingFace 下载的 base ckpt 都缓存在这里，训练 + 推理共享。

## 目录结构 (HuggingFace 标准 cache)

```
base_model/
├── hub/
│   ├── models--lerobot--pi05_base/
│   │   ├── refs/main                          # 当前 commit sha
│   │   ├── blobs/                             # 实际权重文件
│   │   └── snapshots/<sha>/
│   │       ├── config.json
│   │       └── model.safetensors              # ← 加载时读这个
│   ├── models--lerobot--pi0_base/
│   ├── models--lerobot--smolvla_base/
│   └── models--lerobot--pi0_fast_base/
└── README.md (本文件)
```

## 各模型预计占用

| 模型 | repo | 大小 |
|------|------|------|
| Pi 0.5 base | `lerobot/pi05_base` | ~14 GB |
| Pi 0 base | `lerobot/pi0_base` | ~6 GB |
| Pi 0 Fast base | `lerobot/pi0_fast_base` | ~6 GB |
| SmolVLA base | `lerobot/smolvla_base` | ~2 GB |
| GR00T N1.5 | `nvidia/GR00T-N1.5-3B` | ~6 GB |

总盘空间预留 **35GB+**。

## 第一次下载 (国内网络)

```bash
# 配镜像 (推荐)
export HF_ENDPOINT=https://hf-mirror.com

# 或登录 HF (gated 模型需要)
huggingface-cli login

# 然后 worker 触发训练或推理时自动下载
b2r-gpu --server https://robot.box2ai.com
```

## 从老 cache 迁移 (避免重复下载)

如果之前下载在 `~/.cache/huggingface/hub` 或 `/root/autodl-tmp/.cache/hub`：

```bash
# 软链接 (省空间, 推荐)
ln -sfn /root/autodl-tmp/.cache/hub box2robot_gpu_worker/base_model/hub

# 或物理拷贝
cp -r /root/autodl-tmp/.cache/hub box2robot_gpu_worker/base_model/hub
```

## 手动预下载

```bash
# 设 HF_HOME 到本目录
export HF_HOME=$(pwd)/box2robot_gpu_worker/base_model

# 拉具体模型
huggingface-cli download lerobot/pi05_base
huggingface-cli download lerobot/smolvla_base
huggingface-cli download lerobot/pi0_base
```

## 不进 git

`base_model/*` 已在 `box2robot_gpu_worker/.gitignore` 里, 只 README 提交。
