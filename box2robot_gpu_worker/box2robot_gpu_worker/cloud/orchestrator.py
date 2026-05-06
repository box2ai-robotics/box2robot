"""GPU 实例生命周期编排：开机 → 等 SSH 就绪 → 启动 b2r-worker daemon → 关机。

使用前提（用户已手动准备好的 AutoDL 实例）：
  1. 实例已安装 box2robot_gpu_worker 及 LeRobot 依赖
  2. 实例已配置好 SSH 公钥认证（或 ssh_key_path 指定私钥）
  3. 实例环境变量 B2R_SERVER / B2R_TOKEN 已写入 ~/.bashrc，或在 daemon_cmd 内显式 export
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from threading import Lock

from .autodl import AutoDLClient, AutoDLError, Instance

log = logging.getLogger(__name__)


@dataclass
class GpuConfig:
    instance_uuid: str
    ssh_user: str = "root"
    ssh_key_path: str | None = None
    daemon_cmd: str = (
        "source /etc/network_turbo 2>/dev/null; "
        "cd /root/box2robot_gpu_worker && "
        "nohup b2r-worker > /root/worker.log 2>&1 &"
    )
    ssh_ready_timeout: int = 90
    power_on_timeout: int = 240


class GpuOrchestrator:
    """单实例生命周期管理。线程安全（单进程内）。"""

    def __init__(self, autodl: AutoDLClient, cfg: GpuConfig):
        self.autodl = autodl
        self.cfg = cfg
        self._lock = Lock()

    def status(self) -> Instance:
        return self.autodl.get_instance(self.cfg.instance_uuid)

    def ensure_running(self, with_daemon: bool = True) -> Instance:
        """幂等：实例已 running 则直接返回；否则开机+等待+(可选)启动 daemon。"""
        with self._lock:
            inst = self.autodl.get_instance(self.cfg.instance_uuid)
            if not inst.is_running:
                self.autodl.power_on(self.cfg.instance_uuid, mode="gpu")
                inst = self.autodl.wait_until_running(
                    self.cfg.instance_uuid,
                    timeout=self.cfg.power_on_timeout,
                )
            if with_daemon:
                self._wait_ssh(inst)
                self._launch_daemon(inst)
            return inst

    def shutdown(self) -> None:
        with self._lock:
            self.autodl.power_off(self.cfg.instance_uuid)

    def ssh_run(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
        inst = self.autodl.get_instance(self.cfg.instance_uuid)
        if not inst.is_running:
            raise AutoDLError(f"instance not running: status={inst.status}")
        return self._ssh_exec(inst, cmd, timeout=timeout)

    def _ssh_args(self, inst: Instance) -> list[str]:
        if not inst.ssh_host or not inst.ssh_port:
            raise AutoDLError(f"instance has no ssh_host/port: {inst}")
        args = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", "LogLevel=ERROR",
            "-p", str(inst.ssh_port),
        ]
        if self.cfg.ssh_key_path:
            args += ["-i", self.cfg.ssh_key_path]
        args.append(f"{self.cfg.ssh_user}@{inst.ssh_host}")
        return args

    def _ssh_exec(self, inst: Instance, cmd: str, timeout: int) -> subprocess.CompletedProcess:
        args = self._ssh_args(inst) + [cmd]
        log.debug("ssh exec: %s", shlex.join(args))
        return subprocess.run(args, capture_output=True, timeout=timeout)

    def _wait_ssh(self, inst: Instance) -> None:
        deadline = time.time() + self.cfg.ssh_ready_timeout
        while time.time() < deadline:
            r = self._ssh_exec(inst, "echo ok", timeout=15)
            if r.returncode == 0 and b"ok" in r.stdout:
                log.info("ssh ready: %s:%s", inst.ssh_host, inst.ssh_port)
                return
            time.sleep(3)
        raise AutoDLError("ssh not ready before timeout")

    def _launch_daemon(self, inst: Instance) -> None:
        log.info("launching daemon: %s", self.cfg.daemon_cmd)
        r = self._ssh_exec(inst, self.cfg.daemon_cmd, timeout=30)
        if r.returncode != 0:
            raise AutoDLError(
                f"daemon launch failed (rc={r.returncode}): {r.stderr.decode(errors='ignore')[:300]}"
            )
