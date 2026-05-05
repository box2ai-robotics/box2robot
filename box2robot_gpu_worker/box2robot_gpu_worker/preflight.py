"""Preflight checks: dependencies, CUDA/VRAM, path safety.

Called at worker startup. Fails fast with actionable error messages
instead of letting the user wait 23 minutes before crashing inside lerobot.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("box2robot.preflight")


REQUIRED_PACKAGES = [
    ("datasets", "pip install datasets"),
    ("huggingface_hub", "pip install huggingface_hub"),
    ("safetensors", "pip install safetensors"),
    ("draccus", "pip install draccus"),
    ("torch", "pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124"),
    ("numpy", "pip install numpy"),
    ("PIL", "pip install pillow"),
]

VRAM_NEED_GB = {
    "mlp": 1.0,
    "act": 6.0,
    "diffusion": 8.0,
    "smolvla": 12.0,
    "pi0": 18.0,
    "pi0_fast": 18.0,
    "pi05": 18.0,
}


def check_dependencies(strict: bool = True) -> list[tuple[str, str]]:
    """Check required Python packages. Returns list of missing (name, hint)."""
    missing = []
    for pkg, hint in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append((pkg, hint))

    if missing:
        msg = (
            "缺少必需的 Python 依赖包:\n"
            + "\n".join(f"  - {p}: {h}" for p, h in missing)
            + "\n\n请先安装缺失依赖再启动 worker。"
            "\n或运行一键脚本: scripts/setup_windows.bat (Win) / scripts/setup_linux.sh (Linux)"
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
    else:
        logger.info("Dependency check OK (%d packages)", len(REQUIRED_PACKAGES))
    return missing


def check_vram(model_type: str, batch_size: int, strict: bool = True) -> dict:
    """Check available VRAM vs estimated need. Returns {free_gb, total_gb, need_gb, ok}."""
    info = {"free_gb": 0.0, "total_gb": 0.0, "need_gb": 0.0, "ok": True, "available": False}
    try:
        import torch
    except ImportError:
        return info
    if not torch.cuda.is_available():
        info["available"] = False
        if strict:
            raise RuntimeError(
                "未检测到可用 CUDA GPU。\n"
                "请确认 1) NVIDIA 驱动已安装 (nvidia-smi 能跑)，"
                "2) 安装的是 CUDA 版 PyTorch (不是 CPU 版)。\n"
                "诊断: python -c \"import torch; print(torch.cuda.is_available())\""
            )
        return info

    info["available"] = True
    free_b, total_b = torch.cuda.mem_get_info()
    free_gb = free_b / 1e9
    total_gb = total_b / 1e9
    base_need = VRAM_NEED_GB.get(model_type.lower(), 4.0)
    if model_type.lower() in ("act", "diffusion", "mlp"):
        need_gb = base_need + 0.05 * batch_size
    else:
        need_gb = base_need

    info.update({"free_gb": free_gb, "total_gb": total_gb, "need_gb": need_gb})
    info["ok"] = free_gb >= need_gb

    if not info["ok"]:
        msg = (
            f"显存不足: 当前空闲 {free_gb:.1f}GB / 总 {total_gb:.1f}GB, "
            f"{model_type} (batch_size={batch_size}) 预计需要 {need_gb:.1f}GB。\n"
            f"请关闭占用显存的其它程序 (浏览器GPU加速 / Stable Diffusion / 游戏 / Chrome / Edge), "
            f"或降低 batch_size。\n"
            f"诊断: nvidia-smi"
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
    else:
        logger.info(
            "VRAM check OK: free=%.1fGB / total=%.1fGB, need≈%.1fGB (%s, bs=%d)",
            free_gb, total_gb, need_gb, model_type, batch_size,
        )
    return info


def check_path_safety(project_root: Path) -> None:
    """Warn on Windows if project path is too deep (260 char MAX_PATH)."""
    project_root = Path(project_root).resolve()
    path_len = len(str(project_root))
    if sys.platform == "win32":
        deepest_estimated = path_len + 150
        if deepest_estimated > 240:
            logger.warning(
                "项目路径过深 (%d 字符)，Windows 下训练产物路径可能超过 260 字符限制。\n"
                "  当前: %s\n"
                "  建议: 把项目移到更短的路径（如 D:\\b2r\\ 或 C:\\b2r\\），\n"
                "  或开启 Windows 长路径支持: HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem\\LongPathsEnabled = 1",
                path_len, project_root,
            )
    if any(ord(c) > 127 for c in str(project_root)):
        logger.info(
            "项目路径含非 ASCII 字符: %s — 已统一使用 UTF-8 编码，"
            "如遇 subprocess 报错请检查 shell 编码。",
            project_root,
        )


def check_encoding() -> None:
    """Warn if Python default encoding is not UTF-8 (Windows cp936 trap)."""
    import locale
    enc = locale.getpreferredencoding(False).lower()
    if enc not in ("utf-8", "utf8"):
        logger.warning(
            "系统默认编码是 %s (不是 UTF-8)。\n"
            "  代码已显式指定 encoding='utf-8'，但建议设置环境变量:\n"
            "  Windows: set PYTHONUTF8=1\n"
            "  Linux:   export LANG=en_US.UTF-8",
            enc,
        )


def run_all(project_root: Path | None = None, strict_deps: bool = True) -> None:
    """Run all preflight checks at worker startup. VRAM check is deferred to per-job."""
    logger.info("=" * 60)
    logger.info("Box2Robot GPU Worker — preflight check")
    logger.info("=" * 60)
    check_dependencies(strict=strict_deps)
    check_encoding()
    if project_root is None:
        project_root = Path(__file__).parent.parent
    check_path_safety(project_root)
    logger.info("Preflight OK.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    run_all(strict_deps=False)
