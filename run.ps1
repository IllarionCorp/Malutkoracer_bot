$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Сначала создай окружение: py -m venv .venv'
}
Push-Location $PSScriptRoot
try {
    & $pythonExecutable -m booking_bot @args
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
