@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 蔚蓝档案库存导出工具 - 采集
echo ============================================
echo  BA 库存采集（自动找窗口+滚动+点击+识别）
echo  请先：启动游戏并进入「道具」页面
echo  采集期间不要动鼠标键盘，约 3-6 分钟
echo ============================================

set PY_CMD=python
if exist "runtime\python.exe" set PY_CMD=runtime\python.exe
if exist ".venv\Scripts\python.exe" set PY_CMD=.venv\Scripts\python.exe
if exist "venv\Scripts\python.exe" set PY_CMD=venv\Scripts\python.exe
if exist "local_python_path.txt" set /p PY_CMD=<local_python_path.txt

"%PY_CMD%" -u ba_queue_collector.py
echo.
echo ============================================
echo  采集完成！下一步：
echo  请双击运行「2-生成什亭之匣导入文件.bat」生成最终导入文件！
echo ============================================
pause
