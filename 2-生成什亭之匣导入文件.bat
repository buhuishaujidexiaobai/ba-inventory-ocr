@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 蔚蓝档案库存导出工具 - 生成导入文件
echo ========================================================
echo  【第二步：生成什亭之匣导入文件】
echo  正在将采集到的背包数据转换为什亭之匣规范格式...
echo ========================================================
echo.

set PY_CMD=python
if exist "runtime\python.exe" set PY_CMD="runtime\python.exe"
if exist "D:\AI工作区\工具\本地识图OCR\ocr-venv\Scripts\python.exe" set PY_CMD="D:\AI工作区\工具\本地识图OCR\ocr-venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set PY_CMD=".venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" set PY_CMD="venv\Scripts\python.exe"

%PY_CMD% ba_map_items.py
echo.
echo ========================================================
echo  [√] 导入文件已生成完毕！
echo.
echo  正在为您自动打开「输出」文件夹...
echo  提示：前往 什亭之匣 (https://arona.icu) -^> 库存管理 -^> 导入该 JSON 即可！
echo ========================================================
if exist "%~dp0输出" explorer.exe "%~dp0输出"
pause
