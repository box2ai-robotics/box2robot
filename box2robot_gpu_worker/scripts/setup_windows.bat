@echo off
REM Box2Robot GPU Worker — Windows 一键安装脚本
REM 用法: scripts\setup_windows.bat [cu118 | cu121 | cu124 | cu128]
REM 默认 CUDA 12.4
setlocal EnableDelayedExpansion

set CUDA_VER=%1
if "%CUDA_VER%"=="" set CUDA_VER=cu124

echo ============================================================
echo  Box2Robot GPU Worker setup (Windows, %CUDA_VER%)
echo ============================================================

REM 1. 检查 conda
where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 conda. 请先安装 Miniconda/Anaconda.
    echo         https://docs.conda.io/en/latest/miniconda.html
    exit /b 1
)

REM 2. 创建 conda 环境
echo.
echo [1/6] Creating conda environment 'b2r' (python 3.12)...
call conda env list | findstr /B "b2r " >nul
if errorlevel 1 (
    call conda create -n b2r python=3.12 -y || exit /b 1
) else (
    echo   environment 'b2r' already exists, skipping.
)

REM 3. 激活环境
echo.
echo [2/6] Activating 'b2r'...
call conda activate b2r || exit /b 1

REM 4. 安装 CUDA PyTorch
echo.
echo [3/6] Installing PyTorch (%CUDA_VER%)...
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/%CUDA_VER% || exit /b 1

REM 5. 验证 GPU
echo.
echo [4/6] Verifying GPU...
python -c "import torch; assert torch.cuda.is_available(), 'CUDA not available'; print('  GPU:', torch.cuda.get_device_name(0)); print('  VRAM:', round(torch.cuda.get_device_properties(0).total_memory/1e9, 1), 'GB')" || (
    echo [ERROR] PyTorch 检测不到 CUDA. 请检查驱动版本和 CUDA 索引是否匹配.
    exit /b 1
)

REM 6. 安装 LeRobot (本地 submodule)
echo.
echo [5/6] Installing LeRobot from submodule...
if not exist "lerobot\setup.py" if not exist "lerobot\pyproject.toml" (
    echo [ERROR] lerobot\ submodule 未拉取.
    echo   请在仓库根目录执行: git submodule update --init --recursive
    exit /b 1
)
pushd lerobot
pip install -e . --no-build-isolation || (popd & exit /b 1)
popd

REM 7. 安装 GPU Worker (带所有隐式依赖)
echo.
echo [6/6] Installing GPU Worker + dependencies...
pip install -e . || exit /b 1

REM 8. Preflight 自检
echo.
echo Running preflight check...
python -m box2robot_gpu_worker.preflight || (
    echo [WARN] preflight 报告了问题, 但 install 已完成. 请按提示修复.
)

echo.
echo ============================================================
echo  Setup complete.
echo ============================================================
echo.
echo Next:
echo   conda activate b2r
echo   b2r-gpu --server https://robot.box2ai.com
echo.
endlocal
