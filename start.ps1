$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host "已创建 .env，请先填写数据库和 RAGFlow 配置。"
}

Set-Location "$Root\backend"
python -m pip install -r "$Root\requirements.txt"
python run.py
