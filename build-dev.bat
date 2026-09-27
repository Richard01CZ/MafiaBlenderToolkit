@echo off
rem Build the workshop add-on and put it into Blender.
rem
rem This is the build with everything in it - the proving suite, the corpus
rem verifiers, the build script itself - so the Blender it lands in can run
rem the tests against exactly what is installed. Its zip is named -dev so it
rem is never handed out by mistake.
rem
rem Anything typed after this passes straight through, so --no-install and
rem --blender "C:\...\blender.exe" work here as well.
setlocal
set PY=python
where python >nul 2>&1 || set PY=py
%PY% "%~dp0io_mafia_toolkit\tools\build_addon.py" %*
exit /b %errorlevel%
