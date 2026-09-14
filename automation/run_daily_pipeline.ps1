# Scheduling wrapper for the daily fetch + extract pipeline.
#
# This does NOT change how fetch_and_track.py or extract_structured.py
# work internally — it only runs the exact same two commands you'd run
# by hand (`python automation/fetch_and_track.py` then
# `python automation/extract_structured.py`), in the project directory,
# with output captured to a dated log file instead of a terminal.
#
# Registered as a daily Windows Task Scheduler job — see
# automation/README_SCHEDULING.md for the schedule and how to inspect,
# change, or remove it.

$ErrorActionPreference = "Continue"

# Force UTF-8 everywhere so bank-page content with special characters
# (rupee signs, en-dashes, etc. — several source pages use these in
# tenure labels) never crashes stdout/stderr redirection to the log file,
# regardless of the invoking context's console codepage.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# Explicit, not inferred: a live test run through Task Scheduler itself
# (2026-08-15) failed here even though running the exact same script by
# hand seconds earlier worked fine — Playwright's browser IS installed at
# C:\Users\hp\AppData\Local\ms-playwright, but a Task-Scheduler-spawned
# process didn't resolve %LOCALAPPDATA% to find it, even with the task's
# LogonType set to Interactive. Pointing Playwright at the exact known
# path removes any dependency on that resolution working correctly in
# whatever context Task Scheduler happens to use.
$env:PLAYWRIGHT_BROWSERS_PATH = "C:\Users\hp\AppData\Local\ms-playwright"

$ProjectDir = "C:\Users\hp\Indian Bank Rag Project"
$PythonExe  = "C:\Python314\python.exe"
$LogDir     = Join-Path $ProjectDir "data\automation_logs"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("run_{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmmss"))

Set-Location $ProjectDir

"=== Daily fetch+extract run started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Out-File -FilePath $LogFile -Encoding utf8

"--- fetch_and_track.py ---" | Out-File -FilePath $LogFile -Append -Encoding utf8
& $PythonExe "automation\fetch_and_track.py" 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
$fetchExit = $LASTEXITCODE

"--- extract_structured.py ---" | Out-File -FilePath $LogFile -Append -Encoding utf8
& $PythonExe "automation\extract_structured.py" 2>&1 | Out-File -FilePath $LogFile -Append -Encoding utf8
$extractExit = $LASTEXITCODE

"=== Run finished $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (fetch exit=$fetchExit, extract exit=$extractExit) ===" |
    Out-File -FilePath $LogFile -Append -Encoding utf8
