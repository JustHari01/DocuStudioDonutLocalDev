from unittest.mock import patch

import app


def test_flow_backend_disables_uvicorn_console_log_config(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "app_data_dir", lambda: tmp_path)

    with patch("uvicorn.run") as run_mock:
        app._run_flow_backend()

    _, kwargs = run_mock.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8001
    assert kwargs["access_log"] is False
    assert kwargs["log_config"] is None
