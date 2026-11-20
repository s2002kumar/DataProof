#Requires -Version 5.1
<#
.SYNOPSIS
  Runs one stage of the second DataProof AI evaluation (docs/AI_EVALUATION_V2.md), with the
  Anthropic API key typed in hidden.

.DESCRIPTION
  This script does three things and nothing else: it shows what a stage would spend and asks
  for that spend to be typed back; it asks for the API key with the input hidden; and it
  starts the stage with the key in that one process's environment. Every decision (budgets,
  what may start, gates, records) is made by benchmarks/src/dataproof_benchmark/launcher.py,
  which is code under test.

  The key:
    * is read with Read-Host -AsSecureString (masked, never echoed);
    * is put only into the environment block of the launcher's process, which takes it out of
      its own environment at once and gives it to the stage's process alone; never into this
      session's environment, a file, the repository or a log;
    * is dropped when the stage's process has started: the unmanaged copy is zeroed, and the
      managed strings .NET makes on the way are released, though they cannot be zeroed;
    * is searched for in everything the stage printed and in every file it wrote. A match
      stops everything.

  Spending:
    * one stage for each run of this script. Nothing starts the next stage by itself;
    * the launcher refuses a stage whose pilot has not passed at the commit being run, a
      held-out stage that was started before, and any stage while another is left open;
    * before the key is asked for, the stage's largest possible spend is shown, and
      "SPEND <amount>" must be typed exactly.

.PARAMETER Stage
  Status (the default: what has been spent, and whether every record is as it was sealed),
  DevPilot, Dev, Heldout, OldArmPilot, OldArm, AnalystPilot, AnalystDev, AnalystHeldout,
  Resume (take up a stage whose process died: only what was never started is sent),
  Abandon (close such a stage as it stands), or
  DryRunAll (every stage in order with scripted models: no key, no spend).

.PARAMETER DryRun
  Run the one stage with a scripted model: no key, no spend, records under <State>-dryrun.

.PARAMETER State
  Where stage records are kept. Outside the repository: results are committed from there only
  after a stage has been read.

.PARAMETER Python
  The Python of the project's environment. Default: $env:DATAPROOF_PYTHON, or .venv in the
  repository.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File benchmarks\launcher\Run-AiEval2.ps1 -Stage DevPilot
#>
[CmdletBinding()]
param(
    [ValidateSet('Status', 'DevPilot', 'Dev', 'Heldout', 'OldArmPilot', 'OldArm', 'AnalystPilot', 'AnalystDev',
        'AnalystHeldout', 'Resume', 'Abandon', 'DryRunAll')]
    [string]$Stage = 'Status',
    [switch]$DryRun,
    [string]$State = (Join-Path $HOME '.dataproof\ai-eval\v2'),
    [string]$Python = $env:DATAPROOF_PYTHON,
    [string]$Database = 'postgresql://postgres@127.0.0.1:5436/postgres'
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Bench = Join-Path $Repo 'benchmarks'
$KeyVars = @('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_LOG', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_PROFILE')
$Names = @{
    DevPilot = 'dev-pilot'; Dev = 'dev'; Heldout = 'heldout'; OldArmPilot = 'old-arm-pilot'; OldArm = 'old-arm'
    AnalystPilot = 'analyst-pilot'; AnalystDev = 'analyst-dev'; AnalystHeldout = 'analyst-heldout'
}
$Order = @('DevPilot', 'Dev', 'Heldout', 'OldArmPilot', 'OldArm', 'AnalystPilot', 'AnalystDev', 'AnalystHeldout')

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Stop-Eval([string]$Why) { Say "STOPPED: $Why" 'Red'; throw "STOPPED: $Why" }
function Quote([string]$Arg) {
    # A backslash before the closing quote would escape it: double any run of them there.
    $escaped = $Arg.Replace('"', '\"')
    $trailing = $escaped.Length - $escaped.TrimEnd([char]92).Length
    if ($trailing -gt 0) { $escaped += ([string][char]92) * $trailing }
    '"' + $escaped + '"'
}

if (-not $Python) { $Python = Join-Path $Repo '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) {
    Stop-Eval "no Python at $Python. Pass -Python or set DATAPROOF_PYTHON to the project environment's python.exe."
}
$dry = $DryRun -or ($Stage -eq 'DryRunAll')
# Relative to where the command was typed, not to the .NET process's own working directory.
$State = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($State.TrimEnd([char]92, [char]47))
if ($dry) { $State = "$State-dryrun" }
if (($State -eq $Repo) -or $State.StartsWith($Repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Eval "keep the state outside the repository, not in $State"
}

# Start the launcher module. The key, if any, goes into the child's environment block only.
function Invoke-Launcher([string[]]$LauncherArgs, [Security.SecureString]$Secure, [switch]$Capture) {
    $all = @('-m', 'dataproof_benchmark.launcher', '--state', $State, '--database', $Database)
    if ($dry) { $all += '--dry-run' }
    $all += $LauncherArgs
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = ($all | ForEach-Object { Quote $_ }) -join ' '
    $psi.WorkingDirectory = $Bench
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = [bool]$Capture
    foreach ($v in $KeyVars) { if ($psi.EnvironmentVariables.ContainsKey($v)) { $psi.EnvironmentVariables.Remove($v) } }
    $psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $bstr = [IntPtr]::Zero
    $text = ''
    try {
        if ($Secure) {
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
            $psi.EnvironmentVariables['ANTHROPIC_API_KEY'] = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        }
        $p = [System.Diagnostics.Process]::Start($psi)
        # The child has its own copy of the environment block now; drop ours.
        if ($psi.EnvironmentVariables.ContainsKey('ANTHROPIC_API_KEY')) { $psi.EnvironmentVariables.Remove('ANTHROPIC_API_KEY') }
        if ($Capture) { $text = $p.StandardOutput.ReadToEnd() }
        while (-not $p.WaitForExit(1000)) { }
        return @{ Code = $p.ExitCode; Text = $text }
    } finally {
        if ($psi.EnvironmentVariables.ContainsKey('ANTHROPIC_API_KEY')) { $psi.EnvironmentVariables.Remove('ANTHROPIC_API_KEY') }
        if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        [GC]::Collect()
    }
}

function Read-Key {
    foreach ($scope in 'User', 'Machine') {
        if ([Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', $scope)) {
            Say "[launcher] note: ANTHROPIC_API_KEY is saved at $scope scope; it is ignored here (the typed key is used)." 'Yellow'
        }
    }
    $secure = Read-Host -AsSecureString -Prompt 'Anthropic API key (input hidden; Enter to submit)'
    if ($null -eq $secure -or $secure.Length -lt 20) { Stop-Eval 'no key entered' }
    $check = [IntPtr]::Zero
    try {
        $check = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $prefixOk = ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($check)).StartsWith('sk-ant-')
    } finally {
        if ($check -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($check) }
    }
    if (-not $prefixOk) { $secure.Dispose(); Stop-Eval 'that does not look like an Anthropic API key (sk-ant-...)' }
    $secure.MakeReadOnly()
    return $secure
}

# Show what a stage would spend, have that typed back, then start it.
function Invoke-Paid([string[]]$PlanArgs, [string[]]$RunArgs) {
    $plan = Invoke-Launcher $PlanArgs $null -Capture
    Say $plan.Text
    if ($plan.Code -ne 0) { Stop-Eval 'the stage may not start (see above). Nothing was sent.' }
    $approve = $null
    $repository = $null
    foreach ($line in ($plan.Text -split "`n")) {
        if ($line -match '^approve:\s*([0-9.]+)\s*$') { $approve = $Matches[1] }
        if ($line -match '^repository:\s*(.+?)\s*$') { $repository = $Matches[1] }
    }
    if ($repository -and ($repository -ne $Repo)) {
        Stop-Eval "the launcher runs the code of $repository, not of this script's repository $Repo. Pass -Python for this checkout's environment."
    }
    if (-not $approve) { Stop-Eval 'the launcher named no amount to approve' }
    if ($dry) { return (Invoke-Launcher $RunArgs $null).Code }
    Say "This stage can spend up to $approve USD on the Anthropic API. It is not started again by itself." 'Cyan'
    $typed = Read-Host -Prompt "To approve, type exactly: SPEND $approve"
    if ($typed -cne "SPEND $approve") { Stop-Eval 'not approved. Nothing was sent.' }
    $secure = Read-Key
    try {
        return (Invoke-Launcher ($RunArgs + @('--approve', $approve)) $secure).Code
    } finally {
        $secure.Dispose()
        $secure = $null
        [GC]::Collect()
    }
}

$code = 0
try {
    Say 'DataProof AI evaluation v2 (docs/AI_EVALUATION_V2.md)' 'Cyan'
    if ($dry) { Say "DRY RUN: scripted models, no key, no spend. Records: $State" 'Yellow' }
    switch ($Stage) {
        'Status' { $code = (Invoke-Launcher @('status') $null).Code }
        'Abandon' {
            $typed = Read-Host -Prompt 'This closes the open stage for good; what it never started is never sent. Type exactly: ABANDON'
            if ($typed -cne 'ABANDON') { Stop-Eval 'not confirmed. Nothing was changed.' }
            $code = (Invoke-Launcher @('abandon') $null).Code
        }
        'Resume' { $code = Invoke-Paid @('plan', 'resume') @('resume') }
        'DryRunAll' {
            $code = (Invoke-Launcher @('reset') $null).Code
            foreach ($name in $Order) {
                if ($code -ne 0) { break }
                $code = Invoke-Paid @('plan', $Names[$name]) @('run', $Names[$name])
            }
        }
        default { $code = Invoke-Paid @('plan', $Names[$Stage]) @('run', $Names[$Stage]) }
    }
    if ($Stage -ne 'Status') {
        Say ''
        $after = (Invoke-Launcher @('status') $null).Code
        if ($after -ne 0 -and $code -eq 0) { $code = $after }
    }
    if ($code -eq 0) { Say "DONE: $Stage. Records: $State" 'Green' }
    else { Say "$Stage ended with exit code $code. Read the lines above before anything else is started. Records: $State" 'Red' }
} finally {
    Remove-Item Env:\ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
    [GC]::Collect()
    if ([Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Process')) {
        Write-Host '[launcher] WARNING: ANTHROPIC_API_KEY is still set in this session' -ForegroundColor Red
    } else {
        Write-Host '[launcher] ANTHROPIC_API_KEY is not set in this session; the typed key has been dropped from memory.' -ForegroundColor DarkGray
    }
}
exit $code
