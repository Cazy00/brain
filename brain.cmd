@echo off
REM brain — Windows shim so `brain <command>` works in cmd.exe and PowerShell.
REM
REM bin\brain has no .exe/.cmd extension and relies on a shebang, which Windows
REM does not honour. This is the entry point PATH can actually find.
python "%~dp0bin\brain" %*
