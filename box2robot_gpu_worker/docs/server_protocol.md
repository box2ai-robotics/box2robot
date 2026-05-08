# Box2Robot GPU Worker - Server 通信协议文档

本文档定义了 GPU Worker 与 Box2Robot Server 之间的完整通信协议，供自定义开发者参考。

## 目录

- [概述](#概述)
- [基础约定](#基础约定)
- [1. GPU Worker 生命周期](#1-gpu-worker-生命周期)
  - [1.1 设备激活](#11-设备激活-post-apigpuactivate)
  - [1.2 心跳上报](#12-心跳上报-post-apigpuheartbeat)
  - [1.3 任务轮询](#13-任务轮询-get-apigpupoll-job)
  - [1.4 版本升级](#14-版本升级)
- [2. 训练任务生命周期](#2-训练任务生命周期)
  - [2.1 获取任务信息](#21-获取任务信息-get-apitrainingjobsjob_id)
  - [2.2 下载数据集](#22-下载数据集-get-apitrainingjobsjob_iddataset)
  - [2.3 下载图像](#23-下载图像-get-apitrainingjobsjob_idimagestraj_id)
  - [2.4 上报训练进度](#24-上报训练进度-post-apitrainingjobsjob_idprogress)
  - [2.5 上报任务状态](#25-上报任务状态-post-apitrainingjobsjob_idstatus)
- [3. 推理执行生命周期](#3-推理执行生命周期)
  - [3.1 读取舵机状态](#31-读取舵机状态-get-apidevicedevice_idservos)
  - [3.2 读取摄像头帧](#32-读取摄像头帧-get-apicameracamera_idframe)
  - [3.3 发送控制指令](#33-发送控制指令)
  - [3.4 检查推理停止信号](#34-检查推理停止信号-get-apitrainingjobsjob_idcheck-inference)
  - [3.5 摄像头模式切换](#35-摄像头模式切换-post-apicameracamera_idstreammode)
- [4. 数据格式](#4-数据格式)
  - [4.1 轨迹数据结构](#41-轨迹数据结构)
  - [4.2 舵机归一化常量](#42-舵机归一化常量)
  - [4.3 模型配置文件](#43-模型配置文件-b2r_configjson)
- [5. 认证机制](#5-认证机制)
- [6. 错误处理与重试策略](#6-错误处理与重试策略)
- [7. 完整生命周期流程图](#7-完整生命周期流程图)

---

## 概述

GPU Worker 通过 **HTTP/HTTPS** 与 Box2Robot Server 通信 (无 WebSocket)，承担两大职责：

| 职责 | 描述 | 关键接口 |
|------|------|---------|
| **训练** | 下载数据集 → 训练模型 → 上报进度 | `/api/training/jobs/*` |
| **推理** | 读取设备状态 → 模型推理 → 发送控制指令 | `/api/device/*`, `/api/camera/*` |

通信层使用 `httpx` 异步 HTTP 客户端，JSON 格式交互。

---

## 基础约定

| 项目 | 值 |
|------|-----|
| 协议 | HTTP/HTTPS |
| 数据格式 | JSON (`Content-Type: application/json`) |
| 默认 Server URL | `https://robot.box2ai.com` |
| HTTP 客户端超时 | GPU Worker: 30s, Training Worker: 60s |
| 字符编码 | UTF-8 |

### 通用错误响应格式

```json
{
  "error": "错误描述信息"
}
```

| HTTP 状态码 | 含义 |
|------------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 认证失败 |
| 403 | 权限不足 |
| 404 | 资源不存在 |
| 409 | 状态冲突 (任务暂停/取消) |
| 500 | 服务器内部错误 |

---

## 1. GPU Worker 生命周期

### 1.1 设备激活 `POST /api/gpu/activate`

首次启动时注册设备，获取 `device_id` 和绑定码。用户在 APP 中输入绑定码完成绑定后，返回 `token`。

**请求体：**

```json
{
  "gpu_name": "NVIDIA GeForce RTX 4090",
  "vram_gb": 24.0,
  "ram_gb": 32.0,
  "disk_free_gb": 500.0,
  "os": "Windows 11 Pro",
  "cuda_version": "12.1",
  "python_version": "3.10.11",
  "torch_version": "2.2.0",
  "fw_version": "0.6.1"
}
```

**响应 - 需要绑定：**

```json
{
  "status": "need_bind",
  "device_id": "GPU-XXXXXXXXXXXX",
  "bind_code": "A3F82K"
}
```

**响应 - 已激活：**

```json
{
  "status": "activated",
  "device_id": "GPU-XXXXXXXXXXXX",
  "token": "eyJhbGc..."
}
```

**绑定流程：**

1. 首次调用返回 `need_bind` + 6 位绑定码
2. Worker 显示绑定码，等待用户在 APP 中输入
3. 每 **3 秒** 重复调用同一接口轮询绑定状态
4. 超时 **300 秒** (5 分钟) 未绑定则失败
5. 绑定成功后返回 `activated` + `token`

---

### 1.2 心跳上报 `POST /api/gpu/heartbeat`

**频率：** 每 10 秒

**请求体：**

```json
{
  "device_id": "GPU-XXXXXXXXXXXX",
  "token": "eyJhbGc...",
  "fw_version": "0.6.1",
  "gpu_info": {
    "gpu_name": "NVIDIA GeForce RTX 4090",
    "vram_gb": 24.0,
    "ram_gb": 32.0,
    "disk_free_gb": 500.0,
    "os": "Windows 11 Pro",
    "cuda_version": "12.1",
    "python_version": "3.10.11",
    "torch_version": "2.2.0"
  },
  "usage": {
    "cpu_pct": 15.5,
    "ram_used_gb": 8.5,
    "vram_used_gb": 12.0,
    "gpu_pct": 45,
    "disk_free_gb": 500.0
  }
}
```

**响应：**

```json
{
  "status": "ok"
}
```

> Server 会检查心跳间隔，若训练任务 120 秒无进度更新，自动标记为失败。

---

### 1.3 任务轮询 `GET /api/gpu/poll-job`

**频率：** 每 5 秒

**Query 参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `device_id` | string | GPU 设备 ID |
| `token` | string | 认证令牌 |

**响应 - 有训练任务：**

```json
{
  "job": {
    "id": "abc123",
    "model_type": "act",
    "train_steps": 10000,
    "batch_size": 64,
    "chunk_size": 20,
    "pairing_key": "secret-key-xyz",
    "dataset_ids": ["traj1", "traj2"],
    "custom_params": {
      "lr": 0.0001,
      "task": "manipulation task",
      "dtype": "bfloat16"
    },
    "deploy_info": null
  },
  "action": "train",
  "resume_from_step": null
}
```

**响应 - 有推理部署任务：**

```json
{
  "job": {
    "id": "abc123",
    "model_type": "act",
    "pairing_key": "secret-key-xyz",
    "deploy_info": {
      "arm_device_id": "B2R-123456",
      "camera_device_id": "CAM-123456",
      "checkpoint_step": 50000,
      "execution_mode": "original",
      "chunk_params": {}
    },
    "model_path": "/outputs/abc123/model"
  },
  "action": "inference",
  "resume_from_step": null
}
```

**响应 - 无任务：**

```json
{
  "job": null,
  "action": "train",
  "resume_from_step": null
}
```

**响应 - 从暂停恢复：**

```json
{
  "job": { ... },
  "action": "train",
  "resume_from_step": 50000
}
```

**`action` 字段说明：**

| 值 | 含义 |
|-----|------|
| `"train"` | 执行训练任务 |
| `"inference"` | 执行推理部署 |

---

### 1.4 版本升级

#### 检查升级 `GET /api/gpu/upgrade/check`

**频率：** 每 60 秒

**Query 参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `current_version` | string | 当前版本号 |
| `device_id` | string | (可选) GPU 设备 ID |
| `token` | string | (可选) 认证令牌 |

**响应 - 有更新：**

```json
{
  "available": true,
  "version": "0.7.0",
  "changelog": "Bug fixes and performance improvements",
  "size": 5242880,
  "filename": "upgrade.zip"
}
```

**响应 - 无更新：**

```json
{
  "available": false,
  "version": "0.6.1"
}
```

#### 下载升级包 `GET /api/gpu/upgrade/download`

**Query 参数：** `device_id`, `token` (可选)

**响应：** 二进制 ZIP 文件

**响应头：**
- `Content-Disposition: attachment; filename="gpu_worker.zip"`
- `X-Version: 0.7.0`

---

## 2. 训练任务生命周期

训练任务使用 `pairing_key` 进行认证 (非 JWT Token)。

### 2.1 获取任务信息 `GET /api/training/jobs/{job_id}`

**Query 参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `worker` | string | 固定值 `"1"`，标识为 Worker 调用 |
| `key` | string | 任务配对密钥 |

**响应：**

```json
{
  "id": "abc123",
  "model_type": "act",
  "train_steps": 10000,
  "batch_size": 64,
  "chunk_size": 20,
  "custom_params": {
    "lr": 0.0001,
    "task": "manipulation task",
    "override_chunk_size": false,
    "dtype": "bfloat16"
  },
  "dataset_ids": ["ds1", "ds2"]
}
```

---

### 2.2 下载数据集 `GET /api/training/jobs/{job_id}/dataset`

**Query 参数：** `key={pairing_key}`

**响应：**

```json
{
  "job_id": "abc123",
  "model_type": "act",
  "train_steps": 10000,
  "batch_size": 64,
  "chunk_size": 20,
  "custom_params": {},
  "trajectory_count": 2,
  "trajectories": [
    {
      "id": "traj_1777304044_cb4fdd",
      "name": "202604272333_0001",
      "has_images": true,
      "image_count": 120,
      "image_download_url": "/api/training/jobs/abc123/images/traj_1777304044_cb4fdd?key=xxxxx",
      "servo_ids": [1, 2, 3, 4, 5, 6],
      "frames": [ ... ],
      "properties": {}
    }
  ]
}
```

> `frames` 字段的详细结构见 [4.1 轨迹数据结构](#41-轨迹数据结构)。

---

### 2.3 下载图像 `GET /api/training/jobs/{job_id}/images/{traj_id}`

**Query 参数：** `key={pairing_key}`

**响应：** 二进制 ZIP 文件，包含该轨迹所有 JPEG 图像

**响应头：**
- `Content-Type: application/zip`
- `Content-Disposition: attachment; filename="{traj_id}_images.zip"`

**图像文件名格式：** `{seq:06d}_80.jpg` (如 `000000_80.jpg`, `000001_80.jpg`)

---

### 2.4 上报训练进度 `POST /api/training/jobs/{job_id}/progress`

**请求体：**

```json
{
  "key": "secret-key-xyz",
  "step": 1000,
  "total_steps": 10000,
  "metrics": {
    "loss": 0.0512,
    "best_loss": 0.0408,
    "steps_per_sec": 280.5,
    "elapsed_sec": 3.5,
    "log": "INFO: step 1000 loss=0.0512",
    "checkpoints": [100, 500, 1000]
  }
}
```

**`metrics` 字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `loss` | float | 当前 step 的 loss |
| `best_loss` | float | 历史最优 loss |
| `steps_per_sec` | float | 训练速度 |
| `elapsed_sec` | float | 本次上报间隔时长 |
| `log` | string | 日志信息 |
| `checkpoints` | int[] | 已保存的 checkpoint step 列表 |

**正常响应 (200)：**

```json
{
  "id": "abc123",
  "status": "training",
  "current_step": 1000,
  "progress_pct": 10.0,
  "metrics": { ... },
  "updated_at": 1709482000.0
}
```

**暂停信号 (409)：**

```json
{
  "error": "任务已暂停",
  "should_pause": true
}
```

收到此响应后，Worker 应保存当前 checkpoint 并暂停训练，等待 `poll-job` 返回 `resume_from_step`。

**取消信号 (409)：**

```json
{
  "error": "任务已取消",
  "should_stop": true
}
```

收到此响应后，Worker 应立即停止训练，上报 `failed` 或 `cancelled` 状态。

---

### 2.5 上报任务状态 `POST /api/training/jobs/{job_id}/status`

**请求体：**

```json
{
  "key": "secret-key-xyz",
  "status": "training",
  "error_msg": null,
  "model_path": null
}
```

**`status` 有效值与转换流：**

```
pending → downloading → training → completed
                          ↓
                        paused → (resume) → training
                          ↓
                        failed
                        cancelled
```

| 状态 | 含义 | 附加字段 |
|------|------|---------|
| `downloading` | 正在下载数据集 | - |
| `training` | 正在训练 | - |
| `paused` | 已暂停 (等待恢复) | - |
| `completed` | 训练/推理完成 | `model_path` |
| `failed` | 任务失败 | `error_msg` |
| `cancelled` | 用户取消 | - |

**完成时请求示例：**

```json
{
  "key": "secret-key-xyz",
  "status": "completed",
  "model_path": "/outputs/abc123/model"
}
```

**失败时请求示例：**

```json
{
  "key": "secret-key-xyz",
  "status": "failed",
  "error_msg": "CUDA out of memory"
}
```

---

## 3. 推理执行生命周期

推理模式下，Worker 周期性地读取设备状态、运行模型推理、发送控制指令。

### 3.1 读取舵机状态 `GET /api/device/{device_id}/servos`

**频率：** 每个推理周期 (默认 20Hz)

**响应：**

```json
{
  "servos": [
    {"id": 1, "pos": 2048, "speed": 0},
    {"id": 2, "pos": 2048, "speed": 0},
    {"id": 3, "pos": 2048, "speed": 0},
    {"id": 4, "pos": 2048, "speed": 0},
    {"id": 5, "pos": 2048, "speed": 0},
    {"id": 6, "pos": 2048, "speed": 0}
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 舵机 ID |
| `pos` | int | 当前位置 (原始值, 0~4095) |
| `speed` | int | 当前速度 |

---

### 3.2 读取摄像头帧 `GET /api/camera/{camera_id}/frame`

**频率：** 每个推理周期 (仅视觉模型需要)

**响应：** 二进制 JPEG 图像

Worker 需将图像 resize 到 640x480 RGB 后输入模型。

---

### 3.3 发送控制指令

#### 方式 A: 单帧指令 (Original 模式) `POST /api/device/{device_id}/command`

**请求体 - 舵机位置指令：**

```json
{
  "commands": [
    {"id": 1, "position": 2048, "speed": 0},
    {"id": 2, "position": 2048, "speed": 0},
    {"id": 3, "position": 2048, "speed": 0},
    {"id": 4, "position": 2048, "speed": 0},
    {"id": 5, "position": 2048, "speed": 0},
    {"id": 6, "position": 2048, "speed": 0}
  ]
}
```

**请求体 - 力矩控制：**

```json
{
  "torque": true
}
```

#### 方式 B: 批量帧指令 (Chunk 模式) `POST /api/device/{device_id}/inference/batch`

用于 Fixed / Adaptive / Overlap 等 chunk 执行模式，一次发送多帧轨迹。

**请求体：**

```json
{
  "frames": [
    {"t": 0,   "p": [2048, 2048, 2048, 2048, 2048, 2048]},
    {"t": 50,  "p": [2050, 2049, 2047, 2051, 2046, 2048]},
    {"t": 100, "p": [2052, 2050, 2046, 2052, 2044, 2048]}
  ],
  "ids": [1, 2, 3, 4, 5, 6]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `frames[].t` | int | 时间偏移量 (ms)，从 0 开始 |
| `frames[].p` | int[] | 各舵机目标位置，顺序与 `ids` 对应 |
| `ids` | int[] | 舵机 ID 列表 |

---

### 3.4 检查推理停止信号 `GET /api/training/jobs/{job_id}/check-inference`

**频率：** 每 5 秒

**响应：**

```json
{
  "running": true,
  "arm_online": true
}
```

| 字段 | 含义 |
|------|------|
| `running` | `false` 时 Worker 应停止推理循环 |
| `arm_online` | `false` 时机械臂离线，Worker 应停止推理 |

---

### 3.5 摄像头模式切换 `POST /api/camera/{camera_id}/stream/mode`

推理开始前设为 `inference`，结束后恢复 `idle`。

**请求体：**

```json
{
  "mode": "inference"
}
```

| mode | 含义 |
|------|------|
| `"inference"` | 推理模式，持续推送帧 |
| `"idle"` | 空闲模式，停止推流 |

---

## 4. 数据格式

### 4.1 轨迹数据结构

每条轨迹包含一系列帧，每帧记录所有舵机的位置：

```json
{
  "id": "traj_1777304044_cb4fdd",
  "name": "202604272333_0001",
  "leader_id": "B2R-88572174A9F0",
  "follower_ids": [],
  "device_id": "B2R-88572174A9F0",
  "frame_count": 223,
  "duration_ms": 11500,
  "calibration": {
    "1": {"min": 766, "max": 3404, "mid": 2048},
    "2": {"min": 884, "max": 3212, "mid": 2048}
  },
  "frames": [
    {
      "timestamp": 168,
      "seq": 0,
      "positions": [
        {"id": 1, "pos": 2136},
        {"id": 2, "pos": 847},
        {"id": 3, "pos": 3052}
      ],
      "ntp_ts": 1777304033190,
      "role": "leader",
      "device_id": "B2R-88572174A9F0"
    }
  ]
}
```

**帧字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `timestamp` | int | 相对时间戳 (ms)，从录制起点计 |
| `seq` | int | 帧序号，从 0 递增 |
| `positions` | array | 各舵机的位置数组 |
| `positions[].id` | int | 舵机 ID |
| `positions[].pos` | int | 舵机位置原始值 |
| `ntp_ts` | int | NTP 绝对时间戳 (ms) |
| `role` | string | `"leader"` (示教臂) 或 `"follower"` (跟随臂) |
| `device_id` | string | 上报设备 ID |

**数据语义：**

- **state** (follower 帧) = 机器人真实状态 (含负载/惯性)
- **action** (leader 帧) = 人类示教意图
- 两者差异非噪声，反映真实动力学特征

**校准数据：**

| 字段 | 说明 |
|------|------|
| `min` | 舵机物理最小位置 |
| `max` | 舵机物理最大位置 |
| `mid` | 舵机中位值 |

---

### 4.2 舵机归一化常量

不同型号舵机的位置范围不同，归一化时需要对应 `pos_max`：

| 舵机型号 | `pos_max` | 位置范围 |
|----------|-----------|---------|
| STS3215 (默认) | 4095 | 0 ~ 4095 |
| SC09 | 1023 | 0 ~ 1023 |
| Hiwonder HX | 4095 | 0 ~ 4095 |

归一化公式：`normalized = raw_pos / pos_max`

反归一化公式：`raw_pos = normalized * pos_max`

---

### 4.3 模型配置文件 (`b2r_config.json`)

训练完成后在模型输出目录生成，推理时读取：

```json
{
  "model_type": "act",
  "pos_max": 4095,
  "use_vision": true,
  "lerobot_dataset": "box2robot-678e5d2d99e5",
  "lerobot_checkpoint": "outputs/abc123/model/checkpoints/last/pretrained_model",
  "chunk_size": 20,
  "n_servos": 6,
  "task_description": "manipulation task"
}
```

---

## 5. 认证机制

系统使用两种认证方式，**不使用** `Authorization` 请求头：

### 5.1 Token 认证 (GPU Worker 设备级)

- **获取方式：** 激活绑定后由 Server 返回
- **传递方式：** Query 参数 `?device_id={id}&token={token}`
- **适用接口：** `/api/gpu/*` 系列
- **生命周期：** 持久有效，直到设备解绑

### 5.2 Pairing Key 认证 (训练任务级)

- **获取方式：** 从 `poll-job` 响应的 `job.pairing_key` 获取
- **传递方式：** Query 参数 `?key={pairing_key}` 或请求体 `{"key": "..."}` 或请求头 `X-Pairing-Key`
- **适用接口：** `/api/training/jobs/*` 系列
- **生命周期：** 随任务创建生成，任务结束后失效

---

## 6. 错误处理与重试策略

| 场景 | 策略 |
|------|------|
| 心跳失败 | 静默记录日志，下次继续 |
| 任务轮询失败 | 返回空任务，下个周期重试 |
| 普通进度上报失败 | 仅尝试 1 次，不重试 |
| Checkpoint 进度上报失败 | 重试 3 次，间隔 3 秒 |
| 终态状态上报 (completed/failed) | 重试 5 次，指数退避 (3s → 6s → 9s → 12s → 15s) |
| 数据集下载失败 | 上报 `failed` 状态并退出 |

**409 响应处理：**

- `should_pause: true` → 保存 checkpoint → 上报 `paused` → 返回轮询循环等待恢复
- `should_stop: true` → 停止训练 → 上报 `cancelled`

---

## 7. 完整生命周期流程图

```
┌─────────────────────────────────────────────────┐
│                 GPU Worker 启动                   │
└─────────────┬───────────────────────────────────┘
              ▼
    POST /api/gpu/activate
              │
    ┌─────────┴─────────┐
    │ need_bind          │ activated
    │ (显示绑定码)        │ (已绑定)
    │ 每3秒轮询           │
    └─────────┬─────────┘
              │ 获得 token
              ▼
    ┌─────────────────────────────────────────────┐
    │              主循环                           │
    │  ┌── 心跳上报 (10s) ────────────────────┐    │
    │  ├── 任务轮询 (5s) ─────────────────────┤    │
    │  └── 升级检查 (60s) ────────────────────┘    │
    └─────────┬───────────────────────────────────┘
              │ poll-job 返回任务
    ┌─────────┴─────────┐
    │ action=train       │ action=inference
    ▼                    ▼
┌──────────┐      ┌──────────────┐
│ 训练流程  │      │ 推理流程      │
├──────────┤      ├──────────────┤
│ 获取任务  │      │ 加载模型      │
│ ↓        │      │ ↓            │
│ 下载数据  │      │ 开启力矩      │
│ ↓        │      │ 设摄像头模式   │
│ 转换格式  │      │ ↓            │
│ ↓        │      │ ┌──────────┐ │
│ 开始训练  │      │ │ 推理循环  │ │
│ ↓        │      │ │ 读状态    │ │
│ 上报进度  │◄────►│ │ 读图像    │ │
│ ↓        │ 409  │ │ 模型推理  │ │
│ 训练完成  │      │ │ 发送指令  │ │
│ ↓        │      │ │ 检查停止  │ │
│ 上报完成  │      │ └──────────┘ │
└──────────┘      │ ↓            │
    │              │ 关闭力矩     │
    │              │ 恢复摄像头   │
    │              │ 上报完成     │
    │              └──────────────┘
    │                    │
    └────────┬───────────┘
             ▼
      返回主循环，继续轮询
```

---

## 附录：推理执行模式

通过 `deploy_info.execution_mode` 指定：

| 模式 | 推理频率 | 执行频率 | 说明 |
|------|---------|---------|------|
| `original` | ~5Hz | 5Hz | 每步单帧指令，LeRobot `select_action` |
| `fixed` | ~1Hz | 20Hz | 整个 chunk 一次推理，逐帧执行 |
| `adaptive` | 1~5Hz | 20Hz | 动态跳步，根据确定性调整推理频率 |
| `overlap` | ~2Hz | 20Hz | 滑动窗口 + 集成融合 |

`chunk_params` 配置 (adaptive/overlap 模式)：

```json
{
  "max_skip": 5,
  "certainty_threshold": 0.8
}
```
