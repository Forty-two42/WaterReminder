@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

set PY=.venv311\Scripts\python.exe

rem --- 虚拟环境不存在时, 自动查找 Python 3 并创建 ---
if exist "%PY%" goto env_ok

echo [i] 未找到构建环境 .venv311, 开始自动创建 ...

set "BASE_PY="
py -3 -c "import sys" >nul 2>nul && set "BASE_PY=py -3"
if not defined BASE_PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,) else 1)" >nul 2>nul && set "BASE_PY=python"
)
if not defined BASE_PY (
    echo [!] 未找到可用的 Python 3, 请先安装后重试:
    echo     winget install --id Python.Python.3.11 --scope user
    echo     或到 https://www.python.org/downloads/ 下载安装
    exit /b 1
)

%BASE_PY% -m venv .venv311 || exit /b 1
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt || exit /b 1

:env_ok
echo [1/2] 生成图标 water.ico ...
"%PY%" make_icon.py || exit /b 1

echo [2/2] 打包单文件 exe ...
"%PY%" -m PyInstaller ^
    --noconfirm --clean --onefile --windowed ^
    --name WaterReminder ^
    --icon water.ico ^
    --hidden-import pystray._win32 ^
    --exclude-module numpy ^
    --exclude-module unittest ^
    --exclude-module pydoc ^
    --exclude-module email ^
    --exclude-module http ^
    --exclude-module xml ^
    --exclude-module pdb ^
    main.py || exit /b 1

echo.
echo 完成: %~dp0dist\WaterReminder.exe
endlocal
