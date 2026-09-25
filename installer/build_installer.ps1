<#
build_installer.ps1 -- produce installer\dist\AaltoFlow-Setup-<version>.exe

Run from anywhere, on a developer PC:

    powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1

Needs Inno Setup 6 (the free compiler; ISCC.exe). Install it once with:

    winget install --id JRSoftware.InnoSetup -e --scope user

What it does, and why:
  1. Stages the COMMITTED code (`git archive HEAD`) into installer\build\stage.
     Not the working tree: that holds .venv folders (hundreds of MB), caches,
     scan output and whatever you have not committed. A Setup.exe built from
     a commit is reproducible -- its version names the commit it came from.
     Uncommitted changes are therefore NOT in the installer; you get a warning.
  2. Copies YOUR uv.exe into the stage, so the target PC needs neither uv nor
     Python pre-installed (uv fetches Python itself during `uv sync`).
  3. Runs gen_components.py over the stage: every folder with a `module.toml`
     becomes one checkbox in the wizard, named and ordered by that manifest --
     the same list the launcher discovers, so the two cannot drift apart.
  4. Compiles AaltoFlow.iss with the version  <yyyy.mm.dd>-<short hash>.
#>

param(
    # Build from another commit/tag/branch instead of HEAD.
    [string]$Ref = "HEAD"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$stage = Join-Path $here "build\stage"

function Say($msg, $colour = "Gray") { Write-Host $msg -ForegroundColor $colour }

# --- compiler ---------------------------------------------------------------
$iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) {
    Say "Inno Setup 6 (ISCC.exe) not found. Install it with:" "Red"
    Say "    winget install --id JRSoftware.InnoSetup -e --scope user" "Red"
    exit 1
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { Say "uv not found on PATH -- it is bundled into the installer, so it is needed here." "Red"; exit 1 }

# --- version + dirty check ---------------------------------------------------
Push-Location $repo
try {
    $hash = (git rev-parse --short $Ref).Trim()
    $date = (git log -1 --format=%cd --date=format:%Y.%m.%d $Ref).Trim()
    $dirty = (git status --porcelain) -ne $null
} finally { Pop-Location }
$version = "$date-$hash"
if ($dirty -and $Ref -eq "HEAD") {
    Say "WARNING: you have uncommitted changes. The installer is built from the last" "Yellow"
    Say "         COMMIT ($hash) and will not contain them." "Yellow"
}

# --- stage -------------------------------------------------------------------
Say "Staging $Ref ($version) into $stage" "Cyan"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null
$zip = Join-Path $here "build\src.zip"
Push-Location $repo
try { git archive --format=zip -o $zip $Ref } finally { Pop-Location }
Expand-Archive -Path $zip -DestinationPath $stage -Force
Remove-Item $zip

New-Item -ItemType Directory -Force (Join-Path $stage "uv") | Out-Null
Copy-Item $uv (Join-Path $stage "uv\uv.exe")
$uvx = Join-Path (Split-Path $uv) "uvx.exe"
if (Test-Path $uvx) { Copy-Item $uvx (Join-Path $stage "uv\uvx.exe") }
# uv is MIT OR Apache-2.0: redistributing the binary means shipping its licence.
Copy-Item (Join-Path $PSScriptRoot "third_party\uv-LICENSE-*.txt") (Join-Path $stage "uv")
Say "   bundled $((& $uv --version) -join '')"

# --- components from the module.toml manifests -------------------------------
Say "Generating the component list from the staged module.toml files" "Cyan"
# `uv run --no-project` = a bare interpreter: this script only needs tomllib
# from the standard library, and must not pick up any project's environment.
& $uv run --no-project --python 3.12 python (Join-Path $here "gen_components.py") $stage (Join-Path $here "build")
if ($LASTEXITCODE -ne 0) { throw "gen_components.py failed (exit $LASTEXITCODE)" }

# --- icons from the suite's icon.svg files -----------------------------------
Say "Rendering icons" "Cyan"
# PySide6 draws the SVGs (the same renderer the launcher uses) and Pillow packs
# each one into a multi-size .ico. `--with` gets them for this call only.
& $uv run --no-project --python 3.12 --with pyside6 --with pillow python (Join-Path $here "make_icons.py") $stage (Join-Path $here "build\icons")
if ($LASTEXITCODE -ne 0) { throw "make_icons.py failed (exit $LASTEXITCODE)" }

# --- compile -----------------------------------------------------------------
Say "Compiling with $iscc" "Cyan"
Push-Location $here
try {
    & $iscc "/DAppVersion=$version" "/DStageDir=build\stage" "AaltoFlow.iss"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

$exe = Join-Path $here "dist\AaltoFlow-Setup-$version.exe"
$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Say ""
Say "Built $exe  ($mb MB)" "Green"
