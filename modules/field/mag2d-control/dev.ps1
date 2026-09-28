# dev.ps1 -- run uv for THIS project with its virtual environment kept in fast
# local storage (AppData), off OneDrive and off network drives.
#
# Why: if the project lives in OneDrive or on a mapped network drive (T:\ ...),
# putting the .venv next to the code makes those systems lock/sync thousands of
# files, and uv fails with "Access is denied (os error 5)". Keeping the env in
# %LOCALAPPDATA% avoids that entirely, and using one folder per project name
# keeps your several projects (mag2d-control, smb-control, stage-control) apart.
#
# Usage (from the project folder):
#     .\dev.ps1 sync --extra gui
#     .\dev.ps1 run scripts/run_gui.py
#     .\dev.ps1 run pytest
#
# If PowerShell blocks the script, either run once:
#     Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# or launch it as:
#     powershell -ExecutionPolicy Bypass -File .\dev.ps1 run scripts/run_gui.py

$project = Split-Path -Leaf $PSScriptRoot
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA "uv-venvs\$project"
uv @args
