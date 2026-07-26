param(
    [string]$Path = (Join-Path $HOME ".viking-mcp\credentials.json"),
    [string]$McpUrl = "https://viking-marketdata-mcp-production.up.railway.app/mcp"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Path)) {
    throw "Файл не найден: $Path. Сначала запустите save-viking-credentials.ps1."
}

$data = Get-Content $Path -Raw | ConvertFrom-Json
$secureKey = ConvertTo-SecureString $data.api_key_dpapi
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:VIKING_EMAIL = [string]$data.email
    $env:VIKING_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    $env:VIKING_ROLE = [string]$data.role
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}

& (Join-Path $PSScriptRoot "viking-session.ps1") -McpUrl $McpUrl -UseCurrentEnvironment
