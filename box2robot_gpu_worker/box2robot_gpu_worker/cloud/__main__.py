"""调试 CLI。

  export AUTODL_TOKEN=xxx
  python -m box2robot_gpu_worker.cloud list
  python -m box2robot_gpu_worker.cloud status <uuid>
  python -m box2robot_gpu_worker.cloud start  <uuid>           # 开机（不启 daemon）
  python -m box2robot_gpu_worker.cloud start  <uuid> --daemon  # 开机 + SSH 启动 b2r-worker
  python -m box2robot_gpu_worker.cloud stop   <uuid>
"""
from __future__ import annotations

import argparse
import logging
import sys

from .autodl import AutoDLClient
from .orchestrator import GpuConfig, GpuOrchestrator


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="box2robot_gpu_worker.cloud")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("status", "start", "stop"):
        p = sub.add_parser(name)
        p.add_argument("uuid")
    sub.choices["start"].add_argument("--daemon", action="store_true")
    sub.choices["start"].add_argument("--ssh-key", default=None)

    args = parser.parse_args()
    cli = AutoDLClient.from_env()

    if args.cmd == "list":
        for inst in cli.list_instances():
            print(f"{inst.uuid}  {inst.status:10}  {inst.gpu_type:20}  {inst.name}")
        return 0

    if args.cmd == "status":
        inst = cli.get_instance(args.uuid)
        print(f"uuid={inst.uuid} status={inst.status} gpu={inst.gpu_type} ssh={inst.ssh_host}:{inst.ssh_port}")
        return 0

    if args.cmd == "stop":
        cli.power_off(args.uuid)
        print("stop requested")
        return 0

    if args.cmd == "start":
        cfg = GpuConfig(instance_uuid=args.uuid, ssh_key_path=args.ssh_key)
        orch = GpuOrchestrator(cli, cfg)
        inst = orch.ensure_running(with_daemon=args.daemon)
        print(f"running: ssh -p {inst.ssh_port} root@{inst.ssh_host}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
