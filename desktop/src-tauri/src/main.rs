use std::{
    env, fs,
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
};
use tauri::Manager;

struct CoreProcess(Mutex<Option<Child>>);

fn main() {
    tauri::Builder::default()
        .manage(CoreProcess(Mutex::new(None)))
        .setup(|app| {
            start_core(app);
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(Some(monitor)) = window.current_monitor() {
                    let work_area = monitor.work_area();
                    let window_size = window.outer_size()?;
                    let margin = 18;
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
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                stop_core(app);
            }
        });
}

fn start_core(app: &tauri::App) {
    if TcpStream::connect(("127.0.0.1", 8080)).is_ok() {
        return;
    }
    let Some(script) = core_script_path(app) else {
        return;
    };
    if let Ok(child) = spawn_python_core(&script) {
        *app.state::<CoreProcess>().0.lock().expect("core process lock") = Some(child);
    }
}

fn stop_core(app: &tauri::AppHandle) {
    if let Some(mut child) = app
        .state::<CoreProcess>()
        .0
        .lock()
        .expect("core process lock")
        .take()
    {
        let _ = child.kill();
    }
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
