<#
deploy_lab.ps1 -- put the whole AaltoFlow suite on a machine and prove it runs.

Run on the target PC, in PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass
    .\deploy_lab.ps1 -Target "C:\AaltoFlow"

Or without cloning first (downloads just this script):

    irm https://raw.githubusercontent.com/FlashLukas/AaltoFlow/main/tools/deploy_lab.ps1 -OutFile deploy_lab.ps1

(The repo is PRIVATE, so that raw URL needs you to be signed in; the simplest
route is to git clone the repo and run tools\deploy_lab.ps1 from inside it.)

What it does, in order, and stops loudly at the first thing it cannot fix:

  1. Checks git and uv are available (installs uv if missing; uv then fetches
     its own Python, so no system Python is needed).
  2. Clones the repo into -Target, or pulls if it is already there.
  3. `uv sync` for all eight Python projects, with the gui extra where needed.
  4. Runs every project's test suite.
  5. Runs scan-core's headless demo, which writes real netCDF files.
  6. Live check: starts the clMag service, runs a real 9-point field scan
     against it over ZeroMQ, and stops it again.
  7. Prints a table, and writes the same to deploy_report.txt.

Everything runs in SIMULATION. No instrument driver is installed and nothing
talks to hardware -- that is the per-module hardware pass (docs/DEVELOPER_NOTES.md section
11), which needs the vendor runtimes (NI-VISA, NI-DAQmx, Thorlabs Kinesis, IDS
peak) and has to be done one instrument at a time.

WHERE THE VIRTUAL ENVIRONMENTS GO. On a plain local disk each project gets its
own `.venv` folder, which is what mission-control expects (it launches services
with `.venv\Scripts\python.exe` so that Stop kills the real process). If the
target is inside OneDrive, OneDrive locks files in `.venv` and `uv sync` fails
with "Access is denied", so the script puts the environments in
%LOCALAPPDATA%\uv-venvs instead, and says so.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Target,

    [string]$Repo = "https://github.com/FlashLukas/AaltoFlow.git",

    # Skip the live service check, e.g. if services are already running on
    # this PC and holding the ports.
    [switch]$NoLive,

    # Skip cloning and use -Target as it is (used to test the script against a
    # copy that is already on disk).
    [switch]$NoClone
)

$ErrorActionPreference = "Stop"
$started = Get-Date

# The projects are FOUND, not listed: every top-level folder with a
# pyproject.toml (the modules, suite-common, scan-core, mission-control). A
# project gets `--extra gui` when its pyproject declares a `gui` extra. So a new
# module is deployed and tested without editing this script. The list is built
# after the clone/pull (step 2), because before that the folders may not exist.
function Get-SuiteProjects($root) {
    $found = foreach ($py in Get-ChildItem -Path $root -Filter pyproject.toml -Depth 1 -File) {
        if ($py.Directory.FullName -eq (Resolve-Path $root).Path) { continue }   # the root itself
        $text = Get-Content $py.FullName -Raw
        @{ Name = $py.Directory.Name; Gui = [bool]($text -match '(?m)^\s*gui\s*=\s*\[') }
    }
    # suite-common first: everything else depends on it
    @($found | Sort-Object @{ Expression = { $_.Name -ne "suite-common" } }, @{ Expression = { $_.Name } })
}
$Projects = @()

$Results = New-Object System.Collections.ArrayList

function Say($msg, $colour = "Gray") { Write-Host $msg -ForegroundColor $colour }
function Step($msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Record($what, $ok, $detail) {
    [void]$Results.Add([pscustomobject]@{ Check = $what; OK = $ok; Detail = $detail })
    if ($ok) { Say "   OK    $what  $detail" "Green" } else { Say "   FAIL  $what  $detail" "Red" }
}

# Run a native command and capture everything it prints, WITHOUT letting
# PowerShell 5.1 turn its stderr into terminating errors. uv writes progress to
# stderr, and in 5.1 that alone would stop the script with $ErrorActionPreference
# set to Stop, even when uv succeeded.
function Invoke-Native([string]$exe, [string[]]$argv, [string]$cwd) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = ($argv | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
    $psi.WorkingDirectory = $cwd
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    foreach ($k in $script:ChildEnv.Keys) { $psi.EnvironmentVariables[$k] = $script:ChildEnv[$k] }
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    $p.WaitForExit()
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $out.Result + $err.Result }
}

$script:ChildEnv = @{}
# Printed text stays ASCII-safe even through pipes (suite gotcha #14).
$script:ChildEnv["PYTHONIOENCODING"] = "utf-8"

# --------------------------------------------------------------------------- 1
Step "1/7  Tools"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    if (-not $NoClone) {
        Say "git is not installed. Install it from https://git-scm.com/download/win" "Red"
        Say "(or: winget install --id Git.Git -e), open a NEW PowerShell, and re-run." "Red"
        exit 1
    }
} else {
    Record "git" $true ((git --version) -join "")
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "   uv not found -- installing it for this user (from astral.sh)" "Yellow"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    # The installer puts uv in %USERPROFILE%\.local\bin but the CURRENT shell's
    # PATH was read before it existed, so add it for the rest of this run.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say "uv installed but still not on PATH. Open a new PowerShell and re-run." "Red"
        exit 1
    }
}
$uv = (Get-Command uv).Source
Record "uv" $true ((& $uv --version) -join "")

# A globally pinned UV_PROJECT_ENVIRONMENT makes every project share and
# corrupt ONE environment (suite gotcha #8). It bit us once already.
$pinned = [Environment]::GetEnvironmentVariable("UV_PROJECT_ENVIRONMENT", "User")
if ($pinned) {
    Say "   UV_PROJECT_ENVIRONMENT is pinned for this user to: $pinned" "Red"
    Say "   Every project would share that one environment. Clear it with:" "Red"
    Say '   [Environment]::SetEnvironmentVariable("UV_PROJECT_ENVIRONMENT",$null,"User")' "Red"
    exit 1
}

# --------------------------------------------------------------------------- 2
Step "2/7  Source"

$Target = [System.IO.Path]::GetFullPath($Target)
if (-not $NoClone) {
    if (Test-Path (Join-Path $Target ".git")) {
        $r = Invoke-Native "git" @("pull", "--ff-only") $Target
        Record "git pull" ($r.Code -eq 0) ($r.Out.Trim() -split "`n" | Select-Object -Last 1)
        if ($r.Code -ne 0) { Say $r.Out; exit 1 }
    } else {
        $parent = Split-Path $Target -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
        if ((Test-Path $Target) -and (Get-ChildItem $Target -Force | Select-Object -First 1)) {
            Say "   $Target exists, is not empty, and is not a git checkout." "Red"
            Say "   Refusing to clone over it. Point -Target at an empty or new folder." "Red"
            exit 1
        }
        Say "   cloning $Repo (a private repo: git may open a browser to sign in)"
        $r = Invoke-Native "git" @("clone", $Repo, $Target) $parent
        Record "git clone" ($r.Code -eq 0) $Target
        if ($r.Code -ne 0) { Say $r.Out; exit 1 }
    }
}
if (-not (Test-Path (Join-Path $Target "scan-core"))) {
    Say "   $Target does not look like the AaltoFlow repo (no scan-core folder)." "Red"
    exit 1
}
$commit = (Invoke-Native "git" @("log", "-1", "--format=%h %s") $Target).Out.Trim()
Record "revision" $true $commit

# OneDrive locks .venv contents. Decide where the environments live.
$onedrive = @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer) |
    Where-Object { $_ } | ForEach-Object { [System.IO.Path]::GetFullPath($_) }
$underOneDrive = $false
foreach ($od in $onedrive) { if ($Target.StartsWith($od, [StringComparison]::OrdinalIgnoreCase)) { $underOneDrive = $true } }
if ($underOneDrive) {
    Say "   Target is inside OneDrive -- environments go to %LOCALAPPDATA%\uv-venvs" "Yellow"
    Say "   (mission-control will then fall back to 'uv run' for services)." "Yellow"
} else {
    Say "   Target is on local disk -- each project gets its own .venv"
}

function EnvFor($project) {
    if ($underOneDrive) { return (Join-Path $env:LOCALAPPDATA "uv-venvs\$project") }
    return $null
}

# --------------------------------------------------------------------------- 3
Step "3/7  Install dependencies (uv sync) -- the first run downloads PySide6 etc."

$Projects = Get-SuiteProjects $Target
Say ("projects found: " + (($Projects | ForEach-Object { $_.Name }) -join ", "))

foreach ($p in $Projects) {
    $dir = Join-Path $Target $p.Name
    $script:ChildEnv.Remove("UV_PROJECT_ENVIRONMENT")
    $e = EnvFor $p.Name
    if ($e) { $script:ChildEnv["UV_PROJECT_ENVIRONMENT"] = $e }
    $args_ = @("sync")
    if ($p.Gui) { $args_ += @("--extra", "gui") }
    # `uv sync` REMOVES any extra it is not asked for. vna-control keeps its
    # pure-Python VISA driver in an extra called `real`; syncing without it
    # would silently uninstall pyvisa and break `run_service.py --real`.
    $pyproj = Join-Path $dir "pyproject.toml"
    if ((Test-Path $pyproj) -and (Select-String -Path $pyproj -Pattern '^\s*real\s*=' -Quiet)) {
        $args_ += @("--extra", "real")
    }
    $t0 = Get-Date
    $r = Invoke-Native $uv $args_ $dir
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    Record "sync $($p.Name)" ($r.Code -eq 0) "${secs}s"
    if ($r.Code -ne 0) { Say ($r.Out.Trim() -split "`n" | Select-Object -Last 15 | Out-String) "DarkRed" }
}

# --------------------------------------------------------------------------- 4
Step "4/7  Test suites"

$total = 0
foreach ($p in $Projects) {
    $dir = Join-Path $Target $p.Name
    $script:ChildEnv.Remove("UV_PROJECT_ENVIRONMENT")
    $e = EnvFor $p.Name
    if ($e) { $script:ChildEnv["UV_PROJECT_ENVIRONMENT"] = $e }
    # GUI tests render offscreen; without the font dir every label is a box,
    # which does not fail tests but does make any screenshot useless.
    $script:ChildEnv["QT_QPA_PLATFORM"] = "offscreen"
    $script:ChildEnv["QT_QPA_FONTDIR"] = "C:\Windows\Fonts"
    $args_ = @("run")
    if ($p.Gui) { $args_ += @("--extra", "gui") }
    $args_ += @("pytest", "-q", "-p", "no:cacheprovider")
    $r = Invoke-Native $uv $args_ $dir
    $summary = ($r.Out -split "`n" | Where-Object { $_ -match "passed|failed|error" } | Select-Object -Last 1)
    if ($summary -match "(\d+) passed") { $total += [int]$Matches[1] }
    $ok = ($r.Code -eq 0)
    Record "tests $($p.Name)" $ok ($(if ($summary) { $summary.Trim() } else { "no summary line" }))
    if (-not $ok) { Say ($r.Out.Trim() -split "`n" | Select-Object -Last 25 | Out-String) "DarkRed" }
}
$script:ChildEnv.Remove("QT_QPA_PLATFORM")
$script:ChildEnv.Remove("QT_QPA_FONTDIR")
Say "   total tests passed: $total" "Cyan"

# --------------------------------------------------------------------------- 5
Step "5/7  scan-core headless demo"

$sc = Join-Path $Target "scan-core"
$script:ChildEnv.Remove("UV_PROJECT_ENVIRONMENT")
$e = EnvFor "scan-core"
if ($e) { $script:ChildEnv["UV_PROJECT_ENVIRONMENT"] = $e }
$script:ChildEnv["MPLBACKEND"] = "Agg"
$r = Invoke-Native $uv @("run", "--extra", "gui", "python", "run_demo.py") $sc
$wrote = ($r.Out -split "`n" | Where-Object { $_ -match "^wrote" } | Select-Object -Last 1)
Record "run_demo.py" (($r.Code -eq 0) -and $wrote) ($(if ($wrote) { $wrote.Trim() } else { "no output files reported" }))
if ($r.Code -ne 0) { Say ($r.Out.Trim() -split "`n" | Select-Object -Last 20 | Out-String) "DarkRed" }

# --------------------------------------------------------------------------- 6
Step "6/7  Live check: a real scan against a running service"

if ($NoLive) {
    Say "   skipped (-NoLive)"
} elseif (Get-NetTCPConnection -LocalPort 5555 -State Listen -ErrorAction SilentlyContinue) {
    Record "live scan" $false "port 5555 is already in use -- something is running; re-run with -NoLive or stop it"
} else {
    $cm = Join-Path $Target "clMag-control"
    $e = EnvFor "clMag-control"
    # Launch the service with the environment's python directly, NOT `uv run`:
    # uv inserts a wrapper process, and stopping the wrapper orphans the real
    # service, which then keeps holding its port (suite gotcha #7).
    if ($e) { $py = Join-Path $e "Scripts\python.exe" } else { $py = Join-Path $cm ".venv\Scripts\python.exe" }
    if (-not (Test-Path $py)) {
        Record "live scan" $false "no python at $py (did clMag-control sync?)"
    } else {
        $svc = Start-Process -FilePath $py -ArgumentList "scripts\run_service.py" `
            -WorkingDirectory $cm -WindowStyle Hidden -PassThru
        try {
            $up = $false
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep -Milliseconds 500
                if (Get-NetTCPConnection -LocalPort 5555 -State Listen -ErrorAction SilentlyContinue) { $up = $true; break }
            }
            if (-not $up) {
                Record "live scan" $false "clMag service did not open port 5555 within 20 s"
            } else {
                $script:ChildEnv.Remove("UV_PROJECT_ENVIRONMENT")
                $e2 = EnvFor "scan-core"
                if ($e2) { $script:ChildEnv["UV_PROJECT_ENVIRONMENT"] = $e2 }
                $r = Invoke-Native $uv @("run", "--extra", "gui", "python", "run_lab_demo.py") $sc
                $done = ($r.Out -split "`n" | Where-Object { $_ -match "^done in" } | Select-Object -Last 1)
                $worst = 0.0
                foreach ($line in ($r.Out -split "`n")) {
                    if ($line -match "error\s+([+-]?\d+\.\d+)") { $worst = [Math]::Max($worst, [Math]::Abs([double]$Matches[1])) }
                }
                $ok = ($r.Code -eq 0) -and $done
                Record "live scan" $ok ($(if ($done) { "$($done.Trim()), worst field error $worst mT" } else { "scan did not complete" }))
                if (-not $ok) { Say ($r.Out.Trim() -split "`n" | Select-Object -Last 20 | Out-String) "DarkRed" }
            }
        } finally {
            Stop-Process -Id $svc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

# --------------------------------------------------------------------------- 7
Step "7/7  Report"

$failed = @($Results | Where-Object { -not $_.OK })
$elapsed = [int]((Get-Date) - $started).TotalMinutes
$report = @()
$report += "AaltoFlow deployment report"
$report += "machine : $env:COMPUTERNAME ($env:USERNAME)"
$report += "target  : $Target"
$report += "revision: $commit"
$report += "when    : $(Get-Date -Format 'yyyy-MM-dd HH:mm')  (took ~$elapsed min)"
$report += ""
$report += ($Results | Format-Table -AutoSize | Out-String -Width 200)
$report += "tests passed: $total"
if ($failed.Count -eq 0) { $report += "RESULT: everything runs." } else { $report += "RESULT: $($failed.Count) check(s) FAILED." }

$reportPath = Join-Path $Target "deploy_report.txt"
$report | Set-Content -Path $reportPath -Encoding utf8
$report | ForEach-Object { Write-Host $_ }
Say "report written to $reportPath" "Cyan"

if ($failed.Count -gt 0) { exit 1 } else { exit 0 }
