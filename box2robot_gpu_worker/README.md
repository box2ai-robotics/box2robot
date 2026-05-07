# Box2Robot GPU Worker

GPU 算力节点 — 连接 Box2Robot 服务器，自动领取训练/推理任务。

所有操作（数据集选择、训练提交、推理部署）均在 APP 或服务器端完成，GPU Worker 只需安装、启动、绑定。

## 系统要求

- **Python == 3.12** (强制, lerobot/torchcodec 部分子依赖只发布到 3.10~3.12;
  3.13 及以上目前会有 wheel 缺失或 build 失败问题。conda 环境锁 `python=3.12`)
- NVIDIA GPU (RTX 3060+ 推荐)
- NVIDIA 驱动 >= 525.0 (运行 `nvidia-smi` 确认)
- Windows 11 / Ubuntu 22.04+
- 磁盘空间 >= 30GB (PyTorch ~3GB + LeRobot 依赖 ~5GB + VLA 基础模型缓存 ~10GB)

## 安装

### 前置：获取 LeRobot 源码

GPU Worker 依赖 HuggingFace LeRobot 训练框架，需要将其 clone 到 `box2robot_gpu_worker/lerobot/` 目录下（一键脚本和手动安装都需要）：

```bash
cd box2robot_gpu_worker

# clone 到 lerobot/ 子目录（注意末尾的 lerobot 是目标目录名，不能省略）
git clone https://github.com/huggingface/lerobot.git lerobot

# 可选：锁定到当前已验证的 commit（避免上游 breaking change）
cd lerobot && git checkout cb0a9449 && cd ..
```

> 国内网络 clone 慢/超时时，可改用镜像：`git clone https://gitclone.com/github.com/huggingface/lerobot.git lerobot`

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

脚本会自动:
1. 创建 conda 环境 `b2r` (Python 3.12)
2. 安装 CUDA 版 PyTorch
3. 安装 LeRobot 基础包
4. **安装 `lerobot[dataset]` 数据集依赖 (av/datasets/torchcodec)** —
   pi0/pi05/smolvla 等 VLA 模型必需, 缺会报 `'av' is required but not installed`
5. 安装 Box2Robot GPU Worker
6. 运行 `scripts/check_gpu.py` 做完整依赖体检

(脚本不会自动 clone LeRobot, 请先按上一节 "前置: 获取 LeRobot 源码" 完成 clone。)

### Linux / Ubuntu (一键脚本)

```bash
cd box2robot_gpu_worker
bash scripts/setup_linux.sh             # 默认 CUDA 12.4
bash scripts/setup_linux.sh cu128       # 或指定版本
```

脚本逻辑与 Windows 一致 (6 步, 含 `lerobot[dataset]` 安装和体检)。

### Windows (手动安装)

```cmd
REM 1. 创建 conda 环境 (Python 必须 3.12)
conda create -n b2r python=3.12 -y
conda activate b2r

REM 2. 安装 PyTorch (必须指定 CUDA 索引!)
REM    不指定会安装 CPU 版本，GPU 无法使用!
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

REM 3. 验证 GPU
python -c "import torch; print(f'CUDA: {torch.cuda.is_available()}, GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"N/A\"}')"

REM 4. 安装 LeRobot 基础包 (前置已 clone 到 lerobot/ 目录)
cd lerobot
pip install -e . --no-build-isolation
cd ..

REM 5. 安装 LeRobot[dataset] 数据集依赖 (av/datasets/torchcodec)
REM    pi0/pi05/smolvla 等 VLA 模型必需, 缺会报 'av' is required but not installed
pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation

REM 6. 安装 GPU Worker
pip install -e .

REM 7. 完整依赖体检 (强烈建议)
python scripts\check_gpu.py
```

### Ubuntu / Linux (手动安装)

```bash
cd box2robot_gpu_worker

# 1. 创建环境 (Python 必须 3.12)
conda create -n b2r python=3.12 -y
conda activate b2r

# 2. 安装 PyTorch (CUDA)
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124

# 3. 安装 LeRobot 基础包 (前置已 clone 到 lerobot/ 目录)
cd lerobot && pip install -e . --no-build-isolation && cd ..

# 4. 安装 LeRobot[dataset] 数据集依赖 (av/datasets/torchcodec)
#    pi0/pi05/smolvla 等 VLA 模型必需, 缺会报 'av' is required but not installed
pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation

# 5. 安装 GPU Worker
pip install -e .

# 6. 完整依赖体检
python scripts/check_gpu.py
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

## 启动自检 (每次启动自动跑)

`b2r-gpu` 启动时会做 6 项自检, 输出统一报告:

| 项 | 检查内容 | 失败级别 |
|----|---------|----------|
| 依赖 | numpy/httpx/pyarrow/pyyaml/psutil/lerobot/av | BLOCKED — 任一缺则退出 |
| VLA 依赖 | datasets/transformers/accelerate | WARNING — ACT/MLP 不受影响 |
| GPU | torch.cuda.is_available + 编译 CUDA 版本 | BLOCKED (CPU build) / WARNING (驱动太旧) |
| 磁盘 | output_dir 剩余空间 | BLOCKED <5GB / WARNING <30GB (VLA 装不下) |
| HF Hub | huggingface.co 连通性 | WARNING — 不通则 VLA 下载 base 会失败 |
| Server URL | server 可达性 | BLOCKED — 必须能连 |
| 写入权限 | outputs/datasets/cache/HF cache | BLOCKED — 任一不可写则退出 |

报告示例 (一切正常):

```
============================================================
  Box2Robot GPU Worker — 启动自检
============================================================
  [READY] 全部 6 项检查通过, 可以启动
============================================================
```

报告示例 (有问题):

```
  [WARNING] 1 项可启动但受限:
    - VLA 依赖: transformers 未安装
      修复: pip install transformers accelerate

  [BLOCKED] 1 项致命, worker 无法启动:
    - 磁盘: 剩余空间 3.2GB, 不够基本运行 (要 5GB+)
      修复: 清理磁盘或更换 --output 目录
```

要单独跑完整体检（不启动 worker）:

```bash
conda activate b2r
python scripts/check_gpu.py            # 详细报告 + 修复指令
python scripts/check_gpu.py --strict   # CI 模式, 缺关键依赖返回 exit 1
```

## 常见问题: `'av' is required but not installed`

**症状**: 训练 pi0 / pi05 / smolvla 时报错:

```
'av' is required but not installed.
Install it with: pip install 'lerobot[dataset]'
```

**原因**: `lerobot[dataset]` 这个 extras 没装。`av` (PyAV) 是视频解码器, lerobot
加载图像/视频数据集时必需。

**修复** (任选一种):

```bash
conda activate b2r

# 推荐: 装完整 dataset extras (含 av + datasets + torchcodec + ...)
pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation

# 或最小修复, 只装 av (本仓库 setup.py 已把 av 加入主依赖, 重装 worker 即可)
pip install -e . --upgrade

# 修完跑一遍体检确认
python scripts/check_gpu.py
```

> Worker 启动时会自动做依赖预检 (在 banner 之前), 如果 `av` 缺失会直接报错退出
> 并给出修复指令, 不会等到训练时才崩。

## 常见问题: pi05 训练报 `quantile stats not found`

**症状**: 训练 pi05 (不影响 pi0 / smolvla), normalizer 加载阶段报错.

**原因**: pi05 设计上用 `QUANTILES` normalization (q01/q99 鲁棒缩放, 比 mean/std 抗机器人轨迹的
边界值/校准异常值更好). 但 `LeRobotDataset.create()` 默认只算 mean/std, 不算 q01/q99 stats,
两者不一致 → normalizer 加载时找不到 quantile 字段崩.

**自动修复 (Worker v0.6.2+)**: worker 在训练 pi05 之前自动调用
`augment_dataset_with_quantile_stats()` 给 dataset 补算 q01/q10/q50/q90/q99 stats, 写回
`datasets/<repo>/meta/stats.json`. 训练时 pi05 用它**原生设计的 QUANTILES**, 不改模型.
已经算过的 dataset 会被 `has_quantile_stats()` 短路跳过, 重复训练无开销.

**为什么不切换到 MEAN_STD?**

切 MEAN_STD 能让训练跑起来, 但 base 模型是 QUANTILES 训的, fine-tune 改 normalizer
等于让训练分布跟 base 权重的预期分布脱钩, 收敛慢效果差. 正确做法是按模型的最优数据格式
准备 dataset, 而不是改模型迁就 dataset.

**手动补算** (如果自动失败):

```bash
python box2robot_gpu_worker/lerobot/src/lerobot/scripts/augment_dataset_quantile_stats.py \
    --repo-id=box2robot-<fingerprint> \
    --root=box2robot_gpu_worker/datasets/box2robot-<fingerprint>
```

**用户覆盖** (如果坚持用 MEAN_STD):

```python
# APP 提交训练时, custom_params 里填:
custom_params = {
    "normalization_mapping": '{"ACTION":"MEAN_STD","STATE":"MEAN_STD","VISUAL":"IDENTITY"}'
}
```

## 常见问题: 训练时报 `All image features are missing from the batch`

**症状**: 训练 pi0/pi05/smolvla, 数据集加载 OK, forward 时崩.

**原因**: VLA base 训练时用了不同数据集 (pi05_base=droid 的 `exterior_1_left/wrist_left/...`,
pi0_base=aloha 的 `cam_high/cam_left_wrist/...`), 跟我们的 `observation.images.top` 对不上.

**自动修复 (Worker v0.6.2+)**: worker 启动训练前会自动下载 base 的 `config.json`,
读取 `input_features` 中所有 VISUAL key, 生成 `--rename_map` 把 `observation.images.top`
映射到 base 第一个相机. 其余 base cam 由 `prepare_images` 自动用 -1 padding (siglip empty).

如自动失败 (HF 下载不通 / 自定义 base), 可手动指定:
```python
custom_params = {
    "rename_map": '{"observation.images.top": "observation.images.cam_high"}'
}
```

## 常见问题: 无法访问 huggingface.co

**症状**: 启动自检报 `HF Hub: 无法访问 huggingface.co`, 或下载 base 模型超时.

**修复 (国内网络)**: 设置 HuggingFace 镜像:

```bash
export HF_ENDPOINT=https://hf-mirror.com    # Linux
$env:HF_ENDPOINT="https://hf-mirror.com"   # Windows PowerShell
```

或在 `b2r-gpu` 启动前一次性设置:
```bash
HF_ENDPOINT=https://hf-mirror.com b2r-gpu --server https://robot.box2ai.com
```

## 常见问题: 磁盘空间不足

**症状**: 启动自检报 `磁盘: 剩余空间 X.X GB`.

**预期占用**:
- PyTorch + LeRobot 依赖: ~5GB
- VLA base ckpt 缓存 (HF cache): ~10GB/模型 (pi05_base, pi0_base 等)
- 训练 dataset cache: ~1-5GB (取决于数据量)
- 训练输出 (checkpoints): ~5-20GB/任务

**修复**:
1. 清理 HF 缓存: `rm -rf ~/.cache/huggingface/hub/models--*` (会重新下载)
2. 清理旧训练输出: `rm -rf outputs/<old-job-id>/`
3. 清理 dataset 缓存: `rm -rf box2robot_gpu_worker/cache/ds_*`
4. 改用更大磁盘: `b2r-gpu --output /mnt/large-disk/outputs ...`

## 启动

```bash
conda activate b2r
b2r-gpu --server https://robot.box2ai.com
```

首次启动会显示 6 位绑定码：

```
==================================================
  Box2Robot GPU Worker v0.6.2
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

## 开机自启 (可选)

绑定成功后，建议把 Worker 注册成系统服务，机器重启后自动上线领任务。
跨平台脚本: `scripts/install_autostart.py` (Linux 走 systemd, Windows 走 Task Scheduler)。

> 运行前必须先 `conda activate b2r` (脚本从当前 Python 环境定位 `b2r-gpu` 绝对路径)。

### Linux (systemd)

```bash
conda activate b2r
cd box2robot_gpu_worker

# 安装 (默认用户级, 不需 sudo)
python scripts/install_autostart.py install --server https://robot.box2ai.com

# 国内网络: 顺手注入 HF 镜像
python scripts/install_autostart.py install \
    --env HF_ENDPOINT=https://hf-mirror.com

# 用户级服务在登出后会停, 让它常驻 (机器重启自动起):
sudo loginctl enable-linger $USER

# 或装系统级 (真正系统启动时拉起, 不依赖用户登录)
python scripts/install_autostart.py install --system --server https://robot.box2ai.com
```

管理:

```bash
python scripts/install_autostart.py status     # 查看状态
python scripts/install_autostart.py start      # 立即启动
python scripts/install_autostart.py stop       # 停止
python scripts/install_autostart.py uninstall  # 卸载

# 实时日志
journalctl --user -u box2robot-gpu-worker -f          # 用户级
sudo journalctl -u box2robot-gpu-worker -f            # 系统级
```

unit 文件位置:
- 用户级: `~/.config/systemd/user/box2robot-gpu-worker.service`
- 系统级: `/etc/systemd/system/box2robot-gpu-worker.service`

### Windows (Task Scheduler)

```cmd
conda activate b2r
cd box2robot_gpu_worker

REM 安装 (默认 ONLOGON: 当前用户登录时启动, 无需管理员)
python scripts\install_autostart.py install --server https://robot.box2ai.com

REM 国内网络: 注入 HF 镜像
python scripts\install_autostart.py install ^
    --env HF_ENDPOINT=https://hf-mirror.com

REM 改成系统启动时触发 (需以管理员身份打开终端, 任务以 SYSTEM 账户运行)
python scripts\install_autostart.py install --trigger boot
```

管理:

```cmd
python scripts\install_autostart.py status     REM 查看状态
python scripts\install_autostart.py start      REM 立即触发
python scripts\install_autostart.py stop       REM 停止
python scripts\install_autostart.py uninstall  REM 卸载

REM 实时日志 (PowerShell)
Get-Content logs\gpu_worker_autostart.log -Wait -Tail 50
```

任务名: `Box2RobotGpuWorker`
包装脚本: `scripts/_autostart_wrapper.bat` (脚本自动生成, 含崩溃自动重启循环)
日志文件: `logs/gpu_worker_autostart.log`

> Windows ONLOGON 触发不需要管理员, 适合个人工作站; ONSTART 以 SYSTEM 账户跑, 在某些 GPU 驱动下可能拿不到 CUDA 句柄, 不行就改回 ONLOGON 并开自动登录。

### 常见问题

| 现象 | 排查 |
|------|------|
| `[错误] 找不到 b2r-gpu` | 没在 `b2r` conda 环境里跑脚本, 先 `conda activate b2r` |
| Linux 用户级服务重启后没起 | 没开 lingering: `sudo loginctl enable-linger $USER` |
| Windows 任务能启动但看不到输出 | 看 `logs/gpu_worker_autostart.log`, 不是看终端 |
| 自启 Worker 显示新的绑定码 | Worker 数据目录变了, 用 `--output` 指向之前 bind 时的目录 |
| 想改 server / 参数 | 重新跑 `install --server <new>` 即可 (`/f` 强制覆盖旧任务) |

## 支持的模型

| 模型 | 说明 | GPU 需求 |
|------|------|----------|
| MLP | 快速验证，纯 PyTorch | CPU 即可 |
| ACT | Action Chunking Transformer，推荐 | RTX 3060+ |
| Diffusion Policy | 生成式策略 | RTX 3090+ |

## 依赖

| 类别 | 包 | 必需性 | 备注 |
|------|----|--------|------|
| 运行环境 | Python `==3.12` | 必需 | conda 强制锁定 |
| 深度学习 | torch / torchvision / torchaudio (CUDA build) | 必需 | 装 CPU 版 GPU 不可用 |
| 数据/通信 | numpy, pyarrow, httpx, pyyaml, psutil | 必需 | `setup.py` 自动装 |
| 视频解码 | `av>=15,<16` | 必需 | VLA 模型加载视频帧, 缺则崩溃; `setup.py` 已包含 |
| 训练框架 | lerobot (本地源码 `./lerobot`) | 必需 | 含 ACT/Diffusion/SmolVLA/Pi0 等策略 |
| 数据集扩展 | `lerobot[dataset]` (datasets/torchcodec/jsonlines/...) | VLA 必需 | pi0/pi05/smolvla 训练数据加载 |
| VLA 微调 | transformers, accelerate | VLA 必需 | `pip install -e .[vla]` 安装 |
| 训练加速 (可选) | wandb, accelerate | 可选 | `pip install -e .[train]` |

> **AutoDL 云端实例开关机相关功能已迁移到独立子项目 `box2robot_gpu_cloud_manager/`**
> 那是一个云 GPU 资源调度管理节点，常驻在用户自己的常开机器上，代理一组 AutoDL
> 实例做按需开关机。详见 [`../box2robot_gpu_cloud_manager/README.md`](../box2robot_gpu_cloud_manager/README.md)。

完整依赖体检:

```bash
conda activate b2r
python scripts/check_gpu.py    # 输出 GPU + 所有关键依赖的检测结果与修复指令
```
