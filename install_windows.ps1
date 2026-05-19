$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pyCandidates = @("-3.12", "-3.11", "-3.10")
        foreach ($candidate in $pyCandidates) {
            & py $candidate --version *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ Command = "py"; Arguments = @($candidate) }
            }
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @{ Command = "python"; Arguments = @() }
    }

    return $null
}

function Ensure-Python {
    $python = Get-PythonCommand
    if ($python) {
        return $python
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "Python 3.10+ is not installed and winget is unavailable. Install Python manually, then rerun this script."
    }

    Write-Step "Python 3.12 not found. Installing via winget"
    & winget install --exact --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements

    $python = Get-PythonCommand
    if (-not $python) {
        throw "Python installation did not complete successfully. Rerun the script after Python is installed."
    }

    return $python
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]$Python,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $Python.Command @($Python.Arguments + $Arguments)
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Python.Command) $($Python.Arguments + $Arguments -join ' ')"
    }
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Step "Project root: $projectRoot"

$python = Ensure-Python

Write-Step "Checking Python version"
Invoke-Python -Python $python -Arguments @("--version")

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Step "Creating virtual environment"
    Invoke-Python -Python $python -Arguments @("-m", "venv", "venv")
}
else {
    Write-Step "Virtual environment already exists"
}

$venvPython = Join-Path $projectRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment is missing python.exe: $venvPython"
}

Write-Step "Upgrading pip, setuptools and wheel"
& $venvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    throw "Failed to upgrade pip tooling."
}

Write-Step "Installing project dependencies"
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements."
}

Write-Step "Creating runtime directories"
$dirs = @("config", "db", "logs", "arc")
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

if (-not (Test-Path ".\config\.env") -and (Test-Path ".\config\.env.example")) {
    Write-Step "Creating config\.env from config\.env.example"
    Copy-Item ".\config\.env.example" ".\config\.env"
}
elseif (Test-Path ".\config\.env") {
    Write-Step "config\.env already exists"
}
else {
    Write-Step "config\.env.example not found, skipping env creation"
}

Write-Step "Installation complete"
Write-Host "Next steps:" -ForegroundColor Green
Write-Host "1. Fill in config\.env"
Write-Host "2. Copy Telegram session file into config\ if needed"
Write-Host "3. Copy db\ files from the old PC if you want to keep history/settings"
Write-Host "4. Run start.bat"
