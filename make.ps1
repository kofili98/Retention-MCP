param (
    [Parameter(Position=0)]
    [ValidateSet("data", "profile", "analyze", "test", "report", "reproduce", "clean")]
    [string]$Target = "reproduce"
)

$ErrorActionPreference = "Stop"
$env:PYTHONHASHSEED = "0"
$env:TZ = "UTC"

function Run-Data {
    Write-Host "==> [Phase 1] Running analysis.ingest..." -ForegroundColor Cyan
    python -m analysis.ingest
    if ($LASTEXITCODE -ne 0) { throw "analysis.ingest failed" }
}

function Run-Profile {
    Write-Host "==> [Phase 2] Running analysis.health..." -ForegroundColor Cyan
    python -m analysis.health
    if ($LASTEXITCODE -ne 0) { throw "analysis.health failed" }
}

function Run-Analyze {
    Write-Host "==> [Phase 4] Running analysis.metrics & analysis.manifest..." -ForegroundColor Cyan
    python -m analysis.metrics
    if ($LASTEXITCODE -ne 0) { throw "analysis.metrics failed" }
    python -m analysis.manifest
    if ($LASTEXITCODE -ne 0) { throw "analysis.manifest failed" }
}

function Run-Test {
    Write-Host "==> [Phase 4/9] Running pytest suite..." -ForegroundColor Cyan
    python -m pytest tests/ -v
    if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
}

function Run-Report {
    Write-Host "==> [Phase 8] Running agent.weekly_report..." -ForegroundColor Cyan
    python -m agent.weekly_report
    if ($LASTEXITCODE -ne 0) { throw "agent.weekly_report failed" }
}

function Run-Clean {
    Write-Host "==> Cleaning data/raw and data/processed..." -ForegroundColor Yellow
    Remove-Item -Path "data/raw/*.zip", "data/raw/*.xlsx", "data/processed/*" -Force -ErrorAction SilentlyContinue
}

switch ($Target) {
    "data"      { Run-Data }
    "profile"   { Run-Profile }
    "analyze"   { Run-Analyze }
    "test"      { Run-Test }
    "report"    { Run-Report }
    "clean"     { Run-Clean }
    "reproduce" {
        Run-Data
        Run-Profile
        Run-Analyze
        Run-Test
    }
}