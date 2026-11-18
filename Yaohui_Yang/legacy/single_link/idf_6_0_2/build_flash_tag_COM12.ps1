param([string]$Port = "COM12")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "tag")
if (-not (Get-Command idf.py -ErrorAction SilentlyContinue)) { throw "请先在 ESP-IDF PowerShell Terminal 运行（需能找到 idf.py）。" }
idf.py reconfigure
if ($LASTEXITCODE -ne 0) { throw "reconfigure failed" }
idf.py build
if ($LASTEXITCODE -ne 0) { throw "build failed" }
idf.py -p $Port flash monitor
if ($LASTEXITCODE -ne 0) { throw "flash/monitor failed" }
