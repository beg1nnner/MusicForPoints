# Installs Deno (JavaScript runtime required by yt-dlp for YouTube).
# Official build from GitHub releases -> %USERPROFILE%\.deno\bin, added to the user PATH.
$ErrorActionPreference = 'Stop'
$log = Join-Path $PSScriptRoot 'install_deno.log'
Start-Transcript -Path $log -Force | Out-Null
try {
    $bin = Join-Path $env:USERPROFILE '.deno\bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $zip = Join-Path $env:TEMP 'deno-x86_64-pc-windows-msvc.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip'
    Write-Host "Downloading $url"
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Write-Host ("Downloaded {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))
    Expand-Archive -Path $zip -DestinationPath $bin -Force
    Remove-Item $zip -Force
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @(); if ($userPath) { $parts = $userPath -split ';' | Where-Object { $_ } }
    if ($parts -notcontains $bin) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'User')
        Write-Host "Added to user PATH: $bin"
    } else {
        Write-Host "Already in user PATH: $bin"
    }
    $exe = Join-Path $bin 'deno.exe'
    Write-Host "Installed: $exe"
    & $exe --version
    Write-Host 'RESULT: OK'
} catch {
    Write-Host "RESULT: ERROR $($_.Exception.Message)"
    exit 1
} finally {
    Stop-Transcript | Out-Null
}
