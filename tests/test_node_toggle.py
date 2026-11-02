import ctypes
import plistlib
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import node_toggle as nt  # noqa: E402


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_corrupt_config_is_kept_aside(tmp_path, monkeypatch, text):
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(nt, "config_path", lambda demo: path)
    assert nt.load_config(False) == nt.default_config()
    assert (tmp_path / "config.bad.json").read_text(encoding="utf-8") == text


def test_missing_config_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(nt, "config_path", lambda demo: tmp_path / "config.json")
    assert nt.load_config(False) == nt.default_config()
    assert not list(tmp_path.iterdir())


def _window(toggle_keys=(), layer_keys=None):
    toggles = [{"hotkey": hk} for hk in toggle_keys]
    return SimpleNamespace(cfg={"toggles": toggles, "layer_keys": layer_keys or {}})


def test_key_owner_without_vk_compares_key_text():
    w = _window([None, {"mods": [], "vk": None, "text": "B"}])
    assert nt.MainWindow._key_owner(w, {"mods": [], "vk": None, "text": "A"}, 0) is None
    assert nt.MainWindow._key_owner(w, {"mods": [], "vk": None, "text": "B"}, 0) == ("scene", 1)


def test_key_owner_with_vk():
    w = _window([None], {"clip:1": {"hotkey": {"mods": ["ctrl"], "vk": 65, "text": "Ctrl+A"}}})
    assert nt.MainWindow._key_owner(w, {"mods": ["ctrl"], "vk": 65, "text": "Ctrl+A"}, 0) == ("layer", "clip:1")
    assert nt.MainWindow._key_owner(w, {"mods": [], "vk": 65, "text": "A"}, 0) is None


def test_mac_plist_escapes_paths(tmp_path, monkeypatch):
    f = tmp_path / "standby.plist"
    cmd = ["/A&B/python", "/x<y>/node_toggle.py"]
    monkeypatch.setattr(nt, "IS_WIN", False)
    monkeypatch.setattr(nt, "IS_MAC", True)
    monkeypatch.setattr(nt, "_autostart_file", lambda: f)
    monkeypatch.setattr(nt, "_launch_cmd", lambda *extra: [*cmd, *extra])
    nt.set_autostart(True)
    data = plistlib.loads(f.read_bytes())
    assert data["ProgramArguments"] == [*cmd, "--standby"]
    assert data["Label"] == "com.dakillswitch.standby" and data["RunAtLoad"] is True


@pytest.mark.skipif(not nt.IS_WIN, reason="Windows process watcher")
def test_watch_survives_failed_open_process(monkeypatch):
    class NoOpen:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        @staticmethod
        def OpenProcess(*args):
            return 0

    monkeypatch.setattr(ctypes.windll, "kernel32", NoOpen(ctypes.windll.kernel32))
    alive = {"pid": 4242}
    monkeypatch.setattr(nt.ResolveWatch, "_find_pid", staticmethod(lambda: alive["pid"]))
    gone = threading.Event()
    watch = nt.ResolveWatch(gone.set)
    watch.pid = 4242
    threading.Thread(target=watch._watch, daemon=True).start()
    assert not gone.wait(1.5)
    alive["pid"] = None
    assert gone.wait(3)
    assert watch.lost
