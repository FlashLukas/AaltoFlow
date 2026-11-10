<#
Download vendor software that some instrument modules need, and CHECK it.

    powershell -ExecutionPolicy Bypass -File tools\fetch_vendor_software.ps1 -Dest C:\Temp\vendor
    powershell -ExecutionPolicy Bypass -File tools\fetch_vendor_software.ps1 -Dest C:\Temp\vendor -Only thorlabs-sdk,kinesis

Nothing is installed or run: the files land in -Dest, each checked against a
SHA-256. For Thorlabs the hash is the one Thorlabs publishes next to the
download; for Harogic (which publishes none) it is the hash of the file as
downloaded on 2026-11-10, so a mismatch there may only mean a newer release --
the zip then carries its own file list (manifest\files.sha256, checked by its
preflight.ps1). A mismatch is reported and the file kept as *.unverified.

Which module needs what (and what to do with it) -- see each module's README:
  thorlabs-sdk   camera-control, driver "thorlabs" (Zelux / Kiralux):
                 python scripts\setup_thorlabs_sdk.py <this zip>
  thorimagecam   camera-control, driver "thorlabs": the camera's USB driver
                 (install, then CLOSE the program: it holds the camera)
  kinesis        kim-control / kim001-control (and other Kinesis K-Cubes):
                 the FTDI D2XX driver pylablib talks through (close the app)
  vnstudio       vna-control, driver "harogic" (PNB-60): portable, unzip and
                 run bin\VNStudio.exe; System > SCPI > Enable (port 5025).
                 1.0.8 needs a PNB-60 FIRMWARE upgrade at the same time.

Vendor links change; when one fails, take the new one from the vendor page
named below and update the table (and its hash).
Uses curl.exe (part of Windows 10 1803+). Printed text is ASCII (gotcha #14).

-RevocationBestEffort: on a network that cannot reach a certificate's
revocation server, Windows' TLS refuses the download (curl exit 35,
CRYPT_E_REVOCATION_OFFLINE -- harogic.com from the Blue Box, 2026-11-10). This
switch passes curl's --ssl-revoke-best-effort: the revocation check is skipped
ONLY when the server cannot be reached; a certificate that IS revoked still
fails, and every file is still checked against its SHA-256. Opt-in on purpose:
the default keeps the full check.
#>
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string[]]$Only = @(),
    [switch]$RevocationBestEffort
)

$ErrorActionPreference = 'Stop'

$packages = @(
    @{ Key = 'thorlabs-sdk'; File = 'scientific_camera_interfaces-2.3.zip'
       Page = 'https://www.thorlabs.com/software-pages/ThorCam (Programming Interfaces > Windows SDK and Doc. for Scientific Cameras)'
       Url = 'https://media.thorlabs.com/contentassets/039fcbaaafa0457eb2901466cf0b9489/scientific_camera_interfaces-2.3-06_04_26.zip?v=0826102357'
       Sha256 = '48c3d874231072ef3149acc05ec7530e532083e8d9e6b6e45675507109f735ca'; Official = $true },
    @{ Key = 'thorimagecam'; File = 'thorimagecam_v1.3.15_setup.zip'
       Page = 'https://www.thorlabs.com/software-pages/ThorCam (ThorImageCAM)'
       Url = 'https://media.thorlabs.com/contentassets/36c196637f284dfcb6a7fae1a602a074/thorimagecam_v1.3.15_setup.zip?v=0826101429'
       Sha256 = '671a18e5b4c35c08c349eeb3a70fc37cc0b7b7c8862f830f65a1100faeec5b9f'; Official = $true },
    @{ Key = 'kinesis'; File = 'thorlabs_kinesis_setup_1.14.60_x64.exe'
       Page = 'https://www.thorlabs.com/software-pages/Motion_Control (Kinesis 64-Bit Software for 64-Bit Windows)'
       Url = 'https://media.thorlabs.com/contentassets/24d3756d382b4310b72149e2f26aa8ca/thorlabs_kinesis_setup_27990_x64.exe?v=0804065826'
       Sha256 = '298c4cc03a6c3b4d5c174964ae9193d13baca63dab10c1595b4e9ad5d914ff05'; Official = $true },
    # Harogic replaces the file under a new month folder at every release
    # (1.0.6 of 2026-11-02 was gone a week later): the Page is the way back.
    # 1.0.8 (2026-11-10) REQUIRES the PNB-60's firmware to be upgraded with it
    # (to 1.8.9, release note) -- that changes the instrument: ask first.
    @{ Key = 'vnstudio'; File = 'VNStudio-1.0.8-windows.zip'
       Page = 'https://www.harogic.com/harogic-vnstudio-software/ (VNStudio Windows (zip))'
       Url = 'https://www.harogic.com/wp-content/uploads/2026/11/VNStudio.zip'
       Sha256 = '285fcbd3a01e5065e04f337f66f45a8f02d48a6a63fe99147c11106b75eb4e55'; Official = $false }
)

$known = $packages | ForEach-Object { $_.Key }
foreach ($k in $Only) {
    if ($known -notcontains $k) { throw "unknown package '$k' (known: $($known -join ', '))" }
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (-not (Test-Path $curl)) { throw "curl.exe not found ($curl): Windows 10 1803 or newer is needed" }

$failed = 0
foreach ($p in $packages) {
    if ($Only.Count -gt 0 -and $Only -notcontains $p.Key) { continue }
    $out = Join-Path $Dest $p.File
    Write-Host "[$($p.Key)] $($p.File)"
    if (-not (Test-Path $out)) {
        $opts = @('-fsSL', '--retry', '2', '-A', 'Mozilla/5.0')        # -sS: errors only
        if ($RevocationBestEffort) { $opts += '--ssl-revoke-best-effort' }
        & $curl @opts -o $out $p.Url
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  DOWNLOAD FAILED (curl exit $LASTEXITCODE). Get it from: $($p.Page)"
            if ($LASTEXITCODE -eq 35 -and -not $RevocationBestEffort) {
                Write-Host '  (exit 35 is often "the revocation server was offline": run again with'
                Write-Host '   -RevocationBestEffort -- the SHA-256 check still applies)'
            }
            if (Test-Path $out) { Remove-Item $out -Force }
            $failed++
            continue
        }
    } else {
        Write-Host '  already there, checking it'
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $out).Hash.ToLower()
    if ($hash -eq $p.Sha256) {
        $what = if ($p.Official) { "the vendor's published hash" } else { 'the hash recorded 2026-11-10 (vendor publishes none)' }
        Write-Host "  OK: SHA-256 matches $what"
    } else {
        $bad = $out + '.unverified'
        Move-Item -Force -LiteralPath $out -Destination $bad
        Write-Host "  SHA-256 MISMATCH: got $hash"
        Write-Host "  kept as $bad -- compare with the vendor page: $($p.Page)"
        $failed++
    }
}
if ($failed -gt 0) { Write-Host "$failed package(s) need attention"; exit 1 }
Write-Host "all files in $Dest"
