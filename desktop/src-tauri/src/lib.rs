// 小鲸鱼 Agent · Tauri 桌面壳（v1.3.0）
//
// 职责：
//   1. 窗口起来时，静默把 Python 后端（backend_server.exe）拉起来；
//   2. 从它的 stdout 里读出真实端口（"[backend] listening on http://127.0.0.1:5701"），
//      交给前端桥接层；
//   3. 窗口关闭 / 程序退出时，把后端（以及后端拉起的 agent 子进程）彻底杀掉，不留残魂；
//   4. v1.3.0 新增：运行时切换无边框（set_decorations）、托盘图标（关窗驻留）、
//      自重启（安装版没有 Python 解释器，只能靠这个）。
//
// 业务逻辑一行都不在这里 —— 全在 Python 后端里。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::json;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;   // state() / try_state() / get_webview_window() 来自这个 trait

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// CREATE_NO_WINDOW：别弹出黑窗口
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// v1.5.3（QA P0-2）：单次等待上限从 45 秒压到 20 秒，并且**子进程一死就立刻失败**，
/// 不再「死等 45 秒还占着单实例锁」（第二个实例进来只会看到「点了没反应」）。
const SPAWN_TIMEOUT: Duration = Duration::from_secs(20);
/// 启动失败时的重试次数（P0-2：偶发的 5701 争抢/杀软扫描不该让用户重装）
const SPAWN_TRIES: usize = 3;

/// v1.8.7（QA P0）：探活探针的等待上限与次数。
/// 以前是 20 次 x 900ms 打 /api/health，没装 NapCat 的机器上后端要 2 秒才回，
/// 结果健康的后端被当成坏的杀掉 —— 新用户第一眼就是「窗口一闪就没了」。
const SPAWN_PING_MS: u64 = 5000;
const SPAWN_PING_TRIES: usize = 10;

pub struct Backend {
    /// 形如 http://127.0.0.1:5701（拿不到就是空串）
    pub base: String,
    pub child: Mutex<Option<Child>>,
    pub pid: Mutex<Option<u32>>,
    /// v1.9.0：本次启动用的 API Token。写操作必须带它，别的本机进程拿不到。
    pub token: String,
}

impl Backend {
    fn new() -> Self {
        Self {
            base: String::new(),
            child: Mutex::new(None),
            pid: Mutex::new(None),
            token: String::new(),
        }
    }
}

fn exe_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn which(exe: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let mut names: Vec<String> = vec![exe.to_string()];
    if cfg!(windows) {
        names.push(format!("{exe}.exe"));
    }
    for dir in std::env::split_paths(&path) {
        for n in &names {
            let p = dir.join(n);
            if p.is_file() {
                return Some(p);
            }
        }
    }
    None
}

/// 找后端：优先打包进去的 exe，其次源码模式下的 python。
fn backend_cmd() -> Option<Command> {
    let dir = exe_dir();
    let mut candidates: Vec<PathBuf> = vec![
        dir.join("backend_server.exe"),
        dir.join("resources").join("backend_server.exe"),
        dir.join("_up_").join("backend_server.exe"),
    ];
    // cargo tauri dev 时 exe 在 target/debug，往上找项目根
    for anc in dir.ancestors().skip(1).take(4) {
        candidates.push(anc.join("backend_server.py"));
    }

    for p in &candidates {
        if !p.is_file() {
            continue;
        }
        if p.extension().and_then(|e| e.to_str()) == Some("py") {
            // 源码模式：用 python 解释器跑
            let py = which("python").or_else(|| which("python3")).or_else(|| which("py"))?;
            let mut c = Command::new(py);
            c.arg(p);
            if let Some(root) = p.parent() {
                c.current_dir(root);
            }
            return Some(c);
        }
        return Some(Command::new(p));
    }
    None
}

// ==================== v1.5.3：长路径删除 / _MEI 清扫 / 端口文件 ====================

/// Windows 长路径前缀：`C:\a\b` → `\\?\C:\a\b`，UNC `\\srv\share` → `\\?\UNC\srv\share`。
/// 不加这个前缀，路径超过 260 字符时 `remove_dir_all` 会报「文件名或扩展名太长」。
#[cfg(windows)]
fn long_path(p: &Path) -> PathBuf {
    let s = p.to_string_lossy().to_string();
    if s.starts_with(r"\\?\") {
        return p.to_path_buf();
    }
    if let Some(rest) = s.strip_prefix(r"\\") {
        return PathBuf::from(format!(r"\\?\UNC\{rest}"));
    }
    PathBuf::from(format!(r"\\?\{s}"))
}

/// 递归删目录（长路径也删得掉）。删不干净返回 false —— 调用方记一行日志就好，
/// 不要无限加后缀重试（以前那种 `xxx.xjydel.xjydel.xjydel` 就是这么攒出来的）。
fn rm_rf(p: &Path) -> bool {
    #[cfg(windows)]
    {
        let lp = long_path(p);
        if std::fs::remove_dir_all(&lp).is_ok() {
            return true;
        }
        // 兜底：逐个删（有些是只读 / 有残留句柄）
        if let Ok(rd) = std::fs::read_dir(&lp) {
            for e in rd.flatten() {
                let path = e.path();
                if path.is_dir() {
                    let _ = std::fs::remove_dir_all(&path);
                } else {
                    let _ = std::fs::remove_file(&path);
                }
            }
        }
        std::fs::remove_dir(&lp).is_ok()
    }
    #[cfg(not(windows))]
    {
        std::fs::remove_dir_all(p).is_ok()
    }
}

fn local_app_dir() -> PathBuf {
    std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("xiaojingyu-console")
}

/// v1.5.3（P0-3）：退出时自己扫一遍 `_MEI*`。
///
/// 为什么需要：退出时我们会硬杀 `av_server.exe`，它来不及清自己的解包目录；
/// 后端（PyInstaller onefile）虽然会自己清，但它要是被 taskkill 就没人清了。
/// 这里只删「45 秒以上没动过」的，避免误删另一个正在启动的实例。
fn sweep_mei_dirs() {
    let mut dirs: Vec<PathBuf> = Vec::new();
    for k in ["TEMP", "TMP", "LOCALAPPDATA"] {
        if let Ok(v) = std::env::var(k) {
            let v = v.trim().to_string();
            if v.is_empty() {
                continue;
            }
            if k == "LOCALAPPDATA" {
                dirs.push(Path::new(&v).join("Temp"));
            } else {
                dirs.push(PathBuf::from(v));
            }
        }
    }
    dirs.push(std::env::temp_dir());
    dirs.push(Path::new(&std::env::var("USERPROFILE").unwrap_or_default()).join("AppData/Local/Temp"));

    let now = std::time::SystemTime::now();
    let mut seen: Vec<PathBuf> = Vec::new();
    for d in dirs {
        if d.as_os_str().is_empty() {
            continue;
        }
        if seen.iter().any(|x| x == &d) {
            continue;
        }
        seen.push(d.clone());
        let rd = match std::fs::read_dir(&d) {
            Ok(r) => r,
            Err(_) => continue,
        };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().to_lowercase();
            if !name.starts_with("_mei") {
                continue;
            }
            let p = e.path();
            let meta = match std::fs::metadata(&p) {
                Ok(m) => m,
                Err(_) => continue,
            };
            if !meta.is_dir() {
                continue;
            }
            let age = meta
                .modified()
                .ok()
                .and_then(|t| now.duration_since(t).ok())
                .map(|x| x.as_secs())
                .unwrap_or(9_999);
            if age < 45 {
                continue;
            }
            // 先改名：改名成功说明没有进程占着它
            let trashed = d.join(format!("{name}.xjydel"));
            if std::fs::rename(&p, &trashed).is_err() {
                continue;
            }
            if rm_rf(&trashed) {
                eprintln!("[tauri] 清掉残留解包目录 {}", trashed.display());
            } else {
                eprintln!(
                    "[tauri] 解包目录删不掉（长路径或仍被占用），留着下次再试: {}",
                    trashed.display()
                );
            }
        }
    }
}

/// v1.5.3（P0-2）：后端的端口也落一份到磁盘。
///
/// stdout 被壳/杀软接管时 `print()` 会抛 `OSError [Errno 22]`（我们已把后端那行
/// 包了 try/except），可那样一来 Rust 就读不到端口行。落文件之后，
/// 「终端打印失败但服务其实起来了」这种畸形情况也能正常接上。
fn port_file_path() -> PathBuf {
    local_app_dir().join("backend.port")
}

fn read_port_file(max_age_secs: u64) -> Option<String> {
    let p = port_file_path();
    let meta = std::fs::metadata(&p).ok()?;
    let age = meta
        .modified()
        .ok()
        .and_then(|t| std::time::SystemTime::now().duration_since(t).ok())
        .map(|d| d.as_secs())
        .unwrap_or(9_999);
    if age > max_age_secs {
        return None;
    }
    let text = std::fs::read_to_string(&p).ok()?;
    let v: serde_json::Value = serde_json::from_str(text.trim()).ok()?;
    let url = v.get("url")?.as_str()?.trim().to_string();
    if url.is_empty() {
        None
    } else {
        Some(url)
    }
}

/// 启动失败时留一行给用户/客服看（壳闪退时窗口都没了，只有这个文件能说明原因）。
fn write_boot_error(msg: &str) {
    let dir = local_app_dir();
    let _ = std::fs::create_dir_all(&dir);
    let p = dir.join("boot.err.log");
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let mut f = match std::fs::OpenOptions::new().create(true).append(true).open(&p) {
        Ok(f) => f,
        Err(_) => return,
    };
    let _ = writeln!(f, "[{stamp}] {msg}");
    eprintln!("[tauri] {msg}（已记到 {}）", p.display());
}

/// v1.8.7（QA P0）：读 boot.err.log 里最后一条非空记录，塞进错误框。
/// 只读最后 64KB，日志再大也不会卡住启动。
fn last_boot_error() -> String {
    let dir = local_app_dir();
    let p = dir.join("boot.err.log");
    let f = match std::fs::File::open(&p) {
        Ok(f) => f,
        Err(_) => return String::new(),
    };
    let mut buf = Vec::new();
    let _ = f.take(65536).read_to_end(&mut buf);
    let txt = String::from_utf8_lossy(&buf).to_string();
    for line in txt.lines().rev() {
        let t = line.trim();
        if !t.is_empty() {
            return t.to_string();
        }
    }
    String::new()
}

/// v1.8.7（QA P0）：真的敲一次 /api/ping。
///
/// 为什么是 /api/ping 而不是 /api/health：`/api/health` 会现查 NapCat / edge-tts / Node
/// 三个插件，没装 NapCat 的机器上一次要 2 秒多 —— 而壳这边等 900ms 就判死，
/// 于是「健康的后端被自己杀掉、窗口永远出不来」。/api/ping 只回一个常量 JSON，10ms 内返回。
///
/// 为什么要有它：`backend_server.exe` 被更新包写坏时，进程常常能起来、也能打印端口，
/// 但一接受请求就崩 —— 只看「有没有打印 listening on」会把它当成启动成功，
/// 用户看到的就是「窗口一闪就没了，什么提示都没有」。
fn health_ok(base: &str, ms: u64) -> bool {
    let hp = base.trim_start_matches("http://").trim_end_matches('/');
    let port: u16 = match hp.rsplit(':').next().and_then(|p| p.parse().ok()) {
        Some(p) => p,
        None => return false,
    };
    if port == 0 {
        return false;
    }
    let sock = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut s = match TcpStream::connect_timeout(&sock, Duration::from_millis(ms)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = s.set_read_timeout(Some(Duration::from_millis(ms)));
    let _ = s.set_write_timeout(Some(Duration::from_millis(ms)));
    let req = format!("GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if s.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = Vec::new();
    let _ = s.take(8192).read_to_end(&mut buf);
    let txt = String::from_utf8_lossy(&buf).to_string();
    txt.contains("200") && txt.contains("\"ok\"")
}


/// 拉起后端并等它打印端口。
/// v1.9.0：给后端生成一个随机 API Token（32 字节 → hex）。
/// 只通过环境变量交给后端、再通过 backend_info 交给前端，
/// **不落盘**（落盘那份是给「直接跑 exe」的场景用的，见 backend_server.py）。
fn make_api_token() -> String {
    let mut buf = [0u8; 32];
    // 拿系统随机源；失败就退化成时间 + 进程 id 拼的弱随机（只为不 panic）
    #[cfg(unix)]
    {
        use std::io::Read;
        if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
            if f.read_exact(&mut buf).is_ok() {
                return buf.iter().map(|b| format!("{:02x}", b)).collect();
            }
        }
    }
    {
        let seed = format!("{}-{}-{}", std::process::id(),
                           std::time::SystemTime::now()
                               .duration_since(std::time::UNIX_EPOCH)
                               .map(|d| d.as_nanos())
                               .unwrap_or(0),
                           std::ptr::addr_of!(buf) as usize);
        let mut h: u64 = 0xcbf29ce484222325;
        for b in seed.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        for i in 0..32 {
            h ^= h << 13;
            h ^= h >> 7;
            h ^= h << 17;
            buf[i] = (h >> ((i % 8) * 8)) as u8;
        }
    }
    buf.iter().map(|b| format!("{:02x}", b)).collect()
}


fn spawn_backend() -> Backend {
    let mut last = Backend::new();
    for attempt in 1..=SPAWN_TRIES {
        let (st, why) = spawn_backend_once();
        if !st.base.is_empty() {
            if attempt > 1 {
                eprintln!("[tauri] 后端第 {attempt} 次尝试成功: {}", st.base);
            }
            return st;
        }
        eprintln!("[tauri] 后端第 {attempt}/{SPAWN_TRIES} 次启动失败：{why}");
        if attempt < SPAWN_TRIES {
            std::thread::sleep(Duration::from_millis(500));
        }
        last = st;
    }
    last
}

/// 单次尝试：拉起来 + 等端口。**子进程一死立刻返回失败**，绝不等满超时。
fn spawn_backend_once() -> (Backend, String) {
    let mut st = Backend::new();

    let mut cmd = match backend_cmd() {
        Some(c) => c,
        None => {
            return (
                st,
                "没找到 backend_server.exe / backend_server.py".to_string(),
            )
        }
    };

    // v1.9.2（N-01）：验签验的是一份文件，spawn 起的是另一份 —— 这就是报告的
    // 路径规范化绕过。这里把「即将执行的那个程序」规范化后和「验过的那个」比，
    // 不一致就直接失败（上层会弹「后端没能启动」并退出，绝不跑来路不明的 exe）。
    if let Some(expect) = verified_backend() {
        let got_raw = cmd.get_program();
        let got = std::fs::canonicalize(got_raw)
            .unwrap_or_else(|_| std::path::PathBuf::from(got_raw));
        if &got != expect {
            write_boot_error(&format!(
                "后端路径与验签结果不一致：验的是 {}，要起的是 {}",
                expect.display(),
                got.display()
            ));
            return (
                st,
                format!(
                    "要启动的后端和验签通过的不是同一个文件（验 {}，起 {}）—— \
                     疑似被替换，拒绝启动",
                    expect.display(),
                    got.display()
                ),
            );
        }
    }

    cmd.arg("--port")
        .arg("0") // 0 = 让后端自己从 5701 起找空端口
        // v1.5.1（P1-3）：把我们自己的 pid 告诉后端 —— 壳被强杀时后端自杀，
        // 不会留下一个没有界面的孤儿后端继续写记忆库。
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    // v1.2.1 开机自启：把「桌面壳自己」的路径告诉后端。
    // 后端写注册表时要写的是小鲸鱼.exe，而不是 backend_server.exe ——
    // 否则开机只会起一个没有界面的后端，用户以为软件坏了。
    if let Ok(me) = std::env::current_exe() {
        cmd.env("QQ_AGENT_LAUNCHER", me);
    }
    // 这次启动是不是开机自启拉起来的 → 后端收到后会自动 start_agent
    if std::env::args().any(|a| a == "--autostart") {
        cmd.env("QQ_AGENT_AUTOSTART", "1");
    }

    // v1.9.0：写操作必须带 Token。壳生成、只走环境变量传给后端，
    // 再通过 backend_info 交给前端 —— 别的本机进程拿不到。
    let token = make_api_token();
    cmd.env("QQ_AGENT_API_TOKEN", &token);
    st.token = token;

    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    // 起之前先把上一轮留下的端口文件删掉，免得读到陈旧端口
    let _ = std::fs::remove_file(port_file_path());

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return (st, format!("启动进程失败: {e}")),
    };

    *st.pid.lock().unwrap() = Some(child.id());

    let (tx, rx) = mpsc::channel::<String>();

    // stdout：解析端口行，然后继续把输出转到 stderr
    // （必须一直读，管道写满后端会卡死）
    if let Some(out) = child.stdout.take() {
        let tx = tx.clone();
        std::thread::spawn(move || {
            let rd = BufReader::new(out);
            for line in rd.lines().map_while(Result::ok) {
                if let Some(idx) = line.find("listening on ") {
                    let url = line[idx + "listening on ".len()..].trim().to_string();
                    let _ = tx.send(url);
                }
                eprintln!("[backend] {line}");
            }
        });
    }
    // 关键：本函数不再持有发送端。子进程一死管道就 EOF，
    // 读线程结束 → 发送端全丢 → `rx` 立刻变成 Disconnected（见下面的判定）。
    drop(tx);

    // stderr 也要抽干，并且留最后几行做失败原因
    let tail: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    if let Some(err) = child.stderr.take() {
        let tail = tail.clone();
        std::thread::spawn(move || {
            let rd = BufReader::new(err);
            for line in rd.lines().map_while(Result::ok) {
                eprintln!("[backend:err] {line}");
                if let Ok(mut v) = tail.lock() {
                    v.push(line);
                    if v.len() > 12 {
                        v.remove(0);
                    }
                }
            }
        });
    }

    let started = Instant::now();
    let deadline = started + SPAWN_TIMEOUT;
    let mut url: Option<String> = None;
    let mut exit_code: Option<i32> = None;

    loop {
        // 1) 打印出来的端口
        match rx.try_recv() {
            Ok(u) => {
                url = Some(u);
                break;
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                // stdout 断了：可能是子进程死了，也可能只是打印失败。
                // 打印失败的情况下面还有端口文件兜底，所以这里先不跳出。
            }
            Err(mpsc::TryRecvError::Empty) => {}
        }
        // 2) 端口文件兜底（后端 main() 里一定会写）
        if let Some(u) = read_port_file(120) {
            url = Some(u);
            break;
        }
        // 3) 子进程还在吗 —— 秒退立刻失败，不等满 20 秒
        match child.try_wait() {
            Ok(Some(status)) => {
                exit_code = Some(status.code().unwrap_or(-1));
                // 有些后端会「先打印端口再立刻退出」，再给端口文件一次机会
                if let Some(u) = read_port_file(30) {
                    url = Some(u);
                }
                break;
            }
            Ok(None) => {}
            Err(_) => {}
        }
        if Instant::now() > deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(60));
    }

    if let Some(u) = url {
        let base = u.trim_end_matches('/').to_string();
        // v1.8.6（QA P0-4）：光打印了地址不算成功，必须真的应答一次 /api/health。
        let mut alive = false;
        for _ in 0..SPAWN_PING_TRIES {
            if health_ok(&base, SPAWN_PING_MS) {
                alive = true;
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        if alive {
            st.base = base;
            *st.child.lock().unwrap() = Some(child);
            eprintln!("[tauri] 后端就绪: {}", st.base);
            return (st, String::new());
        }
        let pid = child.id();
        kill_tree(pid);
        let _ = child.wait();
        let last_line = tail
            .lock()
            .ok()
            .and_then(|v| v.last().cloned())
            .unwrap_or_default();
        let mut why = format!("后端打印了地址 {base}，但 /api/health 一直不通（程序文件可能损坏）");
        if !last_line.is_empty() {
            why.push_str(&format!("；最后一行输出：{last_line}"));
        }
        return (st, why);
    }

    // 失败：收拾掉这个半死不活的子进程，让单实例锁尽快释放
    let pid = child.id();
    kill_tree(pid);
    let _ = child.wait();
    let ms = started.elapsed().as_millis();
    let last_line = tail
        .lock()
        .ok()
        .and_then(|v| v.last().cloned())
        .unwrap_or_default();
    let mut why = match exit_code {
        Some(c) => format!("进程立刻退出（exit {c}，{ms} ms）"),
        None => format!("等了 {} 秒没拿到端口", SPAWN_TIMEOUT.as_secs()),
    };
    if !last_line.is_empty() {
        why.push_str(&format!("；最后一行输出：{last_line}"));
    }
    (st, why)
}

fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let mut c = Command::new("taskkill");
        c.args(["/PID", &pid.to_string(), "/T", "/F"]);
        c.creation_flags(CREATE_NO_WINDOW);
        c.stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let _ = c.status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill")
            .args(["-9", &pid.to_string()])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
}

/// v1.5.2（QA No-Go 修复一）：先请后端自己退，再兜底砍树。
///
/// 为什么：后端是 PyInstaller onefile，运行时会解出 `%TEMP%\\(_MEI...` 目录，
/// 只有引导进程看到子进程「正常结束」才会删。以前这里直接 kill_tree，
/// 引导进程一起被砍 → 每开一次留几百 MB，几天就把 C 盘塞满。
/// 这里用裸 TCP 发一个 `/api/quit_app`（不引新依赖），等它自己走，
/// 3 秒还没退再砍。
fn ask_backend_quit(base: &str) {
    let hostport = base
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().unwrap_or(5701)),
        None => (hostport.to_string(), 5701u16),
    };
    let addr = format!("{host}:{port}");
    let addrs = match addr.to_socket_addrs() {
        Ok(a) => a.collect::<Vec<_>>(),
        Err(_) => return,
    };
    for a in addrs {
        if let Ok(mut s) = TcpStream::connect_timeout(&a, Duration::from_millis(600)) {
            let _ = s.set_read_timeout(Some(Duration::from_millis(600)));
            let body = "{\"args\":[]}";
            let req = format!(
                "POST /api/quit_app HTTP/1.1\r\nHost: {hostport}\r\nOrigin: http://tauri.localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = s.write_all(req.as_bytes());
            let mut buf = [0u8; 128];
            let _ = s.read(&mut buf);
            return;
        }
    }
}

/// v1.8.4：裸 TCP 发一个 POST /api/{method}（不引新依赖）。
///
/// 为什么单独写一个：退出流程现在要「先请后端收 NapCat，再请它退」，
/// 两次调用要能拿到「发没发出去」的结果。老的 `ask_backend_quit` 只发不退，
/// 为了不把已经稳定的退出链路改坏，这里刻意新增一个通用版本，老函数一行不动。
fn backend_call(base: &str, path: &str, body: &str, read_ms: u64) -> bool {
    let hostport = base
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');
    let (host, port) = match hostport.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().unwrap_or(5701)),
        None => (hostport.to_string(), 5701u16),
    };
    let addr = format!("{host}:{port}");
    let addrs = match addr.to_socket_addrs() {
        Ok(a) => a.collect::<Vec<_>>(),
        Err(_) => return false,
    };
    for a in addrs {
        if let Ok(mut s) = TcpStream::connect_timeout(&a, Duration::from_millis(600)) {
            let _ = s.set_read_timeout(Some(Duration::from_millis(read_ms)));
            let req = format!(
                "POST {path} HTTP/1.1\r\nHost: {hostport}\r\nOrigin: http://tauri.localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            if s.write_all(req.as_bytes()).is_err() {
                return false;
            }
            let mut buf = [0u8; 512];
            let _ = s.read(&mut buf);
            return true;
        }
    }
    false
}

/// v1.8.4：退出前先请后端把 NapCat 的 node 进程收掉。
///
/// 顺序有讲究：`请求后端杀 Node -> 停 av_server -> 停 backend_server -> 关壳`。
/// 为什么不省略这一步：backend 一死，它持有的 Job 句柄会关掉，
/// `KILL_ON_JOB_CLOSE` 理论上会兜住；但「以后端身份主动杀」能拿到准确 pid、
/// 能在杀掉失败（进程是管理员拉起的）时留标记告诉用户，比静默兜底强。
fn ask_backend_kill_nodes(base: &str) {
    if base.is_empty() {
        return;
    }
    if backend_call(base, "/api/kill_nodes_force", "{\"args\":[2.5]}", 2500) {
        eprintln!("[tauri] 已请后端清理 NapCat 进程");
    } else {
        // 后端已经没了 / 端口不通：Job 兜底会处理，但留个标记方便排查
        eprintln!("[tauri] 请后端清理 NapCat 进程失败（后端可能已退出，Job 兜底回收）");
        write_boot_error("退出时没能请后端清理 NapCat 进程（后端已退出或端口不通）");
    }
}


/// v1.5.2：单实例锁（进程活着期间一直持有，进程一死系统自动释放）。
///
/// 用 `%LOCALAPPDATA%\xiaojingyu-console\instance.lock` 上的独占文件锁，
/// 不引第三方依赖；崩溃留下的锁文件不会阻塞下次启动（锁跟着进程句柄走）。
fn single_instance_guard() -> Option<std::fs::File> {
    let dir = std::env::var("LOCALAPPDATA")
        .ok()
        .filter(|s| !s.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let d = dir.join("xiaojingyu-console");
    let _ = std::fs::create_dir_all(&d);
    let p = d.join("instance.lock");
    let f = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .open(&p)
        .ok()?;
    match f.try_lock() {
        Ok(()) => Some(f),
        Err(_) => None,
    }
}

fn stop_backend(state: &Backend) {
    let base = state.base.clone();
    let child_pid = *state.pid.lock().unwrap();

    // v1.8.4：退出顺序 = 先收 NapCat(node) → 再停 av_server / 后端 → 关壳。
    // 放在最前面是因为它依赖后端还活着（要它自己去精确识别并 kill）。
    ask_backend_kill_nodes(&base);

    // v1.5.3（QA P0-1）：**必须先把 child 从锁里 take 出来、让 guard 立刻释放**。
    //
    // 以前是 `if let Some(mut c) = state.child.lock().unwrap().take() { … }`：
    // `if let` 把头里那个临时 MutexGuard 的生命期延长到了整个语句块，
    // 于是超时分支里再写 `*state.child.lock().unwrap() = Some(c)` 就是对
    // 同一把 std::sync::Mutex 重入 —— 非重入锁，直接永久卡死主线程。
    // 用户看到的现象就是「点退出、点关窗，界面僵住不动，任务管理器里还活着」。
    let mut child = state.child.lock().unwrap().take();

    if !base.is_empty() {
        ask_backend_quit(&base);
        // 等它自己退（引导进程这时会清理 %TEMP% 解包目录）
        if let Some(c) = child.as_mut() {
            let deadline = Instant::now() + Duration::from_millis(3000);
            loop {
                match c.try_wait() {
                    Ok(Some(_)) => {
                        eprintln!("[tauri] 后端已自行退出（临时目录已清理）");
                        child = None;
                        break;
                    }
                    Ok(None) => {}
                    Err(_) => break,
                }
                if Instant::now() > deadline {
                    eprintln!("[tauri] 后端 3 秒没退，改为强制结束");
                    break;
                }
                std::thread::sleep(Duration::from_millis(120));
            }
        }
    }

    if let Some(mut c) = child.take() {
        let pid = child_pid.unwrap_or_else(|| c.id());
        eprintln!("[tauri] 强制结束后端进程树 pid={pid}");
        kill_tree(pid);
        let _ = c.wait();
    }

    // 清掉这一轮留下的解包目录（P0-3：强杀 av_server.exe 时它来不及自己清）
    sweep_mei_dirs();

    // 收尾统一清状态；这里已经不再持有 child 锁，安全
    *state.child.lock().unwrap() = None;
    *state.pid.lock().unwrap() = None;
}

/// 界面层状态：托盘开没开（决定关窗是「隐藏」还是「退出」）
pub struct UiState {
    pub tray: Mutex<bool>,
}

impl UiState {
    fn new() -> Self {
        Self { tray: Mutex::new(false) }
    }
}

#[tauri::command]
fn backend_info(state: tauri::State<'_, Backend>) -> serde_json::Value {
    let pid = *state.pid.lock().unwrap();
    json!({
        "ok": !state.base.is_empty(),
        "base": state.base,
        "pid": pid,
        "version": env!("CARGO_PKG_VERSION"),
        // v1.9.0：写操作必须带这个 Token。只交给本页面的 JS，不落盘、不进命令行。
        "token": state.token,
    })
}

/// 前端发现后端失联时可以调这个重启（先杀干净再拉起，重新读端口）
#[tauri::command]
fn backend_restart(state: tauri::State<'_, Backend>) -> serde_json::Value {
    stop_backend(&state);
    let mut fresh = spawn_backend();
    let base = fresh.base.clone();
    // v1.9.0：重启会换一个新 Token，必须一起交给前端，否则重启后所有写操作都被 401。
    let token = fresh.token.clone();
    *state.child.lock().unwrap() = fresh.child.get_mut().unwrap().take();
    *state.pid.lock().unwrap() = fresh.pid.get_mut().unwrap().take();
    json!({ "ok": !base.is_empty(), "base": base, "token": token })
}

// ==================== v1.3.0：窗口外观 / 托盘 / 自重启 ====================

/// 运行时切换无边框（开关能真正生效，不用重启）
#[tauri::command]
fn set_decorations(window: tauri::WebviewWindow, flag: bool) -> serde_json::Value {
    eprintln!("[tauri] set_decorations flag={flag}");
    match window.set_decorations(flag) {
        Ok(_) => json!({ "ok": true, "decorations": flag }),
        Err(e) => json!({ "ok": false, "msg": e.to_string() }),
    }
}

#[tauri::command]
fn get_decorations(window: tauri::WebviewWindow) -> serde_json::Value {
    match window.is_decorated() {
        Ok(v) => json!({ "ok": true, "decorations": v }),
        Err(e) => json!({ "ok": false, "msg": e.to_string() }),
    }
}

fn build_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    let show_i = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "退出小鲸鱼", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &quit_i])?;

    let mut builder = TrayIconBuilder::with_id("main-tray")
        .tooltip("小鲸鱼 Agent 控制台")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main(app),
            "quit" => {
                if let Some(state) = app.try_state::<Backend>() {
                    stop_backend(&state);
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

fn show_main(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

/// 开关托盘图标（前端进设置时按配置调一次）
#[tauri::command]
fn set_tray(app: tauri::AppHandle, state: tauri::State<'_, UiState>, enabled: bool) -> serde_json::Value {
    let mut cur = state.tray.lock().unwrap();
    if enabled {
        if !*cur {
            if let Err(e) = build_tray(&app) {
                eprintln!("[tauri] 托盘创建失败: {e}");
                return json!({ "ok": false, "msg": format!("托盘图标创建失败：{e}") });
            }
            *cur = true;
        }
    } else if *cur {
        app.remove_tray_by_id("main-tray");
        *cur = false;
    }
    json!({ "ok": true, "tray": *cur })
}

/// 真·重启（安装版没有 Python 解释器，不能靠脚本；这里拉自己的 exe 再退出）
#[tauri::command]
fn app_relaunch(app: tauri::AppHandle) -> serde_json::Value {
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => return json!({ "ok": false, "msg": e.to_string() }),
    };
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Some(state) = app.try_state::<Backend>() {
        stop_backend(&state);
    }
    let mut c = Command::new(exe);
    c.args(args);
    #[cfg(windows)]
    c.creation_flags(CREATE_NO_WINDOW);
    match c.spawn() {
        Ok(_) => {
            app.exit(0);
            json!({ "ok": true })
        }
        Err(e) => json!({ "ok": false, "msg": e.to_string() }),
    }
}

/// 原生错误框（壳都没建起来的时候用；不引依赖，直接调 user32）。
///
/// 弹窗放后台线程、主线程最多等 20 秒：用户不点它也不会把进程卡死
/// （`MessageBoxW` 是模态的，直接在主线程调 = 又一种「点了没反应」）。
#[cfg(windows)]
fn message_box(text: &str) {
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    unsafe extern "system" {
        fn MessageBoxW(hwnd: *mut u8, text: *const u16, caption: *const u16, utype: u32) -> i32;
    }
    fn wide(s: &str) -> Vec<u16> {
        OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
    }
    let t = wide(text);
    let c = wide("小鲸鱼");
    let (tx, rx) = mpsc::channel::<()>();
    std::thread::spawn(move || {
        // 0x10 = MB_ICONERROR | MB_OK
        unsafe {
            MessageBoxW(std::ptr::null_mut(), t.as_ptr(), c.as_ptr(), 0x10);
        }
        let _ = tx.send(());
    });
    let _ = rx.recv_timeout(Duration::from_secs(20));
}

#[cfg(not(windows))]
fn message_box(text: &str) {
    eprintln!("{text}");
}

#[tauri::command]
/// v1.6.0：前端明确要「退出」时，后端会被 /api/quit_app 收掉，但壳必须自己退。
/// 以前只调 quit_app → 后端没了、窗口还在（开了托盘更是永远留在托盘里），
/// 用户看到的就是「点了退出没反应」。
fn app_quit(app: tauri::AppHandle, state: tauri::State<'_, Backend>) -> serde_json::Value {
    stop_backend(&state);
    let a = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(250));
        a.exit(0);
    });
    json!({ "ok": true })
}


// ==================== v1.9.1：发行包完整性校验 ====================
// 为什么在壳里做：验证器不能放在被验证的对象里。以前验签写在 backend_server.py，
// 攻击者换掉 backend_server.exe 之后，跑起来的是他的代码，那句「校验通过」也是他说的。
// 现在由壳（小鲸鱼.exe）在拉起后端之前验：Ed25519 验签 + 逐个 sha256 比对。
const PKG_PUBKEY_B64: &str = "7hiOuUQKOs8OJaSHP/rwkDgJFzYIEVABPB8dfcIe3O4=";

fn b64_decode(s: &str) -> Option<Vec<u8>> {
    fn val(c: u8) -> Option<u8> {
        match c {
            b'A'..=b'Z' => Some(c - b'A'),
            b'a'..=b'z' => Some(c - b'a' + 26),
            b'0'..=b'9' => Some(c - b'0' + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    }
    let mut out: Vec<u8> = Vec::new();
    let mut buf: u32 = 0;
    let mut bits: u32 = 0;
    for &c in s.as_bytes() {
        if c == b'=' {
            break;
        }
        let x = val(c)?;
        buf = (buf << 6) | x as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buf >> bits) as u8);
        }
    }
    Some(out)
}

fn sha256_hex(path: &std::path::Path) -> Option<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut f = std::fs::File::open(path).ok()?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    let d = h.finalize();
    let mut s = String::with_capacity(64);
    for b in d.iter() {
        s.push_str(&format!("{:02x}", b));
    }
    Some(s)
}

/// v1.9.2（N-01）：验签通过的那个 backend_server.exe（绝对路径）。
/// 空 = 开发构建 / 还没验过，此时不做路径比对。
static VERIFIED_BACKEND: std::sync::OnceLock<std::path::PathBuf> =
    std::sync::OnceLock::new();

fn verified_backend() -> Option<&'static std::path::PathBuf> {
    VERIFIED_BACKEND.get().filter(|p| !p.as_os_str().is_empty())
}

/// 校验发行包。Ok(路径) = 通过，并交出「验过的那个 backend_server.exe」的
/// 绝对路径（canonicalize 过）；Err(人话) = 必须拦下。
/// 开发构建（debug_assertions）直接放行 —— 源码运行时没有清单是正常的，
/// 此时返回空路径，表示「不做路径比对」。
///
/// v1.9.2（N-01）：以前只返回 Ok(()) ，验的是 resources/ 里那份，而
/// backend_cmd() 优先起程序目录下那份 —— 攻击者只要把程序目录下那个换成
/// 带后门的、清单里那份保持官方文件，验签就白做了。所以必须把路径交出来。
/// v1.9.6：把「验一份清单」单独抽出来。
///
/// 为什么要抽：一台机器上可能同时存在两份清单 —— 老绿色版把全量清单放在
/// 包根，安装包（NSIS）只在 resources/ 下放三行 resources 清单。以前只认
/// 「先找到的那一份」，两份来自不同版本时必然对不上，用户看到的就是
/// 「被篡改」，而且换安装包重装也没用（根目录那份旧清单还在）。
/// v2.0.0：只判「这份清单是不是官方签的」，不判文件内容对不对。
///
/// 为什么要把这一步单独拎出来：跨大版本升级时目录里会躺着别的版本留下的清单。
/// 那些清单**签名是真的**（都是官方签的），只是记的是旧文件的哈希 —— 本函数能
/// 证明「它是官方产物」，所以可以放心当成残骸清掉；而签名不对的清单绝不能删，
/// 那是有人在伪造，必须留着拦启动。
fn manifest_sig_status(mp: &std::path::Path) -> Result<(), String> {
    let text = std::fs::read_to_string(mp).map_err(|e| format!("清单读不出来：{e}"))?;
    let sigp = mp.with_file_name("manifest.sig");
    let sig = std::fs::read(&sigp)
        .map_err(|_| "缺少签名文件 manifest.sig（被删过）".to_string())?;
    let pub_raw = b64_decode(PKG_PUBKEY_B64).ok_or("内置公钥解析失败")?;
    let pk: [u8; 32] = pub_raw
        .as_slice()
        .try_into()
        .map_err(|_| "内置公钥长度不对")?;
    let vk = ed25519_dalek::VerifyingKey::from_bytes(&pk)
        .map_err(|e| format!("内置公钥无效：{e}"))?;
    let sig_arr: [u8; 64] = sig.as_slice().try_into().map_err(|_| {
        format!("签名文件长度不对（{} 字节，应为 64）", sig.len())
    })?;
    let sg = ed25519_dalek::Signature::from_bytes(&sig_arr);
    use ed25519_dalek::Verifier;
    vk.verify(text.as_bytes(), &sg)
        .map_err(|_| "签名校验不通过 —— 清单不是官方签的（有人在伪造）".to_string())
}

/// v2.0.0：清掉「官方签的、但内容对不上当前程序」的旧版清单（连同 .sig）。
///
/// 这是 V1.9.x → V2.0.0 那个「升级完打不开」的正面修法。想清楚两件事：
///   · 只删「签名有效、内容对不上」的：能证明它是官方出的另一个版本的残骸；
///   · 签名无效的一律不删，照样拦启动 —— 伪造清单不但没用，还会被留着当证据。
/// 安全性：删掉别人的清单只会让候选变少。真要放行，还得有一份官方签的、覆盖当前
/// 全部文件（含正在跑的主程序）的清单；这种清单攻击者造不出来。
fn drop_stale_manifests(cands: Vec<std::path::PathBuf>, exe: &std::path::Path) -> (Vec<std::path::PathBuf>, Vec<String>) {
    let mut kept: Vec<std::path::PathBuf> = Vec::new();
    let mut removed: Vec<String> = Vec::new();
    for mp in cands {
        match verify_one_manifest(&mp, exe) {
            Ok(_) => kept.push(mp),
            Err(_why) => {
                if manifest_sig_status(&mp).is_ok() {
                    // 官方签的，但文件对不上 → 别的版本留下的残骸，清掉
                    let mut ok = true;
                    for p in [mp.clone(), mp.with_file_name("manifest.sig")] {
                        if p.exists() {
                            if let Err(e) = std::fs::remove_file(&p) {
                                ok = false;
                                eprintln!("[小鲸鱼] 旧版清单删不掉 {}：{e}", p.display());
                            }
                        }
                    }
                    if ok {
                        eprintln!(
                            "[小鲸鱼] 已清掉一份旧版残留清单（{}）—— 它属于别的版本，留着会让新版自判被篡改。",
                            mp.display()
                        );
                        removed.push(mp.display().to_string());
                    } else {
                        kept.push(mp);
                    }
                } else {
                    // 签名不对：不是残骸，是伪造 —— 留着，让它继续拦启动
                    kept.push(mp);
                }
            }
        }
    }
    (kept, removed)
}

fn verify_one_manifest(mp: &std::path::Path, exe: &std::path::Path) -> Result<std::path::PathBuf, String> {
    let parent = mp.parent().ok_or("清单路径异常")?.to_path_buf();
    let root = if parent.file_name().map(|n| n == "resources").unwrap_or(false) {
        parent.parent().ok_or("清单路径异常")?.to_path_buf()
    } else {
        parent
    };
    let text = std::fs::read_to_string(mp).map_err(|e| format!("清单读不出来：{e}"))?;
    manifest_sig_status(mp)?;
    let mut bad: Vec<String> = Vec::new();
    let mut backend_path: Option<std::path::PathBuf> = None;
    let mut listed_exe = false;
    let rel_self = exe
        .strip_prefix(&root)
        .map(|p| p.to_string_lossy().replace('\\', "/").to_lowercase())
        .unwrap_or_default();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let mut it = line.splitn(2, "  ");
        let want = it.next().unwrap_or("").trim().to_lowercase();
        let rel = it.next().unwrap_or("").trim();
        if rel.is_empty() || want.len() != 64 {
            continue;
        }
        let rel_norm = rel.replace('\\', "/").to_lowercase();
        // v1.9.7：清单里有没有「正在跑的这个 exe」—— 不管它是不是全量清单。
        if rel_norm == rel_self {
            listed_exe = true;
        }
        let p = root.join(rel.replace('/', std::path::MAIN_SEPARATOR_STR));
        if rel_norm.eq_ignore_ascii_case("resources/backend_server.exe") {
            backend_path = Some(std::fs::canonicalize(&p).unwrap_or_else(|_| p.clone()));
        }
        match sha256_hex(&p) {
            Some(got) if got == want => {}
            Some(_) => bad.push(format!("{rel}（内容被改过）")),
            None => bad.push(format!("{rel}（文件不见了）")),
        }
    }
    if !bad.is_empty() {
        return Err(format!("有 {} 个文件对不上：{}", bad.len(), bad.join("；")));
    }
    // v1.9.5（N-17）：正在跑的这个 exe 必须出现在清单里，防「拷进子目录借父清单」。
    // v1.9.7（P0）：这条恢复为**无条件**检查。v1.9.6 为了修误判，给「清单不含主程序」
    // 开了口子（full_manifest），结果攻击者只要放一份只覆盖 3 个 resources 文件的
    // 公开清单，被改过的主程序就能完全冒充官方（复测报告 3.3 的 C 组，零告警）。
    // 真正的修法不是放松检查，而是让安装包的清单也覆盖主程序 —— 见
    // `打包发布.py::sign_tauri_resources()`，它现在会把 小鲸鱼.exe 的哈希写进清单。
    // 清单是构建期签的，主程序在那一刻已经存在，不存在循环信任。
    if !listed_exe {
        return Err(format!(
            "这个程序不在清单里（{}）—— 要么是有人把改过的程序塞进了子目录，\
             要么有人把清单换成了一份覆盖范围更小的。为安全起见不启动。",
            exe.display()
        ));
    }
    backend_path.ok_or_else(|| {
        "清单里没有 resources/backend_server.exe —— 没法确认要启动的是哪个后端".to_string()
    })
}

fn verify_release() -> Result<std::path::PathBuf, String> {
    if cfg!(debug_assertions) {
        return Ok(std::path::PathBuf::new());
    }
    let exe = std::env::current_exe().map_err(|e| format!("拿不到程序路径：{e}"))?;
    let start = match exe.parent() {
        Some(p) => p.to_path_buf(),
        None => return Err("拿不到程序目录".to_string()),
    };
    // v1.9.7（P0）：把上下 4 层的候选清单**全收齐**（以前找到第一层就停）。
    let mut cands: Vec<std::path::PathBuf> = Vec::new();
    let mut base = start.clone();
    for _ in 0..4 {
        for c in [
            base.join("manifest.sha256"),
            base.join("resources").join("manifest.sha256"),
        ] {
            if c.is_file() && !cands.iter().any(|x| x == &c) {
                cands.push(c);
            }
        }
        match base.parent() {
            Some(p) => base = p.to_path_buf(),
            None => break,
        }
    }
    if cands.is_empty() {
        return Err(
            "找不到签名清单 manifest.sha256（发行版必须带清单，缺了说明被删过）".to_string(),
        );
    }
    // v2.0.0：先把「别的版本留下的、官方签过的」清单清掉。
    // 不加这一步，跨大版本升级（V1.9.x → V2.0.0）几乎必然变砖：老绿色版把全量清单
    // 放在程序目录、安装版只在 resources/ 下放一份，两份来自不同版本时必然对不上，
    // 下面的规则「每一份都必须通过」就会一票否决 —— 用户看到「被篡改」，重装也没用
    // （旧的那份还在）。现在只清「签名有效 + 内容对不上」的残骸，签名不对的照样拦。
    let (cands, removed) = drop_stale_manifests(cands, &exe);
    if !removed.is_empty() {
        // 留个痕：万一是误判，boot.err.log 里能查到删了哪几份
        write_boot_error(&format!(
            "启动时清理了 {} 份旧版残留清单：{}",
            removed.len(),
            removed.join("；")
        ));
    }
    if cands.is_empty() {
        return Err(format!(
            "签名清单都被清掉了：目录里只剩「属于别的版本」的清单（已清理 {} 份），\
             没有一份属于当前这个程序。\n\
             如果你刚做过大版本跨代升级（比如 V1.9.x → V2.0.0），这通常是升级没做完整 —— \
             请重新下载【完整安装包】覆盖安装，或使用【恢复工具】。",
            removed.len()
        ));
    }
    // v1.9.7（P0）：这里以前是「任意一份通过就放行」，等于给攻击者留了一个
    // 降级开关 —— 往目录里塞一份覆盖范围更小的官方清单，被改过的主程序就能
    // 借它过关（复测报告 3.3）。现在改成：发现的每一份都必须通过，任何一份
    // 对不上就拒绝启动，并在提示里点名是哪一份。
    let mut errs: Vec<String> = Vec::new();
    let mut ok_backend: Option<std::path::PathBuf> = None;
    for mp in cands.iter() {
        match verify_one_manifest(mp, &exe) {
            Ok(p) => ok_backend = Some(p),
            Err(e) => errs.push(format!("{}：{}", mp.display(), e)),
        }
    }
    if !errs.is_empty() {
        return Err(format!(
            "发现 {} 份清单，其中 {} 份没通过（每份都必须是官方签发的）：\n{}\n\
             提示：多出来的那份通常是旧版本残留 —— 删掉它，或用「恢复官方安装」工具重装一遍。\n\
             如果你刚做过大版本跨代升级（比如 V1.9.x → V2.0.0），也可能是旧版文件残留导致：\n\
             请重新下载【完整安装包】覆盖安装，或使用【恢复工具】。",
            cands.len(),
            errs.len(),
            errs.join("\n")
        ));
    }
    ok_backend.ok_or_else(|| "清单校验没拿到后端路径".to_string())
}

/// v1.9.2（N-02）：增量包用的命令行入口 `小鲸鱼.exe --verify-delta <目录>`。
/// 返回进程退出码：0 = 通过，4 = 不通过。
///
/// 为什么由主程序做：ps1 以前调 payload 里那个还没验过的后端 —— 等于让嫌疑人
/// 自己出示无罪证明，payload 和后端一起换掉就绕过了。这里先用内置公钥验「自己
/// 这套安装」，通过之后 resources/backend_server.exe 才是官方文件，再由它去验
/// 增量包。整条链：主程序 → 清单 → 后端 → 增量包。
fn run_verify_delta(dir: &str, result: Option<&str>) -> i32 {
    let mut notes: Vec<String> = Vec::new();
    let mut rc = 4;
    let verified = match verify_release() {
        Ok(p) => Some(p),
        Err(why) => {
            notes.push(format!("自身完整性校验失败，拒绝验增量包：{why}"));
            None
        }
    };
    if let Some(verified) = verified {
        let mut cands: Vec<std::path::PathBuf> = Vec::new();
        if !verified.as_os_str().is_empty() {
            cands.push(verified);
        }
        if let Ok(exe) = std::env::current_exe() {
            if let Some(d) = exe.parent() {
                cands.push(d.join("resources").join("backend_server.exe"));
                cands.push(d.join("backend_server.exe"));
            }
        }
        let mut done = false;
        for c in cands {
            if !c.is_file() {
                continue;
            }
            match std::process::Command::new(&c)
                .arg("--verify-delta")
                .arg(dir)
                .output()
            {
                Ok(out) => {
                    rc = out.status.code().unwrap_or(4);
                    for blob in [&out.stdout, &out.stderr] {
                        let t = String::from_utf8_lossy(blob).to_string();
                        for line in t.lines() {
                            if !line.trim().is_empty() {
                                notes.push(line.trim().to_string());
                            }
                        }
                    }
                    done = true;
                    break;
                }
                Err(e) => notes.push(format!("调不动 {}：{e}", c.display())),
            }
        }
        if !done && notes.is_empty() {
            notes.push("找不到可用的后端去验增量包".to_string());
        }
    }
    for n in &notes {
        eprintln!("[小鲸鱼] {n}");
    }
    // v1.9.2：Windows 上 GUI 子系统程序没有控制台，PowerShell 里
    // $LASTEXITCODE 会是 null —— 所以把结论另写一份文件，ps1 读文件判定。
    if let Some(rp) = result {
        let mut body = format!("{rc}\n");
        for n in &notes {
            body.push_str(n);
            body.push('\n');
        }
        if let Err(e) = std::fs::write(rp, body) {
            eprintln!("[小鲸鱼] 结果文件写不进去 {}：{e}", rp);
        }
    }
    rc
}

/// v1.9.6：自检入口 `小鲸鱼.exe --verify-self --result-file <文件>`。
/// GUI 程序拿不到控制台，所以结论写进结果文件（第一行是退出码）。
fn run_verify_self(result: Option<&str>) -> i32 {
    let (rc, msg) = match verify_release() {
        Ok(p) => {
            let who = if p.as_os_str().is_empty() {
                "（调试构建，未校验）".to_string()
            } else {
                format!("后端：{}", p.display())
            };
            (0, format!("自身完整性校验通过（{who}）"))
        }
        Err(e) => (4, format!("自身完整性校验失败：{e}")),
    };
    if let Some(rf) = result {
        let _ = std::fs::write(rf, format!("{rc}\r\n{msg}\r\n"));
    }
    rc
}


#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // v1.9.2（N-02）：增量包验签入口，必须在所有 GUI 初始化之前 ——
    // 它要能独立跑完并退出，不建窗口、也不去抢单实例锁。
    if let Some(pos) = std::env::args().position(|a| a == "--verify-delta") {
        let dir = std::env::args().nth(pos + 1).unwrap_or_default();
        let result = std::env::args()
            .position(|a| a == "--result-file")
            .and_then(|p| std::env::args().nth(p + 1));
        std::process::exit(run_verify_delta(&dir, result.as_deref()));
    }
    // v1.9.6：自检入口（排查「装完说被篡改」这类问题用，不建窗口）。
    if std::env::args().any(|a| a == "--verify-self") {
        let result = std::env::args()
            .position(|a| a == "--result-file")
            .and_then(|p| std::env::args().nth(p + 1));
        std::process::exit(run_verify_self(result.as_deref()));
    }
    // v1.5.2（QA No-Go 修复一）：单实例锁。
    // 两个实例会去抢同一个 WebView2 user data 目录，第二个会直接崩在
    // `failed to create webview: HRESULT(0x8007139F)` —— 用户看到的就是「双击没反应/闪退」。
    // 拿到锁的是主实例；拿不到的干净退出，不再让 WebView2 崩。
    let _instance_guard = match single_instance_guard() {
        Some(g) => g,
        None => {
            // v1.8.6（QA P0-4）：拿不到锁的时候别再「什么都不说就走」。
            // 上一次崩溃/被强杀会留下锁和端口文件 —— 用户再双击就是「点了没反应」。
            let alive = read_port_file(9000)
                .map(|u| health_ok(&u, SPAWN_PING_MS))
                .unwrap_or(false);
            if alive {
                eprintln!("[tauri] 已经有一个小鲸鱼在运行，本次直接退出（避免抢 WebView2 目录崩掉）。");
            } else {
                let msg = "小鲸鱼好像还开着，但它的后端已经没响应了。\n\n\
                           这通常是上一次崩溃、被强制结束，或者更新过程中被打断留下的残留。\n\n\
                           处理办法：打开任务管理器，把「小鲸鱼.exe」和「backend_server.exe」\n\
                           都结束掉，然后再打开一次小鲸鱼。";
                write_boot_error("单实例锁被占，但旧实例的后端探测不到响应（疑似上次崩溃残留）");
                message_box(msg);
            }
            return;
        }
    };

    // v1.9.1：拉起后端之前先验包。验不过直接弹窗退出，绝不 spawn ——
    // 否则跑起来的是被换过的 backend_server.exe，验签也就成了摆设。
    match verify_release() {
        Ok(p) => {
            // 记下「验过的那个后端」，spawn 之前要拿它比对（v1.9.2 / N-01）
            let _ = VERIFIED_BACKEND.set(p);
        }
        Err(why) => {
            write_boot_error(&format!("发行包完整性校验失败：{why}"));
            message_box(&format!(
                "小鲸鱼的程序文件被改过，为了安全不启动。\n\n{why}\n\n\
                 如果你没动过它，说明这个包不是从官方渠道来的，或者被别的程序改过。\n\
                 请从官方渠道重新下载完整安装包 / 绿色版覆盖安装。\n\n\
                 如果您刚刚进行了大版本跨代升级，可能是旧版文件残留导致。\
                 请尝试重新下载【完整安装包】覆盖，或使用【恢复工具】。"
            ));
            return;
        }
    }

    // 关键：先同步把后端拉起来，再建窗口 —— 这样前端一加载就能拿到真实端口
    let backend = spawn_backend();

    // v1.5.3（QA P0-2）：后端连续起不来就**立刻退出**，不建窗口、不死等。
    // 以前这里会照样建窗口、前端干等 45 秒「正在连接本地服务…」，
    // 而单实例锁一直被占着 —— 用户再双击一次就是「点了没反应」。
    // 退出时锁随进程释放，用户再点一次就能重来。
    if backend.base.is_empty() {
        // v1.8.7（QA P0）：光说三种可能没用 —— 把 boot.err.log 里真正那一行抄出来。
        let last_err = last_boot_error();
        let mut msg = String::from(
            "后端服务没能启动（连续 3 次）。\n\n\
            如果是刚刚更新过，多半是更新包损坏或没更新完整 ——\n\
            请重新下载完整安装包 / 绿色版覆盖安装。其它常见原因：\n\
            杀毒软件拦了 resources\\backend_server.exe；\n\
            5701~5709 端口被别的程序占着。"
        );
        if !last_err.is_empty() {
            msg.push_str("\n\n真实报错（boot.err.log 最后一行）：\n");
            msg.push_str(&last_err);
        } else {
            msg.push_str("\n\n详细原因已记到：%LOCALAPPDATA%\\xiaojingyu-console\\boot.err.log");
        }
        write_boot_error("后端连续 3 次没能启动，直接退出（不占单实例锁）");
        message_box(&msg);
        std::process::exit(1);
    }

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // v1.2.0 安装向导：目录选择用系统原生对话框
        // （前端通过 invoke('plugin:dialog|open', {options:{directory:true}}) 调）
        .plugin(tauri_plugin_dialog::init())
        .manage(backend)
        .manage(UiState::new())
        .invoke_handler(tauri::generate_handler![
            backend_info,
            backend_restart,
            set_decorations,
            get_decorations,
            set_tray,
            app_relaunch,
            app_quit
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                // v1.3.0：开了托盘就「藏起来」，Agent 继续跑；没开才真退出
                let tray_on = *window.state::<UiState>().tray.lock().unwrap();
                if tray_on {
                    api.prevent_close();
                    let _ = window.hide();
                    return;
                }
                let state = window.state::<Backend>();
                stop_backend(&state);
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app.try_state::<Backend>() {
                    stop_backend(&state);
                }
            }
        });
}
