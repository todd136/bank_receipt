@echo off
REM Nuitka 打包脚本：将 bank_receipt 打成 Windows 单文件 exe
REM 需在项目根目录执行

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM 增大 MSVC 编译器堆空间，缓解 PyMuPDF 生成 C 文件过大导致的 C1002
set CL=/Zm300 %CL%

python -m nuitka --standalone --onefile --msvc=latest --noinclude-unittest-mode=nofollow --noinclude-pytest-mode=nofollow --enable-plugin=no-qt --low-memory --lto=no --jobs=1 --windows-uac-admin --onefile-tempdir-spec="{CACHE_DIR}/todd_dev_studio/bank_receipt" --windows-icon-from-ico=logo.ico --company-name="Todd Dev Studio" --product-name="BankReceiptParser" --file-version=1.0.0.0 --product-version=1.0.0.0 --file-description="用于分发银行回单的自动化工具" --copyright="Copyright (c) 2026 Todd Dev Studio. All rights reserved." --output-filename=bank_receipt src\bank_receipt\main.py

echo.
echo Build finished. Check bank_receipt.exe in current directory.
pause