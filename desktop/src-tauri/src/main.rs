use tauri::Manager;

fn main() {
    tauri::Builder::default()
        .setup(|app| {
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
        .run(tauri::generate_context!())
        .expect("error while running Vibe-Dashcam");
}
