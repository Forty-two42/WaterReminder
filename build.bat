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
    echo [i] 未检测到 Python, 尝试用 winget 自动安装 Python 3.11 ...
    where winget >nul 2>nul || (
        echo [!] 未找到 winget, 请手动安装 Python 后重试:
        echo     winget install --id Python.Python.3.11 --scope user
        echo     或到 https://www.python.org/downloads/ 下载安装
        exit /b 1
    )
    winget install --id Python.Python.3.11 --scope user --silent --accept-source-agreements --accept-package-agreements || (
        echo [!] winget 安装 Python 失败, 请手动安装后重试
        exit /b 1
    )
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
    set "PY311=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    if exist "%PY311%" (
        set "BASE_PY=%PY311%"
    ) else (
        py -3 -c "import sys" >nul 2>nul && set "BASE_PY=py -3"
    )
    if not defined BASE_PY (
        echo [!] 安装完成但未找到 Python, 请重开终端后重试
        exit /b 1
    )
    echo [i] Python 安装完成
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
