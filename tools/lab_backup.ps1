<#
lab_backup.ps1 -- keep the PRIVATE files of a lab installation in a private git repo.

WHY
---
AaltoFlow and AaltoView are public; some files next to them are not meant to be:
  * CLAUDE.local.md (any folder)      personal working notes, loaded by Claude Code
  * suite_local.json                  this PC's ports, real/sim flags, remote
                                      services, data folder, setup name
  * mission-control\profiles.json     the launcher profiles of this lab
  * scan-core\suite_layouts.json      saved control-panel layouts
  * Claude Code's project memory      (optional) %USERPROFILE%\.claude\projects\<this checkout>\memory
All of them are gitignored in the public repos, so without this script they would
exist on one disk only. This script copies them into a separate PRIVATE repo
(e.g. github.com/<you>/AaltoFlow-lab) and back.

USAGE (from the AaltoFlow folder)
---------------------------------
  powershell -ExecutionPolicy Bypass -File tools\lab_backup.ps1 -Mode backup
  powershell -ExecutionPolicy Bypass -File tools\lab_backup.ps1 -Mode restore
  powershell -ExecutionPolicy Bypass -File tools\lab_backup.ps1 -Mode restore -Force

  backup   copy the private files INTO the lab repo, commit and push.
  restore  copy them back OUT of the lab repo (e.g. on a new PC after cloning).
           Existing files are kept unless -Force is given.

  -LabRepo   the private repo's local clone (default: ..\aaltoflow-lab next to
             this checkout). Clone it once: git clone <url> ..\aaltoflow-lab
  -ViewerRoot the AaltoView checkout (default: ..\aaltoview, skipped if absent).
  -NoMemory  leave Claude Code's project memory out.

Layout inside the lab repo:  AaltoFlow\<same paths>, AaltoView\<same paths>,
claude-memory\<files>.
#>
param(
    [Parameter(Mandatory)] [ValidateSet("backup", "restore")] [string]$Mode,
    [string]$LabRepo = "",
    [string]$ViewerRoot = "",
    [switch]$NoMemory,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$flow = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $LabRepo) { $LabRepo = Join-Path (Split-Path $flow) "aaltoflow-lab" }
if (-not $ViewerRoot) { $ViewerRoot = Join-Path (Split-Path $flow) "aaltoview" }
if (-not (Test-Path (Join-Path $LabRepo ".git"))) {
    throw "no lab repo at $LabRepo -- clone your private lab repo there first"
}

# Named files, relative to a checkout. CLAUDE.local.md is found in any folder.
$named = @("suite_local.json", "mission-control\profiles.json", "scan-core\suite_layouts.json")
$skipDirs = @(".venv", ".git", "node_modules", "__pycache__", "build", "dist")

function Get-PrivateFiles($root) {
    $out = @()
    foreach ($n in $named) { if (Test-Path (Join-Path $root $n)) { $out += $n } }
    Get-ChildItem -Path $root -Recurse -Filter "CLAUDE.local.md" -File -ErrorAction SilentlyContinue |
        Where-Object {
            $rel = $_.FullName.Substring($root.Length).TrimStart('\')
            -not ($skipDirs | Where-Object { $rel -like "$_\*" -or $rel -like "*\$_\*" })
        } |
        ForEach-Object { $out += $_.FullName.Substring($root.Length).TrimStart('\') }
    return $out
}

# Claude Code keeps a project's memory under a folder named after the checkout
# path, every character other than a letter or digit replaced by "-".
function Get-MemoryDir($root) {
    $name = ($root -replace '[^A-Za-z0-9]', '-')
    return Join-Path $env:USERPROFILE ".claude\projects\$name\memory"
}

function Copy-One($from, $to) {
    New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
    Copy-Item -LiteralPath $from -Destination $to -Force
}

$pairs = @(@{ Name = "AaltoFlow"; Root = $flow })
if (Test-Path $ViewerRoot) { $pairs += @{ Name = "AaltoView"; Root = (Resolve-Path $ViewerRoot).Path } }

$n = 0
if ($Mode -eq "backup") {
    foreach ($p in $pairs) {
        foreach ($rel in (Get-PrivateFiles $p.Root)) {
            Copy-One (Join-Path $p.Root $rel) (Join-Path $LabRepo (Join-Path $p.Name $rel)); $n++
        }
    }
    if (-not $NoMemory) {
        $mem = Get-MemoryDir $flow
        if (Test-Path $mem) {
            Get-ChildItem $mem -File | ForEach-Object {
                Copy-One $_.FullName (Join-Path $LabRepo "claude-memory\$($_.Name)"); $n++
            }
        }
    }
    Write-Host "copied $n files into $LabRepo"
    Push-Location $LabRepo
    # git writes harmless warnings (line endings) to stderr, and Windows
    # PowerShell 5.1 turns native stderr into an error under "Stop". Judge git
    # by its exit code instead.
    $ErrorActionPreference = "Continue"
    try {
        git add -A 2>$null
        $changes = git status --porcelain
        if ($changes) {
            git commit -q -m "lab backup $(Get-Date -Format 'yyyy-MM-dd HH:mm') from $env:COMPUTERNAME" 2>$null
            if ($LASTEXITCODE -ne 0) { throw "git commit failed in $LabRepo" }
            Write-Host "committed:"
            $changes | ForEach-Object { Write-Host "  $_" }
        } else {
            Write-Host "nothing changed since the last backup"
        }
        # Push every time: a previous run may have committed and then failed to push.
        git push -q 2>$null
        if ($LASTEXITCODE -ne 0) { throw "git push failed -- the backup is committed locally in $LabRepo only" }
        Write-Host "pushed to $(git remote get-url origin)"
    } finally { Pop-Location }
}
else {
    foreach ($p in $pairs) {
        $src = Join-Path $LabRepo $p.Name
        if (-not (Test-Path $src)) { continue }
        Get-ChildItem $src -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($src.Length).TrimStart('\')
            $dest = Join-Path $p.Root $rel
            if ((Test-Path $dest) -and -not $Force) { Write-Host "  kept (exists): $rel"; return }
            Copy-One $_.FullName $dest; $script:n++
        }
    }
    if (-not $NoMemory -and (Test-Path (Join-Path $LabRepo "claude-memory"))) {
        $mem = Get-MemoryDir $flow
        Get-ChildItem (Join-Path $LabRepo "claude-memory") -File | ForEach-Object {
            $dest = Join-Path $mem $_.Name
            if ((Test-Path $dest) -and -not $Force) { return }
            Copy-One $_.FullName $dest; $script:n++
        }
    }
    Write-Host "restored $n files (use -Force to overwrite existing ones)"
}
