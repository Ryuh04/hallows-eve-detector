# Builds dist\HallowsEveDetector.exe: one file, no console window, everything bundled
# (Python, Qt, the OCR models). GitHub builds it this way on every push; it also works
# locally:  powershell -ExecutionPolicy Bypass -File build_exe.ps1
param([string]$Version = "dev")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The version the exe reports, and compares with GitHub's newest release when updating.
$sha = (git rev-parse HEAD 2>$null)
$info = @{ version = $Version; sha = "$sha"; date = (Get-Date -Format "yyyy-MM-dd") } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $PSScriptRoot "build_info.json"), $info)  # UTF-8, no BOM

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name HallowsEveDetector --icon assets\app.ico `
    --add-data "assets;assets" --add-data "build_info.json;." `
    --collect-all rapidocr `
    --exclude-module torch --exclude-module torchvision --exclude-module tensorboard `
    --exclude-module tensorrt --exclude-module paddle --exclude-module openvino `
    --exclude-module numba --exclude-module llvmlite --exclude-module matplotlib `
    --exclude-module tkinter `
    hallows_eve_detector.py
if ($LASTEXITCODE) { exit $LASTEXITCODE }

# Check the exe starts and everything it needs is inside.
$p = Start-Process -FilePath "dist\HallowsEveDetector.exe" -ArgumentList "--self-test" -PassThru -Wait
if ($p.ExitCode -ne 0) { Write-Error "self-test failed (exit $($p.ExitCode))"; exit 1 }
Write-Host ("Built dist\HallowsEveDetector.exe ({0:N0} MB), version $Version" -f ((Get-Item dist\HallowsEveDetector.exe).Length / 1MB))
