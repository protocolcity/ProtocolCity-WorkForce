"""CLI board-discovery story — the door gets found without a browser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workforce import cli  # noqa: E402
from workforce import board  # noqa: E402


def test_open_print_emits_board_url(capsys):
    rc = cli.main(["open", "--print"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "http://127.0.0.1:%d" % board.DEFAULT_PORT


def test_open_print_honors_port_override(capsys):
    rc = cli.main(["open", "--print", "--port", "9111"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "http://127.0.0.1:9111"


def test_open_launches_default_browser(capsys, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append(argv)

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    rc = cli.main(["open"])
    out = capsys.readouterr().out.strip()

    assert rc == 0
    assert out == "http://127.0.0.1:%d" % board.DEFAULT_PORT  # URL always printed
    assert len(calls) == 1
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    assert calls[0] == [opener, out]


def test_open_opener_missing_is_nonfatal_and_url_survives(capsys, monkeypatch):
    def boom(argv, **kw):
        raise OSError("no such opener")

    monkeypatch.setattr("subprocess.Popen", boom)
    rc = cli.main(["open"])
    captured = capsys.readouterr()

    assert rc == 1
    # The URL still reached stdout — the point of the command survives a dead opener.
    assert captured.out.strip() == "http://127.0.0.1:%d" % board.DEFAULT_PORT
    assert "could not launch" in captured.err


def test_daemon_plist_stdout_is_clean_xml_url_hint_on_stderr(capsys):
    rc = cli.main(["daemon-plist"])
    captured = capsys.readouterr()

    assert rc == 0
    # stdout is the plist and nothing else — safe to redirect into a .plist file.
    assert captured.out.startswith("<?xml")
    assert captured.out.rstrip().endswith("</plist>")
    assert "http://127.0.0.1" not in captured.out
    # The door is mentioned, on stderr.
    assert "http://127.0.0.1:%d" % board.DEFAULT_PORT in captured.err


def test_daemon_plist_bakes_data_dir_when_env_set(tmp_path, monkeypatch, capsys):
    """WORKFORCE_DATA_DIR is carried into the plist so the daemon is self-contained."""
    monkeypatch.setenv("WORKFORCE_DATA_DIR", str(tmp_path))
    rc = cli.main(["daemon-plist"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.startswith("<?xml")
    # data_dir appears as both WorkingDirectory and an EnvironmentVariables entry.
    assert str(tmp_path) in captured.out
    assert "WORKFORCE_DATA_DIR" in captured.out
