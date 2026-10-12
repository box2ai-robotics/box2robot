# Box2Robot Skills：从 API Key 到第一次控制

本目录的独立 `b2r.py` 通过云端 HTTP / WebSocket 控制设备，不需要安装 Server 或 GPU Worker。智能体先读 [SKILL.md](SKILL.md)，编排参考见 [SKILLS.md](SKILLS.md)。[网页教程](https://robot.box2ai.com/#/pages/developer/docs?section=api)。

## 网页机器人对话（2026-10）

打开 [机器人对话](https://robot.box2ai.com/#/pages/developer/code-policy)，登录后即可文字提问，每个账号提供 3 次 GPT-6-Astra 免费体验。在“设备与模型”选择已绑定且在线的机械臂和相机，再要求读取状态或拍照；画面会回传到对话，可点击放大。机械臂运动逐次确认后才下发。支持网页语音识别的浏览器可点击“语音输入”，检查识别文本再发送；不支持时可使用系统键盘语音输入。左侧“新对话”和历史列表用于切换任务。

免费额度用完后优先提供平台托管：沿用免费体验的模型服务，用户无需填写模型 Key，预付积分按实际 token 结算，无固定月费。普通输入 ¥100、缓存命中 ¥10、缓存写入 ¥125、输出（含推理）¥500／百万 token。单请求超过 272K 输入时，输入类单价 ×2、输出 ×1.5。每次模型请求的上下文、工具结果和图片均计入用量；1 万普通输入 + 2000 输出约 ¥2。每轮可选 ¥1/3/5/10 费用上限，需主动接受计费；预留后按有效用量结算并退回余量，小于 0.1 元的余量继续保留在 CaP 余额。

也可选择“自定义 API”，填写支持 OpenAI 兼容 Responses 或 Chat Completions 的 HTTP(S) 地址和模型。平台不限制厂商、域名或端口，支持服务器可达的内网服务；无鉴权服务可留空 Key。查询模型列表、搜索和连接测试均为可选，也可直接手填模型开始对话。Key 仅在本次页面会话内保存，刷新后重新填写；平台不扣自定义模型的免费次数或余额，费用由对应厂商收取。切换供应商会将本对话近期文本和所选图片发送给新服务。

## 1. 登录并绑定设备

1. 打开 [工作台](https://robot.box2ai.com)，注册或登录账号。
2. 为机械臂配网，联网后取得设备的 6 位绑定码。
3. 在“设备管理”添加设备，输入绑定码，确认机械臂在线；摄像头需要单独绑定。
4. 在“舵机调试”检查装配、校准和当前位置，结束该设备正在运行的回放、跟随或推理。

API Key 使用当前账号的资源权限，不会替你绑定设备，也不能访问其他账号未共享的设备。

## 2. 一步一步获取 API Key

1. 登录后打开账户 / 个人中心，进入“API 密钥”；也可直接打开 [密钥管理](https://robot.box2ai.com/#/pages/profile/api-keys)。
2. 点击“＋ 生成新密钥”。
3. 填写名称，例如“桌面机械臂脚本”，方便以后识别和吊销。
4. 默认开启“全权限”。只做设备控制时关闭它，勾选“设备控制”；需要其他功能时按下表增加权限。
5. 点击“生成”，在“密钥已生成”窗口点击“复制”，保存到自己的凭证管理器或运行环境。
6. 保存后点击“我已保存”。完整的 `b2r_...` **只显示这一次**，列表中的前缀不能用来登录；丢失后生成新密钥并吊销旧密钥。

| 功能域 | 用途与组合建议 |
|---|---|
| `device` 设备控制 | 设备列表、状态与动作；`/api/device/...` 下的录制和轨迹接口也需要它 |
| `camera` 相机 | 快照、最新帧、实时预览；和设备流程一起用时保留 `device` |
| `data` 数据 | 数据集相关接口；录制、轨迹下载流程建议同时选择 `device` |
| `training` 训练 | 提交 / 查看训练任务；CLI 选轨迹需要 `device` |
| `inference` 推理 | 部署 / 停止推理；CLI 部署还要读任务和设备，选择 `training`、`device` |
| `store` 商店 | 购买、运行与已购技能；自动选择设备时同时选择 `device` |

权限按功能域划分，不是只读 / 只写权限；API 的空权限列表表示全权限。`/api/keys` 的创建、列出和吊销操作需要账号登录 JWT，不能用 API Key 管理 API Key。

**区分两种 Key：**这里生成的是 Box2Robot 平台 Key，用于机器人接口。Code as Policy / 语音配置中的模型供应商 Key，需要到对应厂商控制台获取，两者不能互换。

## 3. 安装并验证连接

从仓库获取（已有代码可跳过 clone）：

```bash
git clone https://github.com/box2ai-robotics/box2robot.git
cd box2robot/box2robot_skills
python -m pip install "aiohttp>=3.9,<4"
python b2r.py --help
```

也可从 [ClawHub 技能包](https://clawhub.ai/boxjod/box2robot-skills) 获取文件。Python 要求见 [pyproject.toml](pyproject.toml)。单独使用 CLI 不需要训练框架子模块；控制设备需要联网。

Windows PowerShell（提示输入，避免把明文写入命令历史）：

```powershell
$env:B2R_SERVER = "https://robot.box2ai.com"
$keyInput = Read-Host "粘贴平台 API Key" -AsSecureString
$env:B2R_API_KEY = [System.Net.NetworkCredential]::new('', $keyInput).Password
python b2r.py devices
```

macOS / Linux Bash：

```bash
export B2R_SERVER="https://robot.box2ai.com"
read -r -s -p 'Platform API Key: ' B2R_API_KEY
echo
export B2R_API_KEY
python b2r.py devices
```

成功后列出设备类型、在线标记 `*` 和完整 `device_id`。列表为空时，先核对账号和绑定关系。

需要跨终端保存凭证时，运行 `python b2r.py login --key "$env:B2R_API_KEY"`（PowerShell）或 `python b2r.py login --key "$B2R_API_KEY"`（Bash）。它会读取设备列表，将凭证和默认设备写入 `~/.b2r_token`，并选择第一台在线机械臂。环境变量方式本身不写入该文件。

也支持 `python b2r.py login`，按提示输入账号密码，不要把真实密码写进脚本。

## 4. 明确指定机械臂，先读状态

从 `devices` 复制实际的机械臂 `device_id`，不要用绑定码、昵称或相机 ID：

```powershell
$env:B2R_DEVICE = "B2R-替换为实际机械臂ID"
python b2r.py status
```

Bash 使用 `export B2R_DEVICE="B2R-替换为实际机械臂ID"`。即使只有一台设备，也建议显式指定；缓存的默认设备可能来自以前的登录。

检查舵机 ID、位置、负载、温度和力矩。CLI 简略状态不显示全部反馈字段；检查 `stale`、限位等详细信息时，使用第 7 节 HTTP 请求或网页舵机调试。

## 5. 完成第一次小幅动作

1. 确认机械臂在线、已校准、周围没有干涉，且没有其他任务控制它。
2. 读取目标关节当前位置和有效限位。编码值不是角度，`2048` 不是所有关节的通用安全位置。
3. 必要时执行 `python b2r.py torque on`，阅读确认提示后输入 `y`。
4. 例如：**只有当 1 号舵机当前在 2048 附近、2068 在校准范围内且路径无干涉时**，才执行下面的小幅动作。

```bash
python b2r.py move 1 2068 300
python b2r.py status
```

5. 对照实际动作和最新反馈确认是否到达。`sent`、`queued` 等响应表示请求已接收 / 下发，不表示动作已完成。

`home` 会产生回零运动；`torque off` 会释放支撑，必须先托住机械臂，不能当作通用急停。动作、力矩、校准、推理部署、商店购买和运行有 CLI 确认提示。非交互调用需要 `--yes`，仅在用户已明确授权该动作和目标后使用；它不会替代设备检查，也不会自动回答训练 / 部署的选择题。

## 6. 进阶功能：完成一个闭环

### 相机快照、取帧与实时画面

先用 `devices` 找到在线相机的完整 ID。将 `CAM-EXAMPLE` 替换为它；务必显式传相机 ID，否则 `snapshot` / `frame` 会沿用默认设备，可能误用机械臂 ID。

```bash
python b2r.py snapshot CAM-EXAMPLE
python b2r.py frame CAM-EXAMPLE frame.jpg
python b2r.py stream CAM-EXAMPLE --latest preview.jpg --duration 15
```

`snapshot` 请求拍照，不直接保存文件；等新帧到达后 `frame` 下载 JPEG。204 表示没有缓存帧，可开启短时 `stream` 后重试。流默认最多 60 秒，`--out frames` 可逐帧保存，`--duration 0` 表示不限时。确认用户允许访问当前画面后再调用。

### 录制、回放、下载与视频

1. 布置固定工位，选定机械臂和相机。手动拖拽前托住机械臂，再通过 `torque off` 释放力矩。
2. 开始录制，完成一次明确的示范，停止并命名：

```bash
python b2r.py record start --cam CAM-EXAMPLE
python b2r.py record status
python b2r.py record stop demo_pick
python b2r.py play
```

3. 从停止响应或 HTTP 轨迹列表取得**完整轨迹 ID**。CLI `play` 列表会截断 ID，不能把显示的前缀当成完整 ID。以下 `TRAJECTORY_ID` 均替换为该 ID。
4. 核对关节映射、校准和起始姿态后回放；在网页对应任务页面停止回放，当前 CLI 没有 `stop-play` 命令。

```bash
python b2r.py play TRAJECTORY_ID
python b2r.py dataset TRAJECTORY_ID ./demo_dataset
python b2r.py download TRAJECTORY_ID ./demo_images
python b2r.py video TRAJECTORY_ID demo.mp4 --fps 5
```

`dataset` 下载轨迹 JSON 与图像，`download` 只下载图像。视频需要有效录制图像，以及本机 `ffmpeg` 或 `opencv-python`。不带 `--cam` 的录制在发现在线相机时会询问是否使用相机。

### ACT 训练与推理

```bash
python b2r.py train --steps 50000 --name demo_pick
python b2r.py jobs
```

1. 交互选择同一任务的有效轨迹，例如 `1,2,3`、`1-5` 或 `all`，核对参数后提交。训练会使用算力和可能的账户额度。
2. 当前 CLI 固定提交 ACT、batch size 32、chunk size 20；更多模型、官方算力和高级参数请使用网页“任务训练 → VLA训练”。
3. 等待任务完成且存在可用模型。使用提交响应或 HTTP 任务列表中的完整任务 ID（`jobs` 展示也会截断 ID）。
4. 部署时交互选择在线 GPU、机械臂、相机和执行模式，核对确认后开始；结束时停止并检查返回状态。

```bash
python b2r.py deploy JOB_ID
python b2r.py stop-infer JOB_ID
```

CLI 部署要求账号下有在线 GPU；网页的官方算力调度和多模型配置不等同于这个交互流程。`stop-infer` 停止指定训练任务的推理，不是所有运动模式的急停。

### 技能商店与网页新功能

```bash
python b2r.py store list "取放"
python b2r.py store info TASK-EXAMPLE
python b2r.py store mine
# 确认价格和适配条件后，付费技能才需要购买
python b2r.py store buy TASK-EXAMPLE
python b2r.py store run TASK-EXAMPLE B2R-EXAMPLE
```

替换为实际技能编号与设备 ID。执行方式由技能类型决定；不能假定所有商店技能都走 GPU 推理。先读详情中的硬件、相机、模型和场景要求。

| 网页能力 | 使用步骤 | 当前单文件 CLI 边界 |
|---|---|---|
| Code as Policy 直接指令 | 任务训练 → Code as Policy训练 → 直接指令；选择机械臂 / 相机，配置模型供应商，写目标与完成条件，生成 → 预演 → 执行 → 确认结果 | 没有 `cap` / `say` 命令；[阅读教程](https://robot.box2ai.com/#/pages/developer/docs?section=code-policy) |
| 示教与上下文技能 | 在 Code as Policy 的示教技能中录制或选择轨迹，补充场景、起始姿态和完成条件，再保存复用 | `record` 仅录制轨迹，不会自动创建上下文技能 |
| 技能仓库 | 打开“技能仓库”，搜索自己的 VLA 模型、策略和上下文技能，再进入对应配置页运行 | `store mine` 是已购商店技能，不是个人技能仓库 |
| 姿态记忆 | 舵机调试中保存当前实际姿态；调用前检查设备 / 限位和运动空间，重新校准后重新记录 | 浏览器本地保存，不会随 API Key 同步到 CLI |

## 7. 直接调用 HTTP API

在已经设置 `B2R_API_KEY` 和 `B2R_DEVICE` 的 PowerShell 中：

```powershell
$baseUrl = $env:B2R_SERVER
$headers = @{ Authorization = "Bearer $env:B2R_API_KEY" }
Invoke-RestMethod -Uri "$baseUrl/api/devices" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/device/$env:B2R_DEVICE/servos" -Headers $headers
# 读取完整轨迹 / 训练任务 ID
Invoke-RestMethod -Uri "$baseUrl/api/device/$env:B2R_DEVICE/trajectories" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/training/jobs" -Headers $headers
```

已确认第 5 节的小幅动作条件后，可以用同一凭证发送命令：

```powershell
$body = @{ id = 1; position = 2068; speed = 300 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/device/$env:B2R_DEVICE/command" -Headers $headers -ContentType "application/json" -Body $body
```

HTTP 不经过 CLI 交互确认。自己的应用需要在发出动作前确认目标与用户意图，并检查 HTTP 状态、响应错误和设备反馈。

## 8. 排障、环境变量与停用

| 现象 | 处理 |
|---|---|
| 401 | 检查完整 Key、服务器地址、是否已吊销，以及旧环境变量是否覆盖新登录 |
| 403 | 检查功能域、资源归属与共享权限；只有 `inference` 可能不足以完成部署前的任务查询 |
| No device / 选错设备 | 设置完整 `B2R_DEVICE`，然后重新读取状态 |
| 相机 204 / 无图像 | 检查相机 ID 和在线状态，短时开启预览后等待新帧 |
| 409 / 设备忙 | 先查看当前任务，使用对应页面停止；不要自动抢占控制 |
| 提交成功但未运动 | 检查设备实际状态；不要因为超时盲目重发动作或购买请求 |
| Unknown command: say / exec / shell | 这些属于另一套项目内 CLI，不是本目录 `b2r.py` 的命令 |

| 变量 | 含义 |
|---|---|
| `B2R_SERVER` | API 基础地址，默认 `https://robot.box2ai.com` |
| `B2R_API_KEY` | 平台 API Key，优先级最高 |
| `B2R_TOKEN` | Bearer 凭证，优先于本地缓存，低于 `B2R_API_KEY` |
| `B2R_DEVICE` | 默认机械臂 ID，覆盖缓存值 |

`python b2r.py logout` 只删除 `~/.b2r_token`，**不会吊销服务器上的 Key，也不会清除环境变量**。PowerShell 用 `Remove-Item Env:B2R_API_KEY, Env:B2R_TOKEN -ErrorAction SilentlyContinue`，Bash 用 `unset B2R_API_KEY B2R_TOKEN` 清除当前终端凭证。永久停用时登录网页，在“API 密钥”找到对应名称并点击“吊销”。

缓存文件包含明文凭证。CLI 尝试设置 POSIX 0600；Windows 还应依赖账号目录的访问控制。不要提交缓存、Key、截图中的密钥或认证请求头到公开仓库。
