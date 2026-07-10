@echo off
rem ============================================================
rem  Hi-Res Music Downloader  -- Launcher
rem  10-platform parallel | FLAC minimum | OST priority | 24-bit/192kHz+ | Lyrics
rem ============================================================

rem -- If already inside the spawned window, jump straight to work --
if "%_HIRESRUN%"=="1" goto :main

rem -- Write a tiny bootstrap script to a temp file, then start it --
rem    This avoids ALL nested-quote problems with "start /k".
set "_BOOT=%TEMP%\hiresboot_%RANDOM%.cmd"
(
    echo @echo off
    echo set _HIRESRUN=1
    echo call "%~f0"
) > "%_BOOT%"
start "Hi-Res Downloader" cmd.exe /k "%_BOOT%"
exit /b

rem ============================================================
rem  Main execution  (only reached inside the spawned window)
rem ============================================================
:main
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

rem Clean up the temp bootstrap file if it still exists
if exist "%_BOOT%" del /f /q "%_BOOT%" >nul 2>&1

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem -- Sockseek credentials --
set SOCKSEEK_USER=zpf10284140
set SOCKSEEK_PASS=zpf123,
set SOCKSEEK_MINBR=3000

rem -- China network acceleration (enabled by default) --
rem    aria2c 16-thread download, GitHub CN mirror, yt-dlp iOS CDN client
set CN_ACCEL=1

rem -- Auto-detect Windows system proxy from registry --
set "PROXY_ARG="
set "SYS_PROXY_ENABLED=0"
set "SYS_PROXY_SERVER="

for /f "tokens=3" %%A in (
    'reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable 2^>nul'
) do set "SYS_PROXY_ENABLED=%%A"

for /f "tokens=2,*" %%A in (
    'reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2^>nul'
) do set "SYS_PROXY_SERVER=%%B"

if "!SYS_PROXY_ENABLED!"=="0x1" (
    if not "!SYS_PROXY_SERVER!"=="" (
        echo !SYS_PROXY_SERVER! | findstr /i "=" >nul 2>&1
        if !errorlevel! == 0 (
            for /f "tokens=1 delims=;" %%S in ("!SYS_PROXY_SERVER!") do (
                for /f "tokens=2 delims==" %%V in ("%%S") do (
                    set "SYS_PROXY_SERVER=%%V"
                )
            )
        )
        echo !SYS_PROXY_SERVER! | findstr /i "://" >nul 2>&1
        if !errorlevel! neq 0 (
            set "SYS_PROXY_SERVER=http://!SYS_PROXY_SERVER!"
        )
        set "PROXY_ARG=--proxy !SYS_PROXY_SERVER!"
        echo [PROXY] System proxy detected: !SYS_PROXY_SERVER!
    ) else (
        echo [PROXY] ProxyEnable=1 but ProxyServer is empty. Skipping proxy.
    )
) else (
    echo [PROXY] System proxy not enabled. Running without proxy.
)

rem -- Use managed Python venv --
set "PYTHON_EXE=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [WARN] Managed venv not found, falling back to system Python.
    set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

rem -- Build CN acceleration argument --
set "CN_ARG="
if "!CN_ACCEL!"=="1" (
    set "CN_ARG=--cn-accelerate"
    echo [CN-ACCEL] China network acceleration enabled (aria2c 16-thread + GitHub mirror^)
) else (
    echo [CN-ACCEL] Disabled. Set CN_ACCEL=1 in run.bat to enable.
)

rem -- Run the downloader (output dir is selected interactively) --
"%PYTHON_EXE%" -u download_music.py ^
    --playlist playlist.txt ^
    --workers 8 ^
    --sockseek-exe "%~dp0sockseek.exe" ^
    --sockseek-user %SOCKSEEK_USER% ^
    --sockseek-pass "%SOCKSEEK_PASS%" ^
    --sockseek-min-bitrate %SOCKSEEK_MINBR% ^
    --sockseek-timeout 180 ^
    --interactive ^
    %PROXY_ARG% ^
    %CN_ARG%

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Downloader exited with error code %errorlevel%.
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo  Done!
echo  Report : %~dp0download_report.json
echo  Log    : %~dp0download_log.txt
echo ============================================================
pause
endlocal
