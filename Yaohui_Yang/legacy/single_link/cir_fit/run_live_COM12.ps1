$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python .\tools\cir_distance.py live --port COM12 --models .\models
if ($LASTEXITCODE -ne 0) { throw 'CIR live prediction failed. Check the Python dependencies and serial port.' }
