@echo off
setlocal EnableDelayedExpansion
title MilRadio Build
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

set PYTHON=C:\Users\johnn\AppData\Local\Programs\Python\Python312\python.exe
set SRC=%~dp0
set DIST=%~dp0dist
set OUTDIR=%~dp0dist\MilRadio

echo.
echo ============================================================
echo  MilRadio Build Script
echo ============================================================
echo.

if not exist "%PYTHON%" (
    echo ERROR: Python not found at %PYTHON%
    echo Edit BUILD.bat and set PYTHON= to your Python 3.12 path.
    pause & exit /b 1
)
echo [1/7] Python OK: %PYTHON%

%PYTHON% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [1/7] Installing PyInstaller...
    %PYTHON% -m pip install pyinstaller --quiet
)
echo [1/7] PyInstaller OK

echo [2/7] Installing dependencies...
%PYTHON% -m pip install -r "%SRC%requirements.txt" --quiet
%PYTHON% -m pip install pystray pillow --quiet
echo [2/7] Dependencies OK

echo [3/7] Cleaning previous build...
if exist "%SRC%build"  rmdir /S /Q "%SRC%build"
if exist "%DIST%"      rmdir /S /Q "%DIST%"
del /Q "%SRC%*.spec" 2>nul
echo [3/7] Clean OK

echo [4/7] Building MilRadio-Server (console)...
%PYTHON% -m PyInstaller --noconfirm --onedir --console --clean ^
  --name "MilRadio-Server" ^
  --icon "%SRC%server.ico" ^
  --add-data "%SRC%radio_config.ini;." ^
  --add-data "%SRC%radio_admin.html;." ^
  --add-data "%SRC%radio_planner.html;." ^
  --add-data "%SRC%milradio_aar_v2.html;." ^
  --add-data "%SRC%planner_data.json;." ^
  --add-data "%SRC%offline_map_data;offline_map_data" ^
  --add-data "%SRC%tabs;tabs" ^
  --add-data "%SRC%help;help" ^
  --add-data "%SRC%opus.dll;." ^
  --add-data "%SRC%libopus-0.dll;." ^
  --add-binary "C:\Users\johnn\AppData\Local\Programs\Python\Python312\Lib\site-packages\libmgrs.cp312-win_amd64.pyd;." ^
  --hidden-import cryptography ^
  --hidden-import scipy.signal ^
  --hidden-import numpy ^
  --hidden-import wave ^
  --hidden-import radio_recorder ^
  "%SRC%radio_web.py"
if errorlevel 1 ( echo ERROR: Server build failed. ^& pause ^& exit /b 1 )
echo [4/7] Server build OK

echo [5/7] Building MilRadio-ServerTray (windowed)...
%PYTHON% -m PyInstaller --noconfirm --onedir --windowed --clean ^
  --name "MilRadio-ServerTray" ^
  --icon "%SRC%server.ico" ^
  --add-data "%SRC%radio_config.ini;." ^
  --add-data "%SRC%radio_admin.html;." ^
  --add-data "%SRC%radio_planner.html;." ^
  --add-data "%SRC%milradio_aar_v2.html;." ^
  --add-data "%SRC%offline_map_data;offline_map_data" ^
  --add-data "%SRC%opus.dll;." ^
  --add-data "%SRC%libopus-0.dll;." ^
  --hidden-import cryptography ^
  --hidden-import scipy.signal ^
  --hidden-import numpy ^
  --hidden-import pystray ^
  --hidden-import pystray._win32 ^
  --hidden-import PIL ^
  --hidden-import PIL.Image ^
  --hidden-import PIL.ImageDraw ^
  "%SRC%radio_server_tray.py"
if errorlevel 1 ( echo ERROR: Tray build failed. ^& pause ^& exit /b 1 )
echo [5/7] Tray build OK

echo [6/8] Building MilRadio client (PySide6)...
%PYTHON% -m PyInstaller --noconfirm --onedir --windowed --clean ^
  --name "MilRadio-Client" ^
  --icon "%SRC%client.ico" ^
  --add-data "%SRC%radio_config.ini;." ^
  --add-data "%SRC%client.png;." ^
  --add-data "%SRC%opus.dll;." ^
  --add-data "%SRC%libopus-0.dll;." ^
  --hidden-import PySide6 ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  --collect-all mgrs ^
  --hidden-import cryptography ^
  --hidden-import scipy.signal ^
  --hidden-import numpy ^
  --hidden-import PySide6 ^
  --hidden-import pynput ^
  --hidden-import pynput.keyboard ^
  --hidden-import pynput.mouse ^
  --hidden-import pyaudio ^
  --add-binary "C:\Users\johnn\AppData\Local\Programs\Python\Python312\Lib\site-packages\libmgrs.cp312-win_amd64.pyd;." ^
  "%SRC%radio_client.py"
if errorlevel 1 ( echo ERROR: Client build failed. ^& pause ^& exit /b 1 )
echo [6/8] Client build OK

echo [7/8] Building MilRadio-Launcher (console)...
%PYTHON% -m PyInstaller --noconfirm --onedir --console --clean ^
  --name "MilRadio-Launcher" ^
  --icon "%SRC%launcher.ico" ^
  "%SRC%radio_launcher.py"
if errorlevel 1 ( echo ERROR: Launcher build failed. ^& pause ^& exit /b 1 )
echo [7/8] Launcher build OK

echo [8/8] Assembling dist\MilRadio\ ...
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

rem Copy the four app folders
xcopy /E /I /Y "%DIST%\MilRadio-ServerTray" "%OUTDIR%\MilRadio-ServerTray\"
if errorlevel 1 ( echo ERROR: xcopy MilRadio-ServerTray failed & pause & exit /b 1 )

xcopy /E /I /Y "%DIST%\MilRadio-Server" "%OUTDIR%\MilRadio-Server\"
if errorlevel 1 ( echo ERROR: xcopy MilRadio-Server failed & pause & exit /b 1 )

xcopy /E /I /Y "%DIST%\MilRadio-Client" "%OUTDIR%\MilRadio-Client\"
if errorlevel 1 ( echo ERROR: xcopy MilRadio-Client failed & pause & exit /b 1 )

xcopy /E /I /Y "%DIST%\MilRadio-Launcher" "%OUTDIR%\MilRadio-Launcher\"
if errorlevel 1 ( echo ERROR: xcopy MilRadio-Launcher failed & pause & exit /b 1 )

rem Create a Windows shortcut (.lnk) at the top level.
rem A .lnk sets WorkingDirectory to the subfolder so _internal\ is found.
powershell -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%OUTDIR%\MilRadio-ServerTray.lnk');$s.TargetPath='%OUTDIR%\MilRadio-ServerTray\MilRadio-ServerTray.exe';$s.WorkingDirectory='%OUTDIR%\MilRadio-ServerTray';$s.Description='MilRadio Server Tray';$s.Save()"
echo Created MilRadio-ServerTray.lnk shortcut

rem Config and assets at the top level
copy /Y "%SRC%radio_config.ini"     "%OUTDIR%\radio_config.ini"
copy /Y "%SRC%requirements.txt"     "%OUTDIR%\requirements.txt"
copy /Y "%SRC%radio_admin.html"     "%OUTDIR%\radio_admin.html"
copy /Y "%SRC%radio_planner.html"   "%OUTDIR%\radio_planner.html"
copy /Y "%SRC%milradio_aar_v2.html" "%OUTDIR%\milradio_aar_v2.html"
copy /Y "%SRC%planner_data.json"    "%OUTDIR%\planner_data.json"
copy /Y "%SRC%opus.dll"             "%OUTDIR%\opus.dll"
copy /Y "%SRC%libopus-0.dll"       "%OUTDIR%\libopus-0.dll"
if exist "%SRC%offline_map_data" xcopy /E /I /Y "%SRC%offline_map_data" "%OUTDIR%\offline_map_data\"
if exist "%SRC%terrain_data"     xcopy /E /I /Y "%SRC%terrain_data"     "%OUTDIR%\terrain_data\"
if exist "%SRC%tabs"             xcopy /E /I /Y "%SRC%tabs"             "%OUTDIR%\tabs\"
if exist "%SRC%help"             xcopy /E /I /Y "%SRC%help"             "%OUTDIR%\help\"
copy /Y "%SRC%README.md"            "%OUTDIR%\README.md" 2>nul
if exist "%SRC%SISO-REF-010.xml" (
    copy /Y "%SRC%SISO-REF-010.xml" "%OUTDIR%\SISO-REF-010.xml"
) else if exist "%SRC%..\sword-atak-gateway\SISO-REF-010.xml" (
    copy /Y "%SRC%..\sword-atak-gateway\SISO-REF-010.xml" "%OUTDIR%\SISO-REF-010.xml"
)

echo.
echo ============================================================
echo  BUILD COMPLETE
echo ============================================================
echo.
echo  Output folder: %OUTDIR%
echo.
echo  dist\MilRadio\
echo    MilRadio-ServerTray.lnk    ^<^-- Double-click this to start server
echo    MilRadio-ServerTray\        ^<^-- or run exe directly from here
echo    MilRadio-Server\            ^<^-- Server started automatically by tray
echo    MilRadio-Client\MilRadio-Client.exe  ^<^-- Run on each operator PC
echo    radio_config.ini            ^<^-- Edit before first run
echo.
rem pause