@echo off
setlocal
REM PyInstaller 打包脚本（产品化配置版）
REM 需在项目根目录执行

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
set RUNTIME_TMPDIR=%LOCALAPPDATA%\todd_dev_studio\bank_receipt
set VERSION_FILE=%SCRIPT_DIR%bank_receipt_version_info.txt

echo [INFO] Build target: %APP_NAME%.exe
echo [INFO] Version: %APP_VERSION%
echo [INFO] Entry: %ENTRY_SCRIPT%

if not exist "%ENTRY_SCRIPT%" (
  echo [ERROR] Entry script not found: %ENTRY_SCRIPT%
  exit /b 2
)

if not exist "%ICON_FILE%" (
  echo [WARN] Icon file not found: %ICON_FILE% ^(build will continue without icon^)
  set ICON_ARG=
) else (
  set ICON_ARG=--icon %ICON_FILE%
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] PyInstaller not installed. Run: pip install pyinstaller
  exit /b 2
)

REM 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "%APP_NAME%.spec" del /f /q "%APP_NAME%.spec"
if exist "%VERSION_FILE%" del /f /q "%VERSION_FILE%"

REM 生成 Windows 版本信息文件（供 PyInstaller 写入 EXE 元数据）
powershell -NoProfile -Command ^
  "$appVersion='%APP_VERSION%';" ^
  "$company='%COMPANY_NAME%';" ^
  "$desc='%FILE_DESCRIPTION%';" ^
  "$copyright='%COPYRIGHT_TEXT%';" ^
  "$product='%PRODUCT_NAME%';" ^
  "$appName='%APP_NAME%';" ^
  "$v=$appVersion.Split('.');" ^
  "$content = @\"" ^
# UTF-8 ^
VSVersionInfo( ^
  ffi=FixedFileInfo( ^
    filevers=($($v[0]), $($v[1]), $($v[2]), $($v[3])), ^
    prodvers=($($v[0]), $($v[1]), $($v[2]), $($v[3])), ^
    mask=0x3f, ^
    flags=0x0, ^
    OS=0x40004, ^
    fileType=0x1, ^
    subtype=0x0, ^
    date=(0, 0) ^
  ), ^
  kids=[ ^
    StringFileInfo([ ^
      StringTable('040904B0', [ ^
        StringStruct('CompanyName', '$company'), ^
        StringStruct('FileDescription', '$desc'), ^
        StringStruct('FileVersion', '$appVersion'), ^
        StringStruct('InternalName', '$appName'), ^
        StringStruct('LegalCopyright', '$copyright'), ^
        StringStruct('OriginalFilename', '$appName.exe'), ^
        StringStruct('ProductName', '$product'), ^
        StringStruct('ProductVersion', '$appVersion') ^
      ]) ^
    ]), ^
    VarFileInfo([VarStruct('Translation', [1033, 1200])]) ^
  ] ^
) ^
\"@; Set-Content -Path '%VERSION_FILE%' -Value $content -Encoding UTF8"

if errorlevel 1 (
  echo [ERROR] Failed to generate version file.
  exit /b 3
)

python -m PyInstaller ^
--noconfirm ^
--clean ^
--onefile ^
--name %APP_NAME% ^
--runtime-tmpdir "%RUNTIME_TMPDIR%" ^
--uac-admin ^
%ICON_ARG% ^
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
%ENTRY_SCRIPT%

if errorlevel 1 (
  echo [ERROR] Build failed.
  if exist "%VERSION_FILE%" del /f /q "%VERSION_FILE%"
  exit /b %errorlevel%
)

if exist "%VERSION_FILE%" del /f /q "%VERSION_FILE%"

echo.
echo [OK] Build finished.
echo [OK] Output: dist\%APP_NAME%.exe
exit /b 0
