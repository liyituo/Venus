# 检测浏览器工具依赖（Node/npx/Chrome）
$ErrorActionPreference = "SilentlyContinue"

function Find-Command($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$node = Find-Command node
$npx = Find-Command npx
$chromePaths = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ }

Write-Host "== Venus 浏览器依赖检测 =="
Write-Host ("node : " + ($(if ($node) { $node } else { "未找到" })))
Write-Host ("npx  : " + ($(if ($npx) { $npx } else { "未找到" })))
if ($chromePaths) {
    Write-Host ("chrome: " + $chromePaths[0])
} else {
    Write-Host "chrome: 未在默认路径找到（Playwright 可能仍可用）"
}

$ready = [bool]($node -or $npx)
if ($ready) {
    Write-Host "`n状态: 可启用浏览器 MCP（POST http://127.0.0.1:8001/api/v1/browser/enable）"
    exit 0
}
Write-Host "`n状态: 请先安装 Node.js (https://nodejs.org)"
exit 1
