param(
    [string]$Port = "COM7"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -c "import pyqtgraph, PySide6, OpenGL" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Starting hardware-accelerated Qt/OpenGL dashboard on $Port..."
    python .\fast_dashboard.py --port $Port
}
else {
    Write-Warning "Qt/OpenGL dependencies are unavailable; using the Matplotlib fallback."
    Write-Host "Install the smooth dashboard with: python -m pip install -r requirements.txt"
    python .\live_dashboard.py --port $Port
}
