# 诚实日报助手 · 服务就绪探测
# 供启动器调用：等待 http://127.0.0.1:8088/api/health 可用，超时则退出码 1。
# 单独成文件是为了避开 cmd.exe 对 PowerShell 一行命令的转义地狱。

param(
    [int]$Port = 8088,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = 'SilentlyContinue'
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

while ((Get-Date) -lt $deadline) {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2 | Out-Null
        exit 0
    } catch {
        Start-Sleep -Milliseconds 500
    }
}

exit 1
