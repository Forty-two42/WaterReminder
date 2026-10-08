@echo off
chcp 65001 >nul
setlocal
cd /d %~dp0

set PY=.venv311\Scripts\python.exe

if not exist "%PY%" (
    echo [!] 未找到构建环境 %PY%
    echo     请先执行:
    echo     "C:\UE_Engine\gsunreal_p4\gs_unreal\Windows\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m venv .venv311
    echo     .venv311\Scripts\python.exe -m pip install pystray pillow pyinstaller
    exit /b 1
)

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
