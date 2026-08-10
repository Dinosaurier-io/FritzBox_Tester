@echo off
REM Einrichtung per Doppelklick, auch ohne git und ohne Vorkenntnisse.
REM
REM Diese Datei gibt es, weil Windows PowerShell-Skripte aus dem Internet
REM standardmaessig blockiert ("ist nicht digital signiert"). Wer das Projekt
REM als ZIP herunterlaedt, kommt mit setup.ps1 allein nicht weiter. Eine
REM Batch-Datei unterliegt dieser Sperre nicht und darf PowerShell gezielt
REM starten.
REM
REM Aufruf:  Doppelklick
REM     oder setup.bat --minimal
REM     oder setup.bat --no-test

setlocal
cd /d "%~dp0"

echo == Dateisperren aufheben ==
REM Aus dem Internet geladene Dateien tragen eine Markierung, die PowerShell
REM zum Verweigern bringt. Sie hier zu entfernen erspart die Fehlermeldung
REM auch beim spaeteren Bauen mit packaging\build.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue"
echo    erledigt

REM Die Optionen von setup.ps1 heissen dort -Minimal und -NoTest. Hier sind
REM zusaetzlich die Linux-Schreibweisen erlaubt, damit die Anleitung im
REM README fuer beide Systeme gilt.
set "optionen="
:naechste
if "%~1"=="" goto starten
if /I "%~1"=="--minimal" set "optionen=%optionen% -Minimal" & goto weiter
if /I "%~1"=="-minimal"  set "optionen=%optionen% -Minimal" & goto weiter
if /I "%~1"=="--no-test" set "optionen=%optionen% -NoTest"  & goto weiter
if /I "%~1"=="-notest"   set "optionen=%optionen% -NoTest"  & goto weiter
echo Unbekannte Option: %~1
echo Erlaubt sind --minimal und --no-test
pause
exit /b 2
:weiter
shift
goto naechste

:starten
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"%optionen%
set "code=%ERRORLEVEL%"

echo.
if not "%code%"=="0" (
    echo Die Einrichtung wurde mit Fehler %code% beendet.
    echo Die Meldung darueber sagt, woran es lag.
)
pause
exit /b %code%
