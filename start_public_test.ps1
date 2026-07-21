$ErrorActionPreference = "Stop"

$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDirectory ".venv-joinmate\Scripts\python.exe"
$installedCloudflaredPath = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$cloudflaredCommand = Get-Command cloudflared -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "JoinMate virtual environment was not found: $pythonPath"
}

if ($null -ne $cloudflaredCommand) {
    $cloudflaredPath = $cloudflaredCommand.Source
}
elseif (Test-Path -LiteralPath $installedCloudflaredPath) {
    $cloudflaredPath = $installedCloudflaredPath
}
else {
    throw "cloudflared was not found. Restart VS Code and try again."
}

$randomBytes = New-Object byte[] 32
$randomGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$randomGenerator.GetBytes($randomBytes)
$randomGenerator.Dispose()
$env:JOINMATE_SESSION_SECRET = [Convert]::ToBase64String($randomBytes)
$env:JOINMATE_HTTPS_ONLY = "1"

$logToken = [Guid]::NewGuid().ToString("N")
$cloudflaredStdOutLog = Join-Path $env:TEMP "joinmate-cloudflared-$logToken.stdout.log"
$cloudflaredStdErrLog = Join-Path $env:TEMP "joinmate-cloudflared-$logToken.stderr.log"
$tunnelProcess = $null

$serverProcess = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $projectDirectory `
    -WindowStyle Hidden `
    -PassThru

try {
    Start-Sleep -Seconds 3
    $serverProcess.Refresh()
    if ($serverProcess.HasExited) {
        throw "JoinMate could not start. Stop any existing server on port 8000 and try again."
    }
    $tunnelProcess = Start-Process `
        -FilePath $cloudflaredPath `
        -ArgumentList "tunnel", "--url", "http://127.0.0.1:8000" `
        -RedirectStandardOutput $cloudflaredStdOutLog `
        -RedirectStandardError $cloudflaredStdErrLog `
        -WindowStyle Hidden `
        -PassThru

    $publicUrl = $null
    for ($attempt = 0; $attempt -lt 60 -and $null -eq $publicUrl; $attempt++) {
        Start-Sleep -Milliseconds 500
        $combinedLog = ""
        if (Test-Path -LiteralPath $cloudflaredStdOutLog) {
            $combinedLog += Get-Content -Raw -LiteralPath $cloudflaredStdOutLog -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $cloudflaredStdErrLog) {
            $combinedLog += Get-Content -Raw -LiteralPath $cloudflaredStdErrLog -ErrorAction SilentlyContinue
        }
        $urlMatch = [regex]::Match($combinedLog, "https://[a-z0-9-]+\.trycloudflare\.com")
        if ($urlMatch.Success) {
            $publicUrl = $urlMatch.Value
        }
        $tunnelProcess.Refresh()
        if ($tunnelProcess.HasExited -and $null -eq $publicUrl) {
            throw "Cloudflare Tunnel stopped before producing a public URL."
        }
    }

    if ($null -eq $publicUrl) {
        throw "Timed out while waiting for the Cloudflare public URL."
    }

    try { Set-Clipboard -Value $publicUrl } catch { }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "PUBLIC JOINMATE URL" -ForegroundColor Green
    Write-Host $publicUrl -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "The URL has been copied to your clipboard." -ForegroundColor Green
    Write-Host "Keep this terminal open. Press Ctrl+C to stop sharing." -ForegroundColor Yellow
    Write-Host ""

    while (-not $tunnelProcess.HasExited) {
        Start-Sleep -Seconds 1
        $tunnelProcess.Refresh()
    }
}
finally {
    if ($null -ne $tunnelProcess -and -not $tunnelProcess.HasExited) {
        Stop-Process -Id $tunnelProcess.Id
    }
    if (-not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id
    }
    if (Test-Path -LiteralPath $cloudflaredStdOutLog) {
        Remove-Item -LiteralPath $cloudflaredStdOutLog -Force
    }
    if (Test-Path -LiteralPath $cloudflaredStdErrLog) {
        Remove-Item -LiteralPath $cloudflaredStdErrLog -Force
    }
    Remove-Item Env:JOINMATE_SESSION_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:JOINMATE_HTTPS_ONLY -ErrorAction SilentlyContinue
}
