# Box2Robot GPU Worker

GPU 算力节点 — 连接 Box2Robot 服务器，自动领取训练/推理任务。

所有操作（数据集选择、训练提交、推理部署）均在 APP 或服务器端完成，GPU Worker 只需安装、启动、绑定。

## 系统要求

- Python >= 3.12
- NVIDIA GPU (RTX 3060+ 推荐)
- NVIDIA 驱动 >= 525.0 (运行 `nvidia-smi` 确认)
- Windows 11 / Ubuntu 22.04+

## 安装

### 前置：拉取 LeRobot submodule

GPU Worker 依赖 HuggingFace LeRobot 训练框架。本仓库已通过 git submodule 关联到 [huggingface/lerobot](https://github.com/huggingface/lerobot) 并锁定到我们已验证兼容的 commit，**clone 主仓库时一并拉取即可**，无需手动 clone。

**方式 A（推荐）：clone 主仓库时直接带上 submodule**

```bash
git clone --recurse-submodules <Box2Robot 主仓库地址>
```

**方式 B：已经 clone 过主仓库，补拉 submodule**

```bash
cd <Box2Robot 主仓库根目录>
git submodule update --init --recursive
```

完成后 `box2robot_gpu_worker/lerobot/` 会自动检出锁定版本，下面一键脚本和手动步骤都能直接使用。LeRobot 上游有 breaking change 不会影响你的本地代码——只有维护者主动 bump 版本时才会跟进。

> 国内网络拉取慢/超时时，可单独进入 `box2robot_gpu_worker/lerobot/` 把 `origin` 改为镜像地址后 `git fetch` 再 `git checkout` 锁定的 commit。

### Windows (推荐: 一键脚本)

```cmd
cd box2robot_gpu_worker

REM 一键安装 (默认 CUDA 12.4)
scripts\setup_windows.bat

REM 或指定 CUDA 版本
scripts\setup_windows.bat cu128    # CUDA 12.8 (最新驱动)
scripts\setup_windows.bat cu124    # CUDA 12.4 (推荐)
scripts\setup_windows.bat cu118    # CUDA 11.8 (旧驱动)
```

脚本会自动创建 conda 环境 `b2r`，安装 CUDA 版 PyTorch + LeRobot + GPU Worker。
（脚本不会自动拉 submodule，请先按上一节"前置：拉取 LeRobot submodule"完成。）

### Windows (手动安装)

```cmd
REM 1. 创建 conda 环境
conda create -n b2r python=3.12 -y
conda activate b2r

REM 2. 安装 PyTorch (必须指定 CUDA 索引!)
REM    不指定会安装 CPU 版本，GPU 无法使用!
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

REM 3. 验证 GPU
python -c "import torch; print(f'CUDA: {torch.cuda.is_available()}, GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"

REM 4. 安装 LeRobot (前置已通过 submodule 拉取到 lerobot/ 目录)
cd lerobot
pip install -e . --no-build-isolation
cd ..

REM 5. 安装 GPU Worker
pip install -e .
```

### Ubuntu / Linux

```bash
cd box2robot_gpu_worker

# 1. 创建环境
conda create -n b2r python=3.12 -y
conda activate b2r

# 2. 安装 PyTorch (CUDA)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# 3. 安装 LeRobot + GPU Worker (前置已通过 submodule 拉取到 lerobot/ 目录)
cd lerobot && pip install -e . --no-build-isolation && cd ..
pip install -e .
```

## 常见问题: GPU 检测不到

**症状**: `nvidia-smi` 正常，但 `torch.cuda.is_available()` 返回 `False`

**原因**: 安装了 CPU 版本的 PyTorch (这是最常见的问题)

**诊断**:

```bash
conda activate b2r
python scripts/check_gpu.py
```

**修复**:

```bash
# 卸载 CPU 版本
pip uninstall torch torchvision torchaudio -y

# 重新安装 CUDA 版本
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

**CUDA 版本选择指南**:

| 你的驱动版本 (nvidia-smi 右上角) | 推荐 CUDA | 安装命令后缀 |
|---|---|---|
| >= 560 | CUDA 12.8 | `--index-url https://download.pytorch.org/whl/cu128` |
| >= 550 | CUDA 12.4 | `--index-url https://download.pytorch.org/whl/cu124` |
| >= 525 | CUDA 12.1 | `--index-url https://download.pytorch.org/whl/cu121` |
| >= 450 | CUDA 11.8 | `--index-url https://download.pytorch.org/whl/cu118` |

## 常见问题: 安装卡死/死机

**原因**: LeRobot 依赖众多 (~50+ 包)，pip 依赖解析消耗大量内存

**解决**:
1. 使用一键脚本 `setup_windows.bat`，会分步安装避免一次性加载
2. 关闭其他大型程序 (浏览器、游戏等) 释放内存
3. 如果仍然卡死，手动分步安装 (见上方手动安装步骤)

## 启动

```bash
conda activate b2r
b2r-gpu --server https://robot.box2ai.com
```

首次启动会显示 6 位绑定码：

```
==================================================
  Box2Robot GPU Worker v0.6.1
  Server: https://robot.box2ai.com
  GPU: NVIDIA GeForce RTX 4090
  VRAM: 24.0 GB
  CUDA: 12.4
==================================================

==================================================
  绑定码: A3F82K
  设备ID: GPU-XXXXXXXXXXXX

  请在 APP 中输入绑定码完成绑定
  (等待绑定中...)
==================================================
```

## 绑定

1. 打开 APP -> GPU 配置页
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

- Python >= 3.12
- PyTorch >= 2.2 (CUDA)
- httpx
- LeRobot (ACT 训练)
