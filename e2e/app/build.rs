fn main() {
    // The app dependency uses Tauri's static Windows runtime. Its linker
    // arguments apply only to its own binaries, so the E2E executable must
    // select the same runtime when it links that library.
    if std::env::var("TARGET").is_ok_and(|target| target.contains("windows-msvc")) {
        for library in [
            "libvcruntimed.lib",
            "vcruntime.lib",
            "vcruntimed.lib",
            "libcmtd.lib",
            "msvcrt.lib",
            "msvcrtd.lib",
            "libucrt.lib",
            "libucrtd.lib",
        ] {
            println!("cargo:rustc-link-arg=/NODEFAULTLIB:{library}");
        }
        for library in ["libcmt.lib", "libvcruntime.lib", "ucrt.lib"] {
            println!("cargo:rustc-link-arg=/DEFAULTLIB:{library}");
        }
        // As a separate binary, this harness also needs the application's
        // common-controls v6 manifest before its Tauri window can start.
        let manifest = std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap())
            .join("../../src-tauri/app.manifest");
        println!("cargo:rustc-link-arg=/MANIFEST:EMBED");
        println!("cargo:rustc-link-arg=/MANIFESTINPUT:{}", manifest.display());
        println!("cargo:rerun-if-changed={}", manifest.display());
    }
}
