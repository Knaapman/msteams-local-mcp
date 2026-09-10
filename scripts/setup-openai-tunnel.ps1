param(
    [Parameter(Mandatory = $true)]
    [string]$TunnelId,

    [string]$ProfileName = "teams-local",
    [string]$TunnelClient = "tunnel-client",
    [string]$DumpExecutable = "msteams-local-dump",
    [string]$HealthListenAddr = "127.0.0.1:0",
    [switch]$SkipLocalSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Require-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "Required command '$Name' was not found on PATH."
    }
    return $cmd.Source
}

if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw "CONTROL_PLANE_API_KEY is not set. Reuse the same runtime tunnel key already used by the Outlook tunnel. Do not put the key in this repository or script."
}

$TunnelClientPath = Require-Command -Name $TunnelClient
$RepoRoot = Split-Path -Parent $PSScriptRoot
$McpExecutablePath = Join-Path $RepoRoot ".venv\Scripts\msteams-local-mcp.exe"

if (-not (Test-Path $McpExecutablePath)) {
    throw "Teams MCP executable not found at '$McpExecutablePath'. Create the virtual environment and install the project first."
}

Write-Host "Using existing CONTROL_PLANE_API_KEY from the environment."
Write-Host "Tunnel client: $TunnelClientPath"
Write-Host "Teams MCP:     $McpExecutablePath"
Write-Host "Profile:       $ProfileName"
Write-Host "Tunnel ID:     $TunnelId"
Write-Host "Health listen: $HealthListenAddr"

if (-not $SkipLocalSmokeTest) {
    $DumpExecutablePath = Require-Command -Name $DumpExecutable
    Write-Host ""
    Write-Host "Running local Teams cache smoke test..."
    & $DumpExecutablePath accounts --limit 10
    if ($LASTEXITCODE -ne 0) {
        throw "Local Teams cache smoke test failed. Run scripts/windows-smoke-test.ps1 for diagnostics before starting the tunnel."
    }
}

# tunnel-client's command parser treats backslashes as escapes except inside
# single-quoted command tokens. Include literal single quotes in the value so a
# Windows path reaches exec.Command unchanged. The quotes are parser syntax and
# are not part of argv[0].
$mcpCommand = "'" + $McpExecutablePath + "'"

Write-Host ""
Write-Host "Creating or refreshing tunnel-client profile '$ProfileName'..."
& $TunnelClientPath init `
    --sample sample_mcp_stdio_local `
    --profile $ProfileName `
    --tunnel-id $TunnelId `
    --mcp-command $mcpCommand `
    --health-listen-addr $HealthListenAddr `
    --force
if ($LASTEXITCODE -ne 0) {
    throw "tunnel-client init failed."
}

Write-Host ""
Write-Host "Validating tunnel configuration..."
& $TunnelClientPath doctor --profile $ProfileName --explain
if ($LASTEXITCODE -ne 0) {
    throw "tunnel-client doctor failed. Check the tunnel ID, runtime-key permissions, local MCP command, and health listener."
}

Write-Host ""
Write-Host "Teams tunnel profile is ready. Start it with:"
Write-Host "  $TunnelClientPath run --profile $ProfileName"
Write-Host ""
Write-Host "Outlook and Teams may use the same CONTROL_PLANE_API_KEY. Keep a separate tunnel ID/profile for each local stdio MCP server."
