@echo off
REM PyInstaller 打包脚本：生成单文件 bank_receipt.exe
REM 需在项目根目录执行

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist bank_receipt.spec del /f /q bank_receipt.spec

python -m PyInstaller ^
--noconfirm ^
--clean ^
--onefile ^
--name bank_receipt ^
--icon logo.ico ^
src\bank_receipt\main.py

if errorlevel 1 exit /b %errorlevel%

echo.
echo Build finished. Check dist\bank_receipt.exe
exit /b 0
