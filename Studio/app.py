import os
import sys

from docurandom.single_instance import SingleInstanceGuard, focus_existing_window
from docurandom.storage import app_data_dir, configure_managed_environment


FOCUS_RETRY_ATTEMPTS = 12
FOCUS_RETRY_DELAY = 0.075


def _run_flow_backend() -> None:
    """Run the bundled local Flow bridge in a background child process."""
    os.environ.setdefault("FLOW_OUTPUT_DIR", str(app_data_dir() / "flow-output"))
    os.environ.setdefault("OPENAI_API_HOST", "127.0.0.1")
    os.environ.setdefault("OPENAI_API_PORT", "8001")
    import uvicorn
    from flow_server.api import app as flow_app

    uvicorn.run(
        flow_app,
        host="127.0.0.1",
        port=8001,
        access_log=False,
        # The packaged GUI executable is built with --windowed, so
        # sys.stdout/sys.stderr are None. Uvicorn's default colour formatter
        # calls sys.stdout.isatty() and crashes before the backend can bind.
        log_config=None,
        timeout_graceful_shutdown=3,
    )


def _run_ui() -> None:
    # Defer the heavier Tk/Pillow imports so a second launch can foreground
    # the existing window and exit immediately.
    from docurandom.ui import run

    run()


def main() -> None:
    configure_managed_environment()
    from docurandom.diagnostics import install_runtime_diagnostics, record_exception

    diagnostics_channel = "flow-backend" if "--flow-backend" in sys.argv[1:] else "application"
    install_runtime_diagnostics(diagnostics_channel)
    instance = SingleInstanceGuard.acquire()
    if instance.already_running:
        try:
            # The mutex is created before Tk and the title exist. Retry for less
            # than one second so a near-simultaneous second launch can still
            # restore the first window instead of silently missing it.
            focus_existing_window(
                retry_attempts=FOCUS_RETRY_ATTEMPTS,
                retry_delay=FOCUS_RETRY_DELAY,
            )
        finally:
            instance.close()
        return

    if instance.acquisition_failed:
        # CreateMutex can be denied across Windows integrity levels. Look for an
        # existing exact-title UI before deliberately failing open.
        existing = focus_existing_window(
            retry_attempts=FOCUS_RETRY_ATTEMPTS,
            retry_delay=FOCUS_RETRY_DELAY,
        )
        if existing.handled:
            instance.close()
            return

    try:
        _run_ui()
    except Exception as exc:
        record_exception("application-main", exc)
        raise
    finally:
        instance.close()


if __name__ == "__main__":
    if "--flow-backend" in sys.argv[1:]:
        configure_managed_environment()
        from docurandom.diagnostics import install_runtime_diagnostics, record_exception

        install_runtime_diagnostics("flow-backend")
        try:
            _run_flow_backend()
        except Exception as exc:
            record_exception("flow-backend-main", exc)
            raise
    else:
        main()
