@echo off
rem Lokaler Build der exe (Windows, Python 3.12 und .NET SDK 8 installiert)
cd /d "%~dp0"
python tools\make_version_file.py || goto :fehler
powershell -NoProfile -ExecutionPolicy Bypass -File tools\collect_lhm.ps1 || goto :fehler
python -m pip install -r requirements.txt || goto :fehler
set PYINSTALLER_COMPILE_BOOTLOADER=1
python -m pip install --no-binary pyinstaller --no-cache-dir pyinstaller || python -m pip install pyinstaller || goto :fehler
pyinstaller --noconfirm --clean System-Monitor-MQTT-MK1.spec || goto :fehler
echo.
echo Fertig: dist\System-Monitor-MQTT-MK1\System-Monitor-MQTT-MK1.exe (ganzen Ordner weitergeben)
pause
exit /b 0
:fehler
echo Build fehlgeschlagen.
pause
exit /b 1
