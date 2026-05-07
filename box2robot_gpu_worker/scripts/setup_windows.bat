@echo off
REM ============================================================
REM Box2Robot GPU Worker — Windows 一键安装脚本
REM
REM 用法:
REM   scripts\setup_windows.bat
REM   scripts\setup_windows.bat cu124    (指定 CUDA 版本)
REM   scripts\setup_windows.bat cu128
REM   scripts\setup_windows.bat cu118
REM ============================================================

setlocal enabledelayedexpansion

echo.
echo ==================================================
echo   Box2Robot GPU Worker — Windows 安装
echo ==================================================
echo.

REM --- Check conda ---
where conda >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 conda!
    echo 请先安装 Miniconda: https://docs.conda.io/en/latest/miniconda.html
    echo 或 Anaconda: https://www.anaconda.com/download
    pause
    exit /b 1
)

REM --- Parse CUDA version arg ---
set CUDA_VER=%1
if "%CUDA_VER%"=="" set CUDA_VER=cu124
echo [信息] CUDA 版本: %CUDA_VER%

REM Map CUDA versions to PyTorch index URLs
if "%CUDA_VER%"=="cu118" (
    set TORCH_INDEX=https://download.pytorch.org/whl/cu118
    set CUDA_LABEL=CUDA 11.8
) else if "%CUDA_VER%"=="cu121" (
    set TORCH_INDEX=https://download.pytorch.org/whl/cu121
    set CUDA_LABEL=CUDA 12.1
) else if "%CUDA_VER%"=="cu124" (
    set TORCH_INDEX=https://download.pytorch.org/whl/cu124
    set CUDA_LABEL=CUDA 12.4
) else if "%CUDA_VER%"=="cu128" (
    set TORCH_INDEX=https://download.pytorch.org/whl/cu128
    set CUDA_LABEL=CUDA 12.8
) else (
    echo [错误] 不支持的 CUDA 版本: %CUDA_VER%
    echo 支持: cu118, cu121, cu124, cu128
    pause
    exit /b 1
)

echo [信息] PyTorch 索引: %TORCH_INDEX%
echo.

REM --- Check if env exists ---
conda env list | findstr /C:"b2r " >nul 2>nul
if %errorlevel% equ 0 (
    echo [信息] conda 环境 'b2r' 已存在，跳过创建
) else (
    echo [步骤 1/5] 创建 conda 环境 (Python 3.12)...
    conda create -n b2r python=3.12 -y
    if %errorlevel% neq 0 (
        echo [错误] conda 环境创建失败!
        pause
        exit /b 1
    )
)

REM --- Activate env ---
echo [步骤 2/5] 激活环境...
call conda activate b2r
if %errorlevel% neq 0 (
    echo [错误] 无法激活 conda 环境 'b2r'
    echo 请尝试: conda init cmd.exe  然后重新打开命令提示符
    pause
    exit /b 1
)

REM --- Install PyTorch with CUDA ---
echo.
echo [步骤 3/5] 安装 PyTorch (%CUDA_LABEL%)...
echo   这一步需要下载约 2-3GB，请耐心等待...
echo.
pip install torch torchvision torchaudio --index-url %TORCH_INDEX%
if %errorlevel% neq 0 (
    echo [错误] PyTorch 安装失败!
    echo 请检查网络连接，或使用国内镜像
    pause
    exit /b 1
)

REM --- Verify GPU ---
echo.
echo [验证] 检查 GPU...
python -c "import torch; g=torch.cuda.is_available(); n=torch.cuda.get_device_name(0) if g else 'N/A'; print(f'  CUDA: {g}  GPU: {n}  torch.version.cuda: {torch.version.cuda}')"
if %errorlevel% neq 0 (
    echo [警告] GPU 验证失败，继续安装...
)

REM --- Install LeRobot (base + dataset extra) ---
echo.
echo [步骤 4/6] 安装 LeRobot 基础包...
echo   注意: 仅安装基础包 (ACT 已包含在基础依赖中)
echo.
if exist "lerobot\pyproject.toml" (
    pip install -e ./lerobot --no-build-isolation
    if %errorlevel% neq 0 (
        echo [错误] LeRobot 基础包安装失败!
        pause
        exit /b 1
    )
) else (
    echo [警告] lerobot 子目录不存在，跳过
    echo 请先 clone: git clone https://github.com/huggingface/lerobot.git lerobot
    pause
    exit /b 1
)

REM --- Install LeRobot[dataset] extras: av (PyAV) + datasets + torchcodec + ... ---
REM    pi0/pi05/smolvla 等 VLA 模型加载图像/视频数据集时需要 av;
REM    缺这一步会在训练时报 "'av' is required but not installed".
echo.
echo [步骤 5/6] 安装 LeRobot 数据集依赖 (av, datasets, torchcodec)...
echo   缺这一步训练 pi0/pi05/smolvla 会崩 ('av' is required but not installed)
pip install "lerobot[dataset] @ file:./lerobot" --no-build-isolation
if %errorlevel% neq 0 (
    echo [警告] lerobot[dataset] 整体安装失败, 尝试逐个安装关键依赖...
    pip install "av>=15.0.0,<16.0.0"
    pip install "datasets>=4.0.0,<5.0.0" "jsonlines>=4.0.0,<5.0.0"
    REM torchcodec 在 Windows 上需要 torch>=2.8, 失败可忽略, av 已能解码常见视频
    pip install "torchcodec>=0.3.0,<0.11.0" 2>nul
)

REM --- Install GPU Worker ---
echo.
echo [步骤 6/6] 安装 Box2Robot GPU Worker...
pip install -e . psutil
if %errorlevel% neq 0 (
    echo [错误] GPU Worker 安装失败!
    pause
    exit /b 1
)

REM --- Final check ---
echo.
echo ==================================================
echo   安装完成! 运行诊断...
echo ==================================================
echo.
python scripts\check_gpu.py

echo.
echo ==================================================
echo   启动命令:
echo     conda activate b2r
echo     b2r-gpu --server https://robot.box2ai.com
echo ==================================================
echo.
pause
