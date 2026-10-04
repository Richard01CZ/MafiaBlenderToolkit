@echo off
rem The MBT CLI, the Mafia Toolkit from a command prompt. Nothing but
rem Blender is needed: it brings its own Python, and this finds Blender.
rem
rem   mbt --install
rem   mbt --install --textures "D:\Hry\Mafia Editovani\maps"
rem   mbt --install "D:\downloads\mafia-toolkit-1.1.0.zip" --open "D:\models\tommy.4ds"
rem   mbt --textures "D:\Hry\Mafia Editovani\maps" --open "D:\models\Tommy.4ds" --check
rem   mbt --open "D:\models\tommy.4ds" --export "D:\out\tommy.4ds"
rem   mbt --open "D:\models\tommy.4ds" --gui
rem   mbt --help
rem
rem It runs from wherever it finds the add-on's code: beside itself, in the
rem project, or inside a zip sitting next to it - so a zip and this file are
rem a whole working copy, with nothing unpacked and nothing installed.
rem
rem Set MAFIA_BLENDER to a blender.exe to pick one yourself; otherwise the
rem newest installed is used. Exit codes: 0 done, 1 a step refused, 2 a
rem command that made no sense, 3 no Blender found, 4 no add-on found.
setlocal enabledelayedexpansion

set "BLENDER=%MAFIA_BLENDER%"
if not defined BLENDER (
  set "BEST=0"
  for %%R in ("%ProgramFiles%\Blender Foundation" "%ProgramFiles(x86)%\Blender Foundation") do (
    if exist "%%~R" (
      for /d %%D in ("%%~R\Blender *") do (
        if exist "%%~D\blender.exe" (
          rem "Blender 5.2" -> 5 and 2, scored so 5.10 beats 5.2.
          for /f "tokens=2" %%V in ("%%~nxD") do (
            for /f "tokens=1,2 delims=." %%A in ("%%V") do (
              set /a "SCORE=%%A*1000+%%B" 2>nul
              if !SCORE! GTR !BEST! (
                set "BEST=!SCORE!"
                set "BLENDER=%%~D\blender.exe"
              )
            )
          )
        )
      )
    )
  )
)

if not defined BLENDER (
  echo [MBT/CLI] No Blender found. Set MAFIA_BLENDER to its blender.exe.
  exit /b 3
)

rem Where the add-on's own code is: beside this file, in the project under it,
rem or still packed in a zip next to it.
set "CLI="
set "MAFIA_ZIP="
if exist "%~dp0cli.py" set "CLI=%~dp0cli.py"
if not defined CLI if exist "%~dp0io_mafia_toolkit\cli.py" set "CLI=%~dp0io_mafia_toolkit\cli.py"
if not defined CLI (
  for /f "delims=" %%Z in ('dir /b /o-n "%~dp0mafia-toolkit*.zip" 2^>nul') do (
    if not defined MAFIA_ZIP set "MAFIA_ZIP=%~dp0%%Z"
  )
)

if not defined CLI if not defined MAFIA_ZIP (
  echo [MBT/CLI] No add-on found beside this file - expected cli.py, an
  echo [MBT/CLI] io_mafia_toolkit folder, or a mafia-toolkit zip.
  exit /b 4
)

rem A run that is meant to leave a window open must not be a background run.
set "WINDOWED="
echo %* | find /i "--gui" >nul && set "WINDOWED=1"
set "MODE=--background"
if defined WINDOWED set "MODE="

if defined CLI (
  "%BLENDER%" %MODE% --python "%CLI%" -- %*
  exit /b %errorlevel%
)

rem Still zipped: unpack the add-on where temporary files go and run it from
rem there. It works from that copy, and --install installs that same copy.
echo [MBT/CLI] Add-on: !MAFIA_ZIP!
set "BOOT=import sys,os,zipfile,tempfile,runpy; where=tempfile.mkdtemp(prefix='mafia-'); zipfile.ZipFile(os.environ['MAFIA_ZIP']).extractall(where); runpy.run_path(os.path.join(where,'io_mafia_toolkit','cli.py'), run_name='__main__')"
"%BLENDER%" %MODE% --python-expr "!BOOT!" -- %*
exit /b %errorlevel%
