<#
postinstall.ps1 -- build the Python environments for an installed AaltoFlow suite.

Setup.exe runs this as its last step, with the modules you ticked. The Start-menu
entry "Rebuild Python environments" runs it with no -Projects, which means
"every module folder that is installed".

Why a separate step at all: the installer only copies SOURCE code. Each project
still needs its own Python + packages (PySide6, numpy, pyzmq, ...) in a
`.venv` next to it. `uv sync` builds that from the project's committed
`uv.lock`, so every PC gets exactly the versions that were tested. uv is
shipped inside the installer (<root>\uv\uv.exe); uv in turn downloads a Python
interpreter and the packages -- so THIS step needs internet (PyPI + GitHub for
the Python build). Re-running it is always safe: uv only fetches what is missing.

Each project gets `.venv` inside its own folder, because mission-control
launches services with `<project>\.venv\Scripts\python.exe` (so Stop kills the
real process -- suite gotcha #7). The installer refuses OneDrive folders for
the same reason: OneDrive locks files inside .venv (gotcha #8).

With no -Projects it syncs suite-common, mission-control, scan-core and every
folder holding a `module.toml` -- the same definition of "a module" the
launcher uses, so a module added to the suite needs no change here.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    # Comma-separated project folder names. Empty = every installed project.
    [string]$Projects = "",

    # Close the window without waiting for Enter (used for silent installs).
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$Root = [System.IO.Path]::GetFullPath($Root)
$uv = Join-Path $Root "uv\uv.exe"
$log = Join-Path $Root "install_log.txt"

# suite-common is a PATH dependency of both mission-control and scan-core
# (`uv sync` fails without the folder), so it is always first.
$Always = @("suite-common", "mission-control")

function Get-Extras($dir) {
    <#
    Which `--extra` flags this project needs.

    Asking the pyproject beats keeping a list here: `uv sync --extra gui` is an
    error for a project that declares no such extra (zpiezo is headless;
    mission-control and suite-common have no extras at all), and a module added
    later would be missed.

    Both extras matter, because `uv sync` REMOVES every extra it is not asked
    for (docs/DEVELOPER_NOTES.md gotcha #29): vna-control keeps its VISA driver in an
    extra called `real`, and syncing with only `gui` would uninstall it, so
    starting that service with --real would fail on the rig.
    #>
    $toml = Join-Path $dir "pyproject.toml"
    if (-not (Test-Path $toml)) { return @() }
    $text = Get-Content $toml -Raw
    $sect = [regex]::Match($text, '(?ms)^\[project\.optional-dependencies\](.*?)(^\[|\z)')
    if (-not $sect.Success) { return @() }
    $body = $sect.Groups[1].Value
    $extras = @()
    foreach ($name in @("gui", "real")) {
        if ($body -match "(?m)^\s*$name\s*=") { $extras += $name }
    }
    return $extras
}

function Resolve-ManagedPython {
    <#
    Work around Windows error 448, "the path cannot be traversed because it
    contains an untrusted mount point".

    uv keeps its managed interpreters as  cpython-3.14-windows-x86_64-none  ->
    a JUNCTION to the real  cpython-3.14.7-windows-x86_64-none . A process that
    Setup.exe started may not follow such a junction, so uv's scan of its own
    managed installs fails and every sync dies -- even though the interpreter is
    sitting right there. The same script run from a normal window (the
    Start-menu "Rebuild Python environments" entry) has no such restriction.

    So: find the REAL versioned folder and hand uv that exact interpreter with
    --python, which needs no junction to be followed.

    Plain .NET calls, not Get-ChildItem: reading the ATTRIBUTES of the entries
    is itself blocked in that context, so anything that inspects every entry
    comes back empty. The NAME is enough to tell them apart -- a three-part
    version is a real folder, a two-part one is the junction.
    #>
    $dir = $env:UV_PYTHON_INSTALL_DIR
    if (-not $dir) { $dir = Join-Path $env:APPDATA "uv\python" }
    if (-not [IO.Directory]::Exists($dir)) { return $null }
    $best = $null; $bestVer = $null
    foreach ($d in [IO.Directory]::GetDirectories($dir)) {
        $name = [IO.Path]::GetFileName($d)
        $m = [regex]::Match($name, '^cpython-(\d+)\.(\d+)\.(\d+)-')
        if (-not $m.Success) { continue }
        $exe = Join-Path $d "python.exe"
        if (-not [IO.File]::Exists($exe)) { continue }
        $ver = [version]("{0}.{1}.{2}" -f $m.Groups[1].Value, $m.Groups[2].Value, $m.Groups[3].Value)
        if (($null -eq $bestVer) -or ($ver -gt $bestVer)) { $bestVer = $ver; $best = $exe }
    }
    return $best
}

function Find-Projects($root) {
    # Every folder with a module.toml is a module (suite_common.discover), plus
    # the always-present folders and scan-core.
    $found = @()
    foreach ($n in ($Always + @("scan-core"))) {
        if (Test-Path (Join-Path $root $n)) { $found += $n }
    }
    foreach ($d in (Get-ChildItem $root -Directory | Sort-Object Name)) {
        if ((Test-Path (Join-Path $d.FullName "module.toml")) -and ($found -notcontains $d.Name)) {
            $found += $d.Name
        }
    }
    return $found
}

function Say($msg, $colour = "Gray") {
    Write-Host $msg -ForegroundColor $colour
    Add-Content -Path $log -Value $msg -Encoding utf8
}

"AaltoFlow environment build  $(Get-Date -Format 'yyyy-MM-dd HH:mm')  on $env:COMPUTERNAME" |
    Set-Content -Path $log -Encoding utf8

if ($Projects.Trim()) {
    $list = $Projects.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    # A module folder alone is not enough: mission-control and scan-core import
    # suite-common through a path dependency, so it must be there and synced.
    foreach ($n in $Always) {
        if (($list -notcontains $n) -and (Test-Path (Join-Path $Root $n))) { $list = @($n) + $list }
    }
} else {
    $list = Find-Projects $Root
}

if (-not (Test-Path $uv)) {
    Say "uv.exe not found at $uv -- the installation is incomplete. Re-run Setup." "Red"
    if (-not $NoPause) { Read-Host "Press Enter to close" }
    exit 1
}

# A User-level UV_PROJECT_ENVIRONMENT would make every project share ONE
# environment (gotcha #8). Ignore it for this run; the process-level variable
# is what uv reads, so clearing it here fixes the build without touching the
# user's settings.
if ($env:UV_PROJECT_ENVIRONMENT) {
    Say "note: ignoring UV_PROJECT_ENVIRONMENT=$env:UV_PROJECT_ENVIRONMENT for this build" "Yellow"
    Remove-Item Env:UV_PROJECT_ENVIRONMENT
}
# uv prints progress to stderr; keep it as plain text in the log.
$env:NO_COLOR = "1"

Say ""
Say "Building Python environments for: $($list -join ', ')" "Cyan"
Say "The first run downloads Python and PySide6 (a few hundred MB). Later runs are quick."
Say ""

# Pin the interpreter when we can (see Resolve-ManagedPython).
$pinnedPython = Resolve-ManagedPython
if ($pinnedPython) {
    Say "using Python: $pinnedPython" "DarkGray"
} else {
    # Nothing to point at, so uv will fetch an interpreter. Put it INSIDE the
    # installation instead of %APPDATA%\uv\python, whose junction layout is
    # what blocks us above -- and which an uninstall would leave behind.
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Root "python"
    Say "no interpreter found yet; uv will install one into $env:UV_PYTHON_INSTALL_DIR" "DarkGray"
}

$failed = @()
$i = 0
foreach ($name in $list) {
    $i++
    $dir = Join-Path $Root $name
    if (-not (Test-Path $dir)) { Say "[$i/$($list.Count)] $name -- not installed, skipped" "DarkGray"; continue }
    $argv = @("sync")
    foreach ($extra in (Get-Extras $dir)) { $argv += @("--extra", $extra) }
    if ($pinnedPython) { $argv += @("--python", $pinnedPython) }
    Say "[$i/$($list.Count)] $name   (uv $($argv -join ' '))" "Cyan"
    $t0 = Get-Date
    Push-Location $dir
    try {
        # Stream uv's output to the window AND the log. `2>&1` in Windows
        # PowerShell 5.1 wraps each stderr line as an error record; with
        # $ErrorActionPreference=Stop that would abort on uv's first progress
        # line, so relax it just around the native call.
        $ErrorActionPreference = "Continue"
        & $uv @argv 2>&1 | ForEach-Object {
            $line = "$_"
            Write-Host "    $line" -ForegroundColor DarkGray
            Add-Content -Path $log -Value "    $line" -Encoding utf8
        }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = "Stop"
        Pop-Location
    }
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    if ($code -eq 0) { Say "    OK  (${secs}s)" "Green" }
    else { Say "    FAILED (exit $code)" "Red"; $failed += $name }
}

Say ""
if ($failed.Count -eq 0) {
    Say "All environments are ready. Start the suite from the Start menu: AaltoFlow > '... Mission Control'." "Green"
    $rc = 0
} else {
    Say "FAILED: $($failed -join ', ')" "Red"
    if ((Get-Content $log -Raw) -match "untrusted mount point") {
        # Windows refused to follow uv's junction for a process Setup started.
        Say "Cause: Windows blocked this installer from reaching uv's Python (error 448)." "Yellow"
        Say "Fix: run Start menu > AaltoFlow > 'Rebuild Python environments'. It is the same" "Yellow"
        Say "step from a normal window, where the restriction does not apply." "Yellow"
    } else {
        Say "Most likely cause: no internet (PyPI / GitHub blocked). Fix the network, then run" "Yellow"
        Say "Start menu > AaltoFlow > 'Rebuild Python environments'. Full log: $log" "Yellow"
    }
    $rc = 1
}
# The LAST line, always, and machine-readable: run_envs_task.ps1 waits for it
# rather than trusting the Task Scheduler's idea of whether we are still running.
Add-Content -Path $log -Value "BUILD FINISHED rc=$rc" -Encoding utf8
if (-not $NoPause) { Read-Host "Press Enter to close" }
exit $rc
