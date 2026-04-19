# Box2Robot GPU Worker

GPU 算力节点 — 连接 Box2Robot 服务器，自动领取训练/推理任务。

所有操作（数据集选择、训练提交、推理部署）均在 APP 或服务器端完成，GPU Worker 只需安装、启动、绑定。

## 安装

```bash
cd box2robot_gpu_worker

# 1. 安装 LeRobot
cd lerobot && pip install -e ".[act]" && cd ..

# 2. 安装 GPU Worker
pip install -e .
```

## 启动

```bash
b2r-gpu --server https://robot.box2ai.com
```

首次启动会显示 6 位绑定码：

```
==================================================
  Box2Robot GPU Worker v0.1.0
  Server: https://robot.box2ai.com
  GPU: NVIDIA GeForce RTX 4090
  VRAM: 24.0 GB
==================================================

==================================================
  绑定码: A3F82K
  设备ID: GPU-XXXXXXXXXXXX

  请在 APP 中输入绑定码完成绑定
  (等待绑定中...)
==================================================
```

## 绑定

1. 打开 APP → GPU 配置页
2. 输入 Worker 显示的 6 位绑定码
3. 绑定成功后 Worker 自动进入待命状态

绑定完成后，Worker 自动：
- 每 10s 发送心跳（GPU 利用率、显存、磁盘）
- 每 5s 轮询待处理任务
- 收到任务后自动下载数据集、训练、上报进度
- 支持远程升级

## 后续操作

绑定后的所有操作都在 APP / 服务器端完成：

| 操作 | 在哪做 |
|------|--------|
| 选择数据集、提交训练 | APP 云端训练页 |
| 查看训练进度、Loss 曲线 | APP 训练监控页 |
| 选择 Checkpoint、部署推理 | APP 推理执行页 |
| 停止训练 / 停止推理 | APP 对应页面 |

## 支持的模型

| 模型 | 说明 | GPU 需求 |
|------|------|----------|
| MLP | 快速验证，纯 PyTorch | CPU 即可 |
| ACT | Action Chunking Transformer，推荐 | RTX 3060+ |
| Diffusion Policy | 生成式策略 | RTX 3090+ |

## 依赖

- Python >= 3.10
- PyTorch >= 2.2 (CUDA)
- httpx
- LeRobot (ACT 训练)
