@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   ============================================
echo     刷新素材 - 爬小红书同赛道热门
echo     大约 2-3 分钟，爬完自动生成 素材.json
echo   ============================================
echo.
"C:\Users\zhuge\AppData\Local\Programs\Python\Python311\python.exe" 刷新素材.py
echo.
echo 完成。现在打开「团队工具.html」点「重新加载素材」即可。
pause
