# Makes "Hallows' Eve Detector" shortcuts (in this folder and on the Desktop) that start the
# app with pythonw.exe, so no console window opens. Run by Install.bat.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @($dir, [Environment]::GetFolderPath("Desktop"))) {
    $link = $shell.CreateShortcut((Join-Path $folder "Hallows' Eve Detector.lnk"))
    $link.TargetPath = $pythonw
    $link.Arguments = '"' + (Join-Path $dir "hallows_eve_detector.py") + '"'
    $link.WorkingDirectory = $dir
    $link.IconLocation = (Join-Path $dir "assets\app.ico") + ",0"
    $link.Description = "Hallows' Eve Detector"
    $link.Save()
}
Write-Host "Shortcuts made: in this folder and on your Desktop."
