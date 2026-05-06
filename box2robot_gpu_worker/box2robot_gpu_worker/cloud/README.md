# cloud — AutoDL GPU 实例生命周期管理

按需开关机一台预配置好的 AutoDL 实例，并在开机后通过 SSH 启动 `b2r-worker` 守护进程接收训练 / 推理任务。用户在 box2robot 平台点 "训练" / "推理" → server 调 `ensure_running()` → AutoDL 开机 → daemon 自动从 server 拉任务执行。

## 文件

| 文件 | 职责 |
|---|---|
| `autodl.py` | AutoDL HTTP 客户端：`list_instances` / `get_instance` / `power_on` / `power_off` / `wait_until_running` |
| `orchestrator.py` | 生命周期：`ensure_running`（幂等开机 + SSH 起 daemon）/ `shutdown` / `ssh_run` |
| `__main__.py` | 调试 CLI |

## ⚠️ 实现说明

调用的是 AutoDL 控制台**内部 API**（`/api/v1/instance/*`，社区逆向得出），**非官方 OpenAPI**。理论上接口结构可能被官方改动；若 `list_instances` 解析失败，调整 `autodl.py:_parse` 的字段映射即可。

官方公开的"弹性部署 API" (`esd_api_doc`) 是 serverless 容器模型（创建即跑、跑完销毁），不适合"长期常驻、按需开关机"的场景。

## 1. 准备工作

### 1.1 控制端（box2robot-server 所在机器）
```bash
pip install requests   # 已在项目依赖中
export AUTODL_TOKEN=<控制台→设置→开发者 Token>
ssh-keygen -t ed25519 -f ~/.ssh/autodl_id   # 若还没专用密钥
```

### 1.2 AutoDL 实例侧（一次性手动准备）
1. 创建实例并选好 GPU / 镜像
2. 把控制端公钥（`~/.ssh/autodl_id.pub`）写进实例的 `~/.ssh/authorized_keys`
3. 安装 `box2robot_gpu_worker` 并验证 `which b2r-worker` 有结果
4. 把环境变量写入 `~/.bashrc`（daemon 需要它们连回 server）：
   ```bash
   export B2R_SERVER=https://robot.box2ai.com
   export B2R_TOKEN=<worker 鉴权 token>
   ```
5. 手动跑一次 `b2r-worker`，确认能注册到 server 并拉到任务后再退出，再交给本模块自动化

## 2. CLI 用法（先手测打通）

```bash
# 列实例（拿到要管理的 uuid）
python -m box2robot_gpu_worker.cloud list

# 查状态
python -m box2robot_gpu_worker.cloud status <uuid>

# 仅开机（不启 daemon），用于第一次调试 SSH 是否通
python -m box2robot_gpu_worker.cloud start <uuid>

# 开机 + 通过 SSH 起 b2r-worker（生产路径）
python -m box2robot_gpu_worker.cloud start <uuid> --daemon --ssh-key ~/.ssh/autodl_id

# 关机
python -m box2robot_gpu_worker.cloud stop <uuid>
```

## 3. Python API 用法

### 3.1 直接控制
```python
from box2robot_gpu_worker.cloud import AutoDLClient

cli = AutoDLClient.from_env()                # 读 AUTODL_TOKEN
print(cli.list_instances())

inst = cli.get_instance("xxxx-uuid")
if not inst.is_running:
    cli.power_on(inst.uuid, mode="gpu")      # mode="non_gpu" 为无卡模式
    inst = cli.wait_until_running(inst.uuid, timeout=240)

print(f"ssh -p {inst.ssh_port} root@{inst.ssh_host}")

cli.power_off(inst.uuid)
```

### 3.2 编排（推荐：开机 + 自动起 daemon）
```python
from box2robot_gpu_worker.cloud import AutoDLClient, GpuConfig, GpuOrchestrator

orch = GpuOrchestrator(
    AutoDLClient.from_env(),
    GpuConfig(
        instance_uuid="xxxx-uuid",
        ssh_user="root",
        ssh_key_path="/root/.ssh/autodl_id",
        # 默认命令已是: source /etc/network_turbo; cd /root/box2robot_gpu_worker; nohup b2r-worker ...
        # 需自定义可覆盖：
        # daemon_cmd="cd /root/box2robot_gpu_worker && nohup b2r-worker > /root/worker.log 2>&1 &",
    ),
)

inst = orch.ensure_running(with_daemon=True)  # 幂等：已开机则跳过开机直接重启 daemon
# ... 用户的训练任务由 daemon 自己从 server 拉取并执行 ...

orch.ssh_run("tail -n 50 /root/worker.log", timeout=15)  # 远程查看 daemon 日志
orch.shutdown()                                           # 用完关机
```

## 4. 接入 box2robot-server

伪代码（写在 `core/routes/training_routes.py` 之类的位置）：

```python
from box2robot_gpu_worker.cloud import AutoDLClient, GpuConfig, GpuOrchestrator

# 模块级单例（线程安全：内部有 Lock）
_orch = GpuOrchestrator(
    AutoDLClient.from_env(),
    GpuConfig(instance_uuid=os.environ["AUTODL_INSTANCE_UUID"],
              ssh_key_path=os.environ["AUTODL_SSH_KEY"]),
)

@router.post("/api/training/jobs")
async def create_training_job(req):
    job = create_job_in_db(req)                       # 1. 入库 pending
    await asyncio.to_thread(_orch.ensure_running, True)  # 2. 开机 + 拉 daemon（阻塞放线程池）
    return {"job_id": job.id}
    # 3. daemon 上线后自己从 server 轮询拉这个 job → 训练 → 上报 checkpoint
```

## 5. 空闲自动关机（骨架未实现，自行选一种）

### 方案 A：服务端定时任务（推荐先用）
Server 每 5 min 查"是否有活跃任务"，连续 N 次都空闲就 `orch.shutdown()`。逻辑集中、易调试。

### 方案 B：实例内自杀
`b2r-worker` 任务队列空闲 N min → `os.system("shutdown -h now")`。无需 server 介入但调试稍麻烦。

## 6. 故障排查

| 现象 | 可能原因 |
|---|---|
| `power_on` 返回 `code != Success` | Token 失效 / 实例已过期 / 余额不足 |
| `wait_until_running` 超时 | GPU 库存紧张排队中 → 调大 `power_on_timeout` 或换可用区 |
| `ssh not ready` | 公钥未装 / `ssh_key_path` 路径错 / 端口被防火墙拦 |
| `daemon launch failed` | 实例上 `b2r-worker` 不在 PATH / `B2R_SERVER` 没写入 `~/.bashrc`（SSH 非交互式 shell 不读 `.bashrc`，需在 `daemon_cmd` 里显式 export） |
| `list_instances` 返回空 / KeyError | AutoDL 改了响应字段 → 改 `autodl.py:_parse` |

## 7. 渐进式开发顺序（按 CLAUDE.md 铁律）

1. **达成**：`start --daemon` 手动跑通 SSH 起 daemon（已完成）
2. **优化**：接入 server 路由，前端按钮触发开机
3. **加固**：加空闲关机、加重试、加 daemon 健康检查、token 失效告警
