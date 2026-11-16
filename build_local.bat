@echo off
rem Lokaler Build der exe (Windows, Python 3.12 und .NET SDK 8 installiert)
cd /d "%~dp0"
python tools\make_version_file.py || goto :fehler
dotnet build lhm\LhmDeps.csproj -c Release -o lhm_out || goto :fehler
del /q lhm_out\LhmDeps.* 2>nul
python -m pip install -r requirements.txt pyinstaller || goto :fehler
pyinstaller --noconfirm --clean System-Monitor-MQTT-MK1.spec || goto :fehler
echo.
echo Fertig: dist\System-Monitor-MQTT-MK1.exe
pause
exit /b 0
:fehler
echo Build fehlgeschlagen.
pause
exit /b 1
