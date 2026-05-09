[English](README_en.md) | 中文

# Box2Robot — 具身智能云平台

**即插即用的机械臂，云端训练、技能共享、AI 智能体控制。**

<div align="center">
  <img src="assets/whole_robot_system.jpg" alt="Box2Robot 系统" width="500"/>
</div>

Box2Robot 是一个开源具身智能平台。将 ESP32 机械臂和视觉模块连接到云端平台，实现数据采集、模型训练和技能共享。无需复杂配置 —— 烧录固件、连 WiFi、绑定设备，即可开始。

> **当前版本：v0.6.6**（机械臂固件 v0.6.6 / 摄像头固件 v0.6.3）

## 快速开始

### 1. 获取硬件

<div align="center">
  <a href="https://item.taobao.com/item.htm?abbucket=5&id=1030962099420">
    <img src="assets/hardware.jpg" alt="Box2AI 机械臂套件" width="400"/>
  </a>
  <br>
  <a href="https://item.taobao.com/item.htm?abbucket=5&id=1030962099420">购买 Box2AI 机械臂套件 (淘宝)</a>
</div>

组装机械臂并将舵机连接到驱动板。固件已预装，如需手动烧录请参阅 [烧录固件](#烧录固件)。

### 摄像头模块安装

<div align="center">
  <img src="assets/Camera_base mounting_nut.jpg" style="width:30%;max-width:240px;"/>
  <img src="assets/Camera_base_mounting_hole.jpg" style="width:30%;max-width:240px;"/>
  <img src="assets/Camera_base_screw_placement_fixation.jpg" style="width:30%;max-width:240px;"/>
</div>

<div align="center">
  <img src="assets/Camera_placement_and_screw_fixation.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/camera_line_connect.jpg" style="width:45%;max-width:360px;"/>
</div>

### 电池托盘与驱动器安装

<div align="center">
  <img src="assets/battery_base_install1.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/battery_base_install2.jpg" style="width:45%;max-width:360px;"/>
</div>

<div align="center">
  <img src="assets/battery_base_and_driver_install.jpg" style="width:45%;max-width:360px;"/>
  <img src="assets/battery_install.jpg" style="width:45%;max-width:360px;"/>
</div>

### 2. 连接设备热点

给设备上电，设备会创建 WiFi 热点：
- **机械臂驱动板：** `Box2Robot_XXXX` (XXXX 为 MAC 后 4 位)
- **视觉语音模块：** `Box2Cam_XXXX`

用手机或电脑连接该热点。会自动弹出配网页面（如未弹出，手动访问 `192.168.4.1`）。

### 3. 配置 WiFi

在配网页面输入 WiFi 名称和密码。设备会保存凭据、重启并连接到你的网络。

### 4. 绑定设备

设备连上 WiFi 后，获取 **6 位绑定码**：
- **机械臂：** 显示在 OLED 屏幕上
- **摄像头：** 通过 TTS 语音播报

然后：
1. 访问 [**https://robot.box2ai.com**](https://robot.box2ai.com/#/)
2. 注册账号
3. 进入 **设备管理 → 绑定设备**
4. 输入 6 位绑定码
5. 完成！

绑定后即可使用全部功能：远程遥控、校准、数据采集、云端训练、技能商店、语音交互。

### 5. 按键操作

驱动盒上的灰色按钮支持以下操作：

| 操作 | 功能 |
|------|------|
| **单击** | 取消力矩 — 释放所有舵机，可自由拖动机械臂 |
| **长按 (3秒以上)** | 重置出厂设置 — 清除已保存的 WiFi 信息，设备重启后重新进入热点配网模式 |

---

## AI 智能体控制 (Skills CLI)

让 **Claude Code**、**GPT** 等 AI 智能体直接控制你的机械臂。Box2Robot 提供两种接入方式：

### 方式一：从 ClawHub 一键安装（推荐）

[**ClawHub · box2robot-skills →**](https://clawhub.ai/boxjod/box2robot-skills)

无需克隆代码，直接从 ClawHub 平台下载并安装 `box2robot-skills` 技能包，AI Agent 即可接入云端控制机械臂。适合大多数用户。

### 方式二：克隆仓库本地使用（开发者）

如果你想自定义 Action、修改源码或离线运行 CLI，克隆仓库使用：

```bash
git clone https://github.com/box2ai-robotics/box2robot.git
cd box2robot/box2robot_skills

# 登录 (token 缓存，后续免登录)
python b2r.py login <username> <password>

# 操控
python b2r.py devices                # 查看设备
python b2r.py home                   # 回零位
python b2r.py move 1 2048 500        # 1号舵机转到2048
python b2r.py torque off             # 释放力矩
python b2r.py record start           # 开始录制
python b2r.py record stop            # 停止录制
python b2r.py play                   # 列出并播放轨迹

# 技能商店（ACT Store —— 浏览/购买/运行他人共享的技能）
python b2r.py store list             # 浏览技能商店
python b2r.py store info <task>      # 查看技能详情
python b2r.py store buy <task>       # 购买付费技能
python b2r.py store run <task>       # 在自己的设备上执行技能
python b2r.py store mine             # 已购技能列表

python b2r.py say "拍个照"            # 自然语言指令
python b2r.py shell                  # 交互式 Shell
```

### 配合 Claude Code 使用

```
"读一下 box2robot_skills/SKILLS.md，然后把1号舵机转到2048"
"录一段轨迹，录完后播放一遍"
"查看舵机状态，然后回零位"
```

详见 `box2robot_skills/SKILLS.md`（AI 智能体调度手册，79 个 Action、预检规则、工作流模板）。

---

## GPU 训练与推理节点（高级）

`box2robot_gpu_worker/` 是 GPU 算力节点，连接 Box2Robot 云端服务器，自动领取训练和推理任务。需要 **RTX 3060+ GPU**，支持 ACT (Action Chunking Transformer)、Diffusion Policy、MLP 等模型。

只需 3 步：安装 → 启动 → 在 APP 中输入绑定码。绑定后 Worker 自动轮询任务、下载数据集、训练模型、上报进度，所有操作均在 APP 端完成。

克隆仓库时**必须带上 `--recurse-submodules`** 拉取 LeRobot 子仓库：

```bash
git clone --recurse-submodules https://github.com/box2ai-robotics/box2robot.git
```

> 已 clone 但没拉 submodule？在仓库根目录执行 `git submodule update --init --recursive` 即可补上 LeRobot。

详见 [box2robot_gpu_worker/README.md](box2robot_gpu_worker/README.md)。

---

## 固件升级

设备出厂已预装最新固件，**推荐使用在线 OTA 升级**：

1. 设备绑定后进入 [https://robot.box2ai.com](https://robot.box2ai.com/#/) → **设备管理**
2. 在设备详情中点击 **固件升级**，云端会自动推送最新固件并完成升级

**手动烧录**（适用于首次烧录空板、设备无法启动、需要刷写自编译固件等场景）：详见 [`bin/README.md`](bin/README.md)。

---

## 更新日志

| 版本 | 日期 | 说明 |
|------|------|------|
| v0.6.6 (arm) | 2026-05-04 | 修复录制中 WS 断连（transport_poll_write）：RAM 录制缓冲 1200→300 帧释放约 80KB BSS，heap 从 30KB 恢复至约 110KB；长录制由 SPIFFS 兜底 |
| v0.6.5 (arm) | 2026-05-02 | CLI 新增 ACT 技能商店命令 (`store list/info/buy/run/mine`)，设备/轨迹/任务列表展示短码 (code)，GPU Worker 训练/推理流程优化 |
| v0.6.3 | 2026-04-26 | 烧录工具说明优化 (esptool / Flash Download Tool 双方案)，bin 文件统一归档 |
| v0.6.1 | 2026-04-19 | GPU 训练推理节点开源，修复幻尔舵机校准偏置写入，新增舵机电压范围选择 (5V/7.4V/12V)，WiFi 主从遥操流畅度优化 |
| v0.5.1 | 2026-04-14 | 云端平台集成、WebSocket 中继、OTA、ESP-NOW 50Hz 双通道、摄像头 MJPEG+ADPCM 音频、语音 AI 链路、自动校准、数据采集对齐 |
| v0.4.5 | 2026-03-23 | (LeRobot-ESP32) 幻尔 LX 舵机支持、自动检测舵机类型 |

## 相关链接

- **云端平台：** [https://robot.box2ai.com](https://robot.box2ai.com/#/)
- **硬件购买：** [淘宝店铺](https://item.taobao.com/item.htm?abbucket=5&id=1030962099420)
- **前代项目 (纯 ESP-NOW)：** [LeRobot-ESP32](https://github.com/box2ai-robotics/lerobot-esp32)
- **LeRobot 框架：** [Hugging Face LeRobot](https://github.com/huggingface/lerobot)

## 开源协议

Apache 2.0 License

---

如果这个项目对你有帮助，请给个 Star！
