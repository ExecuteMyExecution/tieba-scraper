$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if (-not $Launcher) {
        $Launcher = Get-Command python -ErrorAction SilentlyContinue
    }
    if (-not $Launcher) {
        throw "未找到 Python。请先安装 Python 3.10 或更高版本。"
    }
    & $Launcher.Source -m venv (Join-Path $ProjectRoot ".venv")
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
}

Set-Location $ProjectRoot
& $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port 8000

