param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $appDir

Write-Host "使用 Python: $Python"
& $Python -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name "AzurLanePaintingExport" `
    --icon "assets\app.ico" `
    --add-data "assets;assets" `
    --collect-all UnityPy `
    --collect-all texture2ddecoder `
    --collect-all etcpak `
    --collect-all astc_encoder `
    --collect-all archspec `
    --exclude-module tkinter `
    main.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
Write-Host "构建完成: $appDir\dist\AzurLanePaintingExport"
