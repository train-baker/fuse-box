#!/usr/bin/env python3
"""
DaKillswitch (formerly Node Toggle) — switch DaVinci Resolve color nodes on and off with global hotkeys.

  * 5 independent toggles (columns), each with its own shortcut
  * each toggle can hold nodes from Pre-Group, every clip layer (L1, L2, L3 …),
    Post-Group and Timeline — or a whole layer at once
  * scenes, layer keys and node tags act on every enabled clip by default
    (right-click a state chip for "This clip only")
  * layer shortcuts: one key per layer / group to switch it off and on whole
  * node labels: type a Resolve node label (e.g. NR); it starts ARMED (grey), its ON / OFF boxes
    switch every node with that label on every layer of every enabled clip; turns red when a scene
    picks the same nodes with the opposite state
  * turning a layer / group back ON keeps nodes off that should stay off
    (OFF node labels and nodes picked in OFF scenes)
  * warns when a key you assign is already a Resolve shortcut (reads your active
    custom keyboard preset)
  * node picker: click and drag over nodes to tick or untick several at once
  * 3 preset slots, always-on-top status overlay (click a row to toggle)
  * "Start with Resolve": waits in the background from login; when a project opens it shows the overlay
    and disappears when it quits; a second launch just brings the open window forward

Requirements: DaVinci Resolve Studio 19.1+ with
  Preferences > System > General > "External scripting using" = Local,
  Python 3.10+ (64-bit), and:  pip install -r requirements.txt

Run:   python node_toggle.py            (connects to Resolve)
       python node_toggle.py --standby  (wait for Resolve; used by "Start with Resolve")
       python node_toggle.py --demo     (fake Resolve, to try the UI)

Resolve's API can set a node's enabled state but cannot read it, so the app
remembers each toggle's state itself. If you flip nodes by hand inside Resolve,
right-click a toggle's state button and use Force ON / Force OFF to resync.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPalette, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPushButton, QSizePolicy, QStyle, QStyleOptionButton, QVBoxLayout, QWidget,
)

APP_NAME = "DaKillswitch"
APP_VERSION = "0.1.3"
NUM_TOGGLES = 5
NUM_PRESETS = 3
MAX_LAYERS = 16
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Palette sampled from Resolve 21's Fairlight mixer
BG = "#28282e"          # panel
BG_DARK = "#212126"     # headers, window frame
BG_GRID = "#24242a"     # strip area
FIELD = "#1f1f1f"       # input fields
LINE = "#090909"        # dividers and field borders
BTN = "#3b3b3f"         # buttons (R / S / M, In)
BTN_HOVER = "#46464b"
TEXT = "#b8b8ba"        # names
TEXT_DIM = "#8e9092"    # row labels
TEXT_FAINT = "#5a5a60"  # empty / disabled
PLUS_GREY = "#646468"   # "+" between key names: between TEXT_DIM and BTN
TEXT_BRIGHT = "#ffffff"  # panel title
ACCENT = "#c3931d"      # Resolve orange (bus names, active)
BLUE = "#156284"        # selection (assigned bus)
GREEN = "#448f64"       # track colour stripe
RED = "#e64b3d"         # record

ON_COLOR = QColor(ACCENT)
IDLE_COLOR = QColor(BTN)
CONN_COLOR = QColor(GREEN)   # connected to Resolve

# A "slot" is one node graph: "preclip", "clip:1", "clip:2", …, "postclip", "timeline".
SLOT_RE = re.compile(r"^(preclip|postclip|timeline|clip:\d+)$")


def clip_slot(layer: int) -> str:
    return f"clip:{layer}"


def slot_layer(slot: str) -> int | None:
    return int(slot[5:]) if slot.startswith("clip:") else None


def slot_title(slot: str) -> str:
    if slot == "preclip":
        return "Pre-Group"
    if slot == "postclip":
        return "Post-Group"
    if slot == "timeline":
        return "Timeline"
    return f"Layer {slot_layer(slot)}"



# ----------------------------------------------------------------------------- config

def config_path(demo: bool) -> Path:
    if IS_WIN:
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "NodeToggle" / ("config-demo.json" if demo else "config.json")


def default_toggle(i: int) -> dict:
    return {"name": f"Scene {i + 1}", "hotkey": None, "on": None, "one_clip": False, "sel": {}}  # on: None = ARMED (state unknown)


def clean_sel(sel: dict) -> dict:
    out = {}
    for k, v in (sel or {}).items():
        if k == "clip":  # configs from the first version
            k = "clip:1"
        if SLOT_RE.match(k) and v:
            out[k] = [dict(s) for s in v]
    return out


def clean_toggle(i: int, t: dict) -> dict:
    base = default_toggle(i)
    base.update({k: t[k] for k in ("name", "hotkey", "on", "one_clip") if k in t})
    if re.fullmatch(r"T\d+", str(base["name"])):  # old default names T1, T2 … → Scene 1, Scene 2 …
        base["name"] = f"Scene {base['name'][1:]}"
    if base["on"] is not None:
        base["on"] = bool(base["on"])
    base["sel"] = clean_sel(t.get("sel"))
    return base


def default_config() -> dict:
    return {
        "toggles": [default_toggle(i) for i in range(NUM_TOGGLES)],
        "presets": {str(i + 1): None for i in range(NUM_PRESETS)},
        "overlay": {"visible": True, "x": None, "y": None},
        "focus_only": True,
        "post_layer": True,   # mirrors Resolve's "Post Group Node Stack Layer" (not readable by scripts)
        "pinned": False,      # keep the main window above Resolve
        "window": {"x": None, "y": None},  # main window position, restored next launch
        "layer_keys": {},     # slot or "tags" -> {"hotkey", "on", "one_clip"}
        "tags": [],           # node labels: {"name", "on": True|False|None (ARMED), "scope": "F"|"P", "project"}
        "layer_names": {},    # "1" -> "--- L1" (Resolve's layer names aren't readable by scripts)
    }


def load_config(demo: bool) -> dict:
    cfg = default_config()
    path = config_path(demo)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return cfg
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):  # corrupt: keep it aside so the next save doesn't overwrite it
        with contextlib.suppress(OSError):
            path.replace(path.with_name(path.stem + ".bad.json"))
        return cfg
    for i, t in enumerate(data.get("toggles", [])[:NUM_TOGGLES]):
        cfg["toggles"][i] = clean_toggle(i, t)
    cfg["presets"].update({k: v for k, v in data.get("presets", {}).items() if k in cfg["presets"]})
    cfg["overlay"].update(data.get("overlay", {}))
    cfg["focus_only"] = bool(data.get("focus_only", True))
    cfg["post_layer"] = bool(data.get("post_layer", True))
    cfg["pinned"] = bool(data.get("pinned", False))
    if isinstance(data.get("window"), dict):
        cfg["window"].update({k: data["window"].get(k) for k in ("x", "y")})
    lk = data.get("layer_keys", {})
    if isinstance(lk, dict):
        cfg["layer_keys"] = {k: {"hotkey": v.get("hotkey"), "on": bool(v.get("on", True)),
                                 "one_clip": bool(v.get("one_clip", False))}
                             for k, v in lk.items() if isinstance(v, dict) and SLOT_RE.match(k)}
    names = data.get("layer_names", {})
    if isinstance(names, dict):
        cfg["layer_names"] = {str(k): str(v) for k, v in names.items() if str(v).strip()}
    tags = data.get("tags", [])
    if isinstance(tags, list):
        cfg["tags"] = [{"name": str(t["name"]), "on": None if t.get("on", True) is None else bool(t.get("on", True)), "scope": "F" if t.get("scope") == "F" else "P",
                        "project": t.get("project")} for t in tags if isinstance(t, dict) and t.get("name")]
    return cfg


def save_config(cfg: dict, demo: bool) -> None:
    p = config_path(demo)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # never crash over a config write
        print(f"Could not save config: {e}", file=sys.stderr)


def has_selection(t: dict) -> bool:
    return any(t["sel"].values())


# ----------------------------------------------------------------------------- Resolve

def load_resolve_module():
    """Import DaVinciResolveScript, adding Resolve's default paths if needed."""
    try:
        import DaVinciResolveScript as dvr  # type: ignore
        return dvr
    except ImportError:
        pass
    if IS_WIN:
        api = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                           "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
        lib = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    elif IS_MAC:
        api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    else:
        api = "/opt/resolve/Developer/Scripting"
        lib = "/opt/resolve/libs/Fusion/fusionscript.so"
    os.environ.setdefault("RESOLVE_SCRIPT_API", api)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", lib)
    mod_dir = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if mod_dir not in sys.path:
        sys.path.append(mod_dir)
    import DaVinciResolveScript as dvr  # type: ignore
    return dvr


def _as_list(v) -> list:
    """Resolve sometimes returns Lua tables as {1: a, 2: b} dicts."""
    if v is None:
        return []
    if isinstance(v, dict):
        return [v[k] for k in sorted(v)]
    return list(v)


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def match_nodes(nodes: list[tuple], sel: list[dict], skip=None) -> list[int]:
    """'all' = every node (minus those `skip(node)` keeps off); labelled selections match by label
    (works across clips; "ci" = case-insensitive, used by node labels); else by number."""
    out = set()
    for s in sel:
        if s.get("all"):
            return [n[0] for n in nodes if not (skip and skip(n))]
        if s.get("label"):
            if s.get("ci"):
                lab = s["label"].lower()
                out.update(n[0] for n in nodes if n[1] and n[1].lower() == lab)
            else:
                out.update(n[0] for n in nodes if n[1] == s["label"])
        elif 1 <= s.get("index", 0) <= len(nodes):
            out.add(s["index"])
    return sorted(out)


# --- reading node on/off states from a .drx grade (Resolve stores it as zstd-compressed protobuf)

def _zstd(b: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(b)
    except ImportError:
        import zstandard
        return zstandard.ZstdDecompressor().decompress(b, max_output_size=200_000_000)


def _pb_varint(b, i):
    r = sh = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << sh
        sh += 7
        if c < 0x80:
            return r, i


def _pb_parse(b) -> list[tuple]:
    i, out = 0, []
    while i < len(b):
        k, i = _pb_varint(b, i)
        f, t = k >> 3, k & 7
        if t == 0:
            v, i = _pb_varint(b, i)
        elif t == 1:
            v, i = b[i:i + 8], i + 8
        elif t == 5:
            v, i = b[i:i + 4], i + 4
        elif t == 2:
            n, i = _pb_varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            raise ValueError(f"protobuf wire type {t}")
        out.append((f, t, v))
    return out


DRX_ENABLE_PARAM = 0xC0000001  # node parameter holding the enabled flag: 2 = on, 0 = off


def drx_node_states(xml: str) -> list[tuple]:
    """[(label, on)] for every node in a .drx (clip grade and timeline grade)."""
    out = []
    for hexblob in re.findall(r"<Body>([0-9a-fA-F]+)</Body>", xml):
        raw = bytes.fromhex(hexblob)
        data = _zstd(raw[1:] if raw[:1] == b"\x81" else raw)
        top = _pb_parse(data)
        if not top or top[0][1] != 2:
            continue
        for f, t, v in _pb_parse(top[0][2]):
            if f != 7 or t != 2:
                continue
            fields = _pb_parse(v)
            label = next((vv.decode("utf-8", "replace") for ff, tt, vv in fields if ff == 6 and tt == 2), "")
            on = None
            for ff, tt, vv in fields:
                if ff != 10 or tt != 2:
                    continue
                for pf, pt, pv in _pb_parse(vv):
                    if pf != 1 or pt != 2:
                        continue
                    sub = _pb_parse(pv)
                    if any(sf == 1 and sv == DRX_ENABLE_PARAM for sf, st, sv in sub):
                        val = next((sv for sf, st, sv in sub if sf == 2 and st == 2), b"")
                        flag = next((x for xf, _, x in _pb_parse(val) if xf == 2), 0)
                        on = flag != 0
            if on is not None:
                out.append((label, on))
    return out


class ResolveBridge:
    def __init__(self, demo: bool):
        self.demo = demo
        self.resolve = None
        self.error = ""
        self._dvr = None

    def connect(self) -> bool:
        if self.demo:
            import mock_resolve
            self.resolve = mock_resolve.MockResolve()
            return True
        try:
            if self._dvr is None:
                self._dvr = load_resolve_module()
            self.resolve = self._dvr.scriptapp("Resolve")
        except Exception as e:
            self.resolve = None
            self.error = f"Couldn't load Resolve's scripting module: {e}"
            return False
        if self.resolve is None:
            self.error = ("Resolve not found. Make sure DaVinci Resolve Studio is running and "
                          "Preferences > System > General > External scripting is set to Local.")
            return False
        self.error = ""
        return True

    def version(self) -> str:
        v = _safe(getattr(self.resolve, "GetVersionString", None)) if self.resolve else None
        return f"DR {v}" if v else ""

    def context(self):
        pm = self.resolve.GetProjectManager()
        if pm is None:
            raise ConnectionError("lost connection to Resolve")
        proj = pm.GetCurrentProject()
        tl = proj.GetCurrentTimeline() if proj else None
        item = tl.GetCurrentVideoItem() if tl else None
        return proj, tl, item

    @staticmethod
    def layer_cap(proj) -> int:
        try:
            return min(int(_safe(proj.GetSetting, "nodeStackLayers") or MAX_LAYERS), MAX_LAYERS) or MAX_LAYERS
        except (TypeError, ValueError):
            return MAX_LAYERS

    def layer_graphs(self, item, proj=None, cap: int | None = None) -> list:
        """Every node stack layer of a clip, in order (L1, L2, …)."""
        cap = cap or (self.layer_cap(proj) if proj else MAX_LAYERS)
        out = []
        for n in range(1, cap + 1):
            g = _safe(item.GetNodeGraph, n)
            if g is None or _safe(g.GetNumNodes) is None:
                break
            out.append(g)
        return out

    def graphs(self, proj, tl, item) -> dict:
        """slot -> graph for the current clip / timeline."""
        g = {"preclip": None, "postclip": None, "timeline": None}
        if item:
            for n, lg in enumerate(self.layer_graphs(item, proj), 1):
                g[clip_slot(n)] = lg
            grp = _safe(item.GetColorGroup)
            if grp:
                g["preclip"] = _safe(grp.GetPreClipNodeGraph)
                g["postclip"] = _safe(grp.GetPostClipNodeGraph)
        if tl:
            g["timeline"] = _safe(tl.GetNodeGraph)
        return g

    @staticmethod
    def nodes(graph, tools: bool = True) -> list[tuple]:
        """[(index, label, [tools])] — tools only for the picker, they cost an API call per node."""
        return [(i, _safe(graph.GetNodeLabel, i) or "",
                 [str(t) for t in _as_list(_safe(graph.GetToolsInNode, i))] if tools else [])
                for i in range(1, int(_safe(graph.GetNumNodes) or 0) + 1)]

    def apply(self, graph, sel: list[dict], state: bool, skip=None) -> int:
        if graph is None or not sel:
            return 0
        if skip or any(s.get("label") for s in sel):
            nodes = self.nodes(graph, tools=False)
        else:  # plain numbers / whole graph: no label reads needed
            nodes = [(i, "", []) for i in range(1, int(_safe(graph.GetNumNodes) or 0) + 1)]
        return sum(1 for idx in match_nodes(nodes, sel, skip) if graph.SetNodeEnabled(idx, state))

    @staticmethod
    def group_once(item, groups_done: set | None):
        """The clip's color group, or None if it has none / was already done in this sweep."""
        grp = _safe(item.GetColorGroup)
        if not grp:
            return None
        name = _safe(grp.GetName)
        if groups_done is not None:
            if name in groups_done:
                return None
            groups_done.add(name)
        return grp

    def apply_item(self, item, sels: dict, state: bool, groups_done: set | None = None,
                   skip_for=None) -> int:
        """Clip layers + (once per group) pre/post-group graphs of one clip.
        skip_for(slot) -> skip function for "Entire layer" picks (or None)."""
        protect = skip_for or (lambda slot: None)
        n = 0
        for slot, sel in sels.items():
            layer = slot_layer(slot)
            if layer and sel:
                n += self.apply(_safe(item.GetNodeGraph, layer), sel, state, protect(slot))
        if sels.get("preclip") or sels.get("postclip"):
            grp = self.group_once(item, groups_done)
            if grp:
                n += self.apply(_safe(grp.GetPreClipNodeGraph), sels.get("preclip", []), state, protect("preclip"))
                n += self.apply(_safe(grp.GetPostClipNodeGraph), sels.get("postclip", []), state, protect("postclip"))
        return n

    def node_states(self, proj, tl) -> list[tuple] | None:
        """Real on/off state of the current clip's and the timeline's nodes: [(label, on)].
        Resolve's API can't read this, so a still is grabbed, its grade exported as .drx
        (which records every node's enabled flag) and the still deleted again. None = couldn't read."""
        if self.demo:
            item = tl.GetCurrentVideoItem() if tl else None
            graphs = ([lg for lg in self.layer_graphs(item, proj)] if item else []) + ([tl.GetNodeGraph()] if tl else [])
            return [(n["label"], n["enabled"]) for g in graphs for n in getattr(g, "nodes", [])]
        try:
            album = proj.GetGallery().GetCurrentStillAlbum()
            still = tl.GrabStill() if tl and album else None
            if not still:
                return None
            tmp = tempfile.mkdtemp(prefix="dakillswitch_")
            try:
                album.ExportStills([still], tmp, "dks", "drx")
                album.DeleteStills([still])
                files = [f for f in os.listdir(tmp) if f.lower().endswith(".drx")]
                if not files:
                    return None
                return drx_node_states(Path(tmp, files[0]).read_text(encoding="utf-8", errors="replace"))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:
            print(f"Couldn't read node states: {e}", file=sys.stderr)
            return None

    def all_video_items(self, tl) -> list:
        items = []
        for t in range(1, int(tl.GetTrackCount("video") or 0) + 1):
            items += _as_list(tl.GetItemListInTrack("video", t))
        return items


# ----------------------------------------------------------------------------- Resolve's own shortcuts

def _resolve_pref_dirs() -> list[Path]:
    home = Path.home()
    if IS_WIN:
        return [Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Blackmagic Design/DaVinci Resolve/Preferences"]
    if IS_MAC:
        return [home / "Library/Preferences/Blackmagic Design/DaVinci Resolve",
                home / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Preferences"]
    return [home / ".local/share/DaVinciResolve/Preferences"]


def _humanize_action(name: str) -> str:
    base = name.rpartition(".")[2].partition("{")[0]
    base = re.sub(r"^(Context|fuHotkey)_", "", base).replace("_", " ")
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)
    return base.strip().capitalize() or name


class ResolveShortcuts:
    """Reads the keyboard preset Resolve is using (only custom presets are stored on disk).

    keyboard.preset.xml holds a Qt-serialised blob: a list of presets, each
    name · byte length · version · count · [action · n · n×(k · k×keycode)] · base name.
    Key codes are Qt key + modifier bits, so they compare directly with our hotkeys."""

    MOD_BITS = (("shift", 0x02000000), ("ctrl", 0x04000000), ("alt", 0x08000000), ("meta", 0x10000000))

    def __init__(self):
        self.status = "not loaded"   # "ok" / "builtin" / "missing" / "error"
        self.by_combo: dict[tuple, list[str]] = {}
        self._stamp = None

    def load(self, force: bool = False):
        try:
            d = next((p for p in _resolve_pref_dirs() if (p / "keyboard.preset.xml").exists()
                      or (p / "config.user.xml").exists()), None)
            if d is None:
                self.status, self.by_combo = "missing", {}
                return
            files = [d / "config.user.xml", d / "keyboard.preset.xml"]
            stamp = tuple(f.stat().st_mtime if f.exists() else 0 for f in files)
            if stamp == self._stamp and not force:
                return
            self._stamp = stamp
            cfg = files[0].read_text(encoding="utf-8", errors="replace") if files[0].exists() else ""
            mapping = re.search(r"<KeyboardMappings>(-?\d+)</KeyboardMappings>", cfg)
            custom = re.search(r"<CustomKeyboardPreset>(.*?)</CustomKeyboardPreset>", cfg)
            if mapping and mapping.group(1) != "-1":
                self.status, self.by_combo = "builtin", {}
                return
            if not files[1].exists():
                self.status, self.by_combo = "missing", {}
                return
            presets = self._parse(files[1].read_text(encoding="utf-8", errors="replace"))
            want = custom.group(1) if custom else None
            _, table = next(((n, t) for n, t in presets if n == want), presets[0] if presets else (None, None))
            if table is None:
                self.status, self.by_combo = "error", {}
                return
            self.by_combo = {}
            for action, seqs in table.items():
                for seq in seqs:
                    if len(seq) == 1:  # chords (two-step keys) can't clash with a single press
                        self.by_combo.setdefault(self._combo(seq[0]), []).append(action)
            self.status = "ok"
        except Exception as e:  # never crash over Resolve's file
            print(f"Couldn't read Resolve's shortcuts: {e}", file=sys.stderr)
            self.status, self.by_combo = "error", {}

    @staticmethod
    def _parse(xml: str) -> list[tuple[str, dict]]:
        m = re.search(r"<PresetListBA>([0-9a-fA-F]+)</PresetListBA>", xml)
        if not m:
            return []
        b = bytes.fromhex(m.group(1))
        pos = 0

        def u():
            nonlocal pos
            v = int.from_bytes(b[pos:pos + 4], "big")
            pos += 4
            return v

        def qs():
            nonlocal pos
            n = u()
            if n == 0xFFFFFFFF:
                return None
            t = b[pos:pos + n].decode("utf-16-be", errors="replace")
            pos += n
            return t

        u()  # format version
        out = []
        for _ in range(u()):
            name = qs()
            u()  # byte length
            u()  # version
            table = {}
            for _ in range(u()):
                action = qs()
                seqs = []
                for _ in range(u()):
                    seqs.append([u() for _ in range(u())])
                table[action] = seqs
            qs()  # base preset name
            out.append((name, table))
        return out

    @classmethod
    def _combo(cls, code: int) -> tuple:
        mods = frozenset(n for n, bit in cls.MOD_BITS if code & bit)
        if IS_MAC:  # Qt on macOS: ControlModifier = Cmd, MetaModifier = Control
            mods = frozenset({"ctrl": "meta", "meta": "ctrl"}.get(n, n) for n in mods)
        key = QKeySequence(code & 0x01FFFFFF).toString().lower()
        return mods, key

    @staticmethod
    def hotkey_combo(hk: dict) -> tuple:
        key = hk["text"].split("+")[-1].lower() if hk.get("text") else ""
        return frozenset(hk.get("mods", [])), key

    def clash(self, hk) -> list[str]:
        if not hk or self.status != "ok":
            return []
        return [_humanize_action(a) for a in self.by_combo.get(self.hotkey_combo(hk), [])]


# ----------------------------------------------------------------------------- layer names

_LAYER_PS = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]; $S=[System.Windows.Automation.TreeScope]
$p = Get-Process Resolve -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { exit }
$pc = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $p.Id)
$all = $A::RootElement.FindAll($S::Descendants, $pc)
$want = 0
foreach ($e in $all) {
  $c = $e.Current
  if ($c.ClassName -eq 'QLabel' -and $c.Name -eq 'Node Stack Layers') { 'OPEN' }
  elseif ($c.ClassName -eq 'QLabel' -and $c.Name -match '^Layer (\d+)$') { $want = [int]$Matches[1] }
  elseif ($want -and $c.ClassName -eq 'UiPlainLineEdit') {
    'LAYER ' + $want + '|' + $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value; $want = 0 }
  elseif ($c.ClassName -eq 'QCheckBox' -and $c.Name -eq 'Post Group Node Stack Layer') {
    'POST ' + $e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState }
}
"""


_LAYER_WATCH_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]; $S=[System.Windows.Automation.TreeScope]
$parent = __PARENT__; $last = ''
$isLbl = New-Object System.Windows.Automation.PropertyCondition($A::NameProperty, 'Node Stack Layers')
while (Get-Process -Id $parent) {
  $p = Get-Process Resolve | Select-Object -First 1
  $out = ''
  if ($p) {
    $pc = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $p.Id)
    $w = $null
    foreach ($t in $A::RootElement.FindAll($S::Children, $pc)) { if ($t.FindFirst($S::Descendants, $isLbl)) { $w = $t; break } }
    if ($w) {
      $all = $w.FindAll($S::Descendants, [System.Windows.Automation.Condition]::TrueCondition); $want = 0
      foreach ($e in $all) { $c = $e.Current
        if ($c.ClassName -eq 'QLabel' -and $c.Name -match '^Layer (\d+)$') { $want = [int]$Matches[1] }
        elseif ($want -and $c.ClassName -eq 'UiPlainLineEdit') {
          $out += 'LAYER ' + $want + '|' + $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value + "`n"; $want = 0 }
        elseif ($c.ClassName -eq 'QCheckBox' -and $c.Name -eq 'Post Group Node Stack Layer') {
          $out += 'POST ' + $e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState + "`n" } }
    }
  }
  if ($out -and $out -ne $last) { $last = $out; [Console]::Out.Write($out + "END`n"); [Console]::Out.Flush() }
  Start-Sleep -Milliseconds 1500
}
"""


def _parse_layer_lines(lines) -> dict | None:
    names, post = {}, None
    for ln in lines:
        if ln.startswith("LAYER "):
            n, _, v = ln[6:].partition("|")
            if n.isdigit():
                names[n] = v
        elif ln.startswith("POST "):
            post = ln[5:].strip() == "On"
    return {"names": names, "post": post} if names else None


def watch_resolve_layer_settings(emit) -> None:
    """Background: while Resolve's Project Settings window is open, report its layer names /
    Post Group tick whenever they change (so a rename is caught before you press Save and the
    window closes). One hidden PowerShell process that exits by itself when this app exits."""
    if not IS_WIN:
        return
    ps = _LAYER_WATCH_PS.replace("__PARENT__", str(os.getpid()))
    try:
        proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return
    block = []
    for ln in proc.stdout:
        ln = ln.rstrip("\r\n")
        if ln == "END":
            d = _parse_layer_lines(block)
            block = []
            if d:
                emit(d)
        else:
            block.append(ln)


def read_resolve_layer_settings() -> dict | None:
    """Layer names + "Post Group Node Stack Layer" exactly as Resolve shows them in
    Project Settings › General Options › Color. Resolve's scripting API doesn't expose these,
    so they're read from Resolve's window (Windows UI Automation) — only possible while
    the Project Settings window is open. None when it isn't (or not on Windows)."""
    if not IS_WIN:
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _LAYER_PS],
                           capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return None
    lines = r.stdout.splitlines()
    return _parse_layer_lines(lines) if "OPEN" in lines else None


# ----------------------------------------------------------------------------- open with Resolve

AUTOSTART_NAME = "DaKillswitch"


def _launch_cmd(*extra) -> list[str]:
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), str(Path(__file__).resolve()), *extra]


def _autostart_file() -> Path | None:
    if IS_MAC:
        return Path.home() / "Library/LaunchAgents/com.dakillswitch.standby.plist"
    if not IS_WIN:
        return Path.home() / ".config/autostart/dakillswitch.desktop"
    return None


def autostart_enabled() -> bool:
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, AUTOSTART_NAME)
                return True
        except OSError:
            return False
    return _autostart_file().exists()


def set_autostart(on: bool):
    """Start DaKillswitch in standby at login (Windows Run key, macOS LaunchAgent, Linux autostart)."""
    cmd = _launch_cmd("--standby")
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                with contextlib.suppress(OSError):
                    winreg.DeleteValue(k, AUTOSTART_NAME)
        return
    f = _autostart_file()
    if not on:
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        f.write_bytes(plistlib.dumps({"Label": "com.dakillswitch.standby", "ProgramArguments": cmd,
                                      "RunAtLoad": True}))
    else:
        f.write_text(f"[Desktop Entry]\nType=Application\nName={APP_NAME}\nExec={shlex.join(cmd)}\n"
                     "X-GNOME-Autostart-enabled=true\n")


class ResolveWatch:
    """Watches the Resolve process from a background thread, never through its scripting API
    (API calls hang while Resolve shuts down). From that thread:
      on_windows(False) — Resolve has no visible window (quitting, or switching Project Manager ->
                          project); on_windows(True) — a window is back; on_gone() — process exited."""

    def __init__(self, on_gone, on_windows=None, ignore_pid: int | None = None):
        self.on_gone = on_gone
        self.on_windows = on_windows or (lambda up: None)
        self.windows_up = not IS_WIN  # only Windows can see Resolve's windows
        self.ignore_pid = ignore_pid  # a Resolve that is still shutting down from the last session
        self.pid = None
        self.lost = False

    def running(self) -> bool:
        if self.pid is None and not self.lost:
            pid = self._find_pid()
            self.pid = None if pid == self.ignore_pid else pid
            if self.pid:
                threading.Thread(target=self._watch, daemon=True).start()
        return bool(self.pid) and not self.lost

    @staticmethod
    def _find_pid() -> int | None:
        if IS_WIN:
            import ctypes
            from ctypes import wintypes

            class PE(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("pid", wintypes.DWORD),
                            ("heap", ctypes.c_void_p), ("mod", wintypes.DWORD), ("threads", wintypes.DWORD),
                            ("ppid", wintypes.DWORD), ("prio", ctypes.c_long), ("flags", wintypes.DWORD),
                            ("exe", ctypes.c_wchar * 260)]
            k32 = ctypes.windll.kernel32
            snap, e = k32.CreateToolhelp32Snapshot(0x2, 0), PE()
            e.dwSize = ctypes.sizeof(PE)
            try:
                ok = k32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    if e.exe.lower() == "resolve.exe":
                        return e.pid
                    ok = k32.Process32NextW(snap, ctypes.byref(e))
            finally:
                k32.CloseHandle(snap)
            return None
        try:
            out = subprocess.run(["pgrep", "-x", "Resolve" if IS_MAC else "resolve"],
                                 capture_output=True, text=True, timeout=2).stdout.split()
            return int(out[0]) if out else None
        except Exception:
            return None

    def _watch(self):
        if IS_WIN:
            import ctypes
            from ctypes import wintypes
            k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
            handle = k32.OpenProcess(0x00100000, False, self.pid)  # SYNCHRONIZE
            cb_t = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            hwnds, had = [], False

            def main_windows():  # Resolve's project window ("DaVinci Resolve Studio - <project>"), not Project Manager
                found = []

                def cb(h, _):
                    pid = wintypes.DWORD()
                    u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value == self.pid and u32.IsWindowVisible(h) and not u32.GetWindow(h, 4):
                        n = u32.GetWindowTextLengthW(h)
                        t = ctypes.create_unicode_buffer(n + 1)
                        u32.GetWindowTextW(h, t, n + 1)
                        if (t.value.startswith("DaVinci Resolve") and " - " in t.value  # a project is open
                                and not t.value.endswith("Project Manager")):
                            found.append(h)
                    return True
                u32.EnumWindows(cb_t(cb), 0)
                return found

            def alive():
                if handle:
                    return k32.WaitForSingleObject(handle, 150) != 0  # 0 = process exited
                time.sleep(0.5)  # no handle (access denied): look the process up instead
                return self._find_pid() == self.pid

            try:
                while alive():
                    if not any(u32.IsWindow(h) and u32.IsWindowVisible(h) for h in hwnds):
                        hwnds = main_windows()
                    up = bool(hwnds)
                    if up != self.windows_up and (up or had):
                        self.windows_up = up
                        self.on_windows(up)
                    had = had or up
            finally:
                if handle:
                    k32.CloseHandle(handle)
        else:
            while True:
                try:
                    os.kill(self.pid, 0)
                except OSError:
                    break
                time.sleep(0.2)
        self.lost = True
        self.on_gone()


def _foreground_ok() -> bool:
    """Windows only: True if Resolve (or this app) is the focused window."""
    if not IS_WIN:
        return True
    import ctypes
    from ctypes import wintypes
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    if pid.value == os.getpid():
        return True
    h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower() == "resolve.exe"
    finally:
        k32.CloseHandle(h)
    return False


class HotkeyEngine(QObject):
    """Global keyboard listener (pynput). Emits `fired(id)` on the Qt thread:
    an int for a scene (toggle column), a str for a layer key ("clip:2", "postclip", "tags"…)."""
    fired = Signal(object)

    def __init__(self):
        super().__init__()
        self.bindings: list[tuple[int, dict]] = []
        self.focus_only = True
        self.error = ""
        self._listener = None
        self._mods: set[str] = set()
        self._down: set = set()
        self._modmap = {}

    def start(self) -> bool:
        try:
            from pynput import keyboard
        except Exception as e:
            self.error = f"Global hotkeys unavailable ({e}). Install with: pip install pynput"
            return False
        K = keyboard.Key
        for name, keys in {"ctrl": ("ctrl", "ctrl_l", "ctrl_r"), "shift": ("shift", "shift_l", "shift_r"),
                           "alt": ("alt", "alt_l", "alt_r", "alt_gr"), "meta": ("cmd", "cmd_l", "cmd_r")}.items():
            for k in keys:
                if hasattr(K, k):
                    self._modmap[getattr(K, k)] = name
        try:
            self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()
        except Exception as e:
            self.error = f"Global hotkeys unavailable: {e}"
            return False
        return True

    def stop(self):
        if self._listener:
            self._listener.stop()

    def set_bindings(self, bindings: list[tuple]):
        self.bindings = [(k, hk) for k, hk in bindings if hk]

    @staticmethod
    def _vk(key):
        vk = getattr(key, "vk", None)
        if vk is None and hasattr(key, "value"):
            vk = getattr(key.value, "vk", None)
        return vk

    def _current_mods(self) -> set[str]:
        if IS_WIN:  # ask Windows directly, so a missed key-up can't leave a modifier stuck
            import ctypes
            gks = ctypes.windll.user32.GetAsyncKeyState
            m = set()
            if gks(0x11) & 0x8000: m.add("ctrl")
            if gks(0x10) & 0x8000: m.add("shift")
            if gks(0x12) & 0x8000: m.add("alt")
            if (gks(0x5B) | gks(0x5C)) & 0x8000: m.add("meta")
            return m
        return set(self._mods)

    def _on_press(self, key):
        mod = self._modmap.get(key)
        if mod:
            self._mods.add(mod)
            return
        vk = self._vk(key)
        char = (getattr(key, "char", None) or "").lower() or None
        ident = vk if vk is not None else char
        if ident in self._down:  # ignore auto-repeat
            return
        self._down.add(ident)
        if not self.bindings:
            return
        mods = self._current_mods()
        for idx, hk in self.bindings:
            if set(hk.get("mods", [])) != mods:
                continue
            if (vk is not None and hk.get("vk") == vk) or (char and hk.get("char") == char):
                if not self.focus_only or _foreground_ok():
                    self.fired.emit(idx)
                return

    def _on_release(self, key):
        mod = self._modmap.get(key)
        if mod:
            self._mods.discard(mod)
            return
        vk = self._vk(key)
        self._down.discard(vk if vk is not None else (getattr(key, "char", None) or "").lower() or None)


# ----------------------------------------------------------------------------- widgets

class StateDot(QWidget):
    def __init__(self, size=8, parent=None):
        super().__init__(parent)
        self.state = None
        self._size = size
        self.setFixedSize(size + 6, size + 6)

    def set_state(self, state):
        self.state = state
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = CONN_COLOR if self.state is True else QColor(RED) if self.state is False else IDLE_COLOR
        p.setPen(Qt.NoPen)
        if self.state is not None:
            glow = QColor(c)
            glow.setAlpha(60)
            p.setBrush(glow)
            p.drawEllipse(self.rect())
        p.setBrush(c)
        p.drawEllipse(QRectF(3, 3, self._size, self._size))


def key_parts(text: str) -> list[str]:
    """'Ctrl+Alt+Shift+Z' -> ['Ctrl', 'Alt', 'Shift', 'Z'] ('Ctrl++' -> ['Ctrl', '+'])."""
    if text.endswith("+"):  # the key itself is "+"
        head = text[:-1].rstrip("+")
        return (head.split("+") if head else []) + ["+"]
    return text.split("+")


def key_blocks(fm: QFontMetrics, text: str, pad: float = 8) -> list[tuple[str, float, bool]]:
    """'Ctrl+Alt+S' -> [(text, width, boxed)]: one box per modifier, a single '+' before the key's box."""
    *mods, key = key_parts(text)
    out = []
    for m in mods:
        out.append((m, fm.horizontalAdvance(m) + pad, True))
        out.append(("", 3, False))  # gap between modifier boxes
    if mods:
        out[-1] = ("+", fm.horizontalAdvance("+") + 6, False)
    out.append((key, fm.horizontalAdvance(key) + pad, True))
    return out


def draw_key_blocks(p: QPainter, x: float, y: float, h: float, blocks) -> None:
    """[Ctrl] [Alt] [Shift] + [S]: grey boxes like "None", the '+' a step darker."""
    for text, w, boxed in blocks:
        r = QRectF(x, y, w, h)
        if boxed:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(BTN))
            p.drawRoundedRect(r, 2, 2)
        p.setPen(QColor(TEXT_DIM if boxed else PLUS_GREY))
        p.drawText(r, Qt.AlignCenter, text)
        x += w


class HotkeyButton(QPushButton):
    """Click, then press a key combo. Esc cancels, Backspace/Delete clears."""
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hotkey = None
        self.recording = False
        self.stopped_at = 0.0
        self.setObjectName("cell")
        self.setProperty("kind", "hotkey")
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click, then press a key or combo.\nEsc cancels · Backspace clears")
        self.clicked.connect(self.start)
        self._refresh()

    def set_hotkey(self, hk):
        self.hotkey = hk
        self._refresh()

    def paintEvent(self, e):
        if not self.hotkey or self.recording:
            return super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self.font())
        blocks = key_blocks(p.fontMetrics(), self.hotkey["text"])
        if self.objectName() == "pillkey":  # the blocks are the box: no button background behind them
            if self.property("clash"):
                p.setPen(QPen(QColor(RED), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 2, 2)
            draw_key_blocks(p, 1, 1, self.height() - 2, blocks)
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""
        self.style().drawControl(QStyle.CE_PushButton, opt, p, self)
        h = min(16, self.height() - 6)
        draw_key_blocks(p, 6, (self.height() - h) / 2, h, blocks)

    def sizeHint(self):
        s = super().sizeHint()
        if self.objectName() == "pillkey" and self.hotkey and not self.recording:
            s.setWidth(int(sum(w for _, w, _ in key_blocks(QFontMetrics(self.font()), self.hotkey["text"]))) + 3)
        return s

    def set_clash(self, actions: list[str]):
        self.setProperty("clash", bool(actions))
        base = "Click, then press a key or combo.\nEsc cancels · Backspace clears"
        if actions:
            more = f" (+{len(actions) - 3} more)" if len(actions) > 3 else ""
            base = f"⚠ Also a Resolve shortcut: {', '.join(actions[:3])}{more}\nBoth will fire.\n\n" + base
        self.setToolTip(base)
        repolish(self)

    def _refresh(self):
        self.setProperty("recording", self.recording)
        self.setProperty("empty", not self.hotkey and not self.recording)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText("Press a key…" if self.recording else (self.hotkey["text"] if self.hotkey else "None"))

    def start(self):
        self.recording = True
        self._refresh()
        self.grabKeyboard()

    def _stop(self):
        self.stopped_at = time.monotonic()
        self.recording = False
        self.releaseKeyboard()
        self._refresh()

    def focusOutEvent(self, e):
        if self.recording:
            self._stop()
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        if not self.recording:
            return super().keyPressEvent(e)
        k = e.key()
        if k in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_unknown):
            return
        m = e.modifiers()
        if not (m & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier)):
            if k == Qt.Key_Escape:
                return self._stop()
            if k in (Qt.Key_Backspace, Qt.Key_Delete):
                self.hotkey = None
                self._stop()
                self.changed.emit(None)
                return
        # Qt on macOS reports Cmd as ControlModifier and Control as MetaModifier
        ctrl_mod, meta_mod = (Qt.MetaModifier, Qt.ControlModifier) if IS_MAC else (Qt.ControlModifier, Qt.MetaModifier)
        mods, parts = [], []
        for flag, name, label in ((ctrl_mod, "ctrl", "Ctrl"), (Qt.AltModifier, "alt", "Alt"),
                                  (Qt.ShiftModifier, "shift", "Shift"), (meta_mod, "meta", "Cmd" if IS_MAC else "Win")):
            if m & flag:
                mods.append(name)
                parts.append(label)
        key_text = QKeySequence(k).toString() or f"Key {k}"
        parts.append(key_text)
        self.hotkey = {
            "mods": sorted(mods),
            "vk": int(e.nativeVirtualKey()) or None,
            "char": key_text.lower() if len(key_text) == 1 else None,
            "text": "+".join(parts),
        }
        self._stop()
        self.changed.emit(self.hotkey)


class StickyMenu(QMenu):
    """A menu that stays open when you tick a checkable item, so several nodes can be picked.
    Press on a node and drag over others to set them all the same way (like Resolve's track toggles)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_state = None
        self._dragged = set()

    @staticmethod
    def _draggable(a) -> bool:
        return a is not None and a.isEnabled() and a.isCheckable() and a.data() != "all"

    def mousePressEvent(self, e):
        a = self.actionAt(e.position().toPoint())
        if e.button() == Qt.LeftButton and a is not None and a.isEnabled() and a.isCheckable():
            self._drag_state = not a.isChecked()
            self._dragged = {id(a)}
            a.setChecked(self._drag_state)
            if not self._draggable(a):  # "Entire layer" toggles alone, no drag
                self._drag_state = None
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_state is not None and e.buttons() & Qt.LeftButton:
            a = self.actionAt(e.position().toPoint())
            if self._draggable(a) and id(a) not in self._dragged:
                self._dragged.add(id(a))
                if a.isChecked() != self._drag_state:
                    a.setChecked(self._drag_state)
        super().mouseMoveEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):  # Enter closes, like Esc, without ticking anything
            self.close()
            return
        super().keyPressEvent(e)

    def mouseReleaseEvent(self, e):
        if self._dragged:  # the press already applied the change
            self._drag_state = None
            self._dragged = set()
            e.accept()
            return
        a = self.activeAction()
        if a is not None and a.isEnabled() and a.isCheckable():
            a.trigger()
            e.accept()
            return
        super().mouseReleaseEvent(e)


def sel_text(sel: list[dict]) -> str:
    if not sel:
        return "None"
    if any(s.get("all") for s in sel):
        return "All nodes"
    return ", ".join(s["label"] if s.get("label") else f"Node {s['index']:02d}" for s in sel)


class NodePicker(QPushButton):
    """One grid cell: shows the chosen nodes, click for a multi-select dropdown."""
    changed = Signal(int, str, list)  # toggle index, slot, selection

    def __init__(self, toggle_index: int, slot: str, parent=None):
        super().__init__(parent)
        self.toggle_index = toggle_index
        self.slot = slot
        self.sel: list[dict] = []
        self.nodes = None
        self.placeholder = ""
        self.label_clash: list[str] = []  # Node Labels in this cell whose ON/OFF disagrees with this scene
        self._full = ""
        self.setObjectName("cell")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.clicked.connect(self._open_menu)

    def set_label_clash(self, names: list[str]):
        if names != self.label_clash:
            self.label_clash = names
            self._render()
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.label_clash:  # red dot on the right: a Node Label disagrees with this scene
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(CLASH_RED)
            p.drawEllipse(QPointF(self.width() - 11, self.height() / 2), 3.5, 3.5)

    def set_data(self, nodes, placeholder: str, sel: list[dict]):
        self.nodes = nodes
        self.placeholder = placeholder
        self.sel = [dict(s) for s in sel]
        self._render()

    def _missing(self) -> list[dict]:
        if self.nodes is None:
            return []
        return [s for s in self.sel if not s.get("all") and not match_nodes(self.nodes, [s])]

    def _render(self):
        self.setProperty("empty", not self.sel)
        self.setProperty("all", any(s.get("all") for s in self.sel))
        self.setProperty("missing", bool(self.sel) and len(self._missing()) == len(self.sel))
        self.style().unpolish(self)
        self.style().polish(self)
        self._full = sel_text(self.sel)
        tip = self._full if self.sel else "Click to pick nodes"
        if self._missing():
            tip += "\n(not all of these are on this clip)"
        if self.label_clash:
            tip += "\n\nRed dot: Node Label " + ", ".join(f"“{n}”" for n in self.label_clash) + \
                   " is set the opposite way to this scene"
        self.setToolTip(tip)
        self._elide()

    def _elide(self):
        w = max(20, self.width() - 22)
        self.setText(QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, w))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _open_menu(self):
        m = StickyMenu(self)
        layer = slot_layer(self.slot)
        all_label = f"Entire layer L{layer}" if layer else f"Entire {slot_title(self.slot)}"
        a_all = QAction(all_label, m, checkable=True)
        a_all.setData("all")
        a_all.setChecked(any(s.get("all") for s in self.sel))
        m.addAction(a_all)
        clear = m.addAction("Clear")
        m.addSeparator()
        node_actions = []
        if self.nodes is None:
            a = QAction(self.placeholder or "Not available", m)
            a.setEnabled(False)
            m.addAction(a)
        elif not self.nodes:
            a = QAction("No nodes", m)
            a.setEnabled(False)
            m.addAction(a)
        else:
            for idx, label, tools in self.nodes:
                name = label if label else (", ".join(tools) if tools else "(no label)")
                a = QAction(f"{idx:02d}   {name}", m, checkable=True)
                a.setData({"index": idx, "label": label})
                a.setChecked(idx in match_nodes(self.nodes, [s for s in self.sel if not s.get("all")]))
                m.addAction(a)
                node_actions.append(a)
        for s in self._missing():
            a = QAction(f"{s['label'] or 'Node %02d' % s['index']}   (not on this clip)", m, checkable=True)
            a.setData(dict(s))
            a.setChecked(True)
            m.addAction(a)
            node_actions.append(a)
        m.setToolTipsVisible(False)  # no popups while the node list is open

        def sync_enabled():
            for a in node_actions:
                a.setEnabled(not a_all.isChecked())

        def on_all(checked):
            self.sel = [{"all": True}] if checked else []
            if checked:
                for a in node_actions:
                    a.setChecked(False)
            sync_enabled()
            self._emit()

        def on_node(checked, a):
            d = a.data()
            key = d.get("label")
            self.sel = [s for s in self.sel if not s.get("all") and not (
                (key and s.get("label") == key) or (not key and not s.get("label") and s.get("index") == d["index"]))]
            if checked:
                self.sel.append({"index": d["index"], "label": d["label"]})
            self._emit()

        a_all.toggled.connect(on_all)
        for a in node_actions:
            a.toggled.connect(lambda c, a=a: on_node(c, a))
        def on_clear():
            self.sel = []
            self._emit()

        clear.triggered.connect(on_clear)
        sync_enabled()
        m.setMinimumWidth(self.width())
        m.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def _emit(self):
        self._render()
        self.changed.emit(self.toggle_index, self.slot, list(self.sel))


class Overlay(QWidget):
    """Frameless always-on-top status panel. Drag to move, click a row to toggle,
    click the strip at the bottom to open the main window."""
    row_clicked = Signal(int)
    open_main = Signal()
    close_main = Signal()
    quit_all = Signal()
    moved = Signal(int, int)
    ROW_H, PAD, W, BTN_H = 28, 6, 260, 19  # rows: a 24 px field like a layer-shortcut pill + 4 px gap
    SEP_GAP, BTN_GAP = 3, 6  # extra space above the separator / between separator and buttons

    def __init__(self):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.toggles: list[dict] = []
        self._press = None
        self._drag_origin = None
        self._hover_btn = None  # "open" / "close" / "quit" / None
        self.main_open = False  # the close button only shows while the main window is open
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_toggles(self, toggles):
        self.toggles = toggles
        self.setFixedSize(self.W, self.PAD * 2 + self.ROW_H * len(toggles) + self.BTN_H + self.SEP_GAP + self.BTN_GAP)
        self.update()

    def _btn_top(self) -> float:  # the separator line; the button strip is everything below it
        return self.PAD + self.ROW_H * len(self.toggles) + 2 + self.SEP_GAP

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(LINE), 1))
        bg = QColor(BG_GRID)
        bg.setAlpha(240)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        y0 = self.PAD
        name_font = QFont(self.font())
        name_font.setPointSizeF(9)
        key_font = QFont("Consolas" if IS_WIN else "Menlo" if IS_MAC else "DejaVu Sans Mono")
        key_font.setPointSizeF(8)
        kfm = QFontMetrics(key_font)
        for i, t in enumerate(self.toggles):
            y = y0 + i * self.ROW_H
            configured = has_selection(t)
            on = configured and t["on"] is True
            armed = configured and t["on"] is None
            p.setPen(QPen(QColor(LINE), 1))  # each scene in its own field, like Resolve's VCA box
            p.setBrush(QColor(FIELD))
            p.drawRoundedRect(QRectF(7.5, y + 2.5, self.W - 15, self.ROW_H - 5), 3, 3)  # 24 px with its border
            r = QRectF(14, y + self.ROW_H / 2 - 3.5, 7, 7)
            if on:
                p.setPen(Qt.NoPen)
                p.setBrush(ON_COLOR)
            elif armed:  # gold ring: picks set, state unknown
                p.setPen(QPen(ON_COLOR, 1.2))
                p.setBrush(Qt.NoBrush)
                r = r.adjusted(0.6, 0.6, -0.6, -0.6)
            else:
                p.setPen(QPen(QColor(TEXT_FAINT), 1))
                p.setBrush(Qt.NoBrush)
                r = r.adjusted(0.5, 0.5, -0.5, -0.5)
            p.drawEllipse(r)
            p.setFont(name_font)
            p.setPen(QColor(TEXT_BRIGHT) if on else QColor(TEXT) if armed else QColor(TEXT_FAINT))
            blocks = key_blocks(kfm, t["hotkey"]["text"]) if t.get("hotkey") else []
            kw = sum(w for _, w, _ in blocks)
            name = QFontMetrics(name_font).elidedText(t["name"], Qt.ElideRight, self.W - 44 - kw - 8)
            p.drawText(QRectF(32, y, self.W - 44 - kw, self.ROW_H), Qt.AlignLeft | Qt.AlignVCenter, name)
            p.setFont(key_font)
            draw_key_blocks(p, self.W - 12 - kw, y + self.ROW_H / 2 - 7, 14, blocks)
        top = self._btn_top()
        p.setPen(QPen(QColor(LINE), 1))  # divider, like Resolve's
        p.drawLine(QPointF(7, top), QPointF(self.W - 7, top))  # as wide as the scene boxes
        cy = top + self.BTN_GAP + self.BTN_H / 2
        p.setOpacity(1.0 if self._hover_btn == "open" else 0.7)  # app icon: open / bring forward the main window
        s = 15
        p.drawPixmap(QRectF(18 - s / 2, cy - s / 2, s, s).toRect(), _icon_pixmap())  # lined up with the dots
        pen = QPen(ICON_GREY, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # ✕: close (hide) the main window; greyed out while it's already closed
        p.setOpacity((1.0 if self._hover_btn == "close" else 0.7) if self.main_open else 0.22)
        x, d = 40, 4
        p.drawLine(QPointF(x - d, cy - d), QPointF(x + d, cy + d))
        p.drawLine(QPointF(x + d, cy - d), QPointF(x - d, cy + d))
        # power symbol next to it: close everything (main window and overlay)
        p.setOpacity(1.0 if self._hover_btn == "quit" else 0.7)
        x, r = 62, 4.5
        p.drawArc(QRectF(x - r, cy - r + 0.5, 2 * r, 2 * r), 125 * 16, 290 * 16)
        p.drawLine(QPointF(x, cy - r - 0.5), QPointF(x, cy))
        p.setOpacity(1.0)

    def _zone(self, pos):
        if pos.y() < self._btn_top():
            return None
        x = pos.x()
        if x <= 29:
            return "open"
        if x <= 51:
            return "close" if self.main_open else None
        return "quit" if x <= 73 else None

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._drag_origin = self.pos()

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self.move(self._drag_origin + (e.globalPosition().toPoint() - self._press))
        hover = self._zone(e.position())
        if hover != self._hover_btn:
            self._hover_btn = hover
            self.update()

    def leaveEvent(self, e):
        self._hover_btn = None
        self.update()

    def mouseReleaseEvent(self, e):
        if self._press is None:
            return
        delta = e.globalPosition().toPoint() - self._press
        self._press = None
        if delta.manhattanLength() > 4:
            self.moved.emit(self.x(), self.y())
            return
        if e.position().y() >= self._btn_top():
            zone = self._zone(e.position())
            if zone == "open":
                self.open_main.emit()
            elif zone == "close":
                self.close_main.emit()
            elif zone == "quit":
                self.quit_all.emit()
            return
        row = int((e.position().y() - self.PAD) // self.ROW_H)
        if 0 <= row < len(self.toggles):
            self.row_clicked.emit(row)


class FlowLayout(QLayout):
    """Lays items left to right and wraps onto new lines (Qt's classic flow layout)."""

    def __init__(self, parent=None, margins=(0, 0, 0, 0), hspacing=5, vspacing=5):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(*margins)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            sz = it.sizeHint()
            nx = x + sz.width() + self._h
            if nx - self._h > r.right() + 1 and line_h > 0:
                x, y = r.x(), y + line_h + self._v
                nx = x + sz.width() + self._h
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sz))
            x = nx
            line_h = max(line_h, sz.height())
        return y + line_h - rect.y() + m.bottom()


ICON_GREY = QColor(TEXT)
CLASH_RED = QColor(RED)


class IconButton(QPushButton):
    """Tiny flat chip button: a thin painted icon (dot / ring / x / plus) or a letter.
    Grey at 50 % opacity, full strength on hover."""
    color = ICON_GREY

    def __init__(self, kind: str, tip: str = "", w=11, h=16, boxed=False, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.boxed = boxed
        self.setFixedSize(w, h)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(tip)
        self.setAttribute(Qt.WA_Hover)
        self.setObjectName("ib")

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(1.0 if self.underMouse() else 0.5)
        r = QRectF(self.rect())
        if self.boxed:
            p.setPen(QPen(QColor(TEXT_FAINT), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        s = 11.0
        box = QRectF(r.center().x() - s / 2, r.center().y() - s / 2, s, s)
        k = s / 24.0
        pen = QPen(self.color, max(1.0, 1.4 * k * 1.6))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        def pt(x, y):
            return QPointF(box.x() + x * k, box.y() + y * k)

        if self.kind in ("on", "off"):
            p.drawEllipse(QRectF(pt(3, 3), pt(21, 21)))
            if self.kind == "on":
                p.setPen(Qt.NoPen)
                p.setBrush(self.color)
                p.drawEllipse(QRectF(pt(8.5, 8.5), pt(15.5, 15.5)))
        elif self.kind == "x":
            p.drawLine(pt(5, 5), pt(19, 19))
            p.drawLine(pt(19, 5), pt(5, 19))
        elif self.kind == "plus":
            p.drawLine(pt(12, 4), pt(12, 20))
            p.drawLine(pt(4, 12), pt(20, 12))
        else:  # a letter (F / P)
            f = QFont(self.font())
            f.setPointSizeF(7)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(self.color)
            p.drawText(r, Qt.AlignCenter, self.kind)


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


class SceneNameEdit(QLineEdit):
    """Scene name field: Esc cancels the edit, Enter confirms and jumps to the next scene name,
    Shift+Enter to the previous one (both loop)."""
    confirmed = Signal(int)  # +1 next, -1 previous
    cancelled = Signal(str)  # the name to go back to

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._before = text

    def focusInEvent(self, e):
        self._before = self.text()
        super().focusInEvent(e)
        QTimer.singleShot(0, self.selectAll)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.setText(self._before)
            self.cancelled.emit(self._before)
            self.clearFocus()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._before = self.text()
            self.confirmed.emit(-1 if e.modifiers() & Qt.ShiftModifier else 1)
            return
        super().keyPressEvent(e)


class TagStateBox(QPushButton):
    """Small rounded ON / OFF box inside a node-label chip."""

    def __init__(self, text: str, selected: bool, clash: bool, tip: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("tagstate")
        self.setProperty("sel", selected)
        self.setProperty("clash", clash)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(tip)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFixedHeight(16)  # same box as the layer-shortcut key ("None")


class TagChip(QFrame):
    """Name · [ON] [OFF] · ✕.  Grey = ARMED (state unknown), the chosen box gold;
    the other box red when a scene in this preset picks these nodes with the opposite state.
    Right-click: favourite (every project) / this project only."""
    picked = Signal(int, bool)
    scoped = Signal(int)
    removed = Signal(int)

    def __init__(self, index: int, tag: dict, parent=None, clash: bool = False):
        super().__init__(parent)
        self.index = index
        self.tag = tag
        on = tag["on"]
        armed = on is None
        clash = clash and not armed
        self.setObjectName("tag")
        self.setProperty("armed", armed)
        self.setFixedHeight(22)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 5, 0)
        h.setSpacing(2)
        name = QLabel(tag["name"], objectName="tagname")
        name.setProperty("armed", armed)
        h.addWidget(name)
        h.addSpacing(5)
        n = tag["name"]
        b_on = TagStateBox("ON", on is True, clash, f"Switch every “{n}” node ON")
        b_off = TagStateBox("OFF", on is False, clash, f"Switch every “{n}” node OFF")
        b_x = IconButton("x", "Remove label (doesn't change any nodes)")
        h.addWidget(b_on)
        h.addWidget(b_off)
        h.addSpacing(1)
        h.addWidget(b_x)
        b_on.clicked.connect(lambda: self.picked.emit(index, True))
        b_off.clicked.connect(lambda: self.picked.emit(index, False))
        b_x.clicked.connect(lambda: self.removed.emit(index))
        state = "ARMED — state unknown, pick ON or OFF" if armed else ("ON" if on else "OFF")
        scope = "favourite (every project on this machine)" if tag["scope"] == "F" else "this project only"
        self.setToolTip(f"Every node labelled “{n}” in Resolve (any case): all layers, groups and the "
                        f"timeline, on every enabled clip.\nNow: {state} · {scope} (right-click to change)\n"
                        "While OFF, layer keys and “Entire layer” picks leave these nodes off when they switch "
                        "a layer back on."
                        + ("\n\nRed: a scene in this preset uses these nodes with the opposite ON/OFF." if clash else ""))

    def contextMenuEvent(self, e):
        m = QMenu(self)
        fav = m.addAction("Favourite — every project on this machine")
        fav.setCheckable(True)
        fav.setChecked(self.tag["scope"] == "F")
        proj = m.addAction("This project only")
        proj.setCheckable(True)
        proj.setChecked(self.tag["scope"] != "F")
        act = m.exec(e.globalPos())
        if (act is fav and self.tag["scope"] != "F") or (act is proj and self.tag["scope"] == "F"):
            self.scoped.emit(self.index)


class TagEdit(QLineEdit):
    """The inline text cursor that replaces the + square while typing a new tag."""
    submitted = Signal(str)
    cancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tagedit")
        self.setFrame(False)
        self.setFixedHeight(22)
        self.setMaxLength(40)
        self.textChanged.connect(self._fit)
        self._fit("")

    def _fit(self, text):
        self.setFixedWidth(max(12, QFontMetrics(self.font()).horizontalAdvance(text) + 18))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            t = self.text().strip()
            if t:
                self.submitted.emit(t)
            else:
                self.cancelled.emit()
            return
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        if not self.text().strip():
            QTimer.singleShot(0, self.cancelled.emit)


class TagsField(QFrame):
    """One full-width field of tag chips, ending in a grey + square."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tagfield")
        self.flow = FlowLayout(self, margins=(5, 5, 5, 5), hspacing=5, vspacing=5)
        self.tags: list[dict] = []
        self.typing = False
        self.add_tag = None  # callback(name) set by the window
        self.set_label = None  # callback(tag, state) set by the window
        self.clash: set[str] = set()  # lower-case label names whose ON/OFF disagrees with a scene

    def set_tags(self, tags: list[dict]):
        self.tags = tags
        self._rebuild()

    def _rebuild(self):
        while self.flow.count():
            it = self.flow.takeAt(0)
            if it.widget():
                it.widget().hide()  # hide now: deleteLater only removes it on the next event loop pass
                it.widget().deleteLater()
        for i, t in enumerate(self.tags):
            c = TagChip(i, t, self, clash=t["name"].lower() in self.clash)
            c.picked.connect(self._pick)
            c.scoped.connect(self._scope)
            c.removed.connect(self._remove)
            self.flow.addWidget(c)
            c.show()  # visible now, so the flow layout wraps it onto the next row straight away
        if self.typing:
            ed = TagEdit(self)
            ed.submitted.connect(self._submit)
            ed.cancelled.connect(self._cancel)
            self.flow.addWidget(ed)
            ed.show()
            QTimer.singleShot(0, ed.setFocus)
        else:
            plus = IconButton("plus", "Add a node label — type it exactly as it's named in Resolve", 22, 22,
                              boxed=True, parent=self)
            plus.clicked.connect(self._start)
            self.flow.addWidget(plus)
            plus.show()
        self.flow.invalidate()
        self._fit()

    def _start(self):
        self.typing = True
        self._rebuild()

    def _cancel(self):
        if self.typing:
            self.typing = False
            self._rebuild()

    def _submit(self, name):
        if self.add_tag:
            self.add_tag(name)  # the window rebuilds us; typing stays on for the next tag

    def _pick(self, i, state):
        if self.set_label:
            self.set_label(self.tags[i], state)

    def _scope(self, i):
        self.tags[i]["scope"] = "P" if self.tags[i]["scope"] == "F" else "F"
        self.tags[i]["_rescope"] = True
        self.changed.emit()

    def _remove(self, i):
        self.tags[i]["_remove"] = True
        self.changed.emit()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self.flow.heightForWidth(w) + 2 * self.frameWidth()

    def sizeHint(self):
        return QSize(200, self.heightForWidth(self.width() if self.width() > 50 else 800))

    def _fit(self):
        """Claim the height every wrapped row needs (the grid shares height badly for widgets spanning columns)."""
        h = self.heightForWidth(self.width() if self.width() > 50 else 800)
        if h != self.minimumHeight():
            self.setMinimumHeight(h)
        self.updateGeometry()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width():
            self._fit()


class FlowHolder(QWidget):
    """A plain widget whose FlowLayout drives its height (so wrapped chips get room)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self, hspacing=5, vspacing=5)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self.flow.heightForWidth(w)

    def sizeHint(self):
        return QSize(300, self.flow.heightForWidth(self.width() if self.width() > 50 else 800))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if e.oldSize().width() != e.size().width():
            self.updateGeometry()


class PinButton(QPushButton):
    """Pin on top: gold pin when the window stays above Resolve, grey when not."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setObjectName("pin")
        self.setFixedSize(30, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Pin on top: keep this window above Resolve")

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(ACCENT) if self.isChecked() else QColor(TEXT_DIM)
        p.translate(self.width() / 2, self.height() / 2)
        pen = QPen(c, 1.3)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(c if self.isChecked() else Qt.NoBrush)
        p.drawEllipse(QPointF(0, -2.5), 3, 3)          # head
        p.drawLine(QPointF(0, 0.5), QPointF(0, 6))     # needle


class DotButton(QPushButton):
    """7 px on/off dot: gold filled = on, grey ring = off."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.on = True
        self.setFixedSize(11, 16)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("ib")

    def set_on(self, on):
        self.on = on
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QRectF(self.width() / 2 - 3.5, self.height() / 2 - 3.5, 7, 7)
        if self.on:
            p.setPen(Qt.NoPen)
            p.setBrush(ON_COLOR)
        else:
            p.setPen(QPen(QColor(TEXT_FAINT), 1))
            p.setBrush(Qt.NoBrush)
            c = c.adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawEllipse(c)


class LayerPill(QFrame):
    """Layer shortcut chip: on/off dot · name · (1 clip) · key box. Right-click for options."""
    flip = Signal(str)
    menu = Signal(str, object)

    def __init__(self, key: str, title: str, tip: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.setObjectName("pill")
        self.setFixedHeight(24)
        self.setToolTip(tip + "\nClick the dot to flip · right-click for options")
        h = QHBoxLayout(self)
        h.setContentsMargins(6, 0, 4, 0)
        h.setSpacing(6)
        self.dot = DotButton(self)
        self.dot.clicked.connect(lambda: self.flip.emit(self.key))
        self.name = QLabel(title, objectName="pillname")
        self.one = QLabel("1 clip", objectName="one")
        self.one.setToolTip("This clip only")
        self.one.setAlignment(Qt.AlignCenter)
        self.hk = HotkeyButton(self)
        self.hk.setObjectName("pillkey")
        self.hk.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.hk.setFixedHeight(16)
        for w in (self.dot, self.name, self.one, self.hk):
            h.addWidget(w)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.menu.emit(self.key, self.mapToGlobal(pos)))

    def set_state(self, on: bool, one_clip: bool):
        self.dot.set_on(on)
        self.name.setProperty("on", on)
        repolish(self.name)
        self.one.setVisible(one_clip)


# ----------------------------------------------------------------------------- main window

# Resolve's UI face is Open Sans; Noto Sans is the same design and is more often installed with all weights
FONT = '"Noto Sans", "Open Sans", "Segoe UI", "Helvetica Neue", "DejaVu Sans", Arial'
CHIP_FONT = f'"Inter", {FONT}'  # ON / OFF / ARMED: Inter SemiBold sits between Noto's Regular and Bold
MONO = 'Consolas, Menlo, "DejaVu Sans Mono", monospace'
STYLE = f"""
* {{ font-family: {FONT}; font-size: 9pt; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QLabel#title {{ font-size: 10.5pt; font-weight: 600; color: {TEXT_BRIGHT}; }}
QLabel#muted {{ color: {TEXT_DIM}; }}
QLabel#flash {{ color: {ACCENT}; }}
QLabel#flash[idle="true"] {{ color: {TEXT_FAINT}; }}
QLabel#rowlabel {{ color: {TEXT_DIM}; padding-right: 8px; }}
QLabel#rowlabel[layer="true"] {{ color: {TEXT}; }}
QLineEdit#rowedit {{ background: transparent; border: 1px solid transparent; border-radius: 3px; padding: 0 2px;
    margin-left: -3px; color: {TEXT}; }}
QLineEdit#rowedit:hover {{ border-color: {BTN}; }}
QLineEdit#rowedit:focus {{ border-color: {BLUE}; background: {FIELD}; }}
QLabel#chip {{ font-family: {MONO}; font-size: 8pt; padding: 0 2px; }}
QLabel#chip[state="on"] {{ color: {ACCENT}; }}
QLabel#chip[state="off"] {{ color: {TEXT_DIM}; }}
QLabel#chip[state="idle"] {{ color: {TEXT_FAINT}; }}
QLabel#chip[state="armed"] {{ color: {ACCENT}; }}
QLabel#one {{ font-size: 7pt; color: {TEXT_DIM}; border: 1px solid {BTN}; border-radius: 2px; padding: 0 3px;
    min-height: 12px; max-height: 12px; }}
QFrame#grid {{ background: {BG_GRID}; border: 1px solid {LINE}; border-radius: 2px; }}
QFrame#sep {{ background: {LINE}; max-height: 1px; min-height: 1px; }}
QLineEdit#colname {{ background: {BG_DARK}; border: 1px solid transparent; border-top: 2px solid {GREEN};
    border-radius: 0; padding: 7px 8px 3px 8px; color: {TEXT}; font-size: 9pt; }}
QLineEdit#colname:hover {{ border-left-color: {BTN}; border-right-color: {BTN}; border-bottom-color: {BTN}; }}
QLineEdit#colname:focus {{ border-left-color: transparent; border-right-color: transparent;
    border-bottom-color: transparent; background: {FIELD}; color: {TEXT_BRIGHT}; }}
QPushButton {{ background: {BTN}; border: 1px solid {LINE}; border-radius: 3px; padding: 4px 10px; color: {TEXT}; }}
QPushButton:hover {{ background: {BTN_HOVER}; }}
QPushButton:pressed {{ background: {FIELD}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {BG_DARK}; }}
QPushButton:checked {{ background: {BLUE}; color: {LINE}; }}
QPushButton[head="true"]:checked, QPushButton[head="true"]:pressed {{ background: {FIELD}; color: {ACCENT}; }}
QPushButton#ib {{ background: transparent; border: none; padding: 0; }}
QPushButton#cell {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 3px; padding: 3px 8px;
    text-align: left; color: {TEXT}; min-height: 18px; }}
QPushButton#cell:hover {{ border-color: {BTN_HOVER}; }}
QPushButton#cell[empty="true"] {{ color: {TEXT_FAINT}; }}
QPushButton#cell[all="true"] {{ color: {ACCENT}; }}
QPushButton#cell[missing="true"] {{ color: #8a7a5c; font-style: italic; }}
QPushButton#cell[kind="hotkey"] {{ font-family: {MONO}; }}
QPushButton#cell[recording="true"] {{ border-color: {BLUE}; color: {TEXT_BRIGHT}; }}
QPushButton#cell[clash="true"] {{ border: 1px solid {RED}; }}
QPushButton#state {{ font-family: {CHIP_FONT}; font-size: 8pt; font-weight: 600; padding: 0 8px; min-height: 16px; max-height: 16px;
    border: 1px solid {LINE}; border-radius: 2px; }}
QPushButton#state[state="on"] {{ background: {ACCENT}; color: {LINE}; }}
QPushButton#state[state="off"] {{ background: {BTN}; color: {TEXT_DIM}; }}
QPushButton#state[state="idle"] {{ background: {BG_DARK}; color: {TEXT_FAINT}; }}
QPushButton#state[state="armed"] {{ background: {BTN}; color: {ACCENT}; }}
QPushButton#state[state="armed"]:hover {{ background: {BTN_HOVER}; }}
QPushButton#state[state="on"]:hover {{ background: #d4a22a; }}
QPushButton#state[state="off"]:hover {{ background: {BTN_HOVER}; color: {TEXT}; }}
QFrame#pill {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 3px; }}
QLabel#pillname {{ color: {TEXT_FAINT}; }}
QLabel#pillname[on="true"] {{ color: {TEXT}; }}
QPushButton#pillkey {{ background: {BTN}; border: 1px solid transparent; border-radius: 2px; padding: 0 6px;
    font-family: {MONO}; font-size: 8pt; color: {TEXT_DIM}; min-height: 14px; }}
QPushButton#pillkey[recording="true"] {{ border-color: {BLUE}; color: {TEXT_BRIGHT}; }}
QPushButton#pillkey[clash="true"] {{ border-color: {RED}; }}
QFrame#tagfield {{ background: {FIELD}; border: 1px solid {LINE}; border-radius: 3px; }}
QFrame#tag {{ background: {BG_DARK}; border: 1px solid {BTN}; border-radius: 3px; }}
QFrame#tag[armed="true"] {{ background: transparent; border: 1px dashed {BTN_HOVER}; }}
QLabel#tagname {{ color: {TEXT}; }}
QLabel#tagname[armed="true"] {{ color: {TEXT_FAINT}; }}
QPushButton#tagstate {{ background: {BTN}; border: 1px solid transparent; border-radius: 2px; padding: 0 6px;
    font-family: {CHIP_FONT}; font-size: 8pt; font-weight: 600; color: {TEXT_DIM}; min-height: 14px; }}
QPushButton#tagstate:hover {{ color: {TEXT}; }}
QPushButton#tagstate[clash="true"] {{ background: #5e2421; color: {RED}; }}
QPushButton#tagstate[sel="true"] {{ background: {ACCENT}; color: {LINE}; }}
QLineEdit#tagedit {{ background: transparent; border: none; padding: 0 7px 0 8px; color: {TEXT}; }}
QPushButton#pin {{ padding: 0; }}
QPushButton#seg {{ padding: 3px 0; min-width: 24px; max-width: 24px; }}
QCheckBox {{ spacing: 6px; color: {TEXT_DIM}; }}
QCheckBox::indicator {{ width: 12px; height: 12px; border-radius: 2px; border: 1px solid {LINE}; background: {BTN}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; border-color: {LINE}; }}
QCheckBox:disabled {{ color: {TEXT_FAINT}; }}
QMenu {{ background: {BG}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 5px 22px 5px 8px; border-radius: 2px; }}
QMenu::item:selected {{ background: {BTN_HOVER}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::indicator {{ width: 12px; height: 12px; border-radius: 2px; border: 1px solid {LINE}; background: {BTN}; margin-left: 4px; }}
QMenu::indicator:checked {{ background: {BLUE}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 2px; }}
QToolTip {{ background: {BG_DARK}; color: {TEXT}; border: 1px solid {BTN}; padding: 4px 6px; }}
"""


_ICON_PM = None


def _icon_pixmap() -> QPixmap:
    global _ICON_PM
    if _ICON_PM is None:
        _ICON_PM = make_icon().pixmap(64, 64)
    return _ICON_PM


def on_screen(x, y) -> bool:
    """A saved position is only reused if it's still on a connected monitor."""
    return QApplication.screenAt(QPoint(int(x) + 40, int(y) + 20)) is not None


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(BG_DARK))
    p.setPen(QPen(QColor(ACCENT), 3))
    p.drawRoundedRect(QRectF(6, 14, 52, 36), 10, 10)
    p.setPen(Qt.NoPen)
    p.setBrush(ON_COLOR)
    p.drawEllipse(QRectF(34, 22, 20, 20))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    resolve_windows = Signal(bool)  # from the watcher thread
    layer_settings = Signal(object)  # from the layer-name reader thread

    def __init__(self, demo: bool, standby: bool = False, ignore_pid: int | None = None):
        super().__init__()
        self._sized = False  # first fit done: the window has its starting width
        self._grid_need = 0
        self._quitting = False
        self.server = None  # single-instance QLocalServer, set by main()
        self.demo = demo
        self.standby = standby  # launched by "Start with Resolve": stay hidden until Resolve runs
        self._parked = standby
        self._main_closed = False  # main window hidden from the overlay's ✕
        self.cfg = load_config(demo)
        self.bridge = ResolveBridge(demo)
        self.preset_slot = "1"
        self._sig = None
        self._tick = 0
        self._busy = False
        self._rows: list[str] = []
        self._nodes: dict[str, list | None] = {}
        self._reasons: dict[str, str] = {}
        self.pickers: dict[tuple[int, str], NodePicker] = {}
        self.pills: dict[str, LayerPill] = {}
        self._grid_widgets: list[QWidget] = []
        self._layers = 1
        self._proj_key = None
        self.rkeys = ResolveShortcuts()
        self.rkeys.load()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}" + (" — demo" if demo else ""))
        self.setWindowIcon(make_icon())
        self._build_ui()
        if self.cfg.get("pinned"):
            QTimer.singleShot(0, self._apply_pin)

        self.overlay = Overlay()
        self.overlay.row_clicked.connect(self.run_toggle)
        self.overlay.open_main.connect(self.open_main)
        self.overlay.close_main.connect(self.close_main)
        self.overlay.quit_all.connect(self.quit_all)
        self.overlay.moved.connect(self._overlay_moved)
        ov = self.cfg["overlay"]
        self.overlay.set_toggles(self.cfg["toggles"])
        if ov.get("x") is not None and on_screen(ov["x"], ov["y"]):
            self.overlay.move(ov["x"], ov["y"])
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            self.overlay.move(geo.right() - self.overlay.width() - 24, geo.top() + 80)

        self.hotkeys = HotkeyEngine()
        self.hotkeys.fired.connect(self.on_hotkey)
        self.hotkeys.focus_only = self.cfg["focus_only"]
        self._bind()
        if not self.hotkeys.start():
            self.flash(self.hotkeys.error, 0)

        self._show_nodes(self._rows_for(1))
        self.refresh_states()
        self._suspended = False
        self._main_wanted = not standby  # Start with Resolve starts as the overlay alone
        self.resolve_windows.connect(self._on_resolve_windows, Qt.QueuedConnection)
        self.layer_settings.connect(self._apply_layer_settings, Qt.QueuedConnection)
        self._layer_read_busy = False
        self._layer_read_at = 0.0
        if not demo:
            QTimer.singleShot(1500, self._read_layers)
            threading.Thread(target=watch_resolve_layer_settings, args=(self._layer_watch_emit,), daemon=True).start()
        self.watch = ResolveWatch(self._resolve_closed, self._resolve_windows_thread, ignore_pid)
        self._native = [int(self.winId()), int(self.overlay.winId())]  # for hiding from the watcher thread
        if demo or (self.watch.running() and not standby):
            self._parked = False
            self.try_connect()
        else:
            self._set_disconnected("Resolve isn't running — start it and this connects by itself")
        self._keys_hint()

        self._pos_timer = QTimer(self, singleShot=True, interval=400)
        self._pos_timer.timeout.connect(self.save)
        wp = self.cfg["window"]
        if wp.get("x") is not None and on_screen(wp["x"], wp["y"]):
            self.move(wp["x"], wp["y"])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)

    # ---------------------------------------------------------------- UI build
    def _build_ui(self):
        root = QWidget(objectName="root")
        v = QVBoxLayout(root)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(8)

        # header: refresh · clean plate · pin · overlay
        h = QHBoxLayout()
        h.setSpacing(6)
        h.addStretch(1)
        if self.demo:
            nxt = QPushButton("Next clip ▸")
            nxt.setToolTip("Demo: move the playhead to the next clip")
            nxt.clicked.connect(self._demo_next_clip)
            h.addWidget(nxt)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Re-read the node tree from Resolve")
        refresh.clicked.connect(self._refresh_clicked)
        h.addWidget(refresh)
        clean = QPushButton("Clean Plate")
        clean.setToolTip("Reset all scenes: node picks, names and shortcuts (layer keys and node labels stay)")
        clean.clicked.connect(self.clean_plate)
        h.addWidget(clean)
        self.btn_pin = PinButton()
        self.btn_pin.setChecked(self.cfg.get("pinned", False))
        self.btn_pin.toggled.connect(self._set_pinned)
        h.addWidget(self.btn_pin)
        self.btn_overlay = QPushButton("Overlay", checkable=True)
        self.btn_overlay.setChecked(self.cfg["overlay"].get("visible", True))
        self.btn_overlay.toggled.connect(self._set_overlay_visible)
        h.addWidget(self.btn_overlay)
        for i in range(h.count()):
            if w := h.itemAt(i).widget():
                w.setProperty("head", True)  # styled: darker with yellow text when down
        v.addLayout(h)

        # grid
        self.grid_frame = QFrame(objectName="grid")
        self.grid = QGridLayout(self.grid_frame)
        self.grid.setContentsMargins(10, 8, 10, 10)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(4)
        self.grid.setColumnMinimumWidth(0, 112)
        for c in range(1, NUM_TOGGLES + 1):
            self.grid.setColumnStretch(c, 1)
            self.grid.setColumnMinimumWidth(c, 130)
        self.grid_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        v.addWidget(self.grid_frame)
        v.addStretch(1)

        # persistent per-scene widgets
        self.name_edits, self.hotkey_btns, self.state_btns, self.one_marks = [], [], [], []
        for i, t in enumerate(self.cfg["toggles"]):
            ne = SceneNameEdit(t["name"])
            ne.setObjectName("colname")
            ne.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)  # in line with the node names below
            ne.setMaxLength(24)
            ne.setToolTip("Scene name — click to rename\nEnter: confirm, next name · Shift+Enter: confirm, previous name · Esc: cancel")
            ne.textEdited.connect(lambda text, i=i: self._rename(i, text))
            ne.cancelled.connect(lambda text, i=i: self._rename(i, text))
            ne.confirmed.connect(lambda step, i=i: self._next_name(i, step))
            self.name_edits.append(ne)
            hb = HotkeyButton()
            hb.set_hotkey(t.get("hotkey"))
            hb.changed.connect(lambda hk, i=i: self._set_hotkey(i, hk))
            self.hotkey_btns.append(hb)
            sb = QPushButton(objectName="state")
            sb.setMinimumWidth(44)  # ON / OFF / — all the same width
            sb.setCursor(Qt.PointingHandCursor)
            sb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            sb.setToolTip("Click to toggle · right-click for Force ON / OFF and This clip only")
            sb.setContextMenuPolicy(Qt.CustomContextMenu)
            sb.clicked.connect(lambda _=False, i=i: self.run_toggle(i))
            sb.customContextMenuRequested.connect(lambda pos, i=i, sb=sb: self._state_menu(i, sb.mapToGlobal(pos)))
            self.state_btns.append(sb)
            om = QLabel("1 clip", objectName="one")
            om.setAlignment(Qt.AlignCenter)
            om.setToolTip("This scene only acts on the clip under the playhead")
            self.one_marks.append(om)

        self.tags_field = TagsField()
        self.tags_field.add_tag = self._add_tag
        self.tags_field.set_label = self.run_label
        self.tags_field.changed.connect(self._tags_changed)

        # footer: connection · flash · options · presets
        f = QHBoxLayout()
        f.setSpacing(6)
        self.conn_dot = StateDot(7)
        self.conn_label = QLabel("Connecting…", objectName="muted")
        f.addWidget(self.conn_dot)
        f.addWidget(self.conn_label)
        f.addStretch(1)
        self.conn_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.conn_label.setMaximumWidth(520)
        # status line: its own row, above the footer
        self.status = QLabel("", objectName="flash")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setContentsMargins(2, 0, 0, 0)
        v.addWidget(self.status)
        self.chk_auto = QCheckBox("Start with Resolve")
        self.chk_auto.setChecked(autostart_enabled() if not self.demo else False)
        self.chk_auto.setToolTip("Wait in the background from login. When a project opens in Resolve, only the overlay "
                                 "appears (its icon opens this window); it disappears when Resolve quits")
        self.chk_auto.toggled.connect(self._set_autostart)
        f.addWidget(self.chk_auto)
        f.addSpacing(6)
        self.chk_focus = QCheckBox("Keys only in Resolve")
        self.chk_focus.setChecked(self.cfg["focus_only"])
        self.chk_focus.setToolTip("Hotkeys fire only while Resolve (or this window) is focused")
        self.chk_focus.toggled.connect(self._set_focus_only)
        if not IS_WIN:
            self.chk_focus.setEnabled(False)
            self.chk_focus.setToolTip("Windows only")
        f.addWidget(self.chk_focus)
        f.addSpacing(10)
        f.addWidget(QLabel("Preset", objectName="muted"))
        self.seg_btns = []
        for s in range(1, NUM_PRESETS + 1):
            b = QPushButton(str(s), objectName="seg", checkable=True)
            b.clicked.connect(lambda _=False, s=str(s): self._pick_preset(s))
            self.seg_btns.append(b)
            f.addWidget(b)
        self.btn_save = QPushButton("Save")
        self.btn_load = QPushButton("Load")
        self.btn_save.clicked.connect(lambda: self.save_preset(self.preset_slot))
        self.btn_load.clicked.connect(lambda: self.load_preset(self.preset_slot))
        f.addWidget(self.btn_save)
        f.addWidget(self.btn_load)
        v.addLayout(f)
        self._pick_preset("1")

        self.setCentralWidget(root)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self.flash(""))
        self.flash("")

    def _layer_name(self, n: int) -> str:
        return self.cfg["layer_names"].get(str(n)) or f"Layer {n}"

    def _layer_key_title(self, key: str) -> str:
        layer = slot_layer(key)
        return (self.cfg["layer_names"].get(str(layer)) or f"L{layer}") if layer else slot_title(key)

    def _slot_order(self) -> list[str]:
        """Every graph in Resolve's processing order; the post layer (if any) comes after Post-Group."""
        n = self._layers
        post = n if self.cfg["post_layer"] and n > 1 else None
        return (["preclip"] + [clip_slot(k) for k in range(1, n + 1) if k != post] + ["postclip"]
                + ([clip_slot(post)] if post else []) + ["timeline"])

    def _rows_for(self, layers: int) -> list[str]:
        """Grid rows in Resolve's processing order: one row per node stack layer in the project
        (Project Settings › Node Stack Layers), plus any deeper layer a scene still points at."""
        saved = [slot_layer(s) or 0 for t in self.cfg["toggles"] for s in t["sel"]]
        self._layers = max([layers, 1] + saved)
        return self._slot_order()

    def _rebuild_grid(self, rows: list[str]):
        self._rows = rows
        while self.grid.count():
            self.grid.takeAt(0)
        shared = self.name_edits + self.hotkey_btns + self.state_btns + self.one_marks + [self.tags_field]
        for w in shared:
            w.setParent(self.grid_frame)  # keep the shared widgets alive while old rows are deleted
        for w in self._grid_widgets:
            w.hide()
            w.deleteLater()
        self._grid_widgets = []
        self.pickers = {}
        self.pills = {}

        def temp(w):
            self._grid_widgets.append(w)
            return w

        def label(text, layer=False):
            lab = temp(QLabel(text, objectName="rowlabel"))
            lab.setProperty("layer", layer)
            lab.setFixedHeight(26)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)  # like Resolve's row labels
            return lab

        top_right = Qt.AlignRight | Qt.AlignTop
        r = 0
        for i in range(NUM_TOGGLES):
            self.grid.addWidget(self.name_edits[i], r, i + 1)
        r += 1
        for row in rows:
            n = slot_layer(row)
            if n:
                lab = label(self._layer_name(n), layer=True)
                lab.setToolTip("Layer name from Resolve › Project Settings › General Options › Color.\n"
                               "Updates on Refresh while that settings window is open.")
                self.grid.addWidget(lab, r, 0)
            else:
                self.grid.addWidget(label(slot_title(row)), r, 0)
            for i, t in enumerate(self.cfg["toggles"]):
                p = temp(NodePicker(i, row))
                p.set_data(self._nodes.get(row), self._reasons.get(row, "Not connected"), t["sel"].get(row, []))
                p.changed.connect(self._selection_changed)
                self.pickers[(i, row)] = p
                self.grid.addWidget(p, r, i + 1)
            r += 1
        self.grid.addWidget(temp(QFrame(objectName="sep")), r, 0, 1, NUM_TOGGLES + 1)
        r += 1
        # State
        self.grid.addWidget(label("State"), r, 0)
        for i in range(NUM_TOGGLES):
            cell = QHBoxLayout()
            cell.setContentsMargins(1, 0, 0, 0)
            cell.setSpacing(6)
            cell.addWidget(self.state_btns[i])
            cell.addWidget(self.one_marks[i])
            cell.addStretch(1)
            holder = temp(QWidget())
            holder.setLayout(cell)
            self.grid.addWidget(holder, r, i + 1)
        r += 1
        # Scene shortcut
        self.grid.addWidget(label("Scene shortcut"), r, 0)
        for i in range(NUM_TOGGLES):
            self.grid.addWidget(self.hotkey_btns[i], r, i + 1)
        r += 1
        # Layer shortcut
        self.grid.addWidget(label("Layer shortcut"), r, 0, top_right)
        holder = temp(FlowHolder())
        flow = holder.flow
        for key in self._slot_order():
            lk = self.cfg["layer_keys"].setdefault(key, {"hotkey": None, "on": True, "one_clip": False})
            layer = slot_layer(key)
            tip = f"Whole {self._layer_name(layer) if layer else slot_title(key)} on / off"
            tip += "\nTurning it back ON leaves OFF node labels and nodes picked in OFF scenes off"
            pill = LayerPill(key, self._layer_key_title(key), tip, holder)
            pill.hk.set_hotkey(lk.get("hotkey"))
            pill.hk.changed.connect(lambda hk, key=key: self._set_layer_hotkey(key, hk))
            pill.flip.connect(self.run_layer)
            pill.menu.connect(self._layer_menu)
            pill.set_state(lk["on"], lk["one_clip"])
            self.pills[key] = pill
            flow.addWidget(pill)
        self.grid.addWidget(holder, r, 1, 1, NUM_TOGGLES)
        r += 1
        self.grid.addWidget(temp(QFrame(objectName="sep")), r, 0, 1, NUM_TOGGLES + 1)
        r += 1
        # Node labels
        self.grid.addWidget(label("Node Label"), r, 0, top_right)
        self.grid.addWidget(self.tags_field, r, 1, 1, NUM_TOGGLES)
        self._refresh_tags()
        self._mark_clashes()
        QTimer.singleShot(0, self._fit_height)
        QTimer.singleShot(80, self._fit_height)  # again once new cells are styled

    def _refresh_clicked(self):
        if self.bridge.resolve is not None and self._visible_tags():
            self._sync_labels(self._visible_tags())
            self.save()
            self._refresh_tags()
        self.rkeys.load(force=True)
        self._mark_clashes()
        self._keys_hint()
        if self.bridge.resolve:
            self.refresh_context(True)
        else:
            self.try_connect()
        self._read_layers()

    def _read_layers(self):
        """Copy Resolve's layer names / Post Group setting (background thread, see read_resolve_layer_settings)."""
        if self.demo or self._layer_read_busy:
            return
        self._layer_read_busy = True
        self._layer_read_at = time.monotonic()

        def work():
            try:
                self.layer_settings.emit(read_resolve_layer_settings())
            except Exception:
                self.layer_settings.emit(None)
        threading.Thread(target=work, daemon=True).start()

    def _layer_watch_emit(self, d):
        self.layer_settings.emit(("watch", d))

    def _apply_layer_settings(self, d):
        if isinstance(d, tuple):  # from the background watcher, not a Refresh read
            d = d[1]
        else:
            self._layer_read_busy = False
        if not d:
            return
        names = {k: v for k, v in d["names"].items() if v.strip()}
        post = self.cfg["post_layer"] if d["post"] is None else d["post"]
        if names == self.cfg["layer_names"] and post == self.cfg["post_layer"]:
            return
        self.cfg["layer_names"] = names
        self.cfg["post_layer"] = post
        self.save()
        self._rows = []  # rebuild so rows and layer pills show the new names / order
        self._show_nodes(self._rows_for(self._layers))

    def changeEvent(self, e):  # coming back from Resolve's Project Settings: pick up renamed layers
        super().changeEvent(e)
        if e.type() == e.Type.ActivationChange and self.isActiveWindow() \
                and time.monotonic() - self._layer_read_at > 3:
            self._read_layers()

    def _keys_hint(self):
        st = self.rkeys.status
        if st == "builtin":
            self.flash("Resolve is on a built-in keyboard layout: save it as a custom preset "
                       "(Keyboard Customization › Save As) to get shortcut clash warnings", 12000)
        elif st in ("missing", "error"):
            self.flash("Couldn't read Resolve's keyboard shortcuts, so clashes can't be checked", 8000)

    def _mark_clashes(self):
        for b in self.hotkey_btns + [p.hk for p in self.pills.values()]:
            b.set_clash(self.rkeys.clash(b.hotkey))

    def _confirm_key(self, hk, who: str) -> bool:
        """True = keep the key. Asks first when Resolve already uses it."""
        self.rkeys.load()
        hits = self.rkeys.clash(hk)
        if not hits:
            return True
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
        box = QMessageBox(self)
        box.setWindowTitle("Shortcut already used by Resolve")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"{hk['text']} is also Resolve's “{'”, “'.join(hits[:3])}”{more}.")
        box.setInformativeText(f"If you give it to {who}, both will fire when you press it.")
        keep = box.addButton("Use anyway", QMessageBox.AcceptRole)
        box.addButton("Pick another", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is keep

    def _fit_height(self):
        lay = self.centralWidget().layout()
        lay.activate()
        gw = self.grid_frame.width() if self._sized else 1000 - 28
        gh = self.grid.heightForWidth(gw)
        if gh <= 0:
            gh = self.grid.sizeHint().height()
        gh = max(gh, self.grid.minimumSize().height())  # never less than the rows' minimum (wrapped node labels)
        # the grid's plain sizeHint over-estimates wrapped rows; use its height-for-width instead
        frame = self.grid_frame.frameWidth() * 2
        self._grid_need = gh + frame
        # pin the grid to what its rows need: its "Maximum" size policy otherwise caps it at a sizeHint
        # that doesn't count wrapped rows, and a second row of node labels gets clipped
        self.grid_frame.setFixedHeight(gh + frame)
        self.setFixedHeight(self.centralWidget().sizeHint().height())  # already counts the grid's fixed height
        QTimer.singleShot(0, self._fit_adjust)

    def _fit_adjust(self):
        """Correct by whatever the grid actually got vs what it needs (sizeHints of wrapped rows are rough)."""
        self.centralWidget().layout().activate()
        delta = self._grid_need - self.grid_frame.height()
        if delta:
            self.setFixedHeight(self.height() + delta)
        if not self._sized:
            self._sized = True
            self.resize(1000, self.height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._sized and e.oldSize().width() != e.size().width():
            QTimer.singleShot(0, self._fit_height)

    # ---------------------------------------------------------------- helpers
    def flash(self, msg: str, ms: int = 4000):
        idle = not msg
        self.status.setText("" if idle else msg)
        self.status.setToolTip("" if idle else msg)
        self.status.setProperty("idle", idle)
        repolish(self.status)
        if ms:
            self._flash_timer.start(ms)
        else:
            self._flash_timer.stop()

    def save(self):
        save_config(self.cfg, self.demo)

    def _bind(self):
        b = [(i, t.get("hotkey")) for i, t in enumerate(self.cfg["toggles"])]
        b += [(k, v.get("hotkey")) for k, v in self.cfg["layer_keys"].items()]
        self.hotkeys.set_bindings(b)

    def refresh_states(self):
        for i, t in enumerate(self.cfg["toggles"]):
            configured = has_selection(t)
            st = ("armed" if t["on"] is None else "on" if t["on"] else "off") if configured else "idle"
            word = {"on": "ON", "off": "OFF", "armed": "ARMED", "idle": "—"}[st]
            sb = self.state_btns[i]
            sb.setProperty("state", st)
            sb.setText(word)
            sb.setEnabled(configured)
            repolish(sb)
            self.one_marks[i].setVisible(bool(t.get("one_clip")))
        for key, pill in self.pills.items():
            lk = self.cfg["layer_keys"].get(key, {})
            pill.set_state(lk.get("on", True), lk.get("one_clip", False))
        self.overlay.set_toggles(self.cfg["toggles"])
        # Start with Resolve: the overlay is the whole UI until the main window is opened from it
        overlay_only = (self.standby or self._main_closed) and not self.isVisible()
        self.overlay.main_open = self.isVisible()
        self.overlay.update()
        self.overlay.setVisible((self.btn_overlay.isChecked() or overlay_only) and not self._parked)

    # ---------------------------------------------------------------- tags
    def _visible_tags(self) -> list[dict]:
        pk = self._proj_key or "_"
        return [t for t in self.cfg["tags"] if t["scope"] == "F" or t.get("project") == pk]

    def _label_clashes(self) -> set[str]:
        """Node Labels whose ON/OFF disagrees with a scene (not ARMED) that picks a node with that label."""
        out = set()
        for tag in self._visible_tags():
            if tag["on"] is None:  # ARMED: nothing to compare yet
                continue
            name = tag["name"].lower()
            for t in self.cfg["toggles"]:
                if t["on"] is None or t["on"] == tag["on"]:
                    continue
                if any(str(s.get("label") or "").lower() == name for v in t["sel"].values() for s in v):
                    out.add(name)
                    break
        return out

    def _refresh_tags(self):
        self.tags_field.clash = self._label_clashes()
        self.tags_field.set_tags(self._visible_tags())
        # red dot on every scene cell that picks a node whose Node Label is set the other way
        labels = {t["name"].lower(): t for t in self._visible_tags() if t["on"] is not None}
        for (i, slot), picker in self.pickers.items():
            t = self.cfg["toggles"][i]
            names = []
            if t["on"] is not None:
                for sp in t["sel"].get(slot, []):
                    tag = labels.get(str(sp.get("label") or "").lower())
                    if tag and tag["on"] != t["on"] and tag["name"] not in names:
                        names.append(tag["name"])
            picker.set_label_clash(names)
        QTimer.singleShot(0, self._fit_height)

    def _add_tag(self, name: str):
        if any(t["name"].lower() == name.lower() for t in self._visible_tags()):
            self.flash(f"“{name}” is already in Node Label")
        else:
            tag = {"name": name, "on": None, "scope": "P", "project": self._proj_key or "_"}  # ARMED
            self.cfg["tags"].append(tag)
            self._sync_labels([tag], quiet_found=True, keep_state=True)
            self.save()
        self.tags_field.typing = True
        self._refresh_tags()

    def _sync_labels(self, tags: list[dict], quiet_found=False, keep_state=False):
        """Set label chips to the real state of those nodes on the open clip (and timeline).
        ARMED labels (and keep_state) only get checked for existence — they stay grey until ON / OFF is picked."""
        if not tags or self._busy or self.bridge.resolve is None:
            return
        try:
            proj, tl, _ = self.bridge.context()
            states = self.bridge.node_states(proj, tl) if proj and tl else None
        except Exception:
            states = None
        if states is None:
            self.flash("Couldn't read node states from Resolve (open the Color page with a clip)")
            return
        missing = []
        for tag in tags:
            hits = [on for label, on in states if label and label.lower() == tag["name"].lower()]
            if hits:
                if not keep_state and tag["on"] is not None:
                    tag["on"] = any(hits)
            else:
                missing.append(tag["name"])
        if missing:
            self.flash(f"No node labelled {', '.join('“%s”' % m for m in missing)} on this clip")
        elif not quiet_found:
            self.flash("Node Label states synced with the open clip")

    def _tags_changed(self):
        pk = self._proj_key or "_"
        out = []
        for t in self.cfg["tags"]:
            if t.pop("_remove", False):
                continue
            if t.pop("_rescope", False):
                t["project"] = None if t["scope"] == "F" else pk
            out.append(t)
        self.cfg["tags"] = out
        self.save()
        self._refresh_tags()

    def _keep_off(self, slot: str):
        """Skip function for switching a whole graph back ON: leaves off the nodes that should stay off —
        nodes with an OFF node label, and nodes explicitly picked in a scene that is OFF."""
        off_labels = {t["name"].lower() for t in self._visible_tags() if t["on"] is False}
        ex_labels, idx = set(), set()
        for t in self.cfg["toggles"]:
            if t["on"] is not False or not has_selection(t):
                continue
            for s in t["sel"].get(slot, []):
                if s.get("all"):
                    continue
                if s.get("label"):
                    ex_labels.add(s["label"])
                else:
                    idx.add(s.get("index"))
        if not (off_labels or ex_labels or idx):
            return None

        def skip(node):
            i, label = node[0], node[1]
            if label:
                return label.lower() in off_labels or label in ex_labels
            return i in idx
        return skip

    # ---------------------------------------------------------------- edits
    def _next_name(self, i: int, step: int = 1):
        nxt = self.name_edits[(i + step) % NUM_TOGGLES]
        nxt.setFocus(Qt.TabFocusReason)

    def _rename(self, i: int, text: str):
        self.cfg["toggles"][i]["name"] = text.strip() or f"Scene {i + 1}"
        self.save()
        self.refresh_states()

    def _key_owner(self, hk, skip) -> tuple | None:
        def same(o):  # no virtual-key code (some Linux setups): compare the key text instead
            if not o or o.get("mods") != hk["mods"]:
                return False
            if hk["vk"] is None or o.get("vk") is None:
                return o.get("text") == hk["text"]
            return o.get("vk") == hk["vk"]

        for j, t in enumerate(self.cfg["toggles"]):
            if j != skip and same(t.get("hotkey")):
                return ("scene", j)
        for k, v in self.cfg["layer_keys"].items():
            if k != skip and same(v.get("hotkey")):
                return ("layer", k)
        return None

    def _clear_owner(self, owner, hk, new_name):
        kind, who = owner
        if kind == "scene":
            old = self.cfg["toggles"][who]["name"]
            self.cfg["toggles"][who]["hotkey"] = None
            self.hotkey_btns[who].set_hotkey(None)
        else:
            old = self._layer_key_title(who)
            self.cfg["layer_keys"][who]["hotkey"] = None
            if who in self.pills:
                self.pills[who].hk.set_hotkey(None)
        self.flash(f"{hk['text']} moved from {old} to {new_name}")

    def _set_hotkey(self, i: int, hk):
        if hk and not self._confirm_key(hk, self.cfg["toggles"][i]["name"]):
            btn = self.hotkey_btns[i]
            btn.set_hotkey(self.cfg["toggles"][i].get("hotkey"))
            QTimer.singleShot(0, btn.start)
            return
        if hk:
            owner = self._key_owner(hk, i)
            if owner:
                self._clear_owner(owner, hk, self.cfg["toggles"][i]["name"])
        self.cfg["toggles"][i]["hotkey"] = hk
        if self.hotkey_btns[i].hotkey != hk:
            self.hotkey_btns[i].set_hotkey(hk)
        self._mark_clashes()
        self._bind()
        self.save()
        self.refresh_states()

    def _set_layer_hotkey(self, key: str, hk):
        if hk and not self._confirm_key(hk, self._layer_key_title(key)):
            pill = self.pills.get(key)
            if pill:
                pill.hk.set_hotkey(self.cfg["layer_keys"].get(key, {}).get("hotkey"))
                QTimer.singleShot(0, pill.hk.start)
            return
        if hk:
            owner = self._key_owner(hk, key)
            if owner:
                self._clear_owner(owner, hk, self._layer_key_title(key))
        self.cfg["layer_keys"].setdefault(key, {"on": True, "one_clip": False})["hotkey"] = hk
        self._mark_clashes()
        self._bind()
        self.save()

    def _selection_changed(self, i: int, slot: str, sel: list):
        t = self.cfg["toggles"][i]
        if sel:
            t["sel"][slot] = sel
        else:
            t["sel"].pop(slot, None)
        t["on"] = None  # new picks: their current state is unknown until the scene fires
        self.save()
        self.refresh_states()
        self._refresh_tags()

    def _state_menu(self, i: int, gpos):
        t = self.cfg["toggles"][i]
        m = QMenu(self)
        m.addAction("Force ON", lambda: self.run_toggle(i, True))
        m.addAction("Force OFF", lambda: self.run_toggle(i, False))
        m.addSeparator()
        a = m.addAction("This clip only")
        a.setCheckable(True)
        a.setChecked(bool(t.get("one_clip")))
        a.toggled.connect(lambda on: self._set_one_clip(i, on))
        m.exec(gpos)

    def _layer_menu(self, key: str, gpos):
        lk = self.cfg["layer_keys"][key]
        m = QMenu(self)
        m.addAction("Force ON", lambda: self.run_layer(key, True))
        m.addAction("Force OFF", lambda: self.run_layer(key, False))
        if key != "timeline":  # the timeline graph is shared, so "this clip" means nothing there
            m.addSeparator()
            a = m.addAction("This clip only")
            a.setCheckable(True)
            a.setChecked(lk.get("one_clip", False))
            a.toggled.connect(lambda on: self._set_layer_one_clip(key, on))
        m.exec(gpos)

    def _set_layer_one_clip(self, key: str, on: bool):
        self.cfg["layer_keys"][key]["one_clip"] = on
        self.save()
        self.refresh_states()

    def _set_one_clip(self, i: int, on: bool):
        self.cfg["toggles"][i]["one_clip"] = on
        self.save()
        self.refresh_states()

    def showEvent(self, e):  # keep the overlay's ✕ in step with the main window however it was opened
        super().showEvent(e)
        self.overlay.main_open = True
        self.overlay.update()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.overlay.main_open = False
        self.overlay.update()

    def moveEvent(self, e):
        super().moveEvent(e)
        if self.isVisible() and not self.isMinimized():
            self.cfg["window"] = {"x": self.x(), "y": self.y()}
            self._pos_timer.start()  # save shortly after the drag ends

    def show_without_flash(self):
        """Open without the white flash and size jump: dark title bar, then show invisibly and
        fade in once the layout (and pin) have settled."""
        self.winId()
        if IS_WIN:
            import ctypes
            dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(dark), 4)  # dark title bar
        self.setWindowOpacity(0.0)
        self.show()
        QTimer.singleShot(160, lambda: self.setWindowOpacity(1.0))

    def clean_plate(self):
        """Reset every scene: no node picks, default name, no shortcut (layer keys and node labels are kept)."""
        for i in range(len(self.cfg["toggles"])):
            t = self.cfg["toggles"][i] = default_toggle(i)
            self.name_edits[i].setText(t["name"])
            self.hotkey_btns[i].set_hotkey(None)
        self._mark_clashes()
        self._bind()
        self.save()
        self._rows = []  # rebuild the grid without the old picks
        self._show_nodes(self._rows_for(self._layers))
        self.refresh_states()
        self.flash("Clean plate: all scenes reset")

    def _set_autostart(self, on: bool):
        if self.demo:
            return
        try:
            set_autostart(on)
            self.flash("Starts with Resolve from your next login" if on else "Won't start with Resolve any more")
        except Exception as e:
            self.flash(f"Couldn't change Start with Resolve: {e}")

    def _set_pinned(self, on: bool):
        self.cfg["pinned"] = on
        self.save()
        self._apply_pin()
        self.btn_pin.setToolTip("Pinned on top — click to unpin" if on else "Pin on top: keep this window above Resolve")

    def _apply_pin(self):
        """Stay-on-top without QWidget.setWindowFlag (that recreates the window, which flashes white):
        set the flag on the existing native window, and on Windows also set its topmost z-order."""
        on = self.cfg.get("pinned", False)
        self.winId()  # make sure the native window exists
        self.windowHandle().setFlag(Qt.WindowStaysOnTopHint, on)
        if IS_WIN:
            import ctypes
            ctypes.windll.user32.SetWindowPos(int(self.winId()), -1 if on else -2, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0010)  # TOPMOST/NOTOPMOST, keep size/pos/focus

    def _set_overlay_visible(self, on: bool):
        self.cfg["overlay"]["visible"] = on
        self.overlay.setVisible(on)
        self.save()

    def _overlay_moved(self, x, y):
        self.cfg["overlay"].update({"x": x, "y": y})
        self.save()

    def _set_focus_only(self, on: bool):
        self.cfg["focus_only"] = on
        self.hotkeys.focus_only = on
        self.save()

    # ---------------------------------------------------------------- Resolve sync
    def try_connect(self) -> bool:
        ok = self.bridge.connect()
        if not ok:
            self._set_disconnected(self.bridge.error)
        else:
            self._sig = None
            self.refresh_context(force=True)
        return ok

    def _set_disconnected(self, why: str):
        self.bridge.resolve = None
        self._sig = None
        self.conn_dot.set_state(False)
        self.conn_label.setText("Not connected")
        self.conn_label.setToolTip(why)
        self._nodes = {}
        self._reasons = {}
        self._show_nodes(self._rows_for(1))

    def _show_nodes(self, rows: list[str]):
        if rows != self._rows or not self.pickers:
            self._rebuild_grid(rows)
            return
        for (i, slot), p in self.pickers.items():
            p.set_data(self._nodes.get(slot), self._reasons.get(slot, "Not connected"),
                       self.cfg["toggles"][i]["sel"].get(slot, []))

    def _resolve_closed(self):
        """Resolve quit (called from the watcher thread): exit at once, pinned or not, even if the UI
        thread is stuck in a dead API call. Settings are saved on every change, so nothing is lost."""
        if self.demo:
            return
        with contextlib.suppress(Exception):
            self.hotkeys.stop()
            save_config(self.cfg, self.demo)
        if self.standby or autostart_enabled():  # come back by itself the next time Resolve starts
            with contextlib.suppress(Exception):
                if self.server:
                    self.server.close()
                kw = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN
                      else {"start_new_session": True})
                subprocess.Popen(_launch_cmd("--standby", "--after", str(self.watch.pid)), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        os._exit(0)

    def _resolve_windows_thread(self, up: bool):
        """Watcher thread: when Resolve's windows vanish, hide at once with native calls (works even if the
        UI thread is stuck in an API call) and stop calling Resolve; the UI thread catches up via the signal."""
        if not up:
            self._suspended = True
            if IS_WIN:
                import ctypes
                for hwnd in self._native:
                    ctypes.windll.user32.ShowWindowAsync(hwnd, 0)  # SW_HIDE
        self.resolve_windows.emit(up)

    def _wake(self):
        """Resolve's window is up: resume; the main window only comes back if it was open
        (with Start with Resolve it starts as the overlay alone, the window loaded but hidden)."""
        self._parked = False
        self._sig = None  # project may have changed: re-read everything
        if self._main_wanted:
            self.show_without_flash()
        self.refresh_states()

    def _on_resolve_windows(self, up: bool):
        if up:
            self._suspended = False
            if self._parked:
                self._wake()
        else:
            self._main_wanted = self.isVisible()
            self._parked = True
            self.hide()
            self.overlay.hide()

    def open_main(self):
        if self._parked:
            return
        self._main_wanted = True
        if not self.isVisible():
            self.show_without_flash()
        self.raise_()
        self.activateWindow()
        self.refresh_states()

    def close_main(self):
        """Overlay ✕: hide the main window but keep running (hotkeys and overlay stay)."""
        self._main_wanted = False
        self._main_closed = True
        self.hide()
        self.refresh_states()

    def quit_all(self):
        """Overlay power button: close the main window and the overlay (quits DaKillswitch)."""
        self._quitting = True
        self.close()
        QApplication.quit()

    def bring_to_front(self):
        if self._parked:
            return  # standby without Resolve: it will appear when Resolve starts
        self.open_main()

    def poll(self):
        self._tick += 1
        if self._busy:
            return
        if not self.demo and not self.watch.running():  # no Resolve: don't call into its API at all
            if self.bridge.resolve is not None:
                self._set_disconnected("Resolve isn't running")
            return
        if self._suspended:  # Resolve has no window (quitting or opening a project): leave it alone
            return
        if self._parked:  # standby: Resolve's window is up, so appear
            if not self.watch.windows_up:
                return
            self._wake()
        if self.bridge.resolve is None:
            if self._tick % 3 == 0:
                self.try_connect()
            return
        self.refresh_context()

    def refresh_context(self, force: bool = False):
        if self.bridge.resolve is None:
            self._set_disconnected(self.bridge.error or "Not connected")
            return
        try:
            proj, tl, item = self.bridge.context()
            graphs = self.bridge.graphs(proj, tl, item)
            grp = _safe(item.GetColorGroup) if item else None
            sig = (
                proj.GetName() if proj else None,
                tl.GetName() if tl else None,
                _safe(item.GetUniqueId) if item else None,
                _safe(grp.GetName) if grp else None,
                tuple((k, _safe(g.GetNumNodes) if g else None) for k, g in sorted(graphs.items())),
            )
            if sig == self._sig and not force:
                return
            self._sig = sig
            nodes = {k: (self.bridge.nodes(g) if g else None) for k, g in graphs.items()}
            pk = (_safe(getattr(proj, "GetUniqueId", None)) or _safe(proj.GetName)) if proj else None
        except Exception as e:
            self._set_disconnected(f"Lost connection to Resolve ({e})")
            return

        self.conn_dot.set_state(True)
        clip_layers = sum(1 for k in graphs if slot_layer(k))
        try:  # Resolve's Project Settings › Node Stack Layers is the reference for the rows
            layers = int(_safe(proj.GetSetting, "nodeStackLayers") or 0) if proj else 0
        except (TypeError, ValueError):
            layers = 0
        layers = layers or clip_layers or 1
        parts = [v] if (v := self.bridge.version()) else []
        if not proj:
            parts.append("no project open")
        elif not tl:
            parts.append("no timeline open")
        elif item:
            gname = _safe(grp.GetName) if grp else None
            parts.append(item.GetName() + (f"  ({gname})" if gname else ""))
            parts.append(f"{layers} layer{'s' if layers != 1 else ''}")
        else:
            parts.append("no clip under playhead")
        self.conn_label.setText("  ·  ".join(parts))
        self.conn_label.setToolTip(f"{proj.GetName() if proj else ''}  /  {tl.GetName() if tl else ''}")

        no_clip = "No clip under playhead"
        self._reasons = {"preclip": "Clip has no color group" if item else no_clip,
                         "postclip": "Clip has no color group" if item else no_clip,
                         "timeline": "No timeline open"}
        for n in range(1, MAX_LAYERS + 1):
            self._reasons[clip_slot(n)] = f"This clip has no layer {n}" if item else no_clip
        self._nodes = nodes
        if pk != self._proj_key:
            self._proj_key = pk
            self._refresh_tags()
        self._show_nodes(self._rows_for(layers))

    def _demo_next_clip(self):
        self.bridge.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline().advance()
        self.refresh_context()

    # ---------------------------------------------------------------- actions
    def _recording(self) -> bool:
        btns = self.hotkey_btns + [p.hk for p in self.pills.values()]
        return any(b.recording or time.monotonic() - b.stopped_at < 0.4 for b in btns)

    def on_hotkey(self, key):
        if self._recording():
            return  # this key press was the one being recorded
        if QApplication.activeWindow() is self and isinstance(QApplication.focusWidget(), QLineEdit):
            return  # typing a scene name or a tag
        if isinstance(key, int):
            self.run_toggle(key)
        else:
            self.run_layer(key)

    def _sweep(self, title: str, word: str, one_clip: bool, per_item, per_tl):
        """Run per_item over the clip under the playhead (one_clip) or every enabled clip, then per_tl once.
        Returns (nodes changed, where) or None on failure."""
        if self._busy:
            return None
        if self.bridge.resolve is None and not self.try_connect():
            self.flash("Can't toggle: Resolve isn't connected")
            return None
        self._busy = True
        n, where = 0, ""
        try:
            proj, tl, item = self.bridge.context()
            if per_item:
                if not one_clip and tl:
                    items = self.bridge.all_video_items(tl)
                    groups, clips = set(), 0
                    for k, it in enumerate(items):
                        if k % 10 == 0:
                            self.flash(f"{title} → {word}…  {k}/{len(items)} clips", 0)
                            QApplication.processEvents()
                        if _safe(it.GetClipEnabled) is False:
                            continue
                        hit = per_item(it, groups, proj)
                        n += hit
                        clips += bool(hit)
                    where = f"on {clips} clip{'s' if clips != 1 else ''}"
                elif item:
                    n = per_item(item, None, proj)
                    where = "on this clip"
            if per_tl and tl:
                n += per_tl(tl)
        except Exception as e:
            self._set_disconnected(f"Lost connection to Resolve ({e})")
            self.flash("Toggle failed: lost connection to Resolve")
            return None
        finally:
            self._busy = False
        return n, where

    def _report(self, title, word, res):
        n, where = res
        if n:
            self.flash(f"{title} → {word}  ({n} node{'s' if n != 1 else ''}{' ' + where if where else ''})")
        else:
            self.flash(f"{title} → {word}  (none of its nodes found{' ' + where if where else ''})")

    def run_toggle(self, i: int, state: bool | None = None):
        t = self.cfg["toggles"][i]
        if not has_selection(t):
            self.flash(f"{t['name']} has no nodes selected")
            return
        new_state = (t["on"] is False) if state is None else state  # ON → OFF, OFF → ON, ARMED → OFF
        word = "ON" if new_state else "OFF"
        cache = {}

        def skip_for(slot):  # only matters when switching "Entire layer" picks back on
            if not new_state:
                return None
            if slot not in cache:
                cache[slot] = self._keep_off(slot)
            return cache[slot]
        sels = {k: v for k, v in t["sel"].items() if k != "timeline"}
        per_item = (lambda it, g, proj: self.bridge.apply_item(it, sels, new_state, g, skip_for)) if sels else None
        per_tl = (lambda tl: self.bridge.apply(_safe(tl.GetNodeGraph), t["sel"].get("timeline", []), new_state,
                                               skip_for("timeline")))
        res = self._sweep(t["name"], word, bool(t.get("one_clip")), per_item, per_tl)
        if res is None:
            return
        t["on"] = new_state
        self.save()
        self.refresh_states()
        self._refresh_tags()  # Node Labels turn red if they now disagree with this scene
        self._report(t["name"], word, res)

    def run_layer(self, key: str, state: bool | None = None):
        if not SLOT_RE.match(key):
            return
        lk = self.cfg["layer_keys"].setdefault(key, {"hotkey": None, "on": True, "one_clip": False})
        new_state = (not lk["on"]) if state is None else state
        word = "ON" if new_state else "OFF"
        title = self._layer_key_title(key)
        protect = self._keep_off(key) if new_state else None  # back ON: keep the "should stay off" nodes off
        ALL = [{"all": True}]
        b = self.bridge
        per_item = per_tl = None
        if key == "timeline":
            per_tl = lambda tl: b.apply(_safe(tl.GetNodeGraph), ALL, new_state, protect)
        elif key in ("preclip", "postclip"):
            getter = "GetPreClipNodeGraph" if key == "preclip" else "GetPostClipNodeGraph"

            def per_item(it, g, proj):
                grp = b.group_once(it, g)
                return b.apply(_safe(getattr(grp, getter)), ALL, new_state, protect) if grp else 0
        else:
            layer = slot_layer(key)
            per_item = lambda it, g, proj: b.apply(_safe(it.GetNodeGraph, layer), ALL, new_state, protect)
        one = bool(lk.get("one_clip")) and key != "timeline"
        res = self._sweep(title, word, one, per_item, per_tl)
        if res is None:
            return
        lk["on"] = new_state
        self.save()
        self.refresh_states()
        self._report(title, word, res)

    def run_label(self, tag: dict, state: bool | None = None):
        """Switch every node with this label ON / OFF: all layers, groups and the timeline, every enabled clip.
        state None = flip (ARMED → OFF)."""
        new_state = (tag["on"] is False) if state is None else state
        word = "ON" if new_state else "OFF"
        sel = [{"label": tag["name"], "ci": True}]
        b = self.bridge
        cap = {}

        def per_item(it, g, proj):
            if "n" not in cap:
                cap["n"] = b.layer_cap(proj)
            n = sum(b.apply(lg, sel, new_state) for lg in b.layer_graphs(it, cap=cap["n"]))
            grp = b.group_once(it, g)
            if grp:
                n += b.apply(_safe(grp.GetPreClipNodeGraph), sel, new_state)
                n += b.apply(_safe(grp.GetPostClipNodeGraph), sel, new_state)
            return n

        res = self._sweep(f"“{tag['name']}”", word, False, per_item,
                          lambda tl: b.apply(_safe(tl.GetNodeGraph), sel, new_state))
        if res is None:
            return
        n, where = res
        if n:
            tag["on"] = new_state
            self.save()
            self._refresh_tags()
            self.flash(f"“{tag['name']}” → {word}  ({n} node{'s' if n != 1 else ''} {where})")
        else:  # nothing matched: leave the label's state alone
            self.flash(f"No nodes labelled “{tag['name']}” found on enabled clips — check the spelling in Resolve")

    def _pick_preset(self, slot: str):
        self.preset_slot = slot
        for b in self.seg_btns:
            b.setChecked(b.text() == slot)
        has = bool(self.cfg["presets"].get(slot))
        self.btn_load.setEnabled(has)
        self.btn_load.setToolTip(f"Load preset {slot}" if has else f"Preset {slot} is empty")

    def save_preset(self, slot: str):
        self.cfg["presets"][slot] = [{**copy.deepcopy(t), "on": None} for t in self.cfg["toggles"]]
        self.save()
        self._pick_preset(slot)
        self.flash(f"Saved preset {slot}")

    def load_preset(self, slot: str):
        data = self.cfg["presets"].get(slot)
        if not data:
            return
        self.cfg["toggles"] = [clean_toggle(i, data[i] if i < len(data) else {}) for i in range(NUM_TOGGLES)]
        for i, t in enumerate(self.cfg["toggles"]):
            self.name_edits[i].setText(t["name"])
            self.hotkey_btns[i].set_hotkey(t.get("hotkey"))
        self._mark_clashes()
        self._bind()
        self.save()
        self.refresh_states()
        self._rows = []  # force a rebuild in case the preset uses other layers
        if self.bridge.resolve:
            self.refresh_context(force=True)
        else:
            self._show_nodes(self._rows_for(1))
        self.flash(f"Loaded preset {slot} (states reset to ON)")

    def closeEvent(self, e):
        if not self._quitting:  # the window's X only closes the main window; the overlay stays
            e.ignore()
            self.close_main()
            return
        self.hotkeys.stop()
        self.overlay.close()
        self.save()
        super().closeEvent(e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use a fake Resolve to try the UI")
    ap.add_argument("--standby", action="store_true", help="stay hidden until Resolve runs (Start with Resolve)")
    ap.add_argument("--after", type=int, help=argparse.SUPPRESS)  # pid of a Resolve that is shutting down
    args = ap.parse_args()
    if args.demo:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    pal = app.palette()  # dark from the very first paint, before the stylesheet kicks in
    for role in (QPalette.Window, QPalette.Base, QPalette.Button):
        pal.setColor(role, QColor(BG))
    app.setPalette(pal)
    app.setStyleSheet(STYLE)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = f"{AUTOSTART_NAME}-{os.environ.get('USERNAME') or os.environ.get('USER') or 'user'}" + ("-demo" if args.demo else "")
    sock = QLocalSocket()
    sock.connectToServer(name)
    if sock.waitForConnected(300):  # already running: bring that window forward instead of opening another
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(name)
    app.setQuitOnLastWindowClosed(False)  # lives on with only the overlay (or nothing) showing; ⏻ or Resolve quitting ends it
    w = MainWindow(args.demo, standby=args.standby, ignore_pid=args.after)
    w.server = QLocalServer()
    w.server.listen(name)
    w.server.newConnection.connect(lambda: (w.server.nextPendingConnection(), w.bring_to_front()))
    if not w._parked:
        w.show_without_flash()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
