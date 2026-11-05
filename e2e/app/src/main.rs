#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    if std::env::args_os().nth(1).as_deref()
        == Some(std::ffi::OsStr::new(
            donutbrowser_lib::vault_kdf::WORKER_ARGUMENT,
        ))
    {
        std::process::exit(donutbrowser_lib::vault_kdf::run_worker());
    }
    donutbrowser_lib::run_with_builder(|builder| builder.plugin(tauri_wd::init()));
}
