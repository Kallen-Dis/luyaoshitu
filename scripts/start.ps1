#requires -Version 5.1
<#
Windows 本地启动。首次安装依赖，之后复用；只显示配置状态，不打印密钥值。
  start.bat                     自动启动，成功后可选择停止服务或退出窗口
  start.bat -Stop               只停止本脚本记录的进程
  start.bat -Check               只检查，不安装、不创建 .env、不启动服务
  start.bat -SetupOnly           只安装环境与依赖
  start.bat -DevDependencies     另装 pytest / ruff，供 scripts/verify.py 使用
  start.bat -Reinstall           强制重新安装项目依赖（先停止服务）
  start.bat -NoRuntimeInstall    缺 Python / Node 时只提示，不运行 WinGet
  start.bat -NoBrowser           不自动打开浏览器
保存为 UTF-8 BOM，以兼容 Windows PowerShell 5.1 的中文与中文路径。
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$SetupOnly,
    [switch]$Stop,
    [switch]$DevDependencies,
    [switch]$Reinstall,
    [switch]$NoRuntimeInstall,
    [switch]$NoBrowser,
    [switch]$Interactive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# requirements 中含 UTF-8 中文注释；Windows 默认 GBK 时 pip 否则会解码失败。
# 仅影响本启动器及其子进程，不修改机器环境变量或系统区域设置。
$env:PYTHONUTF8 = '1'
# 统一当前进程的 PATH 大小写，避免 PowerShell 5.1 启动带日志重定向的服务时
# 将 Path / PATH 当成重复键；只处理继承的值，不修改用户或系统环境变量。
$processEnv = [Environment]::GetEnvironmentVariables('Process')
$pathNames = @($processEnv.Keys | Where-Object { $_ -ieq 'Path' })
if ($pathNames.Count -gt 0) {
    $pathValues = @($pathNames | ForEach-Object { $processEnv[$_] } | Select-Object -Unique)
    foreach ($pathName in $pathNames) { [Environment]::SetEnvironmentVariable($pathName, $null, 'Process') }
    [Environment]::SetEnvironmentVariable('PATH', ($pathValues -join ';'), 'Process')
}
$processEnv = $null
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$backendDir = Join-Path $root 'backend'
$frontendDir = Join-Path $root 'frontend'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$viteScript = Join-Path $frontendDir 'node_modules\vite\bin\vite.js'
$runDir = Join-Path $root '.cache\launcher'
$statePath = Join-Path $runDir 'services.json'
$backendUrl = 'http://127.0.0.1:8000'
$frontendUrl = 'http://localhost:5173'

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Read-AfterStartAction {
    Write-Host "`n前后端已就绪，请选择：" -ForegroundColor Cyan
    Write-Host '  1. 停止前后端并退出窗口'
    Write-Host '  0. 退出窗口（服务继续运行）'
    while ($true) {
        $choice = Read-Host '输入编号后按回车'
        switch ($choice.Trim()) {
            '1' { return 'stop' }
            '0' { return 'exit' }
            default { Write-Host '请输入 0 或 1。' -ForegroundColor Yellow }
        }
    }
}

function Get-Sha256File([string]$Path) {
    # 直接用 .NET，避免从 PowerShell 7 经 .bat 调用 5.1 时继承模块路径导致 Get-FileHash 不可用。
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

function Invoke-Native([string]$Executable, [string[]]$Arguments, [string]$Directory) {
    Push-Location -LiteralPath $Directory
    try {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "命令执行失败（退出码 $LASTEXITCODE）：$([IO.Path]::GetFileName($Executable))。请检查上方安装提示后重试。"
        }
    } finally {
        Pop-Location
    }
}

function Get-PythonInfo([string]$Executable, [string[]]$Prefix = @()) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    # 跳过 Microsoft Store 的占位别名，避免检查时弹出商店。
    if ($Executable -like '*\Microsoft\WindowsApps\python*.exe') { return $null }
    try {
        $probe = "import json,sys; print(json.dumps({'version': list(sys.version_info[:3]), 'path': sys.executable, 'bits': 64 if sys.maxsize > 2**32 else 32}))"
        $raw = & $Executable @Prefix -B -c $probe 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $info = ($raw -join "`n") | ConvertFrom-Json
        if ($info.version[0] -ne 3 -or $info.version[1] -lt 11 -or $info.version[1] -gt 13 -or $info.bits -ne 64) {
            return $null
        }
        return $info
    } catch { return $null }
}

function Find-Python {
    $info = Get-PythonInfo $venvPython
    if ($null -ne $info) { return $info }
    foreach ($launcher in @(Get-Command py.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        foreach ($selector in @('-3.13', '-3.12', '-3.11')) {
            $info = Get-PythonInfo $launcher.Source @($selector)
            if ($null -ne $info) { return $info }
        }
    }
    foreach ($command in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        $info = Get-PythonInfo $command.Source
        if ($null -ne $info) { return $info }
    }
    foreach ($baseDir in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if (-not $baseDir) { continue }
        foreach ($minor in @(13, 12, 11)) {
            $candidate = if ($baseDir -eq $env:LOCALAPPDATA) {
                Join-Path $baseDir "Programs\Python\Python3$minor\python.exe"
            } else { Join-Path $baseDir "Python3$minor\python.exe" }
            $info = Get-PythonInfo $candidate
            if ($null -ne $info) { return $info }
        }
    }
    return $null
}

function Find-Node {
    $commands = @(Get-Command node.exe -CommandType Application -All -ErrorAction SilentlyContinue)
    foreach ($command in $commands) {
        try {
            $raw = & $command.Source --version 2>$null
            if ($LASTEXITCODE -ne 0 -or "$raw" -notmatch '^v(\d+)\.(\d+)\.(\d+)$') { continue }
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if (-not (($major -eq 22 -and $minor -ge 18) -or $major -ge 24)) { continue }
            $npm = Join-Path (Split-Path -Parent $command.Source) 'npm.cmd'
            if (-not (Test-Path -LiteralPath $npm)) { continue }
            return [pscustomobject]@{ path = $command.Source; version = "$raw"; npm = $npm }
        } catch { continue }
    }
    return $null
}

function Install-Runtime([string]$PackageId, [string]$Description, [string]$ManualUrl) {
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($NoRuntimeInstall -or $null -eq $winget) {
        throw "缺少 $Description。请从 $ManualUrl 安装，安装完重新双击 start.bat（无需卸载已有其他版本）。"
    }
    Write-Step "通过 WinGet 安装 $Description；安装器可能需要你确认许可或 UAC"
    # 不自动接受软件许可，不改变机器的 PowerShell 执行策略。
    Invoke-Native $winget.Source @('install', '--id', $PackageId, '--exact', '--source', 'winget', '--interactive') $root
    # 只刷新本进程 PATH，不修改用户或系统的环境变量。
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:PATH = "$machinePath;$userPath;$env:PATH"
}

function Test-PythonDependencies([string]$Python) {
    $probe = @'
import importlib.metadata as m, importlib.util, pathlib, re, sys
try:
    for name in sys.argv[1:]:
        for line in pathlib.Path(name).read_text(encoding='utf-8-sig').splitlines():
            match = re.match(r'^([\w-]+)(?:\[[^]]+\])?==([^\s#]+)', line.strip())
            if match and m.version(match[1]) != match[2]:
                sys.exit(1)
    for module in ('fastapi', 'uvicorn', 'httpx', 'pydantic', 'httptools', 'yaml', 'dotenv', 'websockets', 'watchfiles'):
        if importlib.util.find_spec(module) is None:
            sys.exit(1)
except (m.PackageNotFoundError, OSError, ImportError):
    sys.exit(1)
'@
    $files = @((Join-Path $backendDir 'requirements.txt'))
    if ($DevDependencies) { $files += Join-Path $backendDir 'requirements-dev.txt' }
    & $Python -B -c $probe @files 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Get-ConfigInfo([string]$Python) {
    $probe = @'
import json
from app.config import get_settings
try:
    s = get_settings()
    print(json.dumps({'valid': True, 'server': bool(s.server_ak) and not s.server_ak.startswith('your_'), 'browser': bool(s.browser_ak) and not s.browser_ak.startswith('your_'), 'agent': s.agent_plan_configured, 'admin': s.admin_enabled}))
except (ValueError, TypeError, OverflowError):
    print(json.dumps({'valid': False}))
'@
    Push-Location -LiteralPath $backendDir
    try {
        $raw = & $Python -B -c $probe 2>$null
        if ($LASTEXITCODE -ne 0) { throw '无法检查 .env；请确认文件可读且使用 UTF-8。' }
        return (($raw -join "`n") | ConvertFrom-Json)
    } finally { Pop-Location }
}

function Show-ConfigInfo($Info) {
    if (-not $Info.valid) { throw '.env 中有无法解析的数值配置，请按 .env.example 检查（不显示配置值）。' }
    Write-Host "服务端 AK：$(if ($Info.server) { '已配置' } else { '未配置，实时分析不可用' })"
    Write-Host "浏览器 AK：$(if ($Info.browser) { '已配置' } else { '未配置，地图底图不可用' })"
    Write-Host "Agent Plan / 管理员审核：$(if ($Info.agent) { '已配置' } else { '未配置' }) / $(if ($Info.admin) { '已配置' } else { '未配置（至少 12 位口令）' })"
    if (-not ($Info.server -and $Info.browser)) {
        Write-Warning '可先查看样例报告与离线模拟；填写根目录 .env 后，用 start.bat -Stop 停止再启动。'
    }
}

function Test-FreePort([int]$Port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $true
    } catch { return $false }
    finally { $listener.Stop() }
}

function Read-ServiceState {
    if (-not (Test-Path -LiteralPath $statePath)) { return $null }
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.root -ne $root) { throw '启动记录属于另一个项目目录；未停止任何进程。请检查 .cache/launcher/services.json。' }
    return $state
}

function Get-ManagedProcess($Entry) {
    if ($null -eq $Entry) { return $null }
    try {
        $process = Get-Process -Id $Entry.pid -ErrorAction Stop
        if ($process.StartTime.ToUniversalTime().ToString('o') -ne $Entry.started_utc -or $process.Path -ne $Entry.executable) {
            return $null # PID 已被其他进程复用；不把旧记录当成停止授权。
        }
        return $process
    } catch { return $null }
}

function New-ServiceEntry($Process, [string]$Executable) {
    # Start-Process 刚返回的对象可能尚未加载 MainModule，不能依赖其 Path 属性。
    # 记录实际启动时使用的绝对路径，再用 Get-Process 的实时信息核验。
    $Process.Refresh()
    return [pscustomobject]@{
        pid = $Process.Id
        started_utc = $Process.StartTime.ToUniversalTime().ToString('o')
        executable = [IO.Path]::GetFullPath($Executable)
    }
}

function Save-ServiceState($Backend, $Frontend) {
    $state = [ordered]@{ root = $root; backend = $Backend; frontend = $Frontend }
    $temporaryPath = Join-Path $runDir 'services.tmp.json'
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $statePath -Force
}

function Stop-ServiceEntry($Entry) {
    $process = Get-ManagedProcess $Entry
    if ($null -eq $process) {
        if ($null -ne $Entry -and (Get-Process -Id $Entry.pid -ErrorAction SilentlyContinue)) {
            Write-Warning "进程 $($Entry.pid) 与启动记录不符，未关闭。"
        }
        return
    }
    # 仅终止经过 PID + 创建时间 + 可执行文件校验的本脚本进程及其子进程。
    & (Join-Path $env:SystemRoot 'System32\taskkill.exe') /PID $process.Id /T /F >$null 2>&1
    if ($LASTEXITCODE -ne 0 -and (Get-ManagedProcess $Entry)) {
        throw "无法停止本脚本启动的进程 $($process.Id)，请检查进程权限。"
    }
}

function Get-HealthyBackend([string]$Url) {
    try {
        $health = Invoke-RestMethod -Uri "$Url/api/health" -TimeoutSec 2
        if ($health.status -notin @('ok', 'degraded') -or $null -eq $health.server_ak_configured) { return $null }
        return $health
    } catch { return $null }
}

function Wait-Service([string]$Url, $Entry, [switch]$Frontend) {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        if (-not (Get-ManagedProcess $Entry)) { throw "服务进程已退出，请查看 $runDir 下的日志。" }
        $health = Get-HealthyBackend $Url
        if ($null -ne $health) {
            if (-not $Frontend) { return }
            try {
                $html = Invoke-WebRequest -Uri "$Url/" -UseBasicParsing -TimeoutSec 2
                if ($html.StatusCode -eq 200 -and $html.Content -match '<div\s+id="root">') { return }
            } catch { }
        }
        Start-Sleep -Milliseconds 400
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "服务未在 60 秒内就绪（$Url），请查看 $runDir 下的日志。"
}

function Invoke-Launcher {
    if ($Stop) {
        Write-Step '停止本脚本启动的前后端'
        $state = Read-ServiceState
        if ($null -eq $state) { Write-Host '没有本脚本的启动记录；未关闭其他终端中的服务。'; return }
        Stop-ServiceEntry $state.frontend
        Stop-ServiceEntry $state.backend
        Remove-Item -LiteralPath $statePath
        Write-Host '停止操作完成；虚拟环境、配置、缓存与用户数据均保留。'
        return
    }

    foreach ($relative in @('backend\app\main.py', 'backend\requirements.txt', 'frontend\index.html', 'frontend\package.json', 'frontend\package-lock.json', 'frontend\vite.config.ts', '.env.example')) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { throw "缺少项目文件 $relative，请下载完整源码。" }
    }
    Write-Step '检查运行环境'
    $python = Find-Python
    $node = Find-Node
    if ($Check) {
        Write-Host "Python：$(if ($python) { ($python.version -join '.') + '，' + $python.path } else { '缺少 64 位 Python 3.11–3.13，推荐 3.13' })"
        Write-Host "Node.js：$(if ($node) { $node.version + '，' + $node.path } else { '缺少 Node.js 22.18+（22.x）或 24+' })"
        Write-Host "8000 / 5173 端口：$(if (Test-FreePort 8000) { '空闲' } else { '占用' }) / $(if (Test-FreePort 5173) { '空闲' } else { '占用' })"
        Write-Host ".env：$(if (Test-Path -LiteralPath (Join-Path $root '.env')) { '已存在，不显示内容' } else { '尚未创建，首次启动会复制 .env.example' })"
        if ($python) { Show-ConfigInfo (Get-ConfigInfo $python.path) }
        if (-not $python -or -not $node) { throw '运行环境尚未齐备；运行 start.bat 安装，或按 README 手动安装。' }
        Write-Host "后端依赖：$(if (Test-PythonDependencies $python.path) { '符合锁定版本' } else { '尚需安装，首次启动会处理' })"
        Write-Host "前端依赖：$(if (Test-Path -LiteralPath $viteScript) { '已安装' } else { '尚需安装，首次启动会处理' })"
        return
    }

    $state = Read-ServiceState
    $backendEntry = if ($null -ne $state) { $state.backend } else { $null }
    $frontendEntry = if ($null -ne $state) { $state.frontend } else { $null }
    $backendRunning = Get-ManagedProcess $backendEntry
    $frontendRunning = Get-ManagedProcess $frontendEntry
    if (($backendRunning -or $frontendRunning) -and ($SetupOnly -or $Reinstall -or $DevDependencies)) {
        throw '要安装或更新依赖，请先运行 start.bat -Stop，避免改变正在使用的虚拟环境或 node_modules。'
    }
    foreach ($service in @(@{ port = 8000; running = $backendRunning }, @{ port = 5173; running = $frontendRunning })) {
        if (-not $service.running -and -not (Test-FreePort $service.port)) {
            throw "端口 $($service.port) 已被其他进程占用。请先关闭原开发终端或检查 netstat -ano；脚本不会自动杀掉未知进程。"
        }
    }

    if ($null -eq $python) {
        Install-Runtime 'Python.Python.3.13' '64 位 Python 3.13' 'https://www.python.org/downloads/windows/'
        $python = Find-Python
        if ($null -eq $python) { throw 'Python 安装后仍未找到兼容解释器；请关闭本窗口后重新双击 start.bat。' }
    }
    if ($null -eq $node) {
        Install-Runtime 'OpenJS.NodeJS.LTS' 'Node.js LTS（含 npm）' 'https://nodejs.org/en/download'
        $node = Find-Node
        if ($null -eq $node) { throw 'Node.js 安装后仍未找到 node / npm；请关闭本窗口后重新双击 start.bat。' }
    }
    Write-Host "Python $($python.version -join '.') / Node.js $($node.version)"
    $envPath = Join-Path $root '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envPath
        Write-Host '已从模板创建 .env；请填自己的 AK，脚本不会覆盖已有配置。'
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if (Test-Path -LiteralPath (Join-Path $root '.venv')) { throw '已有不完整的 .venv；请先改名备份后再启动，脚本不会删除它。' }
        Write-Step '创建项目虚拟环境 .venv'
        Invoke-Native $python.path @('-m', 'venv', (Join-Path $root '.venv')) $root
    }
    if ($null -eq (Get-PythonInfo $venvPython)) { throw '现有 .venv 不是兼容的 64 位 Python 3.11–3.13；请先改名备份再启动。' }
    Write-Step '检查配置（只显示是否配置，不显示密钥）'
    Show-ConfigInfo (Get-ConfigInfo $venvPython)
    $null = New-Item -ItemType Directory -Path $runDir -Force
    if ($Reinstall -or -not (Test-PythonDependencies $venvPython)) {
        if ($backendRunning) { throw '后端依赖变化；请先运行 start.bat -Stop 再启动。' }
        Write-Step '安装后端依赖（只写入 .venv）'
        $requirements = if ($DevDependencies) { 'backend\requirements-dev.txt' } else { 'backend\requirements.txt' }
        Invoke-Native $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--cache-dir', (Join-Path $runDir 'pip-cache'), '-r', (Join-Path $root $requirements)) $root
        if (-not (Test-PythonDependencies $venvPython)) { throw '后端依赖校验未通过，请检查 pip 输出。' }
    } else { Write-Host '后端依赖已就绪，跳过安装。' }

    $lockHash = (Get-Sha256File (Join-Path $frontendDir 'package-lock.json')) + ':' + (Get-Sha256File (Join-Path $frontendDir 'package.json'))
    $markerPath = Join-Path $runDir 'npm-lock.sha256'
    $oldHash = if (Test-Path -LiteralPath $markerPath) { (Get-Content -LiteralPath $markerPath -Raw).Trim() } else { '' }
    if ($Reinstall -or $oldHash -ne $lockHash -or -not (Test-Path -LiteralPath $viteScript)) {
        if ($frontendRunning) { throw '前端依赖变化；请先运行 start.bat -Stop 再启动。' }
        Write-Step '按 package-lock.json 安装前端依赖'
        Invoke-Native $node.npm @('ci', '--include=dev', '--no-audit', '--no-fund', '--cache', (Join-Path $runDir 'npm-cache')) $frontendDir
        $lockHash | Set-Content -LiteralPath $markerPath -Encoding ASCII
    } else { Write-Host '前端依赖配置未变化，跳过安装。' }

    if ($SetupOnly) { Write-Host '环境安装完成；下次双击 start.bat 即可启动。'; return }

    Write-Step '启动并检查前后端'
    $newEntries = @()
    try {
        if (-not $backendRunning) {
            $process = Start-Process -FilePath $venvPython -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $backendDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'backend.stdout.log') -RedirectStandardError (Join-Path $runDir 'backend.stderr.log') -PassThru
            $backendEntry = New-ServiceEntry $process $venvPython
            $newEntries += $backendEntry
        } else { Write-Host '后端已由本脚本启动，复用当前进程。' }
        Save-ServiceState $backendEntry $frontendEntry
        Wait-Service $backendUrl $backendEntry
        if (-not $frontendRunning) {
            # Start-Process 会拼接 ArgumentList，显式保护含空格的源码路径。
            $process = Start-Process -FilePath $node.path -ArgumentList @(('"{0}"' -f $viteScript), '--host', '127.0.0.1', '--port', '5173', '--strictPort') -WorkingDirectory $frontendDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runDir 'frontend.stdout.log') -RedirectStandardError (Join-Path $runDir 'frontend.stderr.log') -PassThru
            $frontendEntry = New-ServiceEntry $process $node.path
            $newEntries += $frontendEntry
        } else { Write-Host '前端已由本脚本启动，复用当前进程。' }
        Save-ServiceState $backendEntry $frontendEntry
        # 直接检查监听的 IPv4 地址，避免 Windows PowerShell 优先尝试 localhost 的 IPv6。
        Wait-Service 'http://127.0.0.1:5173' $frontendEntry -Frontend
    } catch {
        foreach ($entry in $newEntries) { Stop-ServiceEntry $entry }
        Save-ServiceState $(if ($backendRunning) { $backendEntry } else { $null }) $(if ($frontendRunning) { $frontendEntry } else { $null })
        throw
    }
    Write-Host "`n启动成功：$frontendUrl" -ForegroundColor Green
    Write-Host "后端文档：$backendUrl/docs"
    Write-Host "日志目录：$runDir"
    Write-Host '关闭本窗口不会停止服务；也可运行 start.bat -Stop 停止前后端。修改 .env 或后端代码后，需要停止再启动。'
    if (-not $NoBrowser) {
        try { Start-Process -FilePath $frontendUrl }
        catch { Write-Warning "服务已启动，但未能打开默认浏览器；请手动访问 $frontendUrl。" }
    }
}

function Invoke-LockedLauncher {
    $mutex = $null
    $locked = $false
    try {
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $digest = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '') }
        finally { $hasher.Dispose() }
        $mutex = [Threading.Mutex]::new($false, "Local\LuyaoshituLauncher_$digest")
        $locked = $mutex.WaitOne(0)
        if (-not $locked) { throw '另一份启动或停止脚本正在运行，请等它结束后再试。' }
        Invoke-Launcher
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

$exitCode = 0
try {
    if ($Interactive -and ($Check -or $SetupOnly -or $Stop)) {
        throw '-Interactive 仅用于启动，不能与 -Check、-SetupOnly 或 -Stop 同时使用。'
    }
    Invoke-LockedLauncher
    # 启动成功后才提供选择；等待输入时不持有锁，其他窗口仍能管理服务。
    if ($Interactive -and (Read-AfterStartAction) -eq 'stop') {
        $Stop = $true
        Invoke-LockedLauncher
    }
} catch {
    Write-Host "`n启动 / 检查失败：$($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
exit $exitCode
