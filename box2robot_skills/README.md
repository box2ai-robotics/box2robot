# Box2Robot Skills CLI

一行命令控制机械臂。本地或服务器均可使用。

## 快速开始

```bash
# 1. 登录 (token 自动保存到 ~/.b2r_token)
python b2r.py login <username> <password>

# 2. 查看设备
python b2r.py devices

# 3. 操控
python b2r.py home                    # 回零位
python b2r.py move 1 2048             # 1号舵机转到2048
python b2r.py torque off              # 释放力矩
python b2r.py say "回零位"             # 自然语言
python b2r.py shell                   # 交互模式
```

## 全部命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `login [user] [pass]` | 登录，保存 token | `b2r.py login <username> <password>` |
| `devices` | 列出所有设备 | `b2r.py devices` |
| `status [device_id]` | 舵机状态 | `b2r.py status` |
| `move <id> <pos> [speed]` | 移动舵机 | `b2r.py move 1 2048 500` |
| `home` | 回零位 | `b2r.py home` |
| `torque on/off` | 力矩开关 | `b2r.py torque off` |
| `record start/stop/status` | 录制控制 | `b2r.py record start` |
| `play [traj_id]` | 播放轨迹 (无参数=列出) | `b2r.py play` |
| `say "文本"` | 自然语言执行 | `b2r.py say "拍照"` |
| `exec <action> [params]` | 直接调 Action | `b2r.py exec camera.snapshot` |
| `shell` | 交互式 Shell | `b2r.py shell` |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `B2R_SERVER` | 服务器地址 | `https://robot.box2ai.com` |
| `B2R_TOKEN` | JWT Token (覆盖 ~/.b2r_token) | — |
| `B2R_DEVICE` | 默认设备ID (覆盖自动选择) | — |

## 认证机制

```
login → POST /api/auth/login → JWT Token → ~/.b2r_token
后续命令自动读取 token，无需重复登录
Token 有效期内免登录 (服务器端控制过期)
```

## 详细文档

- `SKILLS.md` — AI Agent 调度参考手册 (给 Claude/GPT 等 LLM 用)
- `ACTIONS.md` — 79 个 Action 完整列表 + 参数说明
- `../box2robot_server/box2robot-cli/` — 实现代码
