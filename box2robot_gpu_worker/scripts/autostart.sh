#!/usr/bin/env bash
# 一键 Ubuntu 22.04 开机自启 — 读取当前目录作为工作目录
#
# 用法:
#   cd /your/project/dir
#   sudo bash autostart.sh                      # 交互问启动命令
#   sudo bash autostart.sh "b2r-gpu --server https://robot.box2ai.com"
#   sudo bash autostart.sh -n myapp "python app.py"
#   sudo bash autostart.sh --uninstall          # 卸载（按当前目录推算服务名）
#
# 自动:
#   - 工作目录    = $PWD
#   - 服务名      = 当前目录名 (可用 -n 覆盖)
#   - PATH 注入   = ./venv/bin、conda、CUDA bin (如存在)
#   - 日志        = /var/log/<服务名>.log
#   - 崩溃自动重启 (Restart=always, RestartSec=10)
set -e

[[ $EUID -eq 0 ]] || { echo "[ERR] 请用 sudo 运行"; exit 1; }
command -v systemctl >/dev/null || { echo "[ERR] 系统无 systemd"; exit 1; }

WORKDIR="$PWD"
SVC_NAME=""
SVC_USER="root"
EXEC_CMD=""
UNINSTALL="no"

# ---------- 参数 ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--name)   SVC_NAME="$2"; shift 2 ;;
    -u|--user)   SVC_USER="$2"; shift 2 ;;
    --uninstall) UNINSTALL="yes"; shift ;;
    -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
    *)           EXEC_CMD="$*"; break ;;
  esac
done

# 服务名兜底: 当前目录名 → 转小写 + 替换非法字符
[[ -z "$SVC_NAME" ]] && SVC_NAME=$(basename "$WORKDIR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/^-*//;s/-*$//')
SVC_FILE="/etc/systemd/system/${SVC_NAME}.service"
LOG_FILE="/var/log/${SVC_NAME}.log"

# ---------- 卸载 ----------
if [[ "$UNINSTALL" == "yes" ]]; then
  systemctl stop    "$SVC_NAME" 2>/dev/null || true
  systemctl disable "$SVC_NAME" 2>/dev/null || true
  rm -f "$SVC_FILE"
  systemctl daemon-reload
  echo "[OK] 已卸载: $SVC_NAME"
  exit 0
fi

# ---------- 启动命令: 参数 > 交互 ----------
if [[ -z "$EXEC_CMD" ]]; then
  echo "工作目录: $WORKDIR"
  echo "可执行入口建议:"
  [[ -x "$WORKDIR/venv/bin/b2r-gpu" ]] && echo "  $WORKDIR/venv/bin/b2r-gpu --server https://robot.box2ai.com"
  [[ -x "$WORKDIR/venv/bin/python" ]] && [[ -f "$WORKDIR/app.py" ]] && echo "  $WORKDIR/venv/bin/python app.py"
  command -v b2r-gpu >/dev/null && echo "  $(command -v b2r-gpu) --server https://robot.box2ai.com"
  read -rp "启动命令 (绝对路径或可在 PATH 中找到): " EXEC_CMD
  [[ -z "$EXEC_CMD" ]] && { echo "[ERR] 启动命令必填"; exit 1; }
fi

# ---------- 自动构建 PATH (venv / conda / CUDA) ----------
EXTRA_PATH=""
[[ -d "$WORKDIR/venv/bin" ]]            && EXTRA_PATH+="$WORKDIR/venv/bin:"
[[ -d "$WORKDIR/.venv/bin" ]]           && EXTRA_PATH+="$WORKDIR/.venv/bin:"
# 从 EXEC_CMD 推断 conda env bin (e.g. .../envs/b2r/bin/xxx → 把 .../envs/b2r/bin 加进 PATH)
EXEC_BIN_DIR="$(dirname "$(echo "$EXEC_CMD" | awk '{print $1}')")"
[[ -d "$EXEC_BIN_DIR" && ":$EXTRA_PATH:" != *":$EXEC_BIN_DIR:"* ]] && EXTRA_PATH+="$EXEC_BIN_DIR:"
[[ -d /root/miniconda3/bin ]]           && EXTRA_PATH+="/root/miniconda3/bin:"
[[ -d /opt/conda/bin ]]                 && EXTRA_PATH+="/opt/conda/bin:"
for d in /usr/local/cuda/bin /usr/local/cuda-12.4/bin /usr/local/cuda-12.1/bin; do
  [[ -d "$d" ]] && EXTRA_PATH+="$d:" && break
done

# ---------- 写 service ----------
[[ -f "$SVC_FILE" ]] && systemctl stop "$SVC_NAME" 2>/dev/null || true
touch "$LOG_FILE" && chown "$SVC_USER":"$SVC_USER" "$LOG_FILE" 2>/dev/null || true

cat > "$SVC_FILE" <<EOF
[Unit]
Description=$SVC_NAME (auto-start from $WORKDIR)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$WORKDIR
Environment=PATH=${EXTRA_PATH}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1
ExecStart=$EXEC_CMD
Restart=always
RestartSec=10
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$SVC_FILE"

systemctl daemon-reload
systemctl enable  "$SVC_NAME" >/dev/null
systemctl restart "$SVC_NAME"
sleep 2

# ---------- 报告 ----------
echo
echo "================ 完成 ================"
echo " 服务名     : $SVC_NAME"
echo " 工作目录   : $WORKDIR"
echo " 启动命令   : $EXEC_CMD"
echo " 日志文件   : $LOG_FILE"
if systemctl is-active --quiet "$SVC_NAME"; then
  echo " 状态       : ✓ active (开机自启已启用)"
else
  echo " 状态       : ✗ 启动失败! 看日志:"
  echo "   journalctl -u $SVC_NAME -n 40 --no-pager"
fi
echo
echo " 常用命令:"
echo "   systemctl status   $SVC_NAME"
echo "   systemctl restart  $SVC_NAME"
echo "   systemctl stop     $SVC_NAME"
echo "   journalctl -u $SVC_NAME -f"
echo "   tail -f $LOG_FILE"
echo "   sudo bash $0 --uninstall"
echo "======================================"
