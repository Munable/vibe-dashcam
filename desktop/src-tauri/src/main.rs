#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env, fs,
    io::{Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager, State};

struct CoreProcess(Mutex<Option<Child>>);
struct CoreStatus(Mutex<String>);

fn main() {
    tauri::Builder::default()
        .manage(CoreProcess(Mutex::new(None)))
        .manage(CoreStatus(Mutex::new(String::from("starting"))))
        .invoke_handler(tauri::generate_handler![
            core_launch_status,
            set_window_always_on_top
        ])
        .setup(|app| {
            start_core(app);
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(Some(monitor)) = window.current_monitor() {
                    let work_area = monitor.work_area();
                    let window_size = window.outer_size()?;
                    let margin = 24;
                    let x = work_area.position.x
                        + work_area.size.width as i32
                        - window_size.width as i32
                        - margin;
                    let y = work_area.position.y
                        + work_area.size.height as i32
                        - window_size.height as i32
                        - margin;
                    window.set_position(tauri::PhysicalPosition::new(x, y))?;
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Vibe-Dashcam")
        .run(|app, event| match event {
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => stop_core(app),
            _ => {}
        });
}

#[tauri::command]
fn core_launch_status(status: State<CoreStatus>) -> String {
    status.0.lock().expect("core status lock").clone()
}

#[tauri::command]
fn set_window_always_on_top(app: AppHandle, enabled: bool) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| String::from("window_missing"))?;
    window.set_always_on_top(enabled).map_err(|error| error.to_string())
}

fn start_core(app: &tauri::App) {
    if core_health_ok() {
        set_core_status(app, "ok");
        return;
    }
    if TcpStream::connect(("127.0.0.1", 8080)).is_ok() {
        set_core_status(app, "port_occupied");
        return;
    }
    let program = if cfg!(debug_assertions) {
        core_script_path(app)
    } else {
        core_binary_path(app)
    };
    let Some(program) = program.filter(|path| path.exists()) else {
        set_core_status(app, "core_missing");
        return;
    };
    let child = if cfg!(debug_assertions) {
        spawn_python_core(&program)
    } else {
        spawn_binary_core(&program)
    };
    match child {
        Ok(child) => {
            *app.state::<CoreProcess>().0.lock().expect("core process lock") = Some(child);
            if wait_for_core_health(Duration::from_secs(15)) {
                set_core_status(app, "ok");
            } else {
                stop_core(app.handle());
                set_core_status(app, "core_start_failed");
            }
        }
        Err(_) => set_core_status(app, "core_start_failed"),
    };
}

fn set_core_status(app: &tauri::App, value: &str) {
    *app.state::<CoreStatus>().0.lock().expect("core status lock") = value.to_string();
}

fn core_health_ok() -> bool {
    let Ok(mut stream) = TcpStream::connect(("127.0.0.1", 8080)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let request = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n";
    if stream.write_all(request).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    let healthy_status = response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200");
    healthy_status && response.contains("\"app\"") && response.contains("\"vibe-dashcam\"")
}

fn wait_for_core_health(timeout: Duration) -> bool {
    let started = Instant::now();
    while started.elapsed() < timeout {
        if core_health_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(150));
    }
    false
}

fn stop_core(app: &tauri::AppHandle) {
    if let Some(mut child) = app
        .state::<CoreProcess>()
        .0
        .lock()
        .expect("core process lock")
        .take()
    {
        kill_core_process_tree(&mut child);
        let _ = child.wait();
    }
}

#[cfg(windows)]
fn kill_core_process_tree(child: &mut Child) {
    let mut command = Command::new("taskkill");
    command
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000);
    let _ = command.status();
}

#[cfg(not(windows))]
fn kill_core_process_tree(child: &mut Child) {
    let _ = child.kill();
}

fn core_script_path(app: &tauri::App) -> Option<PathBuf> {
    if cfg!(debug_assertions) {
        let exe = std::env::current_exe().ok()?;
        return exe
            .parent()?
            .ancestors()
            .nth(4)
            .map(|root| root.join("vibe_dashcam").join("vibe_dashcam.py"));
    }
    app.path().resource_dir().ok().map(|dir| dir.join("vibe_dashcam.py"))
}

fn core_binary_path(app: &tauri::App) -> Option<PathBuf> {
    let name = if cfg!(windows) { "vibe-dashcam-core.exe" } else { "vibe-dashcam-core" };
    app.path().resource_dir().ok().map(|dir| {
        let onedir = dir.join("vibe-dashcam-core").join(name);
        if onedir.exists() {
            onedir
        } else {
            dir.join(name)
        }
    })
}

fn spawn_binary_core(program: &Path) -> std::io::Result<Child> {
    let mut command = Command::new(program);
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn()
}

fn spawn_python_core(script: &Path) -> std::io::Result<Child> {
    let mut last_error = None;
    for program in python_candidates() {
        match spawn_with_python(&program, script) {
            Ok(child) => return Ok(child),
            Err(error) => last_error = Some(error),
        }
    }
    Err(last_error.unwrap_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "python not found")))
}

fn python_candidates() -> Vec<PathBuf> {
    let mut candidates = vec![PathBuf::from("python"), PathBuf::from("py")];
    if let Ok(system_root) = env::var("SystemRoot") {
        candidates.push(Path::new(&system_root).join("py.exe"));
    }
    candidates.push(PathBuf::from(r"C:\Windows\py.exe"));
    add_python_dirs(Path::new(r"C:\"), &mut candidates);
    if let Ok(local_app_data) = env::var("LOCALAPPDATA") {
        add_python_dirs(&Path::new(&local_app_data).join("Programs").join("Python"), &mut candidates);
    }
    candidates
}

fn add_python_dirs(root: &Path, candidates: &mut Vec<PathBuf>) {
    if let Ok(entries) = fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
                continue;
            };
            if name.to_ascii_lowercase().starts_with("python") {
                candidates.push(path.join("python.exe"));
            }
        }
    }
}

fn spawn_with_python(program: &Path, script: &Path) -> std::io::Result<Child> {
    let mut command = Command::new(program);
    if program.file_stem().and_then(|value| value.to_str()) == Some("py") {
        command.arg("-3");
    }
    command
        .arg("-B")
        .arg(script)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    command.spawn()
}
