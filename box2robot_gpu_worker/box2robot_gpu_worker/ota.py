"""GPU Worker OTA — 版本检测 / 一键升级 / 依赖完整性 / 自动重启.

升级源 (按优先级):
  1. 服务器镜像 (https://robot.box2ai.com/api/gpu/upgrade/{check,download}) — 国内稳定, 免 git
  2. GitHub 直连 (git pull, 仅 git clone 安装的用户) — fallback

设计:
- 版本检测: 先问服务器, 失败 fallback 到 GitHub raw __init__.py
- 升级方式: 优先服务器 zip → 解压 → pip install -e .; 否则 git pull
- 自动重启: 升级成功后 os.execv 替换当前进程加载新代码
- 离线友好: 网络失败不阻塞 worker 启动, 仅 log 警告

CLI 入口 (经 gpu_worker.py main 分发):
  b2r-gpu check-version    # 比对本地与最新版 (server 优先, GitHub fallback)
  b2r-gpu check-deps       # 检测核心依赖完整性
  b2r-gpu upgrade          # 升级 + 自动重启
  b2r-gpu upgrade --no-restart  # 升级后不自动重启
"""
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional, Tuple

import httpx

from box2robot_gpu_worker import __version__ as LOCAL_VERSION

logger = logging.getLogger("b2r.ota")

# 服务器镜像 (主路径) — gpu_routes.py 提供 /api/gpu/upgrade/{check,download}
DEFAULT_SERVER_URL = "https://robot.box2ai.com"

# GitHub 直连 (fallback)
GITHUB_REPO = "box2ai-robotics/box2robot"
GITHUB_BRANCH = "main"
VERSION_FILE_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
    f"/box2robot_gpu_worker/box2robot_gpu_worker/__init__.py"
)
GITHUB_REPO_URL = f"https://github.com/{GITHUB_REPO}"

# 核心依赖 — import 失败 = worker 无法工作
CORE_DEPS = [
    ("torch", "PyTorch (训练/推理)"),
    ("numpy", "numpy"),
    ("httpx", "HTTP 客户端"),
    ("yaml", "PyYAML"),
    ("psutil", "系统监控"),
    ("pyarrow", "LeRobot 数据集格式"),
    ("av", "PyAV (视频解码, lerobot[dataset] 必需)"),
]

# 重要依赖 — 缺失会限制部分功能
OPTIONAL_DEPS = [
    ("lerobot", "LeRobot (训练框架)"),
    ("datasets", "HuggingFace datasets"),
    ("transformers", "Transformers (VLA 模型必需)"),
    ("accelerate", "accelerate (分布式训练)"),
    ("peft", "PEFT (LoRA 微调)"),
]


def _parse_version(s: str) -> Tuple[int, ...]:
    """'0.6.2' → (0, 6, 2). 非数字段忽略."""
    parts = []
    for x in s.strip().strip('"').strip("'").split("."):
        m = re.match(r"^\d+", x)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def get_local_version() -> str:
    """返回本地 worker 版本号 (来自 __init__.py)."""
    return LOCAL_VERSION


def get_remote_version_from_github(timeout: float = 5.0) -> Optional[str]:
    """从 GitHub 拉 box2robot_gpu_worker 的 __init__.py 解析 __version__. 失败返回 None."""
    try:
        r = httpx.get(VERSION_FILE_URL, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        logger.warning("[OTA] 拉取 GitHub __init__.py 失败: %s", e)
        return None

    m = re.search(r'__version__\s*=\s*["\']([\d.]+)["\']', text)
    if not m:
        logger.warning("[OTA] GitHub __init__.py 找不到 __version__ 字段")
        return None
    return m.group(1)


def get_remote_version_from_server(server_url: str = DEFAULT_SERVER_URL,
                                   timeout: float = 5.0) -> Tuple[Optional[str], Optional[dict]]:
    """从 server 镜像拉版本信息. 返回 (version, info_dict). 失败返回 (None, None).

    info_dict 含 filename / size / changelog, 给后续 download 用.
    server 端 latest.json 没准备好时 (无镜像) available=False, 返回 (None, None).
    """
    try:
        r = httpx.get(
            f"{server_url}/api/gpu/upgrade/check",
            params={"current_version": "0.0.0"},  # 强制让 server 返回 latest, 客户端自己比对
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.debug("[OTA] server 镜像不可用: %s", e)
        return None, None
    if not data.get("available"):
        # available=False 时 server 也带回 version 字段 (跟当前一样)
        ver = data.get("version")
        if ver:
            return ver, data
        return None, None
    return data.get("version"), data


def get_remote_version(timeout: float = 5.0,
                       server_url: str = DEFAULT_SERVER_URL) -> Optional[str]:
    """优先 server, 失败 fallback GitHub."""
    ver, _ = get_remote_version_from_server(server_url, timeout)
    if ver:
        return ver
    return get_remote_version_from_github(timeout)


def compare_versions(local: str, remote: str) -> str:
    """返回 'newer' (本地比远端新) / 'current' (相同) / 'older' (有更新)."""
    l = _parse_version(local)
    r = _parse_version(remote)
    if l < r:
        return "older"
    if l > r:
        return "newer"
    return "current"


def check_version(timeout: float = 5.0,
                  server_url: str = DEFAULT_SERVER_URL) -> dict:
    """返回 {local, remote, status, has_update, source, repo_url}.

    source: 'server' (服务器镜像) / 'github' (直连) / 'offline' (都失败)
    status: 'older' / 'current' / 'newer' / 'offline'
    """
    local = get_local_version()
    # 先试 server
    server_ver, _info = get_remote_version_from_server(server_url, timeout)
    if server_ver:
        status = compare_versions(local, server_ver)
        return {
            "local": local, "remote": server_ver,
            "status": status, "has_update": (status == "older"),
            "source": "server", "repo_url": GITHUB_REPO_URL,
        }
    # fallback GitHub
    gh_ver = get_remote_version_from_github(timeout)
    if gh_ver is None:
        return {
            "local": local, "remote": None,
            "status": "offline", "has_update": False,
            "source": "offline", "repo_url": GITHUB_REPO_URL,
        }
    status = compare_versions(local, gh_ver)
    return {
        "local": local, "remote": gh_ver,
        "status": status, "has_update": (status == "older"),
        "source": "github", "repo_url": GITHUB_REPO_URL,
    }


def _try_import(mod: str) -> Tuple[bool, str]:
    """尝试 import. 返回 (ok, version_or_error)."""
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        return True, str(ver)
    except ImportError as e:
        return False, f"ImportError: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_dependencies() -> dict:
    """返回 {core: [{name, desc, ok, version}], optional: [...], pip_check: str}.

    pip_check 跑 `pip check` 看依赖一致性 (版本冲突).
    """
    result = {"core": [], "optional": [], "pip_check": ""}
    for mod, desc in CORE_DEPS:
        ok, ver = _try_import(mod)
        result["core"].append({"name": mod, "desc": desc, "ok": ok, "version": ver})
    for mod, desc in OPTIONAL_DEPS:
        ok, ver = _try_import(mod)
        result["optional"].append({"name": mod, "desc": desc, "ok": ok, "version": ver})

    # pip check — 探测依赖图冲突
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True, text=True, timeout=30,
        )
        result["pip_check"] = (proc.stdout + proc.stderr).strip() or "No broken requirements found."
        result["pip_check_ok"] = (proc.returncode == 0)
    except Exception as e:
        result["pip_check"] = f"pip check 执行失败: {e}"
        result["pip_check_ok"] = False
    return result


def _find_repo_root() -> Optional[Path]:
    """从当前文件向上找 .git 目录, 确认是否 git clone 安装. 找不到返回 None."""
    p = Path(__file__).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").is_dir():
            return parent
    return None


def _find_install_root() -> Path:
    """安装包目录 (含 setup.py). pip install -e . 进去重装就用这个."""
    p = Path(__file__).resolve()
    # box2robot_gpu_worker/box2robot_gpu_worker/ota.py → 上一级是包目录, 再上一级是 setup.py 所在
    for parent in [p.parent, *p.parents]:
        if (parent / "setup.py").exists() or (parent / "pyproject.toml").exists():
            return parent
    # fallback: 包目录的父
    return p.parent.parent


def _exec_step(cmd: list, cwd: Path, name: str, steps: list) -> bool:
    """跑命令并把结果记录到 steps 里."""
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                              text=True, timeout=300)
        ok = (proc.returncode == 0)
        steps.append({
            "name": name, "cmd": " ".join(cmd), "ok": ok,
            "stdout": proc.stdout.strip()[-2000:],
            "stderr": proc.stderr.strip()[-2000:],
        })
        return ok
    except Exception as e:
        steps.append({"name": name, "cmd": " ".join(cmd), "ok": False,
                      "stdout": "", "stderr": str(e)})
        return False


def _do_upgrade_via_server(info: dict, server_url: str, steps: list) -> dict:
    """走 server 镜像 zip 升级. 不需要 git, 适用所有安装方式 (git clone + pip install).

    info 是 server /api/gpu/upgrade/check 返回的 dict: filename / size / version.
    """
    filename = info.get("filename", "box2robot-gpu-worker.zip")
    size_kb = info.get("size", 0) // 1024
    new_ver = info.get("version", "?")
    logger.info("[OTA] 从 server 下载 %s (v%s, %d KB)", filename, new_ver, size_kb)

    # 1. 下载 zip
    try:
        r = httpx.get(f"{server_url}/api/gpu/upgrade/download",
                      timeout=60.0, follow_redirects=True)
        r.raise_for_status()
        zip_bytes = r.content
        steps.append({"name": "download zip", "cmd": "GET /api/gpu/upgrade/download",
                      "ok": True, "stdout": f"{len(zip_bytes)//1024} KB", "stderr": ""})
    except Exception as e:
        steps.append({"name": "download zip", "cmd": "GET /api/gpu/upgrade/download",
                      "ok": False, "stdout": "", "stderr": str(e)})
        return {"ok": False, "message": f"下载升级包失败: {e}", "steps": steps}

    # 2. 解压到临时目录
    install_root = _find_install_root()
    extract_dir = install_root.parent / "_b2r_upgrade_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(extract_dir)
        steps.append({"name": "extract zip", "cmd": f"unzip → {extract_dir}",
                      "ok": True, "stdout": "", "stderr": ""})
    except Exception as e:
        steps.append({"name": "extract zip", "cmd": "unzip", "ok": False,
                      "stdout": "", "stderr": str(e)})
        return {"ok": False, "message": f"解压失败: {e}", "steps": steps}

    # 3. 找 setup.py 所在目录 (zip 内层结构 box2robot_gpu_worker/setup.py)
    candidates = [extract_dir]
    if extract_dir.is_dir():
        candidates.extend([p for p in extract_dir.iterdir() if p.is_dir()])
    setup_dir = None
    for cand in candidates:
        if (cand / "setup.py").exists() or (cand / "pyproject.toml").exists():
            setup_dir = cand
            break
    if not setup_dir:
        return {"ok": False,
                "message": f"zip 里找不到 setup.py (extracted to {extract_dir})",
                "steps": steps}

    # 4. pip install -e . --no-deps (不重装 torch 等大依赖, 只更新 worker 自身)
    if not _exec_step(
        [sys.executable, "-m", "pip", "install", "-e", str(setup_dir),
         "--no-deps", "--upgrade", "--quiet"],
        setup_dir, "pip install -e .", steps):
        return {"ok": False, "message": "pip install -e . 失败, 看 stderr 详情.",
                "steps": steps}

    return {"ok": True, "new_ver": new_ver, "message": "", "steps": steps}


def _do_upgrade_via_github(steps: list) -> dict:
    """走 git pull 升级. 仅对 git clone + editable 安装的用户有效."""
    repo = _find_repo_root()
    if not repo:
        return {
            "ok": False,
            "message": (
                "找不到 .git 目录, 也无服务器镜像可用 — 无法自动升级.\n"
                f"  请手动: git clone {GITHUB_REPO_URL} && cd box2robot/box2robot_gpu_worker "
                f"&& pip install -e ."
            ),
            "steps": steps,
        }

    if not _exec_step(["git", "pull", "--ff-only"], repo, "git pull", steps):
        return {"ok": False,
                "message": "git pull 失败 — 可能有本地未提交修改, 或网络问题.",
                "steps": steps}

    worker_dir = repo / "box2robot_gpu_worker"
    if not worker_dir.exists():
        worker_dir = repo
    if not _exec_step(
        [sys.executable, "-m", "pip", "install", "-e", ".",
         "--no-deps", "--upgrade", "--quiet"],
        worker_dir, "pip install -e .", steps):
        return {"ok": False, "message": "pip install -e . 失败.", "steps": steps}

    # 读 git pull 后的 __init__.py 拿新版本号
    new_ver = "?"
    init_file = worker_dir / "box2robot_gpu_worker" / "__init__.py"
    if init_file.exists():
        m = re.search(r'__version__\s*=\s*["\']([\d.]+)["\']',
                      init_file.read_text(encoding="utf-8"))
        if m:
            new_ver = m.group(1)
    return {"ok": True, "new_ver": new_ver, "message": "", "steps": steps}


def _restart_self():
    """os.execv 替换当前进程加载新代码. 不返回 (除非失败)."""
    print("\n>>> Worker 即将自动重启加载新代码 ...")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        print(f"\n[!] 自动重启失败: {e}")
        print("    请手动重启 worker (Ctrl+C 当前进程, 重新跑 b2r-gpu)")


def do_upgrade(force: bool = False, auto_restart: bool = True,
               server_url: str = DEFAULT_SERVER_URL) -> dict:
    """一键升级 — 优先 server 镜像 zip, fallback GitHub git pull, 成功后自动重启.

    Args:
      force: 即使版本相同也跑升级 (hotfix 同版本号场景)
      auto_restart: True 时升级成功 os.execv 替换当前进程; False 仅装代码
      server_url: 服务器镜像 URL

    Returns: {ok, message, steps, new_ver, restarted}
    """
    steps = []

    # Step 1: 检查 server 镜像
    server_ver, server_info = get_remote_version_from_server(server_url)

    # Step 2: 决定走哪条路径
    use_server = False
    if server_info and server_info.get("available"):
        # server 说有更新 → 直接走 server
        use_server = True
    elif server_ver:
        # server 在线但版本相同 → 不需要升级 (除非 force)
        if not force:
            return {
                "ok": True, "new_ver": server_ver, "restarted": False,
                "message": f"已是最新版本 v{server_ver} (server 镜像), 无需升级.",
                "steps": steps,
            }
        # force 模式继续走 server 重装
        use_server = True
    else:
        # server 不可用 → 看 GitHub
        gh_ver = get_remote_version_from_github()
        if not force and gh_ver:
            cmp = compare_versions(LOCAL_VERSION, gh_ver)
            if cmp == "current":
                return {
                    "ok": True, "new_ver": gh_ver, "restarted": False,
                    "message": f"已是最新版本 v{gh_ver} (GitHub), 无需升级.",
                    "steps": steps,
                }
            if cmp == "newer":
                return {
                    "ok": True, "new_ver": gh_ver, "restarted": False,
                    "message": (f"本地 v{LOCAL_VERSION} 比 GitHub v{gh_ver} 新 "
                                f"(开发版?), 跳过升级."),
                    "steps": steps,
                }

    # Step 3: 跑升级
    if use_server:
        logger.info("[OTA] 走 server 镜像升级 (%s)", server_url)
        result = _do_upgrade_via_server(server_info, server_url, steps)
    else:
        logger.info("[OTA] server 镜像不可用, 尝试 GitHub git pull")
        result = _do_upgrade_via_github(steps)

    if not result["ok"]:
        return {**result, "restarted": False}

    new_ver = result["new_ver"]
    msg = f"升级完成 v{LOCAL_VERSION} → v{new_ver}."

    # Step 4: 自动重启 (call last, os.execv 不返回)
    if auto_restart:
        msg += " 正在自动重启加载新代码..."
        result_out = {**result, "restarted": True, "message": msg, "new_ver": new_ver}
        # 先把 result 打印出来再 exec, 否则用户看不到 success 消息
        print(msg)
        sys.stdout.flush()
        _restart_self()
        # 走到这说明 execv 失败
        result_out["restarted"] = False
        result_out["message"] = msg + " (但自动重启失败, 请手动重启)"
        return result_out

    return {**result, "restarted": False,
            "message": msg + " 重启 worker 才会加载新代码 (Ctrl+C 后重新跑 b2r-gpu)."}


# ===== 命令行展示 =====
# 注: Windows 默认 GBK 控制台不能编码 ✓✗⚠○. 启动时尝试切 UTF-8, 失败则用 ASCII 符号.

def _ensure_stdout_utf8():
    """Windows GBK 控制台改成 UTF-8 (Python 3.7+ 支持 reconfigure)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_OK_MARK = "[OK]"
_BAD_MARK = "[X]"
_WARN_MARK = "[!]"
_INFO_MARK = "[i]"
_OPT_MARK = "[ ]"


def print_check_version():
    """check-version 子命令: 漂亮打印对比结果. 返回 exit code."""
    _ensure_stdout_utf8()
    info = check_version()
    src_label = {
        "server": "服务器镜像", "github": "GitHub", "offline": "(离线)",
    }.get(info.get("source", "offline"), "?")
    print(f"本地版本: v{info['local']}")
    if info["status"] == "offline":
        print(f"远端版本: 拉取失败 — server 不可达, GitHub 也失败 (网络问题)")
        print(f"仓库:     {info['repo_url']}")
        return 0
    print(f"远端版本: v{info['remote']}  (来源: {src_label})")
    print(f"仓库:     {info['repo_url']}")
    print()
    if info["status"] == "older":
        print(f"{_WARN_MARK} 有新版本可用! 运行 `b2r-gpu upgrade` 一键升级")
        return 1
    if info["status"] == "newer":
        print(f"{_INFO_MARK} 本地版本比远端新 (开发版).")
    else:
        print(f"{_OK_MARK} 已是最新版本.")
    return 0


def print_check_deps():
    """check-deps 子命令: 漂亮打印依赖检测. 返回 exit code (0=ok, 1=core 缺失)."""
    _ensure_stdout_utf8()
    res = check_dependencies()

    print("=== 核心依赖 ===")
    core_ok = True
    for d in res["core"]:
        flag = _OK_MARK if d["ok"] else _BAD_MARK
        ver = d["version"] if d["ok"] else "(缺失)"
        print(f"  {flag} {d['name']:<14} {ver:<15} {d['desc']}")
        if not d["ok"]:
            core_ok = False
            print(f"      错误: {d['version']}")

    print("\n=== 可选依赖 (按需) ===")
    for d in res["optional"]:
        flag = _OK_MARK if d["ok"] else _OPT_MARK
        ver = d["version"] if d["ok"] else "(未装)"
        print(f"  {flag} {d['name']:<14} {ver:<15} {d['desc']}")

    print("\n=== pip 依赖图检查 ===")
    print(f"  {res['pip_check']}")

    if not core_ok:
        print(f"\n{_WARN_MARK} 核心依赖有缺失, 修复:")
        print("  pip install av numpy httpx pyyaml psutil pyarrow")
        print("  pip install torch torchvision torchaudio "
              "--index-url https://download.pytorch.org/whl/cu124")
        return 1
    if not res.get("pip_check_ok", True):
        print(f"\n{_WARN_MARK} pip 报告依赖冲突 (上面), 建议解决.")
        return 1
    print(f"\n{_OK_MARK} 依赖检测通过.")
    return 0


def print_upgrade(force: bool = False, auto_restart: bool = True,
                  server_url: str = DEFAULT_SERVER_URL):
    """upgrade 子命令: 跑升级 + 漂亮打印. 返回 exit code.

    成功且 auto_restart=True 时, 函数不会返回 (os.execv 替换进程).
    返回值仅在: 不需升级 / 升级失败 / auto_restart=False / execv 失败 时出现.
    """
    _ensure_stdout_utf8()
    print(f"开始升级 (本地 v{LOCAL_VERSION}, 服务器源: {server_url})...")
    res = do_upgrade(force=force, auto_restart=auto_restart, server_url=server_url)
    for step in res["steps"]:
        flag = _OK_MARK if step["ok"] else _BAD_MARK
        print(f"  {flag} {step['name']}: {step['cmd']}")
        if step["stdout"]:
            print(f"     stdout: {step['stdout'][:200]}")
        if not step["ok"] and step["stderr"]:
            print(f"     stderr: {step['stderr'][:500]}")
    print()
    print(res["message"])
    return 0 if res["ok"] else 1
