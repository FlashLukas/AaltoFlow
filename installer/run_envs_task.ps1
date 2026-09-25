<#
run_envs_task.ps1 -- build the Python environments in a process Setup did NOT spawn.

WHY THIS EXISTS
---------------
postinstall.ps1 works perfectly from a normal window and fails from inside
Setup.exe, at every single module, with

    error: Failed to query Python interpreter
      Caused by: The path cannot be traversed because it contains an untrusted
                 mount point. (os error 448)

Measured on the lab PC 2026-09-24, and note what it is NOT. It is not the
two-part-version junction in the path: postinstall.ps1 already resolves the real
folder and passes it as --python, the log proves it did so, and every component
of that path is a real directory. uv still fails, because it ENUMERATES its
managed interpreter directory before using anything -- and that directory holds
one junction per version (cpython-3.14 -> cpython-3.14.6). Reading a junction
from a process that Setup started is refused, so no amount of pointing at the
right interpreter helps. The scan happens first.

So the fix is not a better path. It is a different PROCESS. A one-shot scheduled
task is launched by the Task Scheduler service rather than by Setup, and carries
none of the restriction -- the same reason the Start-menu "Rebuild Python
environments" entry always worked.

We create the task, run it, follow its log in THIS window so the user still sees
progress (a first build downloads a few hundred MB and must not look hung), then
delete it.

If the scheduler is not available -- a locked-down PC, group policy -- we fall
back to running postinstall.ps1 directly. That may still hit 448, but then the
user is exactly where they were before this script existed: a clear message and
a Start-menu entry that works.
#>
param(
    [Parameter(Mandatory)] [string]$Root,
    [string]$Projects = "",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$post = Join-Path $here "postinstall.ps1"
$log  = Join-Path $Root "install_log.txt"
$TaskName = "AaltoFlow build Python environments"

function Say($msg, $colour = "Gray") { Write-Host $msg -ForegroundColor $colour }

if (-not (Test-Path $post)) { Say "postinstall.ps1 missing next to this script" "Red"; exit 1 }

# The argument list the worker runs, either way.
$inner = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", "`"$post`"",
    "-Root", "`"$Root`"",
    "-NoPause"
)
if ($Projects) { $inner += @("-Projects", "`"$Projects`"") }

function Invoke-Directly {
    Say "Running the build in this window instead." "Yellow"
    & powershell.exe @inner
    return $LASTEXITCODE
}

# ---- can we use the scheduler at all? ---------------------------------------
$haveScheduler = $null -ne (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)
if (-not $haveScheduler) {
    Say "The Task Scheduler cmdlets are not available on this PC." "Yellow"
    exit (Invoke-Directly)
}

# A stale task from an interrupted install would block registration.
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch { }

$code = $null
try {
    # -WindowStyle Hidden: the task's own console would be a second, confusing
    # window. Progress is followed from the log below instead.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument (@("-WindowStyle", "Hidden") + $inner -join " ") -WorkingDirectory $Root
    # Interactive: it must run in the logged-on user's session, with that user's
    # profile -- uv's interpreters and caches live under %APPDATA%.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    # No time limit: a first build on a slow link can take a long while, and the
    # scheduler's default is to kill the task after three days -- but the
    # default "stop if the computer switches to battery" would kill it in
    # minutes on a laptop, so both are cleared here.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
        -Settings $settings -Force | Out-Null
    Say "Building the environments through the Task Scheduler (this avoids a Windows" "Cyan"
    Say "restriction on processes started by Setup). Progress follows:" "Cyan"
    Say ""
    # The previous run's log ends in its own "BUILD FINISHED" line: remove it,
    # or the follower could take that for this run's end before the worker has
    # even started (postinstall.ps1 writes a fresh log).
    Remove-Item -Path $log -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName

    # ---- follow the worker's log so the window is not silent ----------------
    # DONE = the worker's own last line, "BUILD FINISHED rc=N" (postinstall.ps1).
    # NOT "the task is no longer Running": on 2026-09-25 a build started from
    # Setup was cut off after 9 of 15 modules, twice, while the same wrapper run
    # from a normal window always finished. The follower used to break -- and
    # then UNREGISTER the task, which stops a running build -- as soon as the
    # state read anything but "Running", and a failed lookup reads as $null.
    # Now only the marker ends the wait; a task that is REALLY gone (not Running
    # on several consecutive checks, no marker) is reported, and nothing is
    # unregistered while it runs. Every decision goes to install_trace.txt.
    $trace = Join-Path $Root "install_trace.txt"
    function Trace($msg) { Add-Content -Path $trace -Value "$((Get-Date).ToString('HH:mm:ss.ff')) $msg" -Encoding utf8 }
    Set-Content -Path $trace -Value "run_envs_task follower, started $(Get-Date -Format 's')" -Encoding utf8
    $shown = 0
    $spunUp = $false
    $notRunning = 0
    $lastState = ""
    while ($true) {
        Start-Sleep -Milliseconds 700
        $state = "<lookup failed>"
        try { $state = "$((Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State)" }
        catch { $state = "<lookup failed: $($_.Exception.Message)>" }
        if ($state -ne $lastState) { Trace "task state: $state"; $lastState = $state }
        if ($state -eq "Running") { $spunUp = $true; $notRunning = 0 }
        elseif ($state -in @("Ready", "Disabled")) { $notRunning += 1 }
        $marker = $null
        if (Test-Path $log) {
            $lines = @(Get-Content $log -ErrorAction SilentlyContinue)
            if ($lines.Count -gt $shown) {
                $lines[$shown..($lines.Count - 1)] | ForEach-Object { Write-Host $_ }
                $shown = $lines.Count
            }
            $marker = $lines | Where-Object { $_ -match '^BUILD FINISHED rc=(-?\d+)' } | Select-Object -Last 1
        }
        if ($marker) {
            $code = [int]($marker -replace '^BUILD FINISHED rc=', '')
            Trace "worker finished: $marker"
            break
        }
        # Gone without its marker: it really stopped (killed, crashed). Several
        # checks in a row, so one odd reading cannot end a working build.
        if ($notRunning -ge 5 -and ($spunUp -or $shown -gt 0)) {
            $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
            Trace "task stopped WITHOUT the finish marker; LastTaskResult=$($info.LastTaskResult)"
            Say "The environment build stopped before it finished (task result $($info.LastTaskResult))." "Yellow"
            $code = 1
            break
        }
        if (-not $spunUp -and $shown -eq 0 -and $notRunning -ge 10) {
            Trace "the scheduled task never started"
            throw "the scheduled task did not start"
        }
    }
}
catch {
    Say ""
    Say "Could not build through the Task Scheduler: $($_.Exception.Message)" "Yellow"
    $code = Invoke-Directly
}
finally {
    # Never delete a task that is still running: deleting it stops the build.
    try {
        $st = (Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State
        if ("$st" -ne "Running") {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        }
    } catch { }
}

Say ""
if ($code -eq 0) { Say "Environments built." "Green" }
else { Say "The environment build reported exit code $code -- see $log" "Yellow" }

if (-not $NoPause) { Write-Host "Press Enter to close: " -NoNewline; [void][Console]::ReadLine() }
exit $code
