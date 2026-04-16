# Box2Robot Skills — AI Agent 调度手册

> 本文件供 Claude / GPT / CLAW 等 LLM Agent 在调度 Box2Robot 时参考。
> Agent 通过 `b2r.py` CLI 或 HTTP API 控制机械臂。

---

## 认证

```bash
# 登录 (一次性, token 自动缓存到 ~/.b2r_token)
python b2r.py login <username> <password>

# 或直接设环境变量
export B2R_SERVER="https://robot.box2ai.com"
export B2R_TOKEN="<jwt_token>"
export B2R_DEVICE="B2R-XXXXXXXXXXXX"
```

登录后所有命令自动携带 token，无需重复认证。

---

## CLI 命令速查

### 设备
```bash
b2r.py devices                     # 列出设备 (* 表示在线)
b2r.py status                      # 当前舵机位置和力矩
```

### 舵机控制
```bash
b2r.py torque on                   # 锁定舵机
b2r.py torque off                  # 释放 (可手动拖拽)
b2r.py home                        # 安全回零位
b2r.py move <舵机ID> <位置> [速度]  # 移动单个舵机
# 位置: 0-4095 (中位 2048), 速度: 0-4000 (默认 1000)
# 10° ≈ 114 counts
```

### 录制与回放
```bash
b2r.py record start                # 开始录制
b2r.py record stop [名称]          # 停止录制
b2r.py record status               # 录制状态
b2r.py play                        # 列出所有轨迹
b2r.py play <traj_id>              # 播放轨迹
```

### 高级调用
```bash
b2r.py exec <action> [参数]        # 调用任意 Action
b2r.py say "自然语言"               # 自然语言执行
b2r.py shell                       # 交互式 Shell
```

---

## Action 体系 (79 个)

通过 `b2r.py exec <action_name>` 调用，参数用 JSON 或 key=value 格式。

### device (设备管理)
| Action | 说明 |
|--------|------|
| `device.list` | 列出设备 |
| `device.status` | 设备状态 (控制模式+舵机) |
| `device.bind` | 绑定设备 (需 bind_code) |
| `device.unbind` | 解绑设备 |
| `device.rename` | 改名 |
| `device.wifi_add` | 添加WiFi |
| `device.wifi_clear` | 清除WiFi配置 |
| `device.factory_reset` | 恢复出厂 |

### servo (舵机控制)
| Action | 参数 | 说明 |
|--------|------|------|
| `servo.status` | device_id | 读取位置/负载/温度 |
| `servo.move` | device_id, servo_id, position, [speed] | 移动单个 |
| `servo.move_batch` | device_id, commands:[{id,position,speed}] | 批量移动 |
| `servo.torque` | device_id, enable:bool | 力矩开关 |
| `servo.go_home` | device_id | 回零位 |
| `servo.release_control` | device_id | 释放当前控制 |
| `servo.control_mode` | device_id | 查询控制模式 |
| `servo.set_id` | device_id, old_id, new_id | 烧录舵机ID |

### recording (录制回放)
| Action | 参数 | 说明 |
|--------|------|------|
| `record.start` | device_id, [mode], [camera_id] | 开始录制 (single/dual/phone) |
| `record.stop` | device_id, [name] | 停止录制 |
| `record.status` | device_id | 录制状态 |
| `trajectory.list` | device_id | 轨迹列表 |
| `trajectory.play` | device_id, trajectory_id | 播放 |
| `trajectory.stop` | device_id, trajectory_id | 停止播放 |
| `trajectory.delete` | device_id, trajectory_id | 删除 |
| `trajectory.images` | device_id, trajectory_id | 轨迹图像 |

### pairing (主从配对)
| Action | 参数 | 说明 |
|--------|------|------|
| `pairing.list` | — | 列出配对 |
| `pairing.create` | leader_id, follower_ids, [use_espnow] | 创建配对 |
| `pairing.disconnect` | leader_id | 断开 (保留记录) |
| `pairing.connect` | leader_id | 重连 |
| `pairing.delete` | leader_id | 删除配对 |
| `espnow.discover` | — | 扫描附近设备 |
| `espnow.peers` | device_id | 查看邻居 |

### calibration (校准)
| Action | 参数 | 说明 |
|--------|------|------|
| `calibrate.auto` | device_id, [servo_id=0] | 自动校准 (0=全部) |
| `calibrate.status` | device_id | 校准进度 |
| `calibrate.cancel` | device_id | 取消 |
| `calibrate.manual` | device_id, servos:[{id,min,max,mid}] | 手动 |
| `calibrate.center_offset` | device_id | 写入EEPROM中心偏移 |

### camera (摄像头/语音)
| Action | 参数 | 说明 |
|--------|------|------|
| `camera.status` | device_id | 摄像头状态 |
| `camera.snapshot` | device_id | 拍照 |
| `camera.stream_mode` | device_id, mode | 流模式 (idle/preview/inference) |
| `camera.frame` | device_id | 获取最新一帧 JPEG |
| `camera.voice_start` | device_id | 开麦 |
| `camera.voice_stop` | device_id | 关麦 |
| `camera.tts` | device_id, prompt | TTS播报 |
| `camera.play_sound` | device_id, sound | 播放音效 |
| `camera.record_audio_start` | device_id | 开始录音 |
| `camera.record_audio_stop` | device_id | 停止录音 |
| `camera.recordings` | device_id | 录音列表 |

### training (训练/推理)
| Action | 参数 | 说明 |
|--------|------|------|
| `training.submit` | name, dataset_ids, [model_type], [train_steps] | 提交训练 |
| `training.list` | [page] | 任务列表 |
| `training.status` | job_id | 训练进度 |
| `training.cancel` | job_id | 取消 |
| `training.deploy` | job_id, arm_device_id, gpu_device_id | 部署推理 |
| `training.stop_inference` | job_id | 停止推理 |
| `training.rate` | job_id, rating(1-5) | 评分 |

### store (技能商店)
| Action | 参数 | 说明 |
|--------|------|------|
| `store.list` | [search], [category] | 浏览商店 |
| `store.execute` | task_id, device_id | 执行技能 |
| `store.purchase` | task_id | 购买 |
| `store.favorite` | task_id | 收藏 |
| `store.like` | task_id | 点赞 |
| `store.rate` | task_id, rating(1-5) | 评分 |

### config (配置/OTA)
| Action | 参数 | 说明 |
|--------|------|------|
| `config.get` | device_id | 查看配置 |
| `config.set` | device_id, params:{key:val} | 修改配置 |
| `config.speed` | device_id, speed(100-4000) | 调速 |
| `config.led_brightness` | device_id, brightness(0-255) | LED亮度 |
| `config.volume` | device_id, volume(0-255) | 音量 |
| `config.camera_resolution` | device_id, resolution | 分辨率 |
| `config.bluetooth` | device_id, enable:bool | 蓝牙开关 |
| `ota.check` | — | 检查固件更新 |
| `ota.update` | device_id | 推送更新 |

---

## 自然语言映射

`b2r.py say "文本"` 或交互 Shell 中直接输入：

| 语音 | 映射到 |
|------|--------|
| "回零位" / "回家" | servo.go_home |
| "释放力矩" / "松手" | servo.torque(False) |
| "锁住" | servo.torque(True) |
| "开始录制" / "我教你" | record.start |
| "停止录制" / "录完了" | record.stop |
| "拍照" | camera.snapshot |
| "打开摄像头" | camera.stream_mode(preview) |
| "自动校准" | calibrate.auto |
| "声音大一点" | config.volume(200) |
| "速度快一点" | config.speed(2000) |
| "灯亮一点" | config.led_brightness(200) |
| "1号舵机转到2048" | servo.move(1, 2048) |
| "录制5个数据集" | workflow.batch_record(5) |
| "我教你跳个舞" | workflow.teach_single |

---

## 预检规则

Agent 在执行动作前必须检查：

| 步骤 | 检查内容 | 失败处理 |
|------|---------|---------|
| ① | 设备在线 | "设备离线，检查电源" |
| ② | 设备类型 = arm | "不是机械臂" |
| ③ | 控制模式 = idle | release_control 释放 |
| ④ | 有校准数据 | calibrate.auto 自动校准 |
| ⑤ | 舵机数量匹配 | 轨迹需 N 轴 vs 设备 M 轴 |
| ⑥ | 校准范围兼容 | ratio < 0.5 则不兼容 |
| ⑦ | 视觉任务: cam+gpu 在线 | 提示哪个设备缺失 |

---

## 典型场景编排

### 场景1: "请挥挥手"
```
1. say "挥手" → 搜索本地轨迹/商店
2. 找到 → 预检 → trajectory.play 或 store.execute
3. 没找到 → 建议 "可以录制一个: 我教你挥手"
```

### 场景2: "我教你倒水, 录5个"
```
1. torque off                              # 释放力矩
2. camera.stream_mode(cam_id, "preview")   # 开摄像头
3. 循环5次:
   record.start(mode="single", camera_id)  # 录制
   [用户演示]
   record.stop(name="倒水_{n}")            # 保存
4. training.submit(name="倒水", dataset_ids) # 提交训练
```

### 场景3: 视觉推理闭环
```
1. training.deploy(job_id, arm_id, gpu_id, cam_id)
2. GPU Worker 自动: 取图 → 推理 → 发指令 → arm 执行
3. training.stop_inference(job_id)  # 停止
```

---

## HTTP API 直接调用

不用 CLI 也可以直接调 HTTP API：

```bash
# 登录
curl -X POST https://robot.box2ai.com/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"xxx","password":"xxx"}'
# → {"token": "eyJ..."}

# 设备列表
curl https://robot.box2ai.com/api/devices \
  -H "Authorization: Bearer <token>"

# 移动舵机
curl -X POST https://robot.box2ai.com/api/device/B2R-xxx/command \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"id":1,"position":2048,"speed":1000}'

# 回零位
curl -X POST https://robot.box2ai.com/api/device/B2R-xxx/go_home \
  -H "Authorization: Bearer <token>"
```

完整端点列表见 `box2robot_server/box2robot-cli/box2robot.md`

---

## LLM 对话控制 (chat.py)

通过自然语言 + 智谱AI function calling 控制机械臂，LLM 自动判断何时调用 API。

```bash
cd box2robot_audio

# 纯聊天
python chat.py

# 聊天 + 机械臂控制
python chat.py --robot

# 首次需登录
python chat.py --robot --login <user> <password>

# 指定设备
python chat.py --robot --device B2R-XXXXXXXXXXXX
```

### LLM 可用的 Tools (9 个)

| Tool | 参数 | 说明 |
|------|------|------|
| `list_devices` | — | 列出所有设备及在线状态、类型 |
| `servo_status` | — | 当前舵机 position/load/temp + 力矩状态 |
| `move_servo` | servo_id, position, [speed] | 移动单个舵机 (ID 1-6, pos 0-4095) |
| `go_home` | — | 所有舵机回校准中心位置 |
| `set_torque` | enable:bool | 力矩开(锁)/关(释放) |
| `record_start` | — | 开始录制轨迹 |
| `record_stop` | — | 停止录制并保存 |
| `list_trajectories` | — | 列出已保存的轨迹 |
| `play_trajectory` | traj_id | 按 ID 播放轨迹 |
| `search_and_play` | keyword | 按名称搜索并播放 (如"点头""挥手") |

### API 反馈格式

每个 tool 执行后返回结构化反馈，LLM 据此生成回复：

**成功反馈** — 以 `OK:` 开头：
```
OK: servo 1 moving to 2500 at speed 500. status=queued
OK: torque=ON, stale=False, age=353ms, 6 servos
  ID1: position=2494, load=36, temp=35
  ...
OK: Returning home in 15 steps (max_delta=342)
OK: Recording saved. id=abc123, frames=150, duration=5000ms
OK: 4 devices | active=B2R-XXXX
  白色从臂 | id=B2R-XXXX | type=arm | ONLINE
  黑色主臂 | id=B2R-YYYY | type=arm | OFFLINE
```

**失败反馈** — 以 `ERROR:` 开头：
```
ERROR: Device not connected              → 设备离线，检查电源/WiFi
ERROR: No servo data (stale=True)        → 设备刚开机，等几秒重试
ERROR: Cannot disable torque while Virtual Serial is active → 关闭虚拟串口后重试
ERROR: Already recording                 → 先 record_stop 再开新录制
ERROR: Not recording                     → 无需停止
ERROR: unauthorized                      → Token 过期，需重新登录
ERROR: No device selected                → 需先选择设备
ERROR: Invalid servo_id (0-253)          → 舵机ID超出范围
ERROR: Invalid position (0-4095)         → 位置值超出范围
ERROR: Trajectory not found              → 轨迹ID不存在
ERROR: Cannot connect to server          → 网络问题，检查网络连接
ERROR: Request timeout                   → 请求超时，服务器可能繁忙
```

### 多设备处理

- 启动时自动选择第一个在线的 arm 设备
- 如果有多个 arm 在线，提示用户选择
- 用户可通过 `--device B2R-XXXX` 指定，或在对话中说"切换到黑色主臂"
- `list_devices` 返回中标注 `active=` 表示当前操控的设备

### LLM 行为规则

1. **执行前**: 用户意图不明确时先确认（"你要移动哪个舵机？"）
2. **执行后**: 根据反馈生成回复（成功→告知结果，失败→解释原因+建议）
3. **多步操作**: 录制流程需先释放力矩（set_torque false → record_start）
4. **安全**: 不会在对话中主动执行危险操作（factory_reset、calibrate 等）
5. **闲聊**: 不涉及机械臂的对话不调用任何 tool
