@echo off
pyinstaller ankerclient.spec --clean
if errorlevel 1 exit /b %errorlevel%
echo.
echo Build complete. Executable: dist\AnkerClient.exe
