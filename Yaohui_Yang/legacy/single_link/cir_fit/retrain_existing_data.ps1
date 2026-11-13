$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python .\tools\cir_distance.py train --train .\example_data\V5_LOS_1m_A_R1.zip .\example_data\V5_LOS_2m_A_R1.zip .\example_data\V5_LOS_3m_A_R1.zip --holdout .\example_data\V5_TIMESTAMP_01.zip --out .\results
if ($LASTEXITCODE -ne 0) { throw 'CIR model fitting failed.' }
