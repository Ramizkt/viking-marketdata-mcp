param(
    [string]$McpUrl = "https://viking-marketdata-mcp-production.up.railway.app/mcp",
    [switch]$UseCurrentEnvironment
)

$ErrorActionPreference = "Stop"

function Set-VikingCodexConfig {
    param([string]$Url)

    $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
    New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
    $configPath = Join-Path $codexHome "config.toml"
    $start = "# BEGIN VIKING MARKETDATA MCP"
    $end = "# END VIKING MARKETDATA MCP"
    $block = @"
$start
[mcp_servers.viking_marketdata]
url = "$Url"
env_http_headers = { "X-Viking-Email" = "VIKING_EMAIL", "X-Viking-API-Key" = "VIKING_API_KEY", "X-Viking-Role" = "VIKING_ROLE" }
tool_timeout_sec = 300
$end
"@
    $current = if (Test-Path $configPath) { Get-Content $configPath -Raw } else { "" }
    $pattern = "(?s)" + [regex]::Escape($start) + ".*?" + [regex]::Escape($end)
    if ($current -match $pattern) {
        $updated = [regex]::Replace($current, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
    } else {
        $updated = ($current.TrimEnd() + "`r`n`r`n" + $block + "`r`n").TrimStart()
    }
    Set-Content -Path $configPath -Value $updated -Encoding UTF8
}

if (-not $UseCurrentEnvironment) {
    $email = Read-Host "Viking email"
    $secureKey = Read-Host "Viking API key (ввод скрыт)" -AsSecureString
    $role = Read-Host "Viking role [trader]"
    if ([string]::IsNullOrWhiteSpace($role)) { $role = "trader" }

    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $env:VIKING_EMAIL = $email.Trim()
        $env:VIKING_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        $env:VIKING_ROLE = $role.Trim().ToLowerInvariant()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

Set-VikingCodexConfig -Url $McpUrl
Write-Host "Credentials загружены только в память текущего процесса."
Write-Host "Запускаю Codex App. После закрытия приложения сервер удалит RAM-копию через 15 минут без запросов."
& codex app
