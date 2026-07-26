param(
    [string]$Path = (Join-Path $HOME ".viking-mcp\credentials.json")
)

$ErrorActionPreference = "Stop"
$email = Read-Host "Viking email"
$secureKey = Read-Host "Viking API key (ввод скрыт)" -AsSecureString
$role = Read-Host "Viking role [trader]"
if ([string]::IsNullOrWhiteSpace($role)) { $role = "trader" }

$parent = Split-Path -Parent $Path
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$payload = [ordered]@{
    version = 1
    email = $email.Trim()
    role = $role.Trim().ToLowerInvariant()
    api_key_dpapi = ConvertFrom-SecureString $secureKey
}
$payload | ConvertTo-Json | Set-Content -Path $Path -Encoding UTF8

$acl = Get-Acl $Path
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name,
    "FullControl",
    "Allow"
)
$acl.SetAccessRule($rule)
Set-Acl -Path $Path -AclObject $acl

Write-Host "Зашифрованный файл создан: $Path"
Write-Host "API key может расшифровать только текущая учётная запись Windows."
