@echo off
chcp 65001 >nul
echo ============================================
echo   AgentTeam 虚拟环境安装脚本
echo ============================================
echo.

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 创建虚拟环境
if not exist ".venv" (
    echo [1/3] 创建虚拟环境 .venv ...
    python -m venv .venv
) else (
    echo [1/3] 虚拟环境已存在，跳过创建
)

:: 激活虚拟环境并安装依赖
echo [2/3] 安装依赖 ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q

:: 检查 .env
if not exist ".env" (
    if exist ".env.example" (
        echo [3/3] 复制 .env.example → .env （请编辑填入你的配置）
        copy .env.example .env >nul
    ) else (
        echo [3/3] 未找到 .env.example，请手动创建 .env
    )
) else (
    echo [3/3] .env 已存在，跳过
)

echo.
echo ============================================
echo   安装完成！
echo.
echo   激活虚拟环境：  .venv\Scripts\activate
echo   启动 Relay：    python slack_bot.py
echo   启动本地 Agent：python local_agent.py
echo ============================================
pause
