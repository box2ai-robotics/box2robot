"""AutoDL 实例控制客户端。

调用控制台内部 API（社区逆向，非官方 OpenAPI）：
  POST /api/v1/instance              列表
  POST /api/v1/instance/power_on     开机（payload="non_gpu" 为无卡模式）
  POST /api/v1/instance/power_off    关机

Token 来源：AutoDL 控制台 → 设置 → 开发者 Token。
若接口字段后续被改动，调整 list_instances 的解析即可。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.autodl.com/api/v1"
PowerMode = Literal["gpu", "non_gpu"]


class AutoDLError(RuntimeError):
    pass


@dataclass
class Instance:
    uuid: str
    name: str
    status: str
    gpu_type: str = ""
    ssh_host: str | None = None
    ssh_port: int | None = None
    raw: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self.status == "running"


class AutoDLClient:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: int = 30):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {
            "Authorization": token,
            "Content-Type": "application/json;charset=UTF-8",
        }

    @classmethod
    def from_env(cls, var: str = "AUTODL_TOKEN") -> AutoDLClient:
        token = os.getenv(var)
        if not token:
            raise AutoDLError(f"env {var} not set")
        return cls(token)

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.post(
            url,
            headers=self._headers,
            data=json.dumps(body),
            timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError as e:
            raise AutoDLError(f"{path} non-json response (HTTP {resp.status_code}): {resp.text[:200]}") from e
        if resp.status_code >= 400:
            raise AutoDLError(f"{path} HTTP {resp.status_code}: {data}")
        if data.get("code") not in ("Success", 0, "0", None):
            raise AutoDLError(f"{path} returned: {data}")
        return data

    def list_instances(self, page_size: int = 50) -> list[Instance]:
        body = {
            "date_from": "",
            "date_to": "",
            "page_index": 1,
            "page_size": page_size,
            "status": [],
            "charge_type": [],
        }
        data = self._post("/instance", body)
        items = (data.get("data") or {}).get("list") or []
        return [self._parse(it) for it in items]

    def get_instance(self, uuid: str) -> Instance:
        for inst in self.list_instances():
            if inst.uuid == uuid:
                return inst
        raise AutoDLError(f"instance not found: {uuid}")

    def power_on(self, uuid: str, mode: PowerMode = "gpu") -> dict:
        body: dict[str, Any] = {"instance_uuid": uuid}
        if mode == "non_gpu":
            body["payload"] = "non_gpu"
        log.info("power_on uuid=%s mode=%s", uuid, mode)
        return self._post("/instance/power_on", body)

    def power_off(self, uuid: str) -> dict:
        log.info("power_off uuid=%s", uuid)
        return self._post("/instance/power_off", {"instance_uuid": uuid})

    def wait_until_running(
        self,
        uuid: str,
        timeout: int = 180,
        poll: float = 3.0,
    ) -> Instance:
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            inst = self.get_instance(uuid)
            if inst.status != last_status:
                log.info("instance %s status=%s", uuid, inst.status)
                last_status = inst.status
            if inst.is_running:
                return inst
            if inst.status in {"power_on_failed", "expired", "released"}:
                raise AutoDLError(f"power_on failed, status={inst.status}")
            time.sleep(poll)
        raise AutoDLError(f"instance {uuid} not running after {timeout}s")

    @staticmethod
    def _parse(it: dict) -> Instance:
        snap = it.get("machine_info_snapshot") or {}
        return Instance(
            uuid=it.get("uuid", ""),
            name=it.get("machine_alias") or it.get("name") or "",
            status=it.get("status", "unknown"),
            gpu_type=snap.get("gpu_name") or snap.get("gpu_type") or "",
            ssh_host=it.get("proxy_host") or it.get("ssh_host"),
            ssh_port=it.get("ssh_port"),
            raw=it,
        )
