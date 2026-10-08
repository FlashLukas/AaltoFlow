<#
lab_backup.ps1 -- keep the PRIVATE files of a lab installation in a private git repo.

WHY
---
AaltoFlow and AaltoView are public; some files next to them are not meant to be:
  * CLAUDE.local.md (any folder)      personal working notes, loaded by Claude Code
  * suite_local.json                  this PC's ports, real/sim flags, remote
                                      services, data folder, setup name --
                                      PER PC: stored as suite_local.<PC name>.json
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
  -FromPC    restore only: whose suite_local.json to take (default: this PC's
             name). For a NEW PC that replaces an old one: -FromPC <old name>.
  -WhatIf    restore only: list what WOULD be restored, kept or skipped, and
             copy nothing (e.g. to check for stale copies before -Force).

suite_local.json is PER PC (2026-10-08): it holds that PC's ports, remote cards
and data folder, so the office's and the lab's must not overwrite each other in
the repo, as they did when both were stored as AaltoFlow\suite_local.json. A
backup writes AaltoFlow\suite_local.<COMPUTERNAME>.json; a restore takes this
PC's (or -FromPC's), and only falls back to the old shared file when there is
no per-PC one.

Old flat copies: a backup made before 2026-09-27 stored a module's notes as
AaltoFlow\<key>-control\..., later ones as AaltoFlow\modules\<category>\<key>-control\...
A restore maps the old path onto the module's folder of today, so a repo that
holds BOTH would restore two files onto one destination -- and the stale one
could win (lab PC, 2026-10-08: pm16's notes came back 65 lines short). The
nested copy now always wins, the flat one is reported as skipped, and a backup
warns while the lab repo still holds such flat folders.

Layout inside the lab repo:  AaltoFlow\<same paths>, AaltoView\<same paths>,
claude-memory\<files>.
#>
param(
    [Parameter(Mandatory)] [ValidateSet("backup", "restore")] [string]$Mode,
    [string]$LabRepo = "",
    [string]$ViewerRoot = "",
    [switch]$NoMemory,
    [switch]$Force,
    [string]$FromPC = "",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$flow = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $LabRepo) { $LabRepo = Join-Path (Split-Path $flow) "aaltoflow-lab" }
if (-not $ViewerRoot) { $ViewerRoot = Join-Path (Split-Path $flow) "aaltoview" }
if (-not (Test-Path (Join-Path $LabRepo ".git"))) {
    throw "no lab repo at $LabRepo -- clone your private lab repo there first"
}

# Named files, relative to a checkout. CLAUDE.local.md is found in any folder.
# suite_local.json is NOT in this list: it is per PC (see above).
$named = @("mission-control\profiles.json", "scan-core\suite_layouts.json")
$pcName = $env:COMPUTERNAME
# suite_local.json and its per-PC copies at the top of the repo's AaltoFlow\
$suiteLocalPattern = '^suite_local(\.[^\\]+)?\.json$'
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

# Since 2026-09-27 the instrument modules live in modules\<category>\<key>-control.
# A backup made BEFORE that stores a module's notes as <key>-control\CLAUDE.local.md;
# restoring it must put them into the folder the module has NOW, not recreate
# the old flat folder (which discovery would then report as a leftover).
function Resolve-ModulePath($root, $rel) {
    $first, $rest = $rel -split '\\', 2
    if (-not $rest -or $first -notlike "*-control") { return $rel }
    if (Test-Path (Join-Path $root $first)) { return $rel }     # still flat here
    $mods = Join-Path $root "modules"
    if (-not (Test-Path $mods)) { return $rel }
    $hit = Get-ChildItem -Path $mods -Directory |
        ForEach-Object { Join-Path $_.FullName $first } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($hit) { return (Join-Path $hit.Substring($root.Length).TrimStart('\') $rest) }
    return $rel
}

function Copy-One($from, $to) {
    New-Item -ItemType Directory -Force -Path (Split-Path $to) | Out-Null
    Copy-Item -LiteralPath $from -Destination $to -Force
}

$pairs = @(@{ Name = "AaltoFlow"; Root = $flow })
if (Test-Path $ViewerRoot) { $pairs += @{ Name = "AaltoView"; Root = (Resolve-Path $ViewerRoot).Path } }

# Flat <key>-control folders in the lab repo's AaltoFlow\ for modules that live
# under modules\ today: stale pre-move copies (see "Old flat copies" above).
function Get-StaleFlat($root) {
    $src = Join-Path $LabRepo "AaltoFlow"
    if (-not (Test-Path $src)) { return @() }
    return @(Get-ChildItem $src -Directory -Filter "*-control" | Where-Object {
        (Resolve-ModulePath $root "$($_.Name)\x") -ne "$($_.Name)\x"
    } | ForEach-Object { $_.Name })
}

$n = 0
if ($Mode -eq "backup") {
    $stale = Get-StaleFlat $flow
    $sl = Join-Path $flow "suite_local.json"
    if (Test-Path $sl) {
        Copy-One $sl (Join-Path $LabRepo "AaltoFlow\suite_local.$pcName.json"); $n++
    }
    if ($stale.Count) {
        Write-Warning ("the lab repo still holds old flat copies for: " + ($stale -join ", ") +
                       ". Their modules live under modules\ now; delete AaltoFlow\<key>-control " +
                       "in $LabRepo once the nested copy has everything (a restore skips them).")
    }
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
        # Collect by DESTINATION first: an old flat copy and the nested one map to
        # the same file, and the nested (not re-mapped) one must win whatever
        # order the folders are listed in.
        $byDest = @{}
        Get-ChildItem $src -Recurse -File | ForEach-Object {
            $srcRel = $_.FullName.Substring($src.Length).TrimStart('\')
            # per-PC suite_local files are restored separately (below)
            if ($p.Name -eq "AaltoFlow" -and $srcRel -match $suiteLocalPattern) { return }
            $rel = $srcRel
            if ($p.Name -eq "AaltoFlow") { $rel = Resolve-ModulePath $p.Root $srcRel }
            $item = @{ From = $_.FullName; SrcRel = $srcRel; Mapped = ($rel -ne $srcRel) }
            if ($byDest.ContainsKey($rel)) {
                $old = $byDest[$rel]
                if ($old.Mapped -and -not $item.Mapped) {
                    Write-Host "  skipped old flat copy: $($old.SrcRel)"; $byDest[$rel] = $item
                } else {
                    Write-Host "  skipped old flat copy: $($item.SrcRel)"
                }
            } else { $byDest[$rel] = $item }
        }
        foreach ($rel in ($byDest.Keys | Sort-Object)) {
            $item = $byDest[$rel]
            $dest = Join-Path $p.Root $rel
            if ((Test-Path $dest) -and -not $Force) { Write-Host "  kept (exists): $rel"; continue }
            if ($WhatIf) { Write-Host "  would restore: $rel"; continue }
            Copy-One $item.From $dest; $n++
        }
    }
    # this PC's suite_local.json: its own per-PC copy (or -FromPC's), else the
    # old shared file a backup made before 2026-10-08
    $who = if ($FromPC) { $FromPC } else { $pcName }
    $cands = @((Join-Path $LabRepo "AaltoFlow\suite_local.$who.json"))
    if (-not $FromPC) { $cands += (Join-Path $LabRepo "AaltoFlow\suite_local.json") }
    $slSrc = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
    $slDest = Join-Path $flow "suite_local.json"
    if (-not $slSrc) {
        Write-Host "  no suite_local.json for $who in the lab repo"
    } elseif ((Test-Path $slDest) -and -not $Force) {
        Write-Host "  kept (exists): suite_local.json"
    } elseif ($WhatIf) {
        Write-Host "  would restore: suite_local.json  (from $(Split-Path $slSrc -Leaf))"
    } else {
        Copy-One $slSrc $slDest; $n++
        Write-Host "  suite_local.json from $(Split-Path $slSrc -Leaf)"
    }
    if (-not $NoMemory -and (Test-Path (Join-Path $LabRepo "claude-memory"))) {
        $mem = Get-MemoryDir $flow
        Get-ChildItem (Join-Path $LabRepo "claude-memory") -File | ForEach-Object {
            $dest = Join-Path $mem $_.Name
            if ((Test-Path $dest) -and -not $Force) { return }
            if ($WhatIf) { Write-Host "  would restore: claude-memory\$($_.Name)"; return }
            Copy-One $_.FullName $dest; $script:n++
        }
    }
    if ($WhatIf) { Write-Host "dry run (-WhatIf): nothing was copied" }
    else { Write-Host "restored $n files (use -Force to overwrite existing ones)" }
}
