#!/usr/bin/env python3
"""
跨平台开机自启配置工具 — 把 b2r-gpu 注册成系统服务/计划任务。

Usage:
    # 安装 (默认连公网 server)
    python scripts/install_autostart.py install
    python scripts/install_autostart.py install --server https://robot.box2ai.com

    # 自定义启动参数
    python scripts/install_autostart.py install --output D:/b2r_outputs
    python scripts/install_autostart.py install --env HF_ENDPOINT=https://hf-mirror.com

    # 管理
    python scripts/install_autostart.py status
    python scripts/install_autostart.py start
    python scripts/install_autostart.py stop
    python scripts/install_autostart.py uninstall

平台行为:
    Linux:   写 systemd unit (默认 --user), 绑定 ~/.config/systemd/user/.
             用 --system 写 /etc/systemd/system/ (需 sudo, 真正系统启动时拉起).
             用户级服务用户登出会停, 让它在登出后常驻: sudo loginctl enable-linger $USER

    Windows: 注册 Task Scheduler 任务, 默认 ONLOGON (用户登录时, 无需管理员).
             用 --trigger boot 改 ONSTART (系统启动时, 需以管理员身份运行此脚本,
             并以 SYSTEM 账户跑 — 注意 SYSTEM 账户访问 GPU 通常 OK, 但读用户 HOME 受限).

要求: 运行此脚本前必须先 `conda activate b2r` 并 `pip install -e .`,
脚本会从当前 Python 环境定位 b2r-gpu 的绝对路径写入 unit/task.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "box2robot-gpu-worker"
TASK_NAME = "Box2RobotGpuWorker"
SCRIPT_DIR = Path(__file__).resolve().parent
WORKDIR = SCRIPT_DIR.parent  # box2robot_gpu_worker/


# ───────────────────────── 公共工具 ─────────────────────────

def find_b2r_gpu() -> str | None:
    """定位 b2r-gpu 可执行文件 (优先 PATH, 回退到 sys.executable 同目录)."""
    found = shutil.which("b2r-gpu")
    if found:
        return found
    py = Path(sys.executable)
    candidates = [
        py.parent / "b2r-gpu",                  # Linux conda env
        py.parent / "Scripts" / "b2r-gpu.exe",  # Windows conda env (root python)
        py.parent / "b2r-gpu.exe",              # Windows venv (Scripts already in parent)
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def parse_env(pairs: list[str] | None) -> list[tuple[str, str]]:
    """把 ['HF_ENDPOINT=https://...', 'CUDA_VISIBLE_DEVICES=0'] 解析成 [(K, V), ...]."""
    out = []
    for s in pairs or []:
        if "=" not in s:
            print(f"[警告] 忽略非法 --env: {s} (应为 KEY=VALUE)")
            continue
        k, v = s.split("=", 1)
        out.append((k.strip(), v.strip()))
    return out


def must_b2r_gpu() -> str:
    exe = find_b2r_gpu()
    if not exe:
        print("[错误] 找不到 b2r-gpu 可执行文件.")
        print("       请先 conda activate b2r 并执行 pip install -e . 后再跑本脚本.")
        sys.exit(1)
    return exe


# ───────────────────────── Linux (systemd) ─────────────────────────

SYSTEMD_UNIT = """[Unit]
Description=Box2Robot GPU Worker (auto-start)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={workdir}
ExecStart={exe} --server {server} --output {output}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
{env_lines}

[Install]
WantedBy={target}
"""


def _systemctl(user: bool, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    base = ["systemctl", "--user"] if user else ["sudo", "systemctl"]
    return subprocess.run([*base, *args], check=check)


def linux_install(args) -> None:
    exe = must_b2r_gpu()
    output = (WORKDIR / "outputs").as_posix()
    (WORKDIR / "outputs").mkdir(exist_ok=True)

    env_lines = "\n".join(f"Environment={k}={v}" for k, v in parse_env(args.env))
    target = "default.target" if args.user else "multi-user.target"

    unit = SYSTEMD_UNIT.format(
        workdir=WORKDIR.as_posix(),
        exe=exe,
        server=args.server,
        output=args.output or output,
        env_lines=env_lines,
        target=target,
    )

    if args.user:
        unit_dir = Path.home() / ".config/systemd/user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / f"{SERVICE_NAME}.service"
        unit_path.write_text(unit, encoding="utf-8")
        _systemctl(True, "daemon-reload", check=True)
        _systemctl(True, "enable", SERVICE_NAME, check=True)
        print(f"[OK] 已安装用户级 systemd 服务: {unit_path}")
        print(f"     立即启动:    systemctl --user start {SERVICE_NAME}")
        print(f"     查看状态:    systemctl --user status {SERVICE_NAME}")
        print(f"     实时日志:    journalctl --user -u {SERVICE_NAME} -f")
        print()
        print(f"     [提示] 用户级服务在用户登出后会停. 让它常驻 (重启后自动起):")
        print(f"            sudo loginctl enable-linger $USER")
    else:
        tmp = Path("/tmp") / f"{SERVICE_NAME}.service"
        tmp.write_text(unit, encoding="utf-8")
        unit_path = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
        subprocess.run(["sudo", "mv", str(tmp), str(unit_path)], check=True)
        _systemctl(False, "daemon-reload", check=True)
        _systemctl(False, "enable", SERVICE_NAME, check=True)
        print(f"[OK] 已安装系统级 systemd 服务: {unit_path}")
        print(f"     立即启动:    sudo systemctl start {SERVICE_NAME}")
        print(f"     查看状态:    sudo systemctl status {SERVICE_NAME}")
        print(f"     实时日志:    sudo journalctl -u {SERVICE_NAME} -f")


def linux_uninstall(args) -> None:
    _systemctl(args.user, "disable", "--now", SERVICE_NAME)
    if args.user:
        unit_path = Path.home() / f".config/systemd/user/{SERVICE_NAME}.service"
        if unit_path.exists():
            unit_path.unlink()
    else:
        unit_path = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
        if unit_path.exists():
            subprocess.run(["sudo", "rm", str(unit_path)])
    _systemctl(args.user, "daemon-reload")
    print(f"[OK] {SERVICE_NAME} 已卸载")


def linux_status(args) -> None:
    _systemctl(args.user, "status", SERVICE_NAME)


def linux_start(args) -> None:
    _systemctl(args.user, "start", SERVICE_NAME, check=True)
    print(f"[OK] {SERVICE_NAME} 已启动")


def linux_stop(args) -> None:
    _systemctl(args.user, "stop", SERVICE_NAME, check=True)
    print(f"[OK] {SERVICE_NAME} 已停止")


# ───────────────────────── Windows (Task Scheduler) ─────────────────────────

WIN_WRAPPER_BAT = """@echo off
REM Auto-generated by install_autostart.py — do NOT edit manually.
REM 修改: 重新跑 `python scripts\\install_autostart.py install ...`
{env_lines}
cd /d "{workdir}"
:loop
"{exe}" --server {server} --output "{output}" >> "{logfile}" 2>&1
echo [%DATE% %TIME%] b2r-gpu exited, restarting in 10s... >> "{logfile}"
timeout /t 10 /nobreak >nul
goto loop
"""


def windows_install(args) -> None:
    exe = must_b2r_gpu()
    output = Path(args.output) if args.output else WORKDIR / "outputs"
    output.mkdir(parents=True, exist_ok=True)

    logs_dir = WORKDIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    logfile = logs_dir / "gpu_worker_autostart.log"

    env_lines = "\n".join(f'set "{k}={v}"' for k, v in parse_env(args.env))
    wrapper = WORKDIR / "scripts" / "_autostart_wrapper.bat"
    wrapper.write_text(
        WIN_WRAPPER_BAT.format(
            env_lines=env_lines,
            workdir=str(WORKDIR),
            exe=exe,
            server=args.server,
            output=str(output),
            logfile=str(logfile),
        ),
        encoding="utf-8",
    )

    trigger = "ONSTART" if args.trigger == "boot" else "ONLOGON"
    cmd = [
        "schtasks", "/create",
        "/tn", TASK_NAME,
        "/tr", f'"{wrapper}"',
        "/sc", trigger,
        "/rl", "HIGHEST",
        "/f",
    ]
    if trigger == "ONSTART":
        cmd.extend(["/ru", "SYSTEM"])

    print(f"[执行] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    print(f"[OK] Windows 计划任务已注册: {TASK_NAME}")
    print(f"     触发方式:    {'用户登录时' if trigger == 'ONLOGON' else '系统启动时 (需管理员, 以 SYSTEM 账户跑)'}")
    print(f"     包装脚本:    {wrapper}")
    print(f"     日志文件:    {logfile}")
    print()
    print(f"     立即启动:    schtasks /run /tn {TASK_NAME}")
    print(f"     查看状态:    schtasks /query /tn {TASK_NAME} /v /fo LIST")
    print(f"     停止任务:    schtasks /end /tn {TASK_NAME}")
    print(f"     卸载:       python scripts\\install_autostart.py uninstall")


def windows_uninstall(args) -> None:
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"])
    wrapper = WORKDIR / "scripts" / "_autostart_wrapper.bat"
    if wrapper.exists():
        wrapper.unlink()
    print(f"[OK] {TASK_NAME} 已卸载")


def windows_status(args) -> None:
    subprocess.run(["schtasks", "/query", "/tn", TASK_NAME, "/v", "/fo", "LIST"])


def windows_start(args) -> None:
    subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], check=True)
    print(f"[OK] {TASK_NAME} 已触发启动")


def windows_stop(args) -> None:
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME])
    print(f"[OK] {TASK_NAME} 已停止")


# ───────────────────────── 入口 ─────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="b2r-gpu 开机自启配置工具 (Linux systemd / Windows Task Scheduler)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    inst = sub.add_parser("install", help="安装开机自启")
    inst.add_argument("--server", default="https://robot.box2ai.com", help="Server URL (默认: 公网)")
    inst.add_argument("--output", default=None, help="模型输出目录 (默认: ./outputs)")
    inst.add_argument("--user", action="store_true",
                      help="[Linux] 用户级 systemd (默认). 关闭则装系统级 (需 sudo)")
    inst.add_argument("--system", action="store_true",
                      help="[Linux] 装系统级 systemd 到 /etc/systemd/system/ (需 sudo)")
    inst.add_argument("--trigger", choices=["logon", "boot"], default="logon",
                      help="[Windows] logon=用户登录时(默认,免管理员) | boot=系统启动时(需管理员)")
    inst.add_argument("--env", action="append", default=[],
                      help="额外环境变量, 形如 KEY=VALUE, 可重复 (例: --env HF_ENDPOINT=https://hf-mirror.com)")

    for name, help_text in [
        ("uninstall", "卸载开机自启"),
        ("status", "查看运行状态"),
        ("start", "立即启动服务"),
        ("stop", "停止服务"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--user", action="store_true",
                        help="[Linux] 操作用户级服务 (默认). 关闭则操作系统级")
        sp.add_argument("--system", action="store_true",
                        help="[Linux] 操作系统级服务")

    args = p.parse_args()

    # --system 优先级高于 --user (Linux only)
    if hasattr(args, "system") and args.system:
        args.user = False
    elif hasattr(args, "user") and not args.user:
        # 默认 user 模式 (Linux)
        args.user = True

    is_windows = platform.system() == "Windows"
    handlers = {
        ("install", True):    windows_install,
        ("install", False):   linux_install,
        ("uninstall", True):  windows_uninstall,
        ("uninstall", False): linux_uninstall,
        ("status", True):     windows_status,
        ("status", False):    linux_status,
        ("start", True):      windows_start,
        ("start", False):     linux_start,
        ("stop", True):       windows_stop,
        ("stop", False):      linux_stop,
    }
    handlers[(args.cmd, is_windows)](args)


if __name__ == "__main__":
    main()
