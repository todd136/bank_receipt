@echo off
setlocal EnableDelayedExpansion
REM PyInstaller 打包脚本（产品化配置版）
REM 需在项目根目录执行；双击运行时失败会 pause 便于查看错误

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM =========================
REM 产品化配置（集中管理）
REM =========================
set APP_NAME=bank_receipt
set PRODUCT_NAME=BankReceiptParser
set COMPANY_NAME=Todd Dev Studio
set APP_VERSION=1.0.0.0
set FILE_DESCRIPTION=用于分发银行回单的自动化工具
set COPYRIGHT_TEXT=Copyright (c) 2026 Todd Dev Studio. All rights reserved.
set ICON_FILE=logo.ico
set ENTRY_SCRIPT=src\bank_receipt\main.py
set CONTENTS_DIR=runtime
set VERSION_FILE=bank_receipt_version_info.txt
set ICON_ARG=

echo [INFO] Working dir: %CD%
echo [INFO] Build target: %APP_NAME%.exe
echo [INFO] Version: %APP_VERSION%
echo [INFO] Entry: %ENTRY_SCRIPT%

REM 选择 Python 解释器：优先 python，其次 py -3
set PYTHON_CMD=
where python >nul 2>nul
if not errorlevel 1 set PYTHON_CMD=python
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 set PYTHON_CMD=py -3
)
if not defined PYTHON_CMD (
  echo [ERROR] 未找到 Python。请安装 Python 3 并加入 PATH，或使用 py 启动器。
  goto :fail
)

echo [INFO] Python: %PYTHON_CMD%
%PYTHON_CMD% --version
if errorlevel 1 (
  echo [ERROR] Python 无法运行: %PYTHON_CMD%
  goto :fail
)

if not exist "%ENTRY_SCRIPT%" (
  echo [ERROR] Entry script not found: %ENTRY_SCRIPT%
  echo [HINT] 请在项目根目录执行本脚本（与 src 目录同级）。
  goto :fail
)

if not exist "%VERSION_FILE%" (
  echo [ERROR] Version file not found: %VERSION_FILE%
  goto :fail
)

if exist "%ICON_FILE%" (
  set ICON_ARG=--icon %ICON_FILE%
) else (
  echo [WARN] Icon file not found: %ICON_FILE% ^(build will continue without icon^)
)

%PYTHON_CMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PyInstaller 未安装。
  echo [HINT] 请执行: %PYTHON_CMD% -m pip install pyinstaller
  goto :fail
)

for /f "delims=" %%v in ('%PYTHON_CMD% -m PyInstaller --version 2^>nul') do echo [INFO] PyInstaller: %%v

REM 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /f /q "%APP_NAME%.spec"

echo [INFO] Start PyInstaller build...
%PYTHON_CMD% -m PyInstaller ^
--noconfirm ^
--clean ^
--onedir ^
--name %APP_NAME% ^
--contents-directory "%CONTENTS_DIR%" ^
--uac-admin ^
!ICON_ARG! ^
--hidden-import pymupdf ^
--hidden-import fitz ^
--hidden-import ddddocr ^
--collect-all ddddocr ^
--collect-all cv2 ^
--collect-all onnxruntime ^
--collect-all pypdfium2_raw ^
--collect-all pymupdf ^
--collect-all PIL ^
--version-file "%VERSION_FILE%" ^
"%ENTRY_SCRIPT%"

if errorlevel 1 (
  echo [ERROR] Build failed. See output above.
  goto :fail
)

if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
  echo [ERROR] Build finished but exe not found: dist\%APP_NAME%\%APP_NAME%.exe
  goto :fail
)

echo.
echo [OK] Build finished.
echo [OK] Output: dist\%APP_NAME%\%APP_NAME%.exe
echo [OK] Runtime deps dir: dist\%APP_NAME%\%CONTENTS_DIR%
goto :done

:fail
echo.
echo [FAIL] Build aborted.
if defined GITHUB_ACTIONS goto :done
if /i "%CI%"=="true" goto :done
pause
exit /b 1

:done
if defined GITHUB_ACTIONS exit /b 0
if /i "%CI%"=="true" exit /b 0
pause
exit /b 0
