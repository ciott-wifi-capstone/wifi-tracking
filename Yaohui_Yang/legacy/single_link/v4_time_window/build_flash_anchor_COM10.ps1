param([string]$Port = "COM10")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "anchor")
if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) { throw "请先在 ESP-IDF PowerShell Terminal 运行（需能找到 idf.py）。" }
idf.py set-target esp32s3
if ($LASTEXITCODE -ne 0) { throw "set-target failed" }
idf.py build
if ($LASTEXITCODE -ne 0) { throw "build failed" }
idf.py -p $Port flash monitor
if ($LASTEXITCODE -ne 0) { throw "flash/monitor failed" }
