# Starts the AI Caller server on http://localhost:8000
$py = "$env:USERPROFILE\anaconda3\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
Set-Location $PSScriptRoot\backend
& $py -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
