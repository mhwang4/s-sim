# Claude Desktop 설정(claude_desktop_config.json)에 krihs-s-sim MCP 서버 등록.
# 사용: register_claude.ps1 <run_server.bat 경로> [설정파일 경로(시험용)]
param(
    [Parameter(Mandatory = $true)][string]$BatPath,
    [string]$ConfigPath = (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json')
)
$ErrorActionPreference = 'Stop'
$Name = 'krihs-s-sim'

if (Test-Path $ConfigPath) {
    Copy-Item $ConfigPath "$ConfigPath.bak" -Force          # 기존 설정 백업
    $cfg = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
} else {
    New-Item -ItemType Directory -Force (Split-Path $ConfigPath) | Out-Null
    $cfg = [pscustomobject]@{}
}
if (-not $cfg.PSObject.Properties['mcpServers']) {
    $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
$entry = [pscustomobject]@{ command = $BatPath }
if ($cfg.mcpServers.PSObject.Properties[$Name]) {
    $cfg.mcpServers.$Name = $entry
} else {
    $cfg.mcpServers | Add-Member -NotePropertyName $Name -NotePropertyValue $entry
}
# BOM 없는 UTF-8 필수 — Claude Desktop이 BOM 있는 JSON을 못 읽음
[IO.File]::WriteAllText($ConfigPath, ($cfg | ConvertTo-Json -Depth 20),
    (New-Object System.Text.UTF8Encoding $false))
Write-Host "등록 완료: $ConfigPath (이전 설정은 .bak)"
