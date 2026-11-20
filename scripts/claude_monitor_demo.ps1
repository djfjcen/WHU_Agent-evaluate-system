param(
    [string]$Prompt = 'Reply with exactly: DEMO_OK',
    [string]$WorkingDirectory = (Get-Location).Path,
    [ValidateRange(1, 3600)]
    [int]$TimeoutSeconds = 120,
    [string]$OutputFile = '',
    [string]$Model = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "Working directory does not exist: $WorkingDirectory"
}
$claude = Get-Command claude.exe -CommandType Application | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($OutputFile)) {
    $runDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'runs/legacy'
    if (-not (Test-Path -LiteralPath $runDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
    }
    $OutputFile = Join-Path $runDirectory ("claude-events-{0}.jsonl" -f [guid]::NewGuid())
}
$OutputFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputFile)

# Windows PowerShell 5.1 uses a single native command-line string.
# Escape backslashes before quotes and at the end of each quoted argument.
$arguments = @(
    '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
    '--safe-mode', '--tools', '', '--permission-mode', 'manual', '--no-session-persistence'
)
if ($Model) { $arguments += @('--model', $Model) }
$arguments += @('--', $Prompt)

$utf8 = [System.Text.UTF8Encoding]::new($false)
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $claude.Source
$psi.WorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).ProviderPath
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = $utf8
$psi.StandardErrorEncoding = $utf8
$psi.CreateNoWindow = $true
$psi.Arguments = (($arguments | ForEach-Object { '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }) -join ' ')

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $psi
$startedAt = [DateTimeOffset]::UtcNow
$clock = [System.Diagnostics.Stopwatch]::StartNew()
$eventTypes = @{}
$eventCount = 0
$invalidLineCount = 0
$sessionEvent = $null
$resultEvent = $null
$stderr = $null
$exitCode = $null
$timedOut = $false
$started = $false
$file = [System.IO.File]::Open($OutputFile, [System.IO.FileMode]::CreateNew)
$writer = [System.IO.StreamWriter]::new($file, $utf8)
$writer.AutoFlush = $true
try {
    [Console]::Error.WriteLine("Starting Claude Code in $($psi.WorkingDirectory)")
    [Console]::Error.WriteLine("Event file: $OutputFile")
    $started = $process.Start()
    if (-not $started) { throw 'Could not start Claude Code' }
    $process.StandardInput.Close()
    $stderrTask = $process.StandardError.ReadToEndAsync()

    while ($true) {
        $remainingMs = $TimeoutSeconds * 1000 - [int]$clock.ElapsedMilliseconds
        if ($remainingMs -le 0) { $timedOut = $true; break }
        $lineTask = $process.StandardOutput.ReadLineAsync()
        if (-not $lineTask.Wait($remainingMs)) { $timedOut = $true; break }
        $line = $lineTask.GetAwaiter().GetResult()
        if ($null -eq $line) { break }
        if ([string]::IsNullOrWhiteSpace($line)) { continue }

        try { $event = ConvertFrom-Json -InputObject $line -ErrorAction Stop }
        catch { $invalidLineCount++; continue }
        if ($event -isnot [pscustomobject] -or $event.type -isnot [string] -or -not $event.type) {
            $invalidLineCount++
            continue
        }
        $writer.WriteLine($line)
        $eventTypes[$event.type] = 1 + [int]$eventTypes[$event.type]
        $eventCount++
        if ($event.type -eq 'system' -and $event.subtype -eq 'init') { $sessionEvent = $event }
        if ($event.type -eq 'result') { $resultEvent = $event }
    }

    if (-not $timedOut) {
        $remainingMs = [Math]::Max(0, $TimeoutSeconds * 1000 - [int]$clock.ElapsedMilliseconds)
        $timedOut = -not $process.WaitForExit($remainingMs)
    }
    if (-not $timedOut) {
        $remainingMs = [Math]::Max(0, $TimeoutSeconds * 1000 - [int]$clock.ElapsedMilliseconds)
        $timedOut = -not $stderrTask.Wait($remainingMs)
    }
    if ($timedOut -and -not $process.HasExited) {
        try { $process.Kill() }
        catch { if (-not $process.HasExited) { throw } }
        [void]$process.WaitForExit(1000)
    }
    if ($process.HasExited) { $exitCode = $process.ExitCode }
    if ($stderrTask.Status -eq 'RanToCompletion') { $stderr = $stderrTask.GetAwaiter().GetResult().Trim() }
}
finally {
    $writer.Dispose()
    try {
        if ($started -and -not $process.HasExited) {
            try { $process.Kill() }
            catch { if (-not $process.HasExited) { throw } }
        }
    }
    finally { $process.Dispose() }
}

$success = -not $timedOut -and $exitCode -eq 0 -and $null -ne $resultEvent -and
    $resultEvent.subtype -eq 'success' -and $resultEvent.is_error -eq $false
[ordered]@{
    started_at = $startedAt.ToString('o')
    duration_ms = $clock.ElapsedMilliseconds
    exit_code = $exitCode
    success = $success
    timed_out = $timedOut
    event_count = $eventCount
    invalid_line_count = $invalidLineCount
    event_types = $eventTypes
    session_id = if ($sessionEvent.session_id) { $sessionEvent.session_id } else { $resultEvent.session_id }
    model = $sessionEvent.model
    result = $resultEvent.result
    result_subtype = $resultEvent.subtype
    usage = $resultEvent.usage
    total_cost_usd = $resultEvent.total_cost_usd
    stderr = $stderr
    event_file = $OutputFile
} | ConvertTo-Json -Depth 20

if ($timedOut) { exit 124 }
if ($exitCode -ne 0) { exit $exitCode }
if (-not $success) { exit 1 }
exit 0
