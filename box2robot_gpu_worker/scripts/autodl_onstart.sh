#!/bin/bash
# Box2Robot GPU Worker - AutoDL 开机自启脚本
# 部署位置: /root/onstart.sh
# AutoDL 控制台 "更多 → 自定义服务/开机执行命令" 设为: bash /root/onstart.sh
#
# 行为:
#   - 激活 conda 环境 b2r 的 PATH
#   - 进入工作目录 /root/autodl-tmp/workspace/box2robot/box2robot_gpu_worker
#   - 后台启动 b2r-gpu --server <SERVER> --output <OUTPUT_DIR>
#     OUTPUT_DIR 默认指向 autodl-fs (跨实例共享, 模型推理可在任意实例跑)
#   - 已在跑则跳过 (PID 文件幂等)
#   - 日志: /root/b2r_worker.log
#
# 可调环境变量:
#   B2R_SERVER          worker 连的 server (默认 https://robot.box2ai.com)
#   B2R_OUTPUT_DIR      worker --output, 默认 /root/autodl-fs/box2robot-outputs/pool-default
#   B2R_POOL            pool 名, 用于二级目录隔离 (默认 pool-default)
#   HF_ENDPOINT         HF 镜像
#   PIP_INDEX_URL       pip 镜像

PID_FILE="/root/b2r_worker.pid"
LOG_FILE="/root/b2r_worker.log"

exec >> "$LOG_FILE" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') onstart triggered ====="

# 1. PATH + 国内镜像
export PATH=/root/miniconda3/envs/b2r/bin:/root/miniconda3/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}

# 2. 幂等: 检查 PID 文件
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        # 进一步确认是 b2r-gpu (防 PID 复用)
        if grep -q "b2r-gpu" "/proc/$OLD_PID/cmdline" 2>/dev/null; then
            echo "b2r-gpu 已在运行 (pid=$OLD_PID), 跳过"
            exit 0
        fi
    fi
    rm -f "$PID_FILE"
fi

# 3. 切到 worker 目录 (源码在数据盘, 跨实例重启保留)
WORKDIR="/root/autodl-tmp/workspace/box2robot/box2robot_gpu_worker"
if [ -d "$WORKDIR" ]; then
    cd "$WORKDIR"
    echo "cwd: $WORKDIR"
else
    cd /root
    echo "WARN: $WORKDIR 不存在, 退到 /root"
fi

# 4. 启动
B2R_GPU="/root/miniconda3/envs/b2r/bin/b2r-gpu"
if [ ! -x "$B2R_GPU" ]; then
    echo "ERROR: $B2R_GPU 不存在, 请先 conda activate b2r && pip install -e ."
    exit 1
fi

SERVER="${B2R_SERVER:-https://robot.box2ai.com}"

# 5. 输出目录 → autodl-fs (跨实例共享, 模型可在任意 manager 实例上推理)
# autodl-fs 是 AutoDL 文件存储, 同账号下所有实例自动挂载到 /root/autodl-fs (软链 /autodl-fs/data)
POOL="${B2R_POOL:-pool-default}"
DEFAULT_OUTPUT="/root/autodl-fs/box2robot-outputs/${POOL}"
OUTPUT_DIR="${B2R_OUTPUT_DIR:-$DEFAULT_OUTPUT}"

if [ -d "/root/autodl-fs" ]; then
    mkdir -p "$OUTPUT_DIR" 2>/dev/null
    echo "output dir: $OUTPUT_DIR (autodl-fs, 跨实例共享)"
else
    # 兼容: 没挂载 autodl-fs 的环境 fallback 到本地
    OUTPUT_DIR="$WORKDIR/outputs"
    echo "WARN: /root/autodl-fs 未挂载, fallback 到本地 $OUTPUT_DIR"
fi

echo "starting: $B2R_GPU --server $SERVER --output $OUTPUT_DIR"
nohup "$B2R_GPU" --server "$SERVER" --output "$OUTPUT_DIR" >> "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
disown $PID 2>/dev/null || true
sleep 2

if kill -0 $PID 2>/dev/null; then
    echo "OK: b2r-gpu started pid=$PID (pidfile=$PID_FILE)"
else
    echo "ERROR: b2r-gpu 起动后 2s 内退出, 看上方日志"
    rm -f "$PID_FILE"
    tail -20 "$LOG_FILE"
    exit 1
fi
