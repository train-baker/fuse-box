import ctypes
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import fuse_box as nt


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


def test_script_shortcut_round_trip(tmp_path, monkeypatch):
    f = tmp_path / "Scripts" / "Utility" / "_Fuse Box.py"
    cmd = ["C:\\A&B\\python's\\pythonw.exe", "C:\\x y\\fuse_box.py"]
    monkeypatch.setattr(nt, "_script_shortcut_file", lambda: f)
    monkeypatch.setattr(nt, "_launch_cmd", lambda *extra: [*cmd, *extra])
    nt.set_script_shortcut(True)
    scope = {}
    exec(compile(f.read_text(encoding="utf-8").split("\nif sys.platform")[0], str(f), "exec"), scope)
    assert scope["CMD"] == cmd  # the launcher starts exactly this command
    nt.set_script_shortcut(False)
    assert not f.exists()
    nt.set_script_shortcut(False)  # removing twice is fine


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


@pytest.mark.parametrize("text, parts", [
    ("Ctrl+Alt+Shift+S", ["Ctrl", "Alt", "Shift", "S"]),
    ("F4", ["F4"]),
    ("Ctrl++", ["Ctrl", "+"]),
    ("+", ["+"]),
])
def test_key_parts(text, parts):
    assert nt.key_parts(text) == parts


PRESET = Path(__file__).resolve().parent / "resolve_keyboard_preset.xml"  # a real custom Resolve preset


def _resolve_prefs(tmp_path, mapping, preset=None):
    prefs = tmp_path / "prefs"
    prefs.mkdir()
    custom = f"<CustomKeyboardPreset>{preset}</CustomKeyboardPreset>" if preset else ""
    (prefs / "config.user.xml").write_text(f"<KeyboardMappings>{mapping}</KeyboardMappings>{custom}")
    return prefs


def test_custom_preset_is_saved_to_shortcuts_folder(tmp_path, monkeypatch):
    prefs = _resolve_prefs(tmp_path, -1, "rob-dr-ks-win-260821+2053")
    (prefs / "keyboard.preset.xml").write_bytes(PRESET.read_bytes())
    monkeypatch.setattr(nt, "_resolve_pref_dirs", lambda: [prefs])
    monkeypatch.setattr(nt, "SHORTCUTS_DIR", tmp_path / "shortcuts")
    keys = nt.ResolveShortcuts()
    keys.load()
    assert keys.status == "ok" and keys.by_combo
    assert (tmp_path / "shortcuts" / "rob-dr-ks-win-260821+2053.xml").read_bytes() == PRESET.read_bytes()


@pytest.mark.parametrize("mapping", [0, 3])
def test_builtin_layout_falls_back_to_saved_preset(tmp_path, monkeypatch, mapping):
    prefs = _resolve_prefs(tmp_path, mapping)
    saved = tmp_path / "shortcuts"
    saved.mkdir()
    (saved / "my preset.xml").write_bytes(PRESET.read_bytes())
    monkeypatch.setattr(nt, "_resolve_pref_dirs", lambda: [prefs])
    monkeypatch.setattr(nt, "SHORTCUTS_DIR", saved)
    keys = nt.ResolveShortcuts()
    keys.load()
    assert keys.status == "saved" and keys.source == "my preset" and keys.by_combo


def test_builtin_layout_without_saved_preset(tmp_path, monkeypatch):
    prefs = _resolve_prefs(tmp_path, 0)
    monkeypatch.setattr(nt, "_resolve_pref_dirs", lambda: [prefs])
    monkeypatch.setattr(nt, "SHORTCUTS_DIR", tmp_path / "shortcuts")
    keys = nt.ResolveShortcuts()
    keys.load()
    assert keys.status == "builtin" and not keys.by_combo


def test_plus_key_hotkey_matches_resolve_combo():
    from PySide6.QtCore import Qt
    resolve = nt.ResolveShortcuts._combo(int(Qt.Key_Plus) | 0x04000000)  # Ctrl + "+" as Resolve stores it
    assert nt.ResolveShortcuts.hotkey_combo({"mods": ["ctrl"], "text": "Ctrl++"}) == resolve


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("state", [True, False])
def test_clicking_node_label_name_sets_it_back_to_armed(qapp, state):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel
    field = nt.TagsField()
    field.set_tags([{"name": "CP-LOOK", "on": state, "scope": "P", "project": "x"}])
    changed = []
    field.changed.connect(lambda: changed.append(1))
    field.show()
    chip = field.findChild(nt.TagChip)
    QTest.mouseClick(chip.findChild(QLabel, "tagname"), Qt.LeftButton)
    assert field.tags[0]["on"] is None and changed == [1]
