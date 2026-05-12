use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager};

fn toggle_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let visible = window.is_visible().unwrap_or(false);
        if visible {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

fn setup_tray(app: &AppHandle) {
    let show_item = MenuItem::with_id(app, "show", "Show", true, None::<&str>).expect("create show item");
    let mute_item = MenuItem::with_id(app, "mute", "Mute", true, None::<&str>).expect("create mute item");
    let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>).expect("create quit item");

    let menu = Menu::with_items(app, &[&show_item, &mute_item, &quit_item]).expect("create tray menu");

    let _ = TrayIconBuilder::with_id("atlas-tray")
        .icon(app.default_window_icon().expect("default icon").clone())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_main_window(tray.app_handle());
            }
        })
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => toggle_main_window(app),
            "mute" => {
                let _ = app.emit("tray-mute", ());
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            setup_tray(app.handle());
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
