$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python .\tools\cir_experiment.py wizard --port COM12 --reuse-existing --extra-test-distances 1.5 2.5 5
if ($LASTEXITCODE -ne 0) { throw 'Experiment failed. Read the message above and rerun this command to resume.' }
