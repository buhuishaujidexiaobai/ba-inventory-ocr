@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  BA 库存采集（自动找窗口+滚动+点击+识别）
echo  请先：启动游戏并进入「道具」页面
echo  采集期间不要动鼠标键盘，约 3-6 分钟
echo ============================================

set PY_CMD=python
if exist "D:\AI工作区\工具\本地识图OCR\ocr-venv\Scripts\python.exe" set PY_CMD="D:\AI工作区\工具\本地识图OCR\ocr-venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set PY_CMD=".venv\Scripts\python.exe"

%PY_CMD% -u ba_queue_collector.py collect
echo.
echo 采集完成。下一步：%PY_CMD% ba_map_items.py 生成导入文件
pause
