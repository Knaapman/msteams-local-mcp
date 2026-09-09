param(
    [string]$DumpExecutable = "msteams-local-dump"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-TeamsLevelDbCandidates {
    $local = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($local)) {
        $local = Join-Path $HOME "AppData\Local"
    }

    $pattern = Join-Path $local "Packages\MSTeams_*\LocalCache\Microsoft\MSTeams\EBWebView\*\IndexedDB\https_teams.microsoft.com_0.indexeddb.leveldb"
    return @(Get-ChildItem -Path $pattern -Directory -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
}

$cmd = Get-Command $DumpExecutable -ErrorAction SilentlyContinue
if (-not $cmd) {
    throw "'$DumpExecutable' was not found on PATH. Install this fork first, for example with: pipx install -e ."
}

Write-Host "Microsoft Teams local MCP Windows smoke test"
Write-Host ""
Write-Host "LOCALAPPDATA: $env:LOCALAPPDATA"
Write-Host "Executable:   $($cmd.Source)"

$candidates = Resolve-TeamsLevelDbCandidates
if ($candidates.Count -eq 0) {
    throw "No Teams v2 IndexedDB cache was found under the expected MSTeams MSIX LocalCache path. Start the new Teams desktop client, sign in, open a few chats, then retry."
}

Write-Host "Found $($candidates.Count) Teams cache candidate(s):"
$candidates | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "Testing account discovery using the MCP reader..."
& $cmd.Source accounts --limit 20
if ($LASTEXITCODE -ne 0) {
    throw "Teams cache parsing failed. If Teams is running, the reader should copy the LevelDB before parsing; inspect the Python exception for the exact failing file or schema."
}

Write-Host ""
Write-Host "Smoke test passed. The local Teams cache is discoverable and readable."
