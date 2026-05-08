# AutoDL 实例开机自启 b2r-gpu

**为什么不用 systemd**: AutoDL 容器 systemd 不是 PID 1, `systemctl enable` 写的 unit 不会在容器启动时被触发。已实测，`install_on_autodl.sh` 末尾那段 systemd 配置在 AutoDL 上不生效。

**正确做法**: AutoDL 控制台支持每次开机执行一条命令，把它指向 `/root/onstart.sh`，由它来拉起 worker。

## 部署 (一次性)

### 1. 上传脚本到实例

```bash
# 本地终端 (替换 PORT/HOST 为你的 AutoDL SSH 信息):
scp -P <PORT> box2robot_gpu_worker/scripts/autodl_onstart.sh root@<HOST>:/root/onstart.sh
ssh -p <PORT> root@<HOST> "chmod +x /root/onstart.sh && bash /root/onstart.sh"
```

执行后查 `/root/b2r_worker.log` 应看到 `OK: b2r-gpu started pid=...`。

### 2. 在 AutoDL 控制台配置开机执行

- 登录 https://www.autodl.com → 容器实例
- 找到目标实例 → "更多" → **"自定义服务"** 或 **"开机执行命令"**
- 填入: `bash /root/onstart.sh`
- 保存

之后每次实例从"关机"恢复成"运行中"，AutoDL 会自动跑这条命令，worker 自启。

## 脚本行为

| 项 | 值 |
|---|---|
| 工作目录 | `/root/autodl-tmp/workspace/box2robot/box2robot_gpu_worker` (数据盘, 跨重启保留) |
| Conda env | `b2r` (`/root/miniconda3/envs/b2r`) |
| 入口 | `b2r-gpu --server https://robot.box2ai.com` |
| 日志 | `/root/b2r_worker.log` |
| PID 文件 | `/root/b2r_worker.pid` |
| 幂等性 | 已在跑则跳过 (PID 文件 + `/proc/<pid>/cmdline` 双重确认, 防 PID 复用) |

## 自定义

环境变量覆盖默认值：

```bash
B2R_SERVER=https://your-server.com bash /root/onstart.sh
HF_ENDPOINT=https://hf-mirror.com bash /root/onstart.sh
```

## 常用运维

```bash
# 看实时日志
tail -f /root/b2r_worker.log

# 看 worker 状态
cat /root/b2r_worker.pid && ps -p $(cat /root/b2r_worker.pid) -o pid,etime,cmd

# 手动重启
kill $(cat /root/b2r_worker.pid); bash /root/onstart.sh

# 停掉
kill $(cat /root/b2r_worker.pid) && rm /root/b2r_worker.pid
```

## 实测验证 (2026-05-08)

实例: `connect.westb.seetacloud.com:37527`

- ✅ 首次启动: `OK: b2r-gpu started pid=3640`
- ✅ 重复执行: `b2r-gpu 已在运行 (pid=3640), 跳过`
- ✅ kill 后再跑: 自动启动新进程 (pid=3805)
- ✅ Worker 日志正常: `[HF_HOME] 设为...base_model`, `[OTA] 已是最新版本 v0.6.3`

## 网盘 / 数据持久化

AutoDL 三类存储:

| 路径 | 持久性 | 用途 |
|---|---|---|
| `/root/` 系统盘 | 关机保留, 换镜像丢 | onstart.sh 本身够用 |
| `/root/autodl-tmp/` 数据盘 | 关机保留, 换镜像丢 | **代码 + 数据集 + checkpoint (推荐)** |
| `/root/autodl-fs/` 网盘 | **跨实例持久**, 但慢 | 跨实例共享的模型/配置 |

`/root/autodl-fs` 是 AutoDL 自动挂载的，无需手动 mount。

## 与 install_on_autodl.sh 的关系

`install_on_autodl.sh` 是 manager 自动部署用的全套脚本（装依赖 + 拿绑定码 + 配自启），其末尾的 systemd 段在 AutoDL 不生效。建议: 在该脚本里把 Phase 5 替换成"写 `/root/onstart.sh`"，并提示用户去控制台勾选开机执行。本目录 `autodl_onstart.sh` 是已验证的独立版本，可手动部署使用。
