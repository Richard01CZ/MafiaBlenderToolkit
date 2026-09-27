@echo off
rem Build the add-on as it ships, and install nothing.
rem
rem This build holds the add-on and nothing else: no workshop folder, no test
rem suite, nothing that writes where a user's files are. The zip it leaves
rem beside this file is the one to hand out.
rem
rem It does not install, on purpose - the Blender you work in should keep the
rem workshop build. To install this one anyway, run the build script directly
rem without --no-install.
setlocal
set PY=python
where python >nul 2>&1 || set PY=py
%PY% "%~dp0io_mafia_toolkit\tools\build_addon.py" --release --no-install %*
exit /b %errorlevel%
