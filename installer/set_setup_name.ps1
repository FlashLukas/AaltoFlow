<#
set_setup_name.ps1 -- remember which setup this installation drives.

Setup asks "which setup is this?" (e.g. TR-MOKE) and runs this script, which
writes the answer to settings.setup_name in <root>\suite_local.json -- the file
that already holds this PC's port overrides, remote services and data folder.
Every window then reads it through suite_common.title():
    "TR-MOKE · Mission Control"   instead of   "AaltoFlow · Mission Control".
An empty name removes the key again (titles fall back to AaltoFlow).

It MERGES: everything else in the file is kept. It writes UTF-8 without a BOM
through .NET, never Set-Content -- PowerShell 5.1 would write the Windows code
page (docs/DEVELOPER_NOTES.md gotcha #26) and Python reads the file as UTF-8.

    powershell -ExecutionPolicy Bypass -File set_setup_name.ps1 -Root <suite> -Name "TR-MOKE"
#>
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Name = ""
)
$ErrorActionPreference = "Stop"
$path = Join-Path $Root "suite_local.json"
$Name = $Name.Trim()

$data = $null
if (Test-Path -LiteralPath $path) {
    $text = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    if ($text.Trim()) { $data = $text | ConvertFrom-Json }
}
if ($null -eq $data) { $data = New-Object PSObject }

if (-not ($data.PSObject.Properties.Name -contains "settings") -or $null -eq $data.settings) {
    $data | Add-Member -NotePropertyName settings -NotePropertyValue (New-Object PSObject) -Force
}
$settings = $data.settings
if ($Name) {
    $settings | Add-Member -NotePropertyName setup_name -NotePropertyValue $Name -Force
} elseif ($settings.PSObject.Properties.Name -contains "setup_name") {
    $settings.PSObject.Properties.Remove("setup_name")
}

# -Depth: PowerShell's default of 2 would flatten nested module/remote entries
# into strings like "@{real=True}".
$json = $data | ConvertTo-Json -Depth 20
$tmp = "$path.tmp"
[IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
Move-Item -LiteralPath $tmp -Destination $path -Force
if ($Name) { "setup name: $Name" } else { "setup name: (none -- titles say AaltoFlow)" }
