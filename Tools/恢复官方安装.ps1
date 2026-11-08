<#
  小鲸鱼 · 恢复官方安装（一键）
  ---------------------------------------------------------------
  用途：程序目录被改坏 / 混装 / 启动就提示「被篡改」时，
        把程序文件恢复成官方原版，**不碰你自己的数据**。

  会动（都要备份）：小鲸鱼.exe、resources\ 下的程序文件、manifest.sha256/.sig
  绝不动：config.json、memory\（记忆）、personas（人设）、agents\、
          learn.db、agent.log、resources\av_module\config.json、
          resources\av_module\voices\（自定义音色）、guard.seal、guard_audit.log

  用法（最省事）：
    1) 把这个脚本和「小鲸鱼_V*_绿色版.zip」放在同一个文件夹里
    2) 双击 恢复官方安装.bat（或右键 ps1 → 用 PowerShell 运行）
  也可以手动指定：
    powershell -NoProfile -ExecutionPolicy Bypass -File 恢复官方安装.ps1 -Target "D:\小鲸鱼"
    powershell -NoProfile -ExecutionPolicy Bypass -File 恢复官方安装.ps1 -Target "D:\小鲸鱼" -Source "D:\下载\小鲸鱼_V1.9.6_绿色版.zip"
#>
param(
    [string]$Target = "",
    [string]$Source = "",
    [switch]$Launch,
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

# ---------- v1.9.8：临时目录清理 ----------
# QA 实测：以前这个脚本每跑一次都会在 %TEMP% 里留下 xjy-repair-<时间戳>
# （解包出来的几百兆程序文件）和 xjy-self-data-<时间戳>（自检用的数据目录），
# 而且不管成功失败都不删。跑几次就攒到 1.64 GB，用户磁盘直接爆。
# 现在的规矩：①本次运行产生的东西，成功失败都必须删干净；②顺手把以前
# 几次留下的残渣也扫掉（只扫两小时以前的，免得误删同时开着的另一个窗口）。
$script:TempToClean = New-Object System.Collections.ArrayList

function Register-TempCleanup($path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return }
    if (-not $script:TempToClean.Contains($path)) { [void]$script:TempToClean.Add($path) }
}

function Invoke-TempCleanup {
    foreach ($p in @($script:TempToClean)) {
        try {
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop }
        } catch {
            # 删不掉就留着 —— 下次启动扫描时还会再来一次
        }
    }
    $script:TempToClean.Clear()
}

function Clear-StaleTemp {
    $tmp = [System.IO.Path]::GetTempPath()
    $limit = (Get-Date).AddHours(-2)
    $freed = 0
    $n = 0
    foreach ($pat in @("xjy-repair-*", "xjy-self-data-*", "xjy-inst-check-*")) {
        foreach ($d in @(Get-ChildItem -LiteralPath $tmp -Filter $pat -Directory -ErrorAction SilentlyContinue)) {
            try {
                if ($d.LastWriteTime -gt $limit) { continue }
                $sz = 0
                try { $sz = (Get-ChildItem -LiteralPath $d.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum } catch { }
                Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction Stop
                if ($sz) { $freed += [double]$sz }
                $n++
            } catch { }
        }
    }
    foreach ($f in @(Get-ChildItem -LiteralPath $tmp -Filter "xjy-self-*.txt" -File -ErrorAction SilentlyContinue)) {
        try {
            if ($f.LastWriteTime -gt $limit) { continue }
            Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop
            $n++
        } catch { }
    }
    if ($n -gt 0) {
        Say ("  顺手清掉了 " + $n + " 个以前留下的临时文件，约 " + [math]::Round($freed / 1MB, 1) + " MB") "DarkGray"
    }
}

function Fail($code, $msg, $tips) {
    Write-Host ""
    Say ("✗ " + $msg) "Red"
    if ($tips) { foreach ($t in $tips) { Say ("  - " + $t) "Yellow" } }
    Write-Host ""
    try { Invoke-TempCleanup } catch { }
    if (-not $Quiet) {
        Say "（按任意键关闭）" "DarkGray"
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
    exit $code
}

Say ""
Say "  小鲸鱼 · 恢复官方安装" "Cyan"
Say ("  脚本位置：" + $here) "DarkGray"
Say ""
try { Clear-StaleTemp } catch { }

# ---------- 1) 找程序目录 ----------
function Test-AppDir($p) {
    if ([string]::IsNullOrWhiteSpace($p)) { return $false }
    try { return (Test-Path -LiteralPath (Join-Path $p "小鲸鱼.exe")) } catch { return $false }
}

if ([string]::IsNullOrWhiteSpace($Target)) {
    $probe = $here
    for ($i = 0; $i -lt 5; $i++) {
        if (Test-AppDir $probe) { $Target = $probe; break }
        $up = Split-Path -Parent $probe
        if ([string]::IsNullOrWhiteSpace($up) -or ($up -eq $probe)) { break }
        $probe = $up
    }
}
if ([string]::IsNullOrWhiteSpace($Target)) {
    # 桌面 / 开始菜单快捷方式
    try {
        $sh = New-Object -ComObject WScript.Shell
        $dirs = @([Environment]::GetFolderPath('Desktop'),
                  [Environment]::GetFolderPath('CommonDesktopDirectory'),
                  (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
        foreach ($d in $dirs) {
            if ([string]::IsNullOrWhiteSpace($d) -or -not (Test-Path -LiteralPath $d)) { continue }
            foreach ($l in @(Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue)) {
                if ($l.BaseName -notlike '*小鲸鱼*') { continue }
                try {
                    $tgt = $sh.CreateShortcut($l.FullName).TargetPath
                    if ($tgt) {
                        $td = Split-Path -Parent $tgt
                        if (Test-AppDir $td) { $Target = $td; break }
                    }
                } catch { }
            }
            if (-not [string]::IsNullOrWhiteSpace($Target)) { break }
        }
    } catch { }
}
if ([string]::IsNullOrWhiteSpace($Target)) {
    # v1.9.8-Beta 修：$ErrorActionPreference = "Stop" 下，Join-Path 碰到不存在的盘符
    # （比如没插卡的 E:）会直接抛 DriveNotFoundException 把脚本打死。
    # 所以先确认目录真的存在，再拼路径。
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:USERPROFILE, "C:\", "D:\", "E:\")
    foreach ($root in $roots) {
        if ([string]::IsNullOrWhiteSpace($root)) { continue }
        $rootOk = $false
        try { $rootOk = Test-Path -LiteralPath $root } catch { $rootOk = $false }
        if (-not $rootOk) { continue }
        $cands = New-Object System.Collections.ArrayList
        foreach ($sub in @("小鲸鱼", "Programs\小鲸鱼")) {
            try { [void]$cands.Add((Join-Path $root $sub)) } catch { }
        }
        foreach ($c in $cands) {
            if (Test-AppDir $c) { $Target = $c; break }
        }
        if (-not [string]::IsNullOrWhiteSpace($Target)) { break }
    }
}
while ([string]::IsNullOrWhiteSpace($Target)) {
    Say "  没自动找到程序目录（有小鲸鱼.exe 的那一层）。" "Yellow"
    Say "  把那个文件夹直接拖进这个窗口再回车就行；直接回车则放弃。" "Yellow"
    $in = Read-Host "  程序目录"
    if ([string]::IsNullOrWhiteSpace($in)) { break }
    $in = $in.Trim().Trim([char]34).Trim([char]39)
    if (Test-AppDir $in) { $Target = $in }
    elseif (Test-Path -LiteralPath (Join-Path $in "小鲸鱼.exe")) { $Target = $in }
    else { Say ("  这个目录里没有 小鲸鱼.exe：" + $in) "Red" }
}
if ([string]::IsNullOrWhiteSpace($Target)) { Fail 2 "没有可用的程序目录。" @("确认路径里有 小鲸鱼.exe") }
if (-not (Test-Path -LiteralPath $Target)) { Fail 2 ("目标目录不存在：" + $Target) $null }
$Target = (Resolve-Path -LiteralPath $Target).Path.TrimEnd('\')
Say ("  程序目录：" + $Target) "White"

# ---------- 2) 找官方来源（v1.9.7：只认签名有效的包） ----------
# 支持三种来源：绿色版 zip / 已经解开的绿色版目录 / 安装包解出来的目录
# 为什么必须先验签：以前这里按「文件名里的版本号」挑最大的包，攻击者往下载目录
# 放一个 小鲸鱼_V9.9.9_绿色版.zip（改过的主程序 + 官方那份公开的资源清单），
# 脚本就会自动选中它把木马写进程序目录，还打一个「✓ 官方原版」（复测报告 5.2）。
# 现在只有一个判据：用工具自带的公钥验过签名的包才算候选。
$verifyExe = Join-Path $here "verify_bundle.exe"
if (-not (Test-Path -LiteralPath $verifyExe)) {
    Fail 2 "恢复工具不完整：缺少 verify_bundle.exe（验签器）。" @("请整个解压「恢复工具.zip」，不要只拷 ps1 或 bat 出来")
}
# ---------- v1.9.7.1（P2）：验签器自己也得先验一遍 ----------
# 以前的链条是「脚本调 verify_bundle.exe」，而那个 exe 只有一句 Test-Path ——
# 谁把恢复工具目录里的 verify_bundle.exe 换成「永远返回 0」的假货，整条验签就白做了
# （复测报告 4.3）。这里写死它发布时的 sha256：对不上就不许往下走。
# 换新版的验签器时这个常量必须跟着改；打包脚本会核对，忘了会直接打不出包。
$verifyExeSha256 = "eb1b183b240942a8592d2899a41ecf5f6b70042976b5000425e1606f5550c434"
$verifyGot = ""
try {
    $verifyGot = (Get-FileHash -Algorithm SHA256 -LiteralPath $verifyExe).Hash
} catch {
    $verifyGot = ""
}
if ([string]::IsNullOrWhiteSpace($verifyGot)) {
    Fail 2 "读不出 verify_bundle.exe 的哈希，恢复工具可能损坏。" @("请重新下载「恢复工具.zip」并整个解压")
}
if ($verifyGot.ToLower() -ne $verifyExeSha256.ToLower()) {
    Fail 2 "verify_bundle.exe 不是随这个脚本发布的那个（哈希对不上）—— 有人换过验签器。" @("请从官方渠道重新下载「恢复工具.zip」", "不要用别人给的 verify_bundle.exe 覆盖它")
}
function Convert-VerifyCode([int]$code) {
    if ($code -eq 0) { return "签名校验通过" }
    if ($code -eq 2) { return "验签器用法不对（工具损坏？）" }
    if ($code -eq 4) { return "没通过签名校验（不是官方包，或内容被改过）" }
    if ($code -lt 0) { return "验签器没能启动" }
    return ("验签器异常退出（" + $code + "）")
}
function Test-Bundle([string]$zipPath) {
    if ([string]::IsNullOrWhiteSpace($zipPath)) { return @{ ok = $false; msg = "路径为空" } }
    $code = -1
    try {
        $null = (& $verifyExe $zipPath 2>&1)
        $code = $LASTEXITCODE
    } catch {
        $code = -1
    }
    if ($null -eq $code) { $code = -1 }
    return @{ ok = ($code -eq 0); msg = (Convert-VerifyCode $code); code = $code }
}
function Test-BundleDir([string]$dirPath) {
    if ([string]::IsNullOrWhiteSpace($dirPath)) { return @{ ok = $false; msg = "路径为空" } }
    $code = -1
    try {
        $null = (& $verifyExe "--dir" $dirPath 2>&1)
        $code = $LASTEXITCODE
    } catch {
        $code = -1
    }
    if ($null -eq $code) { $code = -1 }
    return @{ ok = ($code -eq 0); msg = (Convert-VerifyCode $code); code = $code }
}
$zip = ""
$srcDir = ""
if (-not [string]::IsNullOrWhiteSpace($Source)) {
    if (Test-Path -LiteralPath $Source -PathType Leaf) { $zip = (Resolve-Path -LiteralPath $Source).Path }
    elseif (Test-Path -LiteralPath $Source -PathType Container) { $srcDir = (Resolve-Path -LiteralPath $Source).Path }
    else { Fail 2 ("来源不存在：" + $Source) $null }
    if (-not [string]::IsNullOrWhiteSpace($zip)) {
        Say "  正在验签指定的包…" "DarkGray"
        $vr = Test-Bundle $zip
        if (-not $vr.ok) {
            Say ("  看一眼详细原因：" + $vr.msg) "Red"
            & $verifyExe $zip
            Fail 2 ("这个包不能用：" + [System.IO.Path]::GetFileName($zip)) @("从官方渠道重新下载「小鲸鱼_V*_绿色版.zip」")
        }
        Say ("  " + $vr.msg) "Green"
    }
}
if ([string]::IsNullOrWhiteSpace($zip) -and [string]::IsNullOrWhiteSpace($srcDir)) {
    $cands = @()
    foreach ($d in @($here, (Split-Path -Parent $here), $Target, (Split-Path -Parent $Target),
                     [Environment]::GetFolderPath('Desktop'), (Join-Path $env:USERPROFILE 'Downloads'))) {
        if ([string]::IsNullOrWhiteSpace($d) -or -not (Test-Path -LiteralPath $d)) { continue }
        $cands += @(Get-ChildItem -LiteralPath $d -Filter "小鲸鱼_V*_绿色版.zip" -File -ErrorAction SilentlyContinue)
    }
    $cands = @($cands | Sort-Object FullName -Unique)
    if ($cands.Count -gt 0) {
        Say ("  找到 {0} 个绿色版包，逐个验签…" -f $cands.Count) "DarkGray"
        $okList = @()
        foreach ($c in $cands) {
            $vr = Test-Bundle $c.FullName
            if ($vr.ok) {
                Say ("    OK   " + $c.Name) "Green"
                $okList += $c
            } else {
                Say ("    跳过 " + $c.Name + " —— " + $vr.msg) "Red"
            }
        }
        if ($okList.Count -eq 0) {
            Fail 2 "找到的绿色版包没有一个通过签名校验。" @("从官方渠道重新下载「小鲸鱼_V*_绿色版.zip」", "别用来路不明的包，哪怕文件名写的是最新版")
        }
        # 只在「签名有效」的里面挑版本号最大的
        $best = $okList | Sort-Object { try { [version](($_.BaseName -replace '^小鲸鱼_V', '' -replace '_绿色版$', '')) } catch { [version]'0.0.0' } } | Select-Object -Last 1
        $zip = $best.FullName
        Say ("  选用官方包：" + (Split-Path -Leaf $zip)) "White"
    }
}
if ([string]::IsNullOrWhiteSpace($zip) -and [string]::IsNullOrWhiteSpace($srcDir)) {
    Say "" 
    Say "  没找到「小鲸鱼_V*_绿色版.zip」。" "Yellow"
    Say "  请把它放到本脚本旁边（或用 -Source 指定路径），也可以把解压后的目录告诉我。" "Yellow"
    $in = Read-Host "  绿色版 zip / 已解开的目录（直接回车放弃）"
    if ([string]::IsNullOrWhiteSpace($in)) { Fail 2 "没有官方来源，无法恢复。" $null }
    $in = $in.Trim().Trim([char]34).Trim([char]39)
    if (Test-Path -LiteralPath $in -PathType Leaf) { $zip = (Resolve-Path -LiteralPath $in).Path }
    elseif (Test-Path -LiteralPath $in -PathType Container) { $srcDir = (Resolve-Path -LiteralPath $in).Path }
    else { Fail 2 ("这个路径不对：" + $in) $null }
}

# 解压到临时目录
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("xjy-repair-" + $stamp)
Register-TempCleanup $work
if (-not (Test-Path -LiteralPath $work)) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
if (-not [string]::IsNullOrWhiteSpace($zip)) {
    Say ("  解压官方包（{0:N1} MB）…" -f ((Get-Item -LiteralPath $zip).Length / 1MB)) "DarkGray"
    try { Expand-Archive -LiteralPath $zip -DestinationPath $work -Force }
    catch { Fail 2 ("解压失败：" + $_.Exception.Message) @("确认 zip 没坏、空间够") }
    $srcDir = $work
}
# zip 里可能套了一层目录
if (-not (Test-Path -LiteralPath (Join-Path $srcDir "小鲸鱼.exe"))) {
    $inner = @(Get-ChildItem -LiteralPath $srcDir -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "小鲸鱼.exe") })
    if ($inner.Count -gt 0) { $srcDir = $inner[0].FullName }
}
# v1.9.7：目录来源同样必须验签 —— 否则「先解压再拿目录当来源」就是同一条投毒路
Say "  正在验签来源目录…" "DarkGray"
$vrd = Test-BundleDir $srcDir
if (-not $vrd.ok) {
    Say ("  看一眼详细原因：" + $vrd.msg) "Red"
    & $verifyExe "--dir" $srcDir
    Fail 2 ("来源目录不能用：" + $srcDir) @("用官方的「小鲸鱼_V*_绿色版.zip」，别用来路不明的解包目录")
}
Say ("  " + $vrd.msg) "Green"
if (-not (Test-Path -LiteralPath (Join-Path $srcDir "小鲸鱼.exe"))) {
    Fail 2 "官方来源里没有 小鲸鱼.exe，看着不像绿色版包。" @("用官方发布的「小鲸鱼_V*_绿色版.zip」")
}
Say ("  官方来源：" + $srcDir) "DarkGray"

# ---------- 3) 关掉「这个目录里」正在跑的程序 ----------
# v1.9.7：以前按进程名一刀切，会顺手杀掉另一份安装（比如测试版 + 正式版并存，
# 或者另一块盘上的另一套目录），造成真实事故（复测报告 5.3）。
# 现在只碰 Path 落在目标目录下面的进程。
function Get-TargetProcs {
    $names = @("小鲸鱼", "backend_server", "av_server")
    $found = @()
    foreach ($pr in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            if ($names -notcontains $pr.ProcessName) { continue }
        } catch { continue }
        $pp = $null
        try { $pp = $pr.Path } catch { $pp = $null }
        if ([string]::IsNullOrWhiteSpace($pp)) { continue }
        if ($pp.StartsWith($Target + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            $found += $pr
        }
    }
    return $found
}
$busy = @(Get-TargetProcs)
if ($busy.Count -gt 0) {
    Say ""
    Say ("  这个目录里还有 {0} 个程序在跑，先关掉它们…" -f $busy.Count) "Yellow"
    foreach ($pr in $busy) {
        try { $pr | CloseMainWindow | Out-Null } catch { }
    }
    Start-Sleep -Seconds 3
    foreach ($pr in @(Get-TargetProcs)) {
        try { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
    Start-Sleep -Seconds 2
    $still = @(Get-TargetProcs)
    if ($still.Count -gt 0) {
        $nm = ($still | ForEach-Object { $_.ProcessName }) -join "、"
        Fail 3 ("还是关不掉：" + $nm) @("在任务管理器里结束这几个进程再重跑（别的目录里的实例不受影响）")
    }
    Say "  已关闭。" "DarkGray"
} else {
    Say "  这个目录里没有正在运行的程序。" "DarkGray"
}

# ---------- 4) 先备份要覆盖的程序文件 ----------
# 用户数据一律不在备份/覆盖范围内
$PROTECT = @(
    "config.json",
    "memory", "memory.db", "agents", "personas", "personas.json", "persona_faces",
    "learn.db", "agent.log", "data", "diary", "audit", "reflect",
    "guard.seal", "guard_audit.log",
    "resources\av_module\config.json",
    "resources\av_module\voices",
    "resources\av_module\runtime",
    "resources\av_module\@AutomationLog.txt"
)

function Is-Protected($rel) {
    $r = $rel -replace "/", "\"
    foreach ($p in $PROTECT) {
        if ($r -ieq $p) { return $true }
        if ($r.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

# 官方包里要写进去的文件（只取程序文件，跳过受保护项）
$files = New-Object System.Collections.ArrayList
$rootExe = Join-Path $srcDir "小鲸鱼.exe"
[void]$files.Add($rootExe)
foreach ($name in @("manifest.sha256", "manifest.sig")) {
    $p = Join-Path $srcDir $name
    if (Test-Path -LiteralPath $p) { [void]$files.Add($p) }
}
foreach ($name in @("README.md", "使用说明.txt", "使用说明_EN.txt")) {
    $p = Join-Path $srcDir $name
    if (Test-Path -LiteralPath $p) { [void]$files.Add($p) }
}
$resSrc = Join-Path $srcDir "resources"
if (Test-Path -LiteralPath $resSrc) {
    foreach ($f in @(Get-ChildItem -LiteralPath $resSrc -Recurse -File)) {
        $rel = "resources" + ($f.FullName.Substring($resSrc.Length))
        if (Is-Protected $rel) { continue }
        if ($f.Name -like "*.log" -or $f.Name -like "*.bak*") { continue }
        [void]$files.Add($f.FullName)
    }
}

$backup = Join-Path $Target ("_repair_backup\official-" + $stamp)
$nBak = 0
Say ""
Say ("  准备写入 {0} 个程序文件，先把将被覆盖的原文件备份到：" -f $files.Count) "DarkGray"
Say ("    " + $backup) "DarkGray"
foreach ($f in $files) {
    $rel = $f.Substring($srcDir.Length).TrimStart('\')
    $dst = Join-Path $Target $rel
    if (Is-Protected $rel) { continue }
    if (Test-Path -LiteralPath $dst) {
        $bak = Join-Path $backup $rel
        $d = Split-Path -Parent $bak
        if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
        try { Copy-Item -LiteralPath $dst -Destination $bak -Force; $nBak++ } catch { }
    }
}
Say ("  已备份 {0} 个文件" -f $nBak) "DarkGray"

# ---------- 5) 清掉会让校验打架的残留文件 ----------
# v1.9.7.1（P2）：根目录的 manifest.* 也算残留。下面第 6 步会从来源里重新拷一份
# 正确的进来（绿色版来源→根目录，安装包解出来的来源→resources\），但先删一次才不会
# 出现「根目录留着上一版清单、resources 下是新清单」的打架局面：主程序的自检要求
# 附近**每一份**清单都通过，一份过期的根清单就足以让自检失败（复测报告 4.2）。
$stale = @(
    (Join-Path $Target "manifest.sha256"),
    (Join-Path $Target "manifest.sig"),
    (Join-Path $Target "resources\manifest.sha256"),
    (Join-Path $Target "resources\manifest.sig"),
    (Join-Path $Target "_update_tmp")
)
foreach ($p in $stale) {
    if (Test-Path -LiteralPath $p) {
        Say ("  清掉残留：" + $p.Substring($Target.Length).TrimStart('\')) "Yellow"
        try { Remove-Item -LiteralPath $p -Recurse -Force } catch { }
    }
}
# 老的解包目录/旧启动器残留
foreach ($p in @((Join-Path $Target "backend_server.spec"), (Join-Path $Target "build_version_info.txt"))) {
    if (Test-Path -LiteralPath $p) { try { Remove-Item -LiteralPath $p -Force } catch { } }
}

# ---------- 6) 写入官方文件 ----------
$nOk = 0
$bad = @()
foreach ($f in $files) {
    $rel = $f.Substring($srcDir.Length).TrimStart('\')
    $dst = Join-Path $Target $rel
    $d = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    try {
        Copy-Item -LiteralPath $f -Destination $dst -Force -ErrorAction Stop
        $nOk++
    } catch {
        $bad += ($rel + "（" + $_.Exception.Message + "）")
    }
}
Say ""
Say ("  已写入 {0} 个文件" -f $nOk) "Green"
if ($bad.Count -gt 0) {
    Say ("  写入失败 {0} 个：" -f $bad.Count) "Red"
    foreach ($b in $bad) { Say ("    - " + $b) "Red" }
    # v1.9.7：写了一半就停下，也必须退回去 —— 半写状态比「没动」更危险。
    Say "  正在把已经写进去的文件还原回去…" "Yellow"
    $nBack2 = 0
    foreach ($f in $files) {
        $rel = $f.Substring($srcDir.Length).TrimStart('\')
        $dst = Join-Path $Target $rel
        $bak = Join-Path $backup $rel
        if (Test-Path -LiteralPath $bak) {
            try { Copy-Item -LiteralPath $bak -Destination $dst -Force -ErrorAction Stop; $nBack2++ } catch { }
        } else {
            if (Test-Path -LiteralPath $dst) { try { Remove-Item -LiteralPath $dst -Force -Recurse } catch { } }
        }
    }
    Say ("  已还原 {0} 个文件。" -f $nBack2) "Yellow"
    Say "  多半是程序还在运行占着文件。关掉小鲸鱼后重跑本脚本即可。" "Yellow"
    Say ("  备份在：" + $backup) "DarkGray"
    Fail 9 "没有全部写完，已自动回滚。" $null
}

# ---------- 7) 校验一遍 ----------
$exe = Join-Path $Target "小鲸鱼.exe"
$resFile = Join-Path ([System.IO.Path]::GetTempPath()) ("xjy-self-" + $stamp + ".txt")
$selfData = Join-Path ([System.IO.Path]::GetTempPath()) ("xjy-self-data-" + $stamp)
Register-TempCleanup $resFile
Register-TempCleanup $selfData
$oldDataDir = $env:QQ_AGENT_DATA_DIR
$rc = -1
$out = @()
try {
    Remove-Item -LiteralPath $resFile -Force -ErrorAction SilentlyContinue
    $env:QQ_AGENT_DATA_DIR = $selfData
    if (-not (Test-Path -LiteralPath $env:QQ_AGENT_DATA_DIR)) { New-Item -ItemType Directory -Path $env:QQ_AGENT_DATA_DIR -Force | Out-Null }
    $proc = Start-Process -FilePath $exe -ArgumentList @("--verify-self", "--result-file", $resFile) -PassThru -Wait -WindowStyle Hidden
    if ($null -ne $proc -and $null -ne $proc.ExitCode) { $rc = [int]$proc.ExitCode }
    if (Test-Path -LiteralPath $resFile) {
        $txt = @(Get-Content -LiteralPath $resFile -Encoding UTF8)
        if ($txt.Count -gt 0) { try { $rc = [int]("$($txt[0])".Trim()) } catch { } }
        if ($txt.Count -gt 1) { $out = $txt[1..($txt.Count - 1)] }
    }
} catch { }

# v1.9.8：自检用的数据目录是临时的，别让它跟着后面启动的程序走 ——
# 否则用户的数据会写进马上要被删掉的 %TEMP% 目录里。
if ([string]::IsNullOrEmpty($oldDataDir)) {
    try { Remove-Item Env:\QQ_AGENT_DATA_DIR -ErrorAction SilentlyContinue } catch { }
} else {
    $env:QQ_AGENT_DATA_DIR = $oldDataDir
}

Say ""
if ($rc -eq 0) {
    Say "  ✓ 恢复完成：文件校验通过（官方原版）" "Green"
} elseif ($rc -eq -1) {
    # v1.9.7：没验证过就是没验证过，不能打勾（复测报告 5.1）
    Say "  ? 文件已写入，但没拿到自检结果（程序没启动起来）。" "Yellow"
    Say "    这不等于「恢复成功」—— 请手动打开一次小鲸鱼确认。" "Yellow"
} else {
    Say ("  ! 自检没通过（返回 {0}），正在把备份还原回去…" -f $rc) "Red"
    foreach ($l in $out) { Say ("    " + $l) "Yellow" }
    # v1.9.7：自检失败必须真的退回去，不能只提示一句黄字。
    $nBack = 0
    foreach ($f in $files) {
        $rel = $f.Substring($srcDir.Length).TrimStart('\')
        $dst = Join-Path $Target $rel
        $bak = Join-Path $backup $rel
        if (Test-Path -LiteralPath $bak) {
            $d = Split-Path -Parent $dst
            if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
            try { Copy-Item -LiteralPath $bak -Destination $dst -Force -ErrorAction Stop; $nBack++ } catch { }
        } else {
            # 备份里没有 = 这个文件是这次新加的，回滚时删掉
            if (Test-Path -LiteralPath $dst) {
                try { Remove-Item -LiteralPath $dst -Force -Recurse } catch { }
            }
        }
    }
    Say ("  已还原 {0} 个文件，程序目录回到跑这个工具之前的样子。" -f $nBack) "Yellow"
    Say ("  备份还在：" + $backup) "DarkGray"
    Fail 9 "自检没通过，已自动回滚。" @("程序目录现在和运行本工具之前一样", "把上面的提示截图发给我们")
}

# ---------- 8) 说明保留了什么 ----------
Say ""
Say "  你的数据都在，一个没动：" "White"
Say "    · 记忆 / 日记 / 永久记忆（memory\ 或 memory.db）" "Gray"
Say "    · 人设、头像（personas\、persona_faces\）" "Gray"
Say "    · 设置（config.json）、自定义音色（resources\av_module\voices\）" "Gray"
Say "    · 日志与备份（agent.log、_backup\、_repair_backup\）" "Gray"
Say "    · 防护章的锁（guard.seal）" "Gray"
Say "  注意：guard.seal 也属于你的数据，本工具不会换锁。" "DarkGray"
Say "        如果之前在设置里关过注入防护，恢复后请去「设置 → 安全」确认那几个开关是开着的。" "DarkGray"
Say ""
Say ("  程序文件的原件备份在：" + $backup) "DarkGray"
Say "  想回退这次恢复：把上面目录里的文件拷回程序目录即可。" "DarkGray"
Say ""

if ($Launch) {
    Say "  正在启动小鲸鱼…" "Cyan"
    try { Start-Process -FilePath $exe -WorkingDirectory $Target | Out-Null } catch { }
    Start-Sleep -Seconds 2
    try { Invoke-TempCleanup } catch { }
    exit 0
}

if ($Quiet) { try { Invoke-TempCleanup } catch { }; exit 0 }
$ans = Read-Host "  现在就打开小鲸鱼吗？(Y/n)"
if ([string]::IsNullOrWhiteSpace($ans) -or $ans -match '^[Yy]') {
    try { Start-Process -FilePath $exe -WorkingDirectory $Target | Out-Null } catch { }
}
Say ""
Say "  完成。可以关掉这个窗口了。" "DarkGray"
try { Invoke-TempCleanup } catch { }
Start-Sleep -Seconds 2
exit 0
