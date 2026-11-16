#!/usr/bin/env python3
"""
Fuse Box (formerly Node Toggle) — switch DaVinci Resolve color nodes on and off with global hotkeys.

  * 5 independent toggles (columns), each with its own shortcut
  * each toggle can hold nodes from Pre-Group, every clip layer (L1, L2, L3 …),
    Post-Group and Timeline — or a whole layer at once
  * scenes, layer keys and node labels act on the clip under the playhead; tick "Apply to all clips in
    the Timeline" for every enabled clip (right-click a scene key for "This clip only")
  * layer states: ON / OFF switch a layer / group whole over the scenes (one key each), HOLD puts the
    scenes' and node labels' states back
  * node labels: type a Resolve node label (e.g. NR); it starts on HOLD (grey dot), its ON / OFF dots
    switch every node with that label on every layer; turns red when a scene picks the same nodes
    with the opposite state
  * a layer's HOLD, and "Entire layer" picks switched back ON, keep nodes off that should stay off
    (OFF node labels and nodes picked in OFF scenes)
  * warns when a key you assign is already a Resolve shortcut (reads your active
    custom keyboard preset)
  * node picker: click and drag over nodes to tick or untick several at once
  * 3 preset slots
  * Settings › "Script Shortcut" (on by default): adds _Fuse Box to Resolve's Workspace > Scripts menu, a second way to open it;
    a second launch just brings the open window forward

Requirements: DaVinci Resolve Studio 19.1+ with
  Preferences > System > General > "External scripting using" = Local,
  Python 3.10+ (64-bit), and:  pip install -r requirements.txt

Run:   python fuse_box.py            (connects to Resolve)
       python fuse_box.py --standby  (wait in the background until Resolve runs)
       python fuse_box.py --demo     (fake Resolve, to try the UI)

Resolve's API can set a node's enabled state but cannot read it, so the app
remembers each toggle's state itself. If you flip nodes by hand inside Resolve,
right-click a toggle's state button and use Force ON / Force OFF to resync.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QFontMetricsF,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

APP_NAME = "Fuse Box"
APP_VERSION = "0.3.4 r21"
NUM_TOGGLES = 4
NUM_PRESETS = 3
MAX_LAYERS = 16
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# Palette sampled from Resolve 21's Fairlight mixer
BG = "#28282e"          # panel
BG_DARK = "#212126"     # headers, window frame
BG_GRID = "#24242a"     # strip area
BORDER = "#16161a"      # the main window's outer edge (Windows 11): BG_GRID's step-darker edge, 15 % darker
ALERT_TITLE = "#181818"  # an alert's title bar: Windows' dark #202020, a quarter darker
ALERT_TITLE_TEXT = "#ababab"  # its title: Windows' white, a third darker
TITLE_TEXT = "#a6926e"  # title-bar text (Windows 11): the muted gold of a missing scene pick, 20% brighter
TITLE_BG = "#202020"    # Windows 11's own dark title bar, for the faint circle in its icon slot
FIELD = "#1f1f1f"       # input fields
EDGE = "#18181c"        # outline of the grid box, grid dividers
BTN = "#3b3b3f"         # buttons (R / S / M, In)
BTN_HOVER = "#46464b"
MENU_HOVER = "#343440"  # hsl(240, 7%, 22%): the highlighted item in a menu (Settings, a scene's node list)
TEXT = "#b8b8ba"        # names
TEXT_DIM = "#8e9092"    # row labels
TEXT_FAINT = "#5a5a60"  # empty / disabled
PLUS_GREY = "#646468"   # "+" between key names: between TEXT_DIM and BTN
TEXT_BRIGHT = "#ffffff"  # panel title
ACCENT = "#c3931d"      # Resolve orange (bus names, active)
BLUE = "#2d4f6b"        # selection: the Scene 4 bar's blue
GREEN = "#448f64"       # track colour stripe
# the bar over each scene name, Resolve clip colours: Green, Apricot, Purple, Blue, Tan
SCENE_COLORS = [GREEN, "#ffa833", "#9972a0", "#4376a1"]
RED = "#e64b3d"         # record
ACTION_GOLD = "#9c7617"  # what just happened ("Scene 1 → ON …") and "Apply to all clips": ACCENT, a fifth darker
ALERT_GOLD = "#c3941d"   # the key and commands in an alert: ACTION_GOLD a quarter brighter, same hue
CLASH_TEXT = "#b83c31"  # RED, a fifth darker: a Node Label set against a scene, a picked node gone (italic)
STATE_DOT = {"HOLD": "#919191", "ON": ACCENT, "OFF": RED}  # state dots, lit when picked

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
    folder, old = base / APP_NAME, base / "NodeToggle"  # the settings folder before the rename to Fuse Box
    if old.is_dir() and not folder.exists():  # move it over once, settings and all
        with contextlib.suppress(OSError):
            old.rename(folder)
        if old.is_dir():  # couldn't move it (in use): keep using it rather than start blank
            folder = old
    return folder / ("config-demo.json" if demo else "config.json")


def default_toggle(i: int) -> dict:
    return {"name": f"Scene {i + 1}", "hotkey": None, "on": None, "one_clip": False, "sel": {}}  # on: None = HOLD (state unknown)


def clean_sel(sel: dict | None) -> dict:
    out = {}
    for k, v in (sel or {}).items():
        if k == "clip":  # configs from the first version
            k = "clip:1"
        if SLOT_RE.match(k) and v:
            out[k] = [dict(s) for s in v]
    return out


def clean_toggle(i: int, t: dict) -> dict:
    base = default_toggle(i)
    base.update({k: t[k] for k in ("name", "hotkey", "one_clip") if k in t})  # "on" stays None: HOLD
    if re.fullmatch(r"T\d+", str(base["name"])):  # old default names T1, T2 … → Scene 1, Scene 2 …
        base["name"] = f"Scene {base['name'][1:]}"
    base["sel"] = clean_sel(t.get("sel"))
    return base


def default_config() -> dict:
    return {
        "toggles": [default_toggle(i) for i in range(NUM_TOGGLES)],
        "presets": {str(i + 1): None for i in range(NUM_PRESETS)},
        "focus_only": True,
        "script_shortcut": True,  # _Fuse Box in Resolve's Workspace › Scripts menu
        "post_layer": True,   # mirrors Resolve's "Post Group Node Stack Layer" (not readable by scripts)
        "pinned": False,      # keep the main window above Resolve
        "window": {"x": None, "y": None},  # main window position, restored next launch
        "layer_keys": {},     # slot -> {"hotkey", "on"}
        "tags": [],           # node labels: {"name", "on": True|False|None (HOLD), "scope": "F"|"P", "project",
                              #               "hotkey"}
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
    cfg["focus_only"] = bool(data.get("focus_only", True))
    cfg["script_shortcut"] = bool(data.get("script_shortcut", True))
    cfg["post_layer"] = bool(data.get("post_layer", True))
    cfg["pinned"] = bool(data.get("pinned", False))
    if isinstance(data.get("window"), dict):
        cfg["window"].update({k: data["window"].get(k) for k in ("x", "y")})
    lk = data.get("layer_keys", {})
    if isinstance(lk, dict):
        cfg["layer_keys"] = {k: {"hotkey": v.get("hotkey"), "on": None}
                             for k, v in lk.items() if isinstance(v, dict) and SLOT_RE.match(k)}
    names = data.get("layer_names", {})
    if isinstance(names, dict):
        cfg["layer_names"] = {str(k): str(v) for k, v in names.items() if str(v).strip()}
    tags = data.get("tags", [])
    if isinstance(tags, list):
        cfg["tags"] = [{"name": str(t["name"]), "on": None, "scope": "F" if t.get("scope") == "F" else "P",
                        "project": t.get("project"), "hotkey": t.get("hotkey")}
                       for t in tags if isinstance(t, dict) and t.get("name")]
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
        top = _pb_parse(_zstd(raw[1:] if raw[:1] == b"\x81" else raw))
        if not top or top[0][1] != 2:
            continue
        for f, t, v in _pb_parse(top[0][2]):
            if f == 7 and t == 2:
                _number, label, on = _drx_node_state(_pb_parse(v))
                if on is not None:
                    out.append((label, on))
    return out


def _drx_node_state(fields) -> tuple[int, str, bool | None]:
    """(node number, label, on) from a node record of a .drx grade."""
    number = next((v for f, t, v in fields if f == 2 and t == 0), 0)
    label = next((v.decode("utf-8", "replace") for f, t, v in fields if f == 6 and t == 2), "")
    on = None
    for f, t, v in fields:
        if f != 10 or t != 2:
            continue
        for pf, pt, pv in _pb_parse(v):
            if pf != 1 or pt != 2:
                continue
            sub = _pb_parse(pv)
            if any(sf == 1 and sv == DRX_ENABLE_PARAM for sf, st, sv in sub):
                val = next((sv for sf, st, sv in sub if sf == 2 and st == 2), b"")
                on = next((x for xf, _, x in _pb_parse(val) if xf == 2), 0) != 0
    return number, label, on


def drx_graph_states(xml: str) -> dict[str, dict[int, bool]]:
    """{"clip:1": {node number: on}, "clip:2": …, "timeline": …} from a still's .drx: its first grade is the clip
    (layer 1 at the top, layers 2+ in field 5 with their number in field 1), the second the timeline."""
    out = {}
    for n, hexblob in enumerate(re.findall(r"<Body>([0-9a-fA-F]+)</Body>", xml)[:2]):
        raw = bytes.fromhex(hexblob)
        top = _pb_parse(_zstd(raw[1:] if raw[:1] == b"\x81" else raw))
        if not top or top[0][1] != 2:
            continue
        grades = [("timeline" if n else "clip:1", top[0][2])]
        if not n:  # the clip's other layers
            for f, t, v in top:
                if f == 5 and t == 2:
                    layer = next((x for xf, xt, x in _pb_parse(v) if xf == 1 and xt == 0), 0)
                    grades.append((f"clip:{layer}", v))
        for slot, grade in grades:
            nodes = {}
            for f, t, v in _pb_parse(grade):
                if f == 7 and t == 2:
                    number, _label, on = _drx_node_state(_pb_parse(v))
                    if number and on is not None:
                        nodes[number] = on
            if nodes:
                out[slot] = nodes
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
            graphs = (list(self.layer_graphs(item, proj)) if item else []) + ([tl.GetNodeGraph()] if tl else [])
            return [(n["label"], n["enabled"]) for g in graphs for n in getattr(g, "nodes", [])]
        xml = self._still_grade(proj, tl)
        return drx_node_states(xml) if xml else None

    def graph_states(self, proj, tl) -> dict[str, dict[int, bool]] | None:
        """Real on/off of the current clip's layers and the timeline, by node number (see drx_graph_states);
        pre- / post-group graphs aren't in a still. None = couldn't read."""
        if self.demo:
            item = tl.GetCurrentVideoItem() if tl else None
            graphs = self.graphs(proj, tl, item)
            return {k: {i: n["enabled"] for i, n in enumerate(g.nodes, 1)} for k, g in graphs.items()
                    if g and (k == "timeline" or slot_layer(k))}
        xml = self._still_grade(proj, tl)
        return drx_graph_states(xml) if xml else None

    @staticmethod
    def _still_grade(proj, tl) -> str | None:
        """The current clip's grade as .drx text: a still is grabbed, exported and deleted again."""
        try:
            album = proj.GetGallery().GetCurrentStillAlbum()
            still = tl.GrabStill() if tl and album else None
            if not still and tl and album:  # Resolve refuses a grab within ~0.5 s of the last one
                time.sleep(0.5)
                still = tl.GrabStill()
            if not still:
                return None
            tmp = tempfile.mkdtemp(prefix="fusebox_")
            try:
                album.ExportStills([still], tmp, "dks", "drx")
                album.DeleteStills([still])
                files = [f for f in os.listdir(tmp) if f.lower().endswith(".drx")]
                return Path(tmp, files[0]).read_text(encoding="utf-8", errors="replace") if files else None
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


SHORTCUTS_DIR = config_path(False).parent / "shortcuts"  # saved copies of Resolve's custom presets


class ResolveShortcuts:
    """Reads the keyboard preset Resolve is using (only custom presets are stored on disk).

    keyboard.preset.xml holds a Qt-serialised blob: a list of presets, each
    name · byte length · version · count · [action · n · n×(k · k×keycode)] · base name.
    Key codes are Qt key + modifier bits, so they compare directly with our hotkeys."""

    MOD_BITS = (("shift", 0x02000000), ("ctrl", 0x04000000), ("alt", 0x08000000), ("meta", 0x10000000))

    def __init__(self):
        self.status = "not loaded"   # "ok" / "saved" (a copy in SHORTCUTS_DIR) / "builtin" / "missing" / "error"
        self.source = ""             # name of the preset the clashes are checked against
        self.by_combo: dict[tuple, list[str]] = {}
        self._stamp = None

    def load(self, force: bool = False):
        try:
            d = next((p for p in _resolve_pref_dirs() if (p / "keyboard.preset.xml").exists()
                      or (p / "config.user.xml").exists()), None)
            files = [d / "config.user.xml", d / "keyboard.preset.xml"] if d else []
            stamp = tuple(f.stat().st_mtime if f.exists() else 0 for f in files)
            if stamp == self._stamp and not force:
                return
            self._stamp = stamp
            status, xml, want = self._read_resolve(files)
            if status == "ok":
                if self._use(xml, want):
                    self.status, self.source = "ok", want or ""
                    self._snapshot(files[1], self.source)
                    return
                status = "error"  # Resolve's file holds no presets
            saved = self._newest_snapshot()  # Resolve's layout isn't on disk: use the last custom preset seen
            if saved and self._use(saved.read_text(encoding="utf-8", errors="replace"), saved.stem):
                self.status, self.source = "saved", saved.stem
                return
            self.status, self.source, self.by_combo = status, "", {}
        except Exception as e:  # never crash over Resolve's file
            print(f"Couldn't read Resolve's shortcuts: {e}", file=sys.stderr)
            self.status, self.source, self.by_combo = "error", "", {}

    @staticmethod
    def _read_resolve(files: list[Path]) -> tuple[str, str, str | None]:
        """(status, keyboard.preset.xml text, active custom preset name) from Resolve's preferences."""
        if not files:
            return "missing", "", None
        cfg = files[0].read_text(encoding="utf-8", errors="replace") if files[0].exists() else ""
        mapping = re.search(r"<KeyboardMappings>(-?\d+)</KeyboardMappings>", cfg)
        custom = re.search(r"<CustomKeyboardPreset>(.*?)</CustomKeyboardPreset>", cfg)
        if mapping and mapping.group(1) != "-1":
            return "builtin", "", None
        if not files[1].exists():
            return "missing", "", None
        return "ok", files[1].read_text(encoding="utf-8", errors="replace"), custom.group(1) if custom else None

    def _use(self, xml: str, want: str | None) -> bool:
        """Index the preset named `want` (else the first) by key combo. False if the file has none."""
        presets = self._parse(xml)
        table = next((t for n, t in presets if n == want), presets[0][1] if presets else None)
        if table is None:
            return False
        self.by_combo = {}
        for action, seqs in table.items():
            for seq in seqs:
                if len(seq) == 1:  # chords (two-step keys) can't clash with a single press
                    self.by_combo.setdefault(self._combo(seq[0]), []).append(action)
        return True

    @staticmethod
    def _snapshot(src: Path, name: str):
        """Keep a copy of Resolve's custom presets in SHORTCUTS_DIR, named after the active one."""
        safe = re.sub(r'[\\/:*?"<>|]', "_", name or "keyboard.preset")  # characters Windows filenames can't hold
        dst = SHORTCUTS_DIR / f"{safe}.xml"
        data = src.read_bytes()
        with contextlib.suppress(OSError):
            if not dst.exists() or dst.read_bytes() != data:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data)

    @staticmethod
    def _newest_snapshot() -> Path | None:
        return max(SHORTCUTS_DIR.glob("*.xml"), key=lambda f: f.stat().st_mtime, default=None)

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
                table[action] = [[u() for _ in range(u())] for _ in range(u())]
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
        key = key_parts(hk["text"])[-1].lower() if hk.get("text") else ""
        return frozenset(hk.get("mods", [])), key

    def clash(self, hk) -> list[str]:
        if not hk or self.status not in ("ok", "saved"):
            return []
        return [_humanize_action(a) for a in self.by_combo.get(self.hotkey_combo(hk), [])]


# ----------------------------------------------------------------------------- layer names

# Project Settings is a direct child of Resolve's main window (class UiProjectSettingsWidgetImp). Both readers
# list only Resolve's windows and their direct children to find it, then read inside it alone: searching the
# whole interface makes Resolve answer UI Automation widget by widget on its own UI thread (2 s for one pass).
_FIND_SETTINGS_PS = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A=[System.Windows.Automation.AutomationElement]; $S=[System.Windows.Automation.TreeScope]
$isSettings = New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, 'UiProjectSettingsWidgetImp')
function Find-Settings($p) {
  if ($p.MainWindowHandle -ne 0) {  # straight to the main window: no walk over every window on the desktop
    return $A::FromHandle($p.MainWindowHandle).FindFirst($S::Children, $isSettings) }
  $pc = New-Object System.Windows.Automation.PropertyCondition($A::ProcessIdProperty, $p.Id)
  foreach ($t in $A::RootElement.FindAll($S::Children, $pc)) {  # no main window handle: look through all of them
    if ($t.Current.ClassName -eq 'UiProjectSettingsWidgetImp') { return $t }
    $w = $t.FindFirst($S::Children, $isSettings)
    if ($w) { return $w } }
  return $null }
function Read-Layers($w) {
  $out = ''; $want = 0
  foreach ($e in $w.FindAll($S::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) { $c = $e.Current
    if ($c.ClassName -eq 'QLabel' -and $c.Name -match '^Layer (\d+)$') { $want = [int]$Matches[1] }
    elseif ($want -and $c.ClassName -eq 'UiPlainLineEdit') {
      $out += 'LAYER ' + $want + '|' + $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value + "`n"; $want = 0 }
    elseif ($c.ClassName -eq 'QCheckBox' -and $c.Name -eq 'Post Group Node Stack Layer') {
      $out += 'POST ' + $e.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState + "`n" } }
  return $out }
"""

_LAYER_WATCH_PS = "$ErrorActionPreference = 'SilentlyContinue'\n" + _FIND_SETTINGS_PS + r"""
$isView = New-Object System.Windows.Automation.PropertyCondition($A::ClassNameProperty, 'UiNodeGraphDropDownButton')
$parent = __PARENT__; $last = ''; $view = $null; $lastView = ''; $tick = 0
while (Get-Process -Id $parent) {
  $p = Get-Process Resolve | Select-Object -First 1
  $out = ''; $w = $null
  if ($p) { $w = Find-Settings $p; if ($w) { $out = Read-Layers $w } }
  # the node editor's graph dropdown ("Clip - L1", "Timeline"...): found once (a search every 3 s while it's
  # missing), then only its name is read
  $tick++
  if ($p -and -not $view -and $p.MainWindowHandle -ne 0 -and $tick % 3 -eq 1) {
    $view = $A::FromHandle($p.MainWindowHandle).FindFirst($S::Descendants, $isView) }
  $name = ''
  if ($view) { try { if (-not $view.Current.IsOffscreen) { $name = $view.Current.Name } } catch { $view = $null } }
  if ($name -ne $lastView) { $lastView = $name; [Console]::Out.Write("VIEW|" + $name + "`nEND`n"); [Console]::Out.Flush() }
  if ($out -and $out -ne $last) { $last = $out; [Console]::Out.Write($out + "END`n"); [Console]::Out.Flush() }
  elseif (-not $w -and $last) { $last = ''; [Console]::Out.Write("CLOSED`nEND`n"); [Console]::Out.Flush() }
  # open: look often, so the last rename is read before Save closes the window; closed: a cheap check each second
  if ($w) { Start-Sleep -Milliseconds 500 } else { Start-Sleep -Milliseconds 1000 }
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
    """Background: while Resolve's Project Settings window is open, report its layer names and Post Group
    tick whenever they change (so the last ones are caught before Save closes it), and "closed" once it closes.
    One hidden PowerShell process that exits by itself when this app exits."""
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
            if len(block) == 1 and block[0].startswith("VIEW|"):
                d = {"view": block[0][5:]}
            else:
                d = "closed" if block == ["CLOSED"] else _parse_layer_lines(block)
            block = []
            if d:
                emit(d)
        else:
            block.append(ln)



# ----------------------------------------------------------------------------- updates

# Releases are tagged "v0.3.3-r21": the app's version, then the Resolve version it's built for (APP_VERSION's "r21").
# The repository must be public: the check and the downloads use GitHub without signing in.
GITHUB_REPO = "train-baker/fuse-box"


def _version_key(v: str) -> tuple[tuple[int, ...], str]:
    """'0.3.3 r21' or 'v0.3.3-r21' -> ((0, 3, 3), 'r21')."""
    m = re.match(r"v?(\d+(?:\.\d+)*)[ -]?(r\d+)?$", v.strip())
    return (tuple(int(x) for x in m.group(1).split(".")), m.group(2) or "") if m else ((), "")


def _github(url: str) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def latest_release() -> dict:
    """The newest release for this app's Resolve version (same "r21"): {"tag", "version", "url", "newer"}.
    Raises when GitHub can't be reached (offline, or the repository isn't public)."""
    mine, build = _version_key(APP_VERSION)
    best = None
    for rel in json.loads(_github(f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30")):
        if rel.get("draft") or rel.get("prerelease"):
            continue
        ver, b = _version_key(rel.get("tag_name", ""))
        if ver and b == build and (best is None or ver > best[0]):
            best = (ver, rel)
    if best is None:
        return {"tag": None, "version": APP_VERSION, "url": None, "newer": False}
    ver, rel = best
    return {"tag": rel["tag_name"], "version": f"{'.'.join(map(str, ver))} {build}".strip(),
            "url": rel.get("html_url"), "newer": ver > mine}


def _git_sha(data: bytes) -> str:
    """The id GitHub gives a file's contents (a git blob's sha1)."""
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def can_self_update() -> str | None:
    """None when this copy can update its own files; else why not."""
    if getattr(sys, "frozen", False):
        return "packaged"
    if (APP_DIR / ".git").exists():
        return "git"  # a working copy: update it with git, never overwrite it
    return None


def update_files(tag: str) -> list[str]:
    """Download only the files of release `tag` that differ from this copy (compared by GitHub's file ids, each
    download checked against its id), then put them in place. Returns the paths that changed."""
    from urllib.parse import quote
    tree = json.loads(_github(f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{quote(tag)}?recursive=1"))
    new = {}
    for item in tree.get("tree", []):
        path = item["path"]
        if item["type"] != "blob" or path.startswith(".") or "/." in path:  # .github, .gitignore: not the app
            continue
        local = APP_DIR / path
        if local.exists():
            data = local.read_bytes()
            if item["sha"] in (_git_sha(data), _git_sha(data.replace(b"\r\n", b"\n"))):  # same (any line endings)
                continue
        data = _github(f"https://raw.githubusercontent.com/{GITHUB_REPO}/{quote(tag)}/{quote(path)}")
        if _git_sha(data) != item["sha"]:
            raise RuntimeError(f"{path} didn't download correctly")
        new[path] = data
    for path, data in new.items():  # all downloaded and checked first: never half an update
        local = APP_DIR / path
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, local)
    return sorted(new)


def install_requirements() -> bool:
    """pip install -r requirements.txt with this Python (a release added a package)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
    r = subprocess.run([str(exe), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
                       capture_output=True, **kw)
    return r.returncode == 0


# ----------------------------------------------------------------------------- open with Resolve

AUTOSTART_NAME = "Fuse Box"


def _launch_cmd(*extra) -> list[str]:
    if getattr(sys, "frozen", False):  # a packaged app (PyInstaller): the executable is the app
        return [sys.executable, *extra]
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), str(Path(__file__).resolve()), *extra]


# Resolve's Workspace › Scripts menu lists the .py / .lua files in its Scripts folders
SCRIPT_SHORTCUT = '''"""_{app}: opens {app} from DaVinci Resolve's Workspace › Scripts menu.

Written by {app} (Settings › "Script Shortcut", which also removes it). Starts the app in its own process,
so Resolve stays responsive; if it is already running, that window comes forward.
"""
import subprocess
import sys

CMD = {cmd!r}
if sys.platform == "win32":
    subprocess.Popen(CMD, creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
else:
    subprocess.Popen(CMD, start_new_session=True)
'''


def _script_shortcut_file() -> Path:
    """The launcher in Resolve's user Scripts folder; the leading _ lists it above the other scripts."""
    if IS_WIN:
        base = Path(os.environ.get("APPDATA", Path.home())) / "Blackmagic Design/DaVinci Resolve/Support/Fusion"
    elif IS_MAC:
        base = Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion"
    else:
        base = Path.home() / ".local/share/DaVinciResolve/Fusion"
    return base / "Scripts" / "Utility" / f"_{APP_NAME}.py"


def set_script_shortcut(on: bool):
    """Add or remove Workspace › Scripts › _Fuse Box in Resolve: a second way to open this app."""
    f = _script_shortcut_file()
    if not on:
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(SCRIPT_SHORTCUT.format(app=APP_NAME, cmd=_launch_cmd()), encoding="utf-8")


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


def _foreground_ok(app_pid: int) -> bool:
    """Windows only: True if Resolve (or this app, process app_pid) is the focused window."""
    if not IS_WIN:
        return True
    import ctypes
    from ctypes import wintypes
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    if pid.value == app_pid:
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


class HotkeyListener:
    """Global keyboard listener (pynput): calls `on_fire(id)` for a bound hotkey, an int for a scene
    (toggle column), a str for a layer key ("clip:2", "postclip", "tags"…).
    It runs in a process of its own (hotkey_process): Windows holds every key press, in every app, until
    the keyboard hook has answered, and Resolve's scripting module keeps Python's lock (the GIL) while it
    waits on Resolve, so a hook inside the app held Resolve's own keys (Space to stop playback) for up to 1 s."""

    def __init__(self, on_fire, app_pid: int):
        self.on_fire = on_fire
        self.app_pid = app_pid
        self.bindings: list[tuple[int | str, dict]] = []
        self.focus_only = True
        self._mods: set[str] = set()
        self._down: set = set()
        self._modmap = {}

    def start(self):
        from pynput import keyboard
        K = keyboard.Key
        for name, keys in {"ctrl": ("ctrl", "ctrl_l", "ctrl_r"), "shift": ("shift", "shift_l", "shift_r"),
                           "alt": ("alt", "alt_l", "alt_r", "alt_gr"), "meta": ("cmd", "cmd_l", "cmd_r")}.items():
            for k in keys:
                if hasattr(K, k):
                    self._modmap[getattr(K, k)] = name
        listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        listener.daemon = True
        listener.start()

    @staticmethod
    def _vk(key):
        vk = getattr(key, "vk", None)
        if vk is None and hasattr(key, "value"):
            vk = getattr(key.value, "vk", None)
        return vk

    WIN_MOD_VKS = (("ctrl", (0x11,)), ("shift", (0x10,)), ("alt", (0x12,)), ("meta", (0x5B, 0x5C)))  # left/right Win

    def _current_mods(self) -> set[str]:
        if IS_WIN:  # ask Windows directly, so a missed key-up can't leave a modifier stuck
            import ctypes
            gks = ctypes.windll.user32.GetAsyncKeyState
            return {name for name, vks in self.WIN_MOD_VKS if any(gks(vk) & 0x8000 for vk in vks)}
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
                if not self.focus_only or _foreground_ok(self.app_pid):
                    self.on_fire(idx)
                return

    def _on_release(self, key):
        mod = self._modmap.get(key)
        if mod:
            self._mods.discard(mod)
            return
        vk = self._vk(key)
        self._down.discard(vk if vk is not None else (getattr(key, "char", None) or "").lower() or None)


def hotkey_process(app_pid: int):
    """`--hotkeys <app pid>`: the listener's own process. Reads {"bindings", "focus_only"} lines on stdin,
    writes the JSON id of each hotkey pressed (or {"error": …}) on stdout, and ends when the app closes stdin."""
    def send(msg):
        print(json.dumps(msg), flush=True)

    hk = HotkeyListener(send, app_pid)
    try:
        hk.start()
    except ImportError as e:
        send({"error": f"Global hotkeys unavailable ({e}). Install with: pip install pynput"})
        return
    except Exception as e:
        send({"error": f"Global hotkeys unavailable: {e}"})
        return
    for line in sys.stdin:
        msg = json.loads(line)
        hk.bindings = [(k, h) for k, h in msg["bindings"] if h]
        hk.focus_only = msg["focus_only"]


class HotkeyEngine(QObject):
    """The app's side of the hotkey process (see HotkeyListener for why it is a process):
    emits `fired(id)` on the Qt thread for each hotkey, or `failed(message)` if hotkeys can't run."""
    fired = Signal(object)
    failed = Signal(str)

    def __init__(self, focus_only: bool):
        super().__init__()
        self.bindings: list[tuple] = []
        self.focus_only = focus_only
        self._proc = None

    def start(self):
        try:
            self._proc = subprocess.Popen(_launch_cmd("--hotkeys", str(os.getpid())), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                          creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        except OSError as e:
            self.failed.emit(f"Global hotkeys unavailable: {e}")
            return
        threading.Thread(target=self._read, args=(self._proc,), daemon=True).start()
        self._send()

    def _read(self, proc):
        for line in proc.stdout:
            msg = json.loads(line)
            if isinstance(msg, dict):
                self.failed.emit(msg["error"])
                return
            self.fired.emit(msg)
        if self._proc is proc:  # ended without stop()
            self.failed.emit("Global hotkeys stopped: restart Fuse Box to get them back")

    def _send(self):
        if self._proc:
            with contextlib.suppress(OSError):  # the process is gone: _read has said so
                self._proc.stdin.write(json.dumps({"bindings": self.bindings, "focus_only": self.focus_only}) + "\n")
                self._proc.stdin.flush()

    def set_bindings(self, bindings: list[tuple]):
        self.bindings = bindings
        self._send()

    def set_focus_only(self, on: bool):
        self.focus_only = on
        self._send()

    def stop(self):
        proc, self._proc = self._proc, None
        if proc:
            with contextlib.suppress(OSError):
                proc.stdin.close()  # the process ends on its own


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


def text_width(font: QFont, text: str) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)


class TailLabel(QLabel):
    """Label that, short of room, keeps its start and the last TAIL characters before the file extension, plus the
    extension: 'Reel-Rob-Borges-Pro…250617T2200.mov'."""
    TAIL = 11

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._full = super().text()

    def setText(self, text: str):
        self._full = text
        self.updateGeometry()
        self._elide()

    def _room(self, text: str) -> QSize:
        m = self.contentsMargins()
        return QSize(math.ceil(text_width(self.font(), text)) + m.left() + m.right() + 1,
                     super().sizeHint().height())

    def sizeHint(self):
        return self._room(self._full)

    def _cut(self) -> int:
        """Where the kept end starts: TAIL characters before the extension (".mov"), if there is one."""
        ext = re.search(r"\.\w{1,5}$", self._full)
        return max(0, (ext.start() if ext else len(self._full)) - self.TAIL)

    def minimumSizeHint(self):
        return self._room("…" + self._full[self._cut():])

    def _elide(self):
        t, w, f = self._full, self.contentsRect().width(), self.font()
        if (cut := self._cut()) and text_width(f, t) > w:
            head, tail = t[:cut], "…" + t[cut:]
            while head and text_width(f, head + tail) > w:
                head = head[:-1]
            t = head.rstrip() + tail
        super().setText(t)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()


class Block(NamedTuple):
    text: str
    width: float
    boxed: bool  # a modifier box; False for the gap, the '+' and the key


def key_blocks(font: QFont, text: str, pad: float = 8) -> list[Block]:
    """'Ctrl+Alt+S' -> [Ctrl] [Alt] + S: one box per modifier, then a single '+' and the key, unboxed."""
    def box(t: str) -> Block:
        return Block(t, text_width(font, t) + pad, True)

    *mods, key = key_parts(text)
    out: list[Block] = []
    for m in mods:
        if out:
            out.append(Block("", 3, False))  # gap between modifier boxes
        out.append(box(m))
    if mods:
        out.append(Block("+", text_width(font, "+") + 6, False))
    out.append(Block(key, text_width(font, key) + 6, False))
    return out


def blocks_width(blocks: list[Block]) -> float:
    return sum(b.width for b in blocks)


def draw_key_blocks(p: QPainter, x: float, y: float, h: float, blocks: list[Block]) -> None:
    """[Ctrl] [Alt] [Shift] + S: grey boxes like "None", the '+' a step darker, the key brighter to read."""
    for i, b in enumerate(blocks):
        r = QRectF(x, y, b.width, h)
        if b.boxed:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(BTN))
            p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor(TEXT_DIM if b.boxed else TEXT if i == len(blocks) - 1 else PLUS_GREY))
        p.drawText(r, Qt.AlignCenter, b.text)
        x += b.width


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
        # the painted ✕ of the node-label chips, not a letter, so it can't be read as an X key; a layer pill
        # takes it into its own row, otherwise it sits at the cell's right end
        self.clear = IconButton("x", "Remove this shortcut", parent=self)
        self.clear.clicked.connect(self.clear_hotkey)
        self._refresh()

    def set_hotkey(self, hk):
        self.hotkey = hk
        self._refresh()

    def clear_hotkey(self):
        self.hotkey = None
        self._refresh()
        self.changed.emit(None)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.clear.parent() is self:
            self.clear.move(self.width() - self.clear.width() - 7, (self.height() - self.clear.height()) // 2)

    def paintEvent(self, e):
        if not self.hotkey or self.recording:
            return super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self.font())
        blocks = key_blocks(p.font(), self.hotkey["text"])
        if self.objectName() == "pillkey":  # the blocks are the box: no button background behind them
            if self.property("clash"):
                p.setPen(QPen(QColor(RED), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
            draw_key_blocks(p, 1, 1, self.height() - 2, blocks)
            return
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""
        self.style().drawControl(QStyle.CE_PushButton, opt, p, self)
        h = min(15, self.height() - 6)
        draw_key_blocks(p, 6, (self.height() - h) / 2, h, blocks)

    def sizeHint(self):
        s = super().sizeHint()
        if self.objectName() == "pillkey" and self.hotkey and not self.recording:
            s.setWidth(int(blocks_width(key_blocks(self.font(), self.hotkey["text"]))) + 3)
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
        repolish(self)
        self.clear.setVisible(bool(self.hotkey) and not self.recording)
        self.setText("Press a key" if self.recording else (self.hotkey["text"] if self.hotkey else "None"))

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


class MissingNodeAction(QWidgetAction):
    """A picked node that is no longer in the clip's node tree: a menu row like the others, its name in
    italic dark gold (a style sheet can't colour one menu item, so the row draws itself).
    The menu still handles the clicks and drags: the row lets the mouse through."""

    def __init__(self, text: str, menu: QMenu):
        super().__init__(menu)
        self.setCheckable(True)
        row = _MissingRow(self, text, menu)
        self.setDefaultWidget(row)
        self.changed.connect(row.update)
        menu.hovered.connect(lambda _a: row.update())


class _MissingRow(QWidget):
    TEXT_X = 25  # where the menu puts an item's text, after its tick box (QMenu::item padding + indicator)

    def __init__(self, action: QWidgetAction, text: str, menu: QMenu):
        super().__init__()
        self.action, self.text, self.menu = action, text, menu
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        f = QFont(self.font())
        f.setItalic(True)
        self.setFont(f)

    def sizeHint(self):
        return QSize(self.TEXT_X + math.ceil(text_width(self.font(), self.text)) + 20, 22)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        a = self.action
        if self.menu.activeAction() is a and a.isEnabled():  # QMenu::item:selected
            p.setBrush(QColor(MENU_HOVER))
            p.drawRoundedRect(QRectF(self.rect()), 5, 5)
        if not a.isEnabled():
            p.setOpacity(0.4)
        p.setBrush(QColor(BLUE if a.isChecked() else BTN))  # QMenu::indicator
        p.drawRoundedRect(QRectF(5, (self.height() - 11) / 2, 11, 11), 3, 3)
        p.setPen(QColor(CLASH_TEXT))
        p.drawText(QRectF(self.TEXT_X, 0, self.width() - self.TEXT_X, self.height()),
                  Qt.AlignLeft | Qt.AlignVCenter, self.text)


class DotAction(QWidgetAction):
    """A menu item with a status dot on its right, drawn like the connection dot (glow and all)."""

    def __init__(self, text: str, color: QColor | None, menu: QMenu):
        super().__init__(menu)
        self.setText(text)
        row = _DotRow(self, text, color, menu)
        self.setDefaultWidget(row)
        menu.hovered.connect(lambda _a: row.update())


class _DotRow(QWidget):
    DOT = 7

    def __init__(self, action: QWidgetAction, text: str, color: QColor | None, menu: QMenu):
        super().__init__()
        self.action, self.text, self.color, self.menu = action, text, color, menu
        self.setAttribute(Qt.WA_TransparentForMouseEvents)  # the menu itself handles hover and click

    def sizeHint(self):
        return QSize(_MissingRow.TEXT_X + math.ceil(text_width(self.font(), self.text)) + 40, 22)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.menu.activeAction() is self.action:  # QMenu::item:selected
            p.setBrush(QColor(MENU_HOVER))
            p.drawRoundedRect(QRectF(self.rect()), 5, 5)
        if self.color is not None:  # the dot, right-aligned
            d, x, y = self.DOT, self.width() - self.DOT - 10, (self.height() - self.DOT) / 2
            glow = QColor(self.color)
            glow.setAlpha(60)
            p.setBrush(glow)
            p.drawEllipse(QRectF(x - 3, y - 3, d + 6, d + 6))
            p.setBrush(self.color)
            p.drawEllipse(QRectF(x, y, d, d))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(_MissingRow.TEXT_X, 0, self.width() - _MissingRow.TEXT_X, self.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, self.text)


class StickyMenu(QMenu):
    """A menu that stays open when you tick a checkable item, so several nodes can be picked.
    Press on a node and drag over others to set them all the same way (like Resolve's track toggles)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_state = None
        self._dragged = set()

    @staticmethod
    def _draggable(a) -> bool:
        return a is not None and a.isEnabled() and a.isCheckable() and isinstance(a.data(), dict)  # nodes only

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


def pick_name(s: dict, missing: bool = False) -> str:
    """A picked node's name: its label, else "Node 02" (just "Node" once it's gone from the clip)."""
    return s.get("label") or ("Node" if missing else f"Node {s['index']:02d}")


def sel_text(sel: list[dict], missing: list[dict] = ()) -> str:
    if not sel:
        return "None"
    if any(s.get("all") for s in sel):
        return "All nodes"
    return ", ".join(pick_name(s, missing=s in missing) for s in sel)


class NodePicker(QPushButton):
    """One grid cell: shows the chosen nodes, click for a multi-select dropdown."""
    changed = Signal(int, str, list)  # toggle index, slot, selection
    menu_open = Signal(bool)  # True before the node menu opens, False once it has closed (one undo step)

    def __init__(self, toggle_index: int, slot: str, parent=None):
        super().__init__(parent)
        self.toggle_index = toggle_index
        self.slot = slot
        self.sel: list[dict] = []
        self.nodes = None
        self.placeholder = ""
        self.label_clash: list[str] = []  # what in this cell is set against this scene: 'Node Label “NR”', 'L1'…
        self._full = ""
        self._miss: list[dict] = []  # picks not on this clip, worked out in _render
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
        if self._mixed():  # some picks gone from the clip: those names in italic dark gold, the rest as usual
            self._paint_mixed()
        if self.label_clash:  # red dot on the right: a Node Label or the Layer State disagrees with this scene
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

    def _mixed(self) -> bool:
        """Some, not all, of the picks are missing (all missing: the style sheet greys the whole cell gold)."""
        return bool(self._miss) and len(self._miss) < len(self.sel)

    def _text_right(self) -> int:
        return self.width() - (34 if self.label_clash else 22)  # clear of the red dot

    def _paint_mixed(self):
        p = QPainter(self)
        missing = self._miss
        font, gone = self.font(), QFont(self.font())
        gone.setItalic(True)
        x, right = 8.0, self._text_right()  # 8: the cell's text padding
        for i, s in enumerate(self.sel):
            name = pick_name(s, missing=s in missing)
            f, colour = (gone, CLASH_TEXT) if s in missing else (font, TEXT)
            for piece, pf, pc in ((name, f, colour), (", " if i < len(self.sel) - 1 else "", font, TEXT)):
                if not piece:
                    continue
                w = text_width(pf, piece)
                if x + w > right:  # out of room: end on an ellipsis
                    p.setFont(font)
                    p.setPen(QColor(TEXT))
                    p.drawText(QRectF(x, 0, right - x, self.height()), Qt.AlignLeft | Qt.AlignVCenter, "…")
                    return
                p.setFont(pf)
                p.setPen(QColor(pc))
                p.drawText(QRectF(x, 0, w, self.height()), Qt.AlignLeft | Qt.AlignVCenter, piece)
                x += w

    def _render(self):
        self.setProperty("empty", not self.sel)
        self.setProperty("all", any(s.get("all") for s in self.sel))
        self._miss = self._missing()
        self.setProperty("missing", bool(self.sel) and len(self._miss) == len(self.sel))
        repolish(self)
        self._full = sel_text(self.sel, self._miss)
        tip = self._full if self.sel else "Click to pick nodes"
        if self._miss:
            tip += "\n(not all of these are on this clip)"
        if self.label_clash:
            against = [w for w in self.label_clash if not w.endswith(")")]
            over = [w for w in self.label_clash if w.endswith(")")]
            if against:
                tip += (f"\n\nRed dot: {', '.join(against)} "
                        f"{'is' if len(against) == 1 else 'are'} set the opposite way to this scene")
            if over:
                tip += f"\n\nRed dot: {', '.join(over)}; HOLD puts them back"
        self.setToolTip(tip)
        self._elide()

    def _elide(self):
        if self._mixed():  # painted piece by piece (_paint_mixed)
            self.setText("")
            return
        w = max(20, self._text_right())
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
        for s in self._miss:
            label = pick_name(s, missing=True)
            a = MissingNodeAction(f"{label}   (not on this clip)", m)
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
        self.menu_open.emit(True)
        m.exec(self.mapToGlobal(self.rect().bottomLeft()))
        m.deleteLater()
        self.menu_open.emit(False)

    def _emit(self):
        self._render()
        self.changed.emit(self.toggle_index, self.slot, list(self.sel))


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

    def __init__(self, kind: str, tip: str = "", w=11, h=16, boxed=False, parent=None, icon=11.0):
        super().__init__(parent)
        self.kind = kind
        self.icon = icon  # the icon's drawing box, px
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
        r = QRectF(self.rect())
        if self.boxed:  # a grey key like "None", no stroke, the icon lighter
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(BTN_HOVER if self.underMouse() else BTN))
            p.drawRoundedRect(r, 4, 4)
        else:
            p.setOpacity(1.0 if self.underMouse() else 0.5)
        s = self.icon
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
            f.setPixelSize(8)
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


STATE_WORDS = ((None, "HOLD"), (True, "ON"), (False, "OFF"))  # state -> word, in display order


def label_key(tag: dict) -> str:
    """A Node Label's binding name for the hotkey process (labels match in any case)."""
    return "label:" + tag["name"].lower()


def next_state(on):
    """HOLD → ON → OFF → HOLD: what a click on a layer key's or node label's name picks next."""
    return {None: True, True: False, False: None}[on]


class StatePick(QPushButton):
    """A HOLD / ON / OFF pick: a 7 px dot, no stroke, in its STATE_DOT colour when picked and at 30 % of it
    when not. with_text writes the word after the dot (the State row)."""

    def __init__(self, text: str, selected: bool, tip: str, with_text: bool = False, parent=None):
        super().__init__(parent)
        self.word = text
        self.sel = selected
        self.with_text = with_text
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(tip)
        self.setAttribute(Qt.WA_Hover)  # repaints on enter / leave
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def _text_font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(11)
        f.setWeight(QFont.DemiBold)
        return f

    def sizeHint(self):
        w = 9  # the dot's cell
        if self.with_text:
            w += 3 + math.ceil(QFontMetricsF(self._text_font()).horizontalAdvance(self.word))
        return QSize(w, 16)

    def minimumSizeHint(self):
        return self.sizeHint()

    def set_selected(self, on: bool):
        self.sel = on
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        hover = self.underMouse() and self.isEnabled()
        c = QColor(STATE_DOT[self.word])
        if not self.sel:  # its own colour, faint: 30 %, a little more under the mouse
            c.setAlphaF(0.45 if hover else 0.3)
        p.setBrush(c)
        cy = self.height() / 2
        p.drawEllipse(QPointF(4.5, cy), 3.5, 3.5)
        if self.with_text:
            p.setFont(self._text_font())
            p.setPen(QColor(TEXT_FAINT if not self.isEnabled() else TEXT if self.sel or hover else TEXT_DIM))
            p.drawText(QRectF(12, 0, self.width() - 12, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.word)


def state_dots(on, tips: dict, pick, layout: QHBoxLayout, with_text: bool = False) -> dict:
    """The three HOLD / ON / OFF dots (state -> StatePick), added to layout; pick(state) on a click."""
    dots = {}
    for state, word in STATE_WORDS:
        b = StatePick(word, on is state, tips[state], with_text)
        b.clicked.connect(lambda _=False, s=state: pick(s))
        layout.addWidget(b)
        dots[state] = b
    return dots


class SceneState(QFrame):
    """● HOLD  ● ON  ● OFF for one scene, the current dot lit; the words are part of the click target.
    HOLD parks the scene: no nodes change, and its shortcut does nothing until ON / OFF is picked;
    ON / OFF switch the scene's nodes."""
    picked = Signal(object)  # None (HOLD), True (ON) or False (OFF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scenestate")
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.boxes = state_dots(None, {None: "HOLD: leave the nodes as they are — the scene's shortcut\ndoes nothing "
                                             "until you pick ON or OFF",
                                       True: "Switch this scene's nodes ON", False: "Switch this scene's nodes OFF"},
                                self.picked.emit, h, with_text=True)

    def set_state(self, on, configured: bool):
        self.setEnabled(configured)
        for state, b in self.boxes.items():
            b.set_selected(configured and on is state)


class TagChip(QFrame):
    """Name · HOLD ON OFF dots · ✕, the current dot lit; the whole chip red when a scene in this preset
    picks these nodes with the opposite state. Click the name: HOLD → ON → OFF (HOLD changes no nodes).
    Right-click: favourite (every project) / this project only."""
    picked = Signal(int, object)  # index, None (HOLD) / True / False
    scoped = Signal(int)
    removed = Signal(int)
    keyed = Signal(int, object)  # index, its new shortcut (None: removed)

    def __init__(self, index: int, tag: dict, parent=None, clash: bool = False):
        super().__init__(parent)
        self.index = index
        self.tag = tag
        on = tag["on"]
        armed = on is None
        clash = clash and not armed
        self.setObjectName("tag")
        self.setProperty("armed", armed)
        self.setProperty("clash", clash)
        self.setFixedHeight(21)  # room around the 16 px dots
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 5, 0)
        h.setSpacing(2)
        self.name = QLabel(tag["name"], objectName="tagname")
        self.name.setProperty("armed", armed)
        self.name.setCursor(Qt.PointingHandCursor)
        self.name.setProperty("clash", clash)
        h.addWidget(self.name)
        h.addSpacing(4)
        n = tag["name"]
        dots = QHBoxLayout()
        dots.setSpacing(3)
        state_dots(on, {None: f"HOLD: leave the “{n}” nodes as they are", True: f"Switch every “{n}” node ON",
                        False: f"Switch every “{n}” node OFF"}, lambda st: self.picked.emit(index, st), dots)
        h.addLayout(dots)
        h.addSpacing(4)
        self.hk = HotkeyButton(self)  # the shortcut, as in a Layer State pill: flips the label ON / OFF
        self.hk.setObjectName("pillkey")
        self.hk.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.hk.setFixedHeight(15)
        self.hk.set_hotkey(tag.get("hotkey"))
        self.hk.changed.connect(lambda hk: self.keyed.emit(index, hk))
        key = QHBoxLayout()
        key.setSpacing(2)
        key.addWidget(self.hk)
        key.addWidget(self.hk.clear)
        h.addLayout(key)
        b_x = IconButton("x", "Remove label (doesn't change any nodes)")
        h.addSpacing(5)
        h.addWidget(b_x)
        b_x.clicked.connect(lambda: self.removed.emit(index))
        state = "HOLD — nodes left as they are, pick ON or OFF" if armed else ("ON" if on else "OFF")
        scope = "favourite (every project on this machine)" if tag["scope"] == "F" else "this project only"
        self.setToolTip(f"Every node labelled “{n}” in Resolve (any case): all layers, groups and the "
                        f"timeline, on this clip (every enabled clip with “Apply to all clips” ticked).\nNow: {state} · {scope} (right-click to change)\n"
                        "Click the name to step HOLD → ON → OFF (HOLD changes no nodes); its shortcut flips "
                        "ON / OFF (none on HOLD).\n"
                        "While OFF, a Layer State's HOLD and “Entire layer” picks leave these nodes off when they switch "
                        "a layer back on."
                        + ("\n\nRed: a scene in this preset uses these nodes with the opposite ON/OFF." if clash else ""))

    def mousePressEvent(self, e):  # the name label passes clicks up; accept so the release comes here too
        e.setAccepted(e.button() == Qt.LeftButton)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.name.geometry().contains(e.position().toPoint()):
            self.picked.emit(self.index, next_state(self.tag["on"]))

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
        self.setFixedHeight(21)  # a node-label chip's height
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
        self.chips: list[TagChip] = []
        self.set_hotkey = None  # (tag, hotkey): set by the window
        self.setObjectName("tagfield")
        self.flow = FlowLayout(self, margins=(2, 2, 2, 2), hspacing=5, vspacing=5)  # + the 1 px border: 3 px around the chips
        self.tags: list[dict] = []
        self.typing = False
        self.add_tag = None  # callback(name) set by the window
        self.set_label = None  # callback(tag, state) set by the window
        self.hold_label = None  # callback(tag): back to HOLD, set by the window
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
        self.chips = []
        for i, t in enumerate(self.tags):
            c = TagChip(i, t, self, clash=t["name"].lower() in self.clash)
            c.picked.connect(self._pick)
            c.scoped.connect(self._scope)
            c.removed.connect(self._remove)
            c.keyed.connect(lambda i, hk: self.set_hotkey and self.set_hotkey(self.tags[i], hk))
            self.chips.append(c)
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
            plus = IconButton("plus", "Add a node label — type it exactly as it's named in Resolve", 20, 20,
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
        if state is not None:
            if self.set_label:
                self.set_label(self.tags[i], state)
            return
        if self.tags[i]["on"] is not None and self.hold_label:
            self.hold_label(self.tags[i])

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
        self.flow = FlowLayout(self, hspacing=5, vspacing=3)  # rows 3 px apart, like the grid's
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


class GridFrame(QFrame):
    """The grid's box, outlined at exactly one device pixel: at fractional display scaling a style-sheet
    border can land between pixel rows and blur to two."""

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        dpr = self.devicePixelRatioF()
        p.scale(1 / dpr, 1 / dpr)  # paint in device pixels
        o = self.mapTo(self.window(), QPoint())
        ox, oy = o.x() * dpr, o.y() * dpr  # may fall between the window's device pixels
        x0, y0 = math.ceil(ox), math.ceil(oy)  # outermost whole pixels inside the frame
        x1, y1 = math.floor(ox + self.width() * dpr) - 1, math.floor(oy + self.height() * dpr) - 1
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(EDGE), 1))
        p.setBrush(Qt.NoBrush)
        r = QRectF(x0 - ox + 0.5, y0 - oy + 0.5, x1 - x0, y1 - y0)  # pixel centres: the straight runs stay sharp
        p.drawRoundedRect(r, 9 * dpr, 9 * dpr)  # the radius of QFrame#grid's background


class Divider(QWidget):
    """The grid's 1 px divider, on exactly one device pixel row like GridFrame's outline, so every divider
    looks the same at fractional display scaling."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(11)  # 5 px above and below the line

    def paintEvent(self, e):
        p = QPainter(self)
        dpr = self.devicePixelRatioF()
        p.scale(1 / dpr, 1 / dpr)  # paint in device pixels
        oy = self.mapTo(self.window(), QPoint()).y() * dpr  # may fall between the window's device pixels
        y = math.floor(oy + self.height() * dpr / 2)  # the whole device row at the middle
        p.fillRect(QRectF(0, y - oy, self.width() * dpr, 1), QColor(EDGE))


class ArrowButton(QPushButton):
    """Undo / Redo: a header button with a U-turn arrow instead of text (redo is undo mirrored).
    Off, it keeps its grey box and only the arrow fades, so the header's gaps read evenly."""

    def __init__(self, redo: bool, parent=None):
        super().__init__(parent)
        self.redo = redo
        self.setObjectName("arrow")
        self.setFixedWidth(26)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        if self.redo:
            p.scale(-1, 1)
        pen = QPen(QColor(TEXT if self.isEnabled() else TEXT_FAINT), 1.5)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        u = QPainterPath()  # ↶ drawn like SF Symbols' arrow.uturn.backward: one smooth curve, round ends
        u.moveTo(-5, -2)
        u.lineTo(1, -2)
        u.cubicTo(4.2, -2, 5.5, 0.2, 5.5, 1.8)  # round the right side…
        u.cubicTo(5.5, 3.6, 4, 5, 1.5, 5)  # …and back in, underneath
        p.drawPath(u)
        p.drawPolyline([QPointF(-2, -5), QPointF(-5, -2), QPointF(-2, 1)])  # an open arrowhead


class PinButton(QPushButton):
    """Pin on top: gold pin when the window stays above Resolve, grey when not."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setObjectName("pin")
        self.setFixedSize(26, 22)
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


class LayerPill(QFrame):
    """Layer State chip: name · HOLD ON OFF dots · (1 clip) · key box. Click a dot to pick it, the name to step
    HOLD → ON → OFF. ON / OFF switch the whole layer over the scenes; HOLD puts the scenes' and node labels' states
    back (its shortcut does nothing). Red when a scene picks nodes on it with the opposite state."""
    picked = Signal(str, object)  # key, None (HOLD) / True (ON) / False (OFF)

    def __init__(self, key: str, title: str, tip: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.on = None
        self.setObjectName("pill")
        self.setFixedHeight(22)
        self._tip = tip + "\nClick a dot to pick it, the name to step HOLD → ON → OFF"
        self.setToolTip(self._tip)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 4, 0)
        h.setSpacing(6)
        self.name = QLabel(title, objectName="pillname")
        self.name.setCursor(Qt.PointingHandCursor)  # clicking the name steps the state (see mouseReleaseEvent)
        h.addWidget(self.name)
        dots = QHBoxLayout()
        dots.setSpacing(2)
        self.dots = state_dots(None, {None: "HOLD: the shortcut does nothing, no nodes change",
                                      True: "Switch the whole layer ON", False: "Switch the whole layer OFF"},
                               lambda s: self.picked.emit(self.key, s), dots)
        h.addLayout(dots)
        self.hk = HotkeyButton(self)
        self.hk.setObjectName("pillkey")
        self.hk.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.hk.setFixedHeight(15)
        key = QHBoxLayout()
        key.setSpacing(2)  # the ✕ sits close to its key, like in a node-label chip
        key.addWidget(self.hk)
        key.addWidget(self.hk.clear)
        h.addLayout(key)

    def mousePressEvent(self, e):  # labels pass clicks up to the pill; accept so the release comes here too
        e.setAccepted(e.button() == Qt.LeftButton)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.name.geometry().contains(e.position().toPoint()):
            self.picked.emit(self.key, next_state(self.on))

    def set_state(self, on, clash: list[str] = ()):
        """clash: what is set the other way to this layer: scenes that pick nodes on it, nodes that were the
        other way before it left HOLD."""
        self.on = on
        for state, b in self.dots.items():
            b.set_selected(on is state)
        self.name.setProperty("on", on is not None)
        for w in (self, self.name):
            w.setProperty("clash", bool(clash))
            repolish(w)
        self.setToolTip(self._tip + (f"\n\nRed, set the other way: {'; '.join(clash)}. This layer's "
                                     f"{'ON' if on else 'OFF'} wins; HOLD puts them back" if clash else ""))


# ----------------------------------------------------------------------------- main window

# The fonts/ folder ships with the app and is loaded at start (load_fonts), so Windows and macOS look the same.
# Everything is Inter. Sizes are in px: a pt is 1/96 in on Windows but 1/72 in on macOS.
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # _MEIPASS: a packaged app
FONTS_DIR = APP_DIR / "fonts"
ALERT_ICON = APP_DIR / "assets" / "alert.ico"  # the alerts' sign
FONT = '"Inter 18pt", "Inter", "Segoe UI", "Helvetica Neue", Arial'
SCENE_BARS = "\n".join(f'QLineEdit#colname[scene="{i}"] {{ border-top-color: {QColor(c).darker(150).name()}; }}'
                       for i, c in enumerate(SCENE_COLORS))  # the scene colours, a third darker
STYLE = f"""
* {{ font-family: {FONT}; font-size: 11px; color: {TEXT}; }}
QFrame#grid * {{ font-size: 12px; }}  /* the grid reads a step bigger than the window around it */
QMainWindow, QWidget#root {{ background: {BG}; }}
QLabel#muted {{ color: {TEXT_DIM}; }}
QLabel#flash {{ color: {ACTION_GOLD}; }}
QCheckBox#allclips {{ color: {ACTION_GOLD}; }}
QLabel#flash[idle="true"] {{ color: {TEXT_FAINT}; }}
QLabel#rowlabel {{ color: #808283; padding-right: 8px; }}
QLabel#rowlabel[layer="true"] {{ color: #a6a6a7; }}
QLabel#rowlabel[open="true"] {{ color: {ACTION_GOLD}; }}  /* the graph open in Resolve's node editor */
QLabel#one {{ font-size: 8px; color: {TEXT_DIM}; border: 1px solid {BTN}; border-radius: 4px; padding: 0 3px;
    min-height: 11px; max-height: 11px; }}
QFrame#grid {{ background: {BG_GRID}; border: none; border-radius: 9px; }}
QLineEdit#colname {{ background: {BG_DARK}; border: 1px solid transparent; border-top: 2px solid {GREEN};
    border-radius: 6px; padding: 6px 7px 3px 7px; color: {TEXT}; font-size: 12px; }}
QLineEdit#colname:hover {{ background: #29292e; }}
{SCENE_BARS}
QLineEdit#colname:focus {{ border-left-color: transparent; border-right-color: transparent;
    border-bottom-color: transparent; background: {FIELD}; color: {TEXT_BRIGHT}; }}
QPushButton {{ background: {BTN}; border: 1px solid transparent; border-radius: 6px; padding: 3px 8px; color: {TEXT}; }}
QPushButton:hover {{ background: {BTN_HOVER}; }}
QPushButton:pressed {{ background: {FIELD}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {BG_DARK}; }}
QPushButton#arrow:disabled {{ background: {BTN}; }}
QPushButton:checked {{ background: {BLUE}; color: {TEXT}; }}
QPushButton[head="true"]:checked, QPushButton[head="true"]:pressed {{ background: {FIELD}; color: {ACCENT}; }}
QPushButton#ib {{ background: transparent; border: none; padding: 0; }}
QPushButton#cell {{ background: {FIELD}; border: 1px solid transparent; border-radius: 6px; padding: 3px 7px;
    text-align: left; color: {TEXT}; min-height: 16px; }}
QPushButton#cell:hover {{ background: #29292b; }}
QPushButton#cell[empty="true"] {{ color: {TEXT_FAINT}; }}
QPushButton#cell[all="true"] {{ color: {ACCENT}; }}
QPushButton#cell[missing="true"] {{ color: {CLASH_TEXT}; font-style: italic; }}
QPushButton#cell[recording="true"] {{ color: {TEXT}; }}
QPushButton#cell[clash="true"] {{ border: 1px solid {RED}; }}
QFrame#pill {{ background: {FIELD}; border: 1px solid transparent; border-radius: 6px; }}
QLabel#pillname {{ color: {TEXT_FAINT}; }}
QLabel#pillname[on="true"] {{ color: {TEXT}; }}
QFrame#pill[clash="true"] {{ background: #2e2326; border-color: #5a2b28; }}  /* as a Node Label set against a scene */
QLabel#pillname[clash="true"] {{ color: {CLASH_TEXT}; }}
QPushButton#pillkey {{ background: {BTN}; border: 1px solid transparent; border-radius: 4px; padding: 0 6px;
    font-size: 12px; color: {TEXT_DIM}; min-height: 13px; }}
QPushButton#pillkey[recording="true"] {{ color: {TEXT}; }}
QPushButton#pillkey[clash="true"] {{ border-color: {RED}; }}
QFrame#tagfield {{ background: {FIELD}; border: 1px solid transparent; border-radius: 8px; }}
QFrame#tag {{ background: {BG_DARK}; border: 1px solid {BTN}; border-radius: 5px; }}
QFrame#tag[armed="true"] {{ background: transparent; border: 1px solid {BTN_HOVER}; }}
QFrame#tag[clash="true"] {{ background: #2e2326; border-color: #5a2b28; }}  /* a hint of red */
QLabel#tagname {{ color: {TEXT}; }}
QLabel#tagname[armed="true"] {{ color: {TEXT_FAINT}; }}
QLabel#tagname[clash="true"] {{ color: {CLASH_TEXT}; }}
QLineEdit#tagedit {{ background: transparent; border: none; padding: 0 7px 0 8px; color: {TEXT}; }}
QPushButton#pin {{ padding: 0; }}
QPushButton#seg {{ padding: 2px 0; min-width: 20px; max-width: 20px; }}
QPushButton#seg[sel="true"]:!checked {{ background: {FIELD}; }}  /* picked, scenes differ from it; blue: they match */
QCheckBox {{ spacing: 6px; color: {TEXT_DIM}; }}
QCheckBox::indicator {{ width: 11px; height: 11px; border-radius: 3px; border: 1px solid transparent; background: {BTN}; }}
QCheckBox::indicator:checked {{ background: {BLUE}; border-color: transparent; }}
QCheckBox:disabled {{ color: {TEXT_FAINT}; }}
QMenu {{ background: {BG}; border: 1px solid #34343a; border-radius: 8px; padding: 4px; }}  /* a shade lighter than BG */
QMenu::item {{ padding: 4px 20px 4px 7px; border-radius: 5px; }}
QMenu::item:selected {{ background: {MENU_HOVER}; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: #1c1c20; margin: 4px 2px; }}  /* the grid divider's contrast on the menu */
QMenu::indicator {{ width: 11px; height: 11px; border-radius: 3px; border: 1px solid transparent; background: {BTN}; margin-left: 4px; }}
QMenu::indicator:checked {{ background: {BLUE}; }}
QToolTip {{ background: {FIELD}; color: {TEXT_DIM}; border: none; border-radius: 6px; padding: 2px 4px; }}  /* like the cells; text like the checkboxes */
"""


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


UNDO_STEPS = 8  # how far Undo / Redo of node picks reach
WINDOW_W = 904  # starting width: four scene columns


class MainWindow(QMainWindow):
    resolve_windows = Signal(bool)  # from the watcher thread
    layer_settings = Signal(object)  # from the layer-name reader thread
    update_checked = Signal(object)  # from the update check thread: latest_release() or None (couldn't check)

    def __init__(self, demo: bool, standby: bool = False, ignore_pid: int | None = None):
        super().__init__()
        self._sized = False  # first fit done: the window has its starting width
        self._grid_need = 0
        self.server = None  # single-instance QLocalServer, set by main()
        self.demo = demo
        self.standby = standby  # launched with --standby: stay hidden until Resolve runs
        self._parked = standby
        self.cfg = load_config(demo)  # every scene, layer key and node label starts on HOLD
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
        self._pending_layers = None  # names / Post Group read from an open Project Settings, applied once it closes
        self.seg_btns: list[QPushButton] = []  # preset numbers (built with the footer; save() may run before)
        self._scenes_seen = None  # the scenes as last compared with the selected preset
        self._proj_key = None
        self.rkeys = ResolveShortcuts()
        self.rkeys.load()
        self._undo, self._redo = deque(maxlen=UNDO_STEPS), deque(maxlen=UNDO_STEPS)  # node-pick snapshots
        self._picks_before, self._menu_up = [], False
        self._open_view, self._row_labels = "", {}  # Resolve's node editor dropdown text; grid row -> its label

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}" + (" — demo" if demo else ""))
        self.setWindowIcon(make_icon())
        self._build_ui()
        if self.cfg.get("pinned"):
            QTimer.singleShot(0, self._apply_pin)

        self.hotkeys = HotkeyEngine(self.cfg["focus_only"])
        self.hotkeys.fired.connect(self.on_hotkey)
        self.hotkeys.failed.connect(lambda msg: self.flash(msg, 0))
        self._bind()
        self.hotkeys.start()

        self._show_nodes(self._rows_for(1))
        self.refresh_states()
        self._suspended = False
        self._main_wanted = not standby  # standby: hotkeys only, the window opens from Resolve's Scripts menu
        self.resolve_windows.connect(self._on_resolve_windows, Qt.QueuedConnection)
        self.layer_settings.connect(self._apply_layer_settings, Qt.QueuedConnection)
        if not demo:
            threading.Thread(target=watch_resolve_layer_settings, args=(self.layer_settings.emit,), daemon=True).start()
        self._release = None  # latest_release(), once checked
        self.update_checked.connect(self._on_update_checked, Qt.QueuedConnection)
        if not demo:
            self._check_updates()
        self.watch = ResolveWatch(self._resolve_closed, self._resolve_windows_thread, ignore_pid)
        self._native = [int(self.winId())]  # for hiding from the watcher thread
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
        v.setContentsMargins(8, 15, 8, 10)  # the grid box's left line under the title's first letter
        v.setSpacing(8)

        # header: refresh · clean plate · pin
        h = QHBoxLayout()
        h.setSpacing(6)
        h.addStretch(1)
        if self.demo:
            nxt = QPushButton("Next clip ▸")
            nxt.setToolTip("Demo: move the playhead to the next clip")
            nxt.clicked.connect(self._demo_next_clip)
            h.addWidget(nxt)
        self.btn_undo, self.btn_redo = ArrowButton(False), ArrowButton(True)
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        h.addWidget(self.btn_undo)
        h.addWidget(self.btn_redo)
        self._sync_undo()
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
        for b in (self.btn_undo, self.btn_redo, self.btn_pin):  # icon buttons as tall as the text ones
            b.setFixedHeight(refresh.sizeHint().height())
        for i in range(h.count()):
            if w := h.itemAt(i).widget():
                w.setProperty("head", True)  # styled: darker with yellow text when down
        v.addLayout(h)

        # grid
        self.grid_frame = GridFrame(objectName="grid")
        self.grid = QGridLayout(self.grid_frame)
        self.grid.setContentsMargins(10, 8, 10, 10)
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(3)
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
            ne.setProperty("scene", str(i % len(SCENE_COLORS)))
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
            sb = SceneState()
            sb.setToolTip("Right-click for This clip only")
            sb.setContextMenuPolicy(Qt.CustomContextMenu)
            sb.picked.connect(lambda state, i=i: self._pick_state(i, state))
            sb.customContextMenuRequested.connect(lambda pos, i=i, sb=sb: self._state_menu(i, sb.mapToGlobal(pos)))
            self.state_btns.append(sb)
            om = QLabel("1 clip", objectName="one")
            om.setAlignment(Qt.AlignCenter)
            om.setToolTip("This scene only acts on the clip under the playhead")
            self.one_marks.append(om)

        self.tags_field = TagsField()
        self.tags_field.add_tag = self._add_tag
        self.tags_field.set_label = self.run_label
        self.tags_field.hold_label = self.hold_label
        self.tags_field.set_hotkey = self._set_label_hotkey
        self.tags_field.changed.connect(self._tags_changed)

        # footer: connection · flash · options · presets
        f = QHBoxLayout()
        f.setSpacing(6)
        self.conn_dot = StateDot(7)
        self.conn_label = TailLabel("Connecting…", objectName="muted")  # takes the room left; the clip name ends it
        # "DR 21 · clip [+] (group)": the + box (every other clip too) sits right after the clip name
        self.all_clips_box = IconButton("plus", "…and every other enabled clip in the timeline", 12, 12,
                                        boxed=True, icon=8.0)
        self.all_clips_box.setAttribute(Qt.WA_TransparentForMouseEvents)  # a sign, not a button
        self.all_clips_box.hide()
        self.conn_rest = QLabel("", objectName="muted")
        self._has_clip = False
        f.addWidget(self.conn_dot)
        f.addWidget(self.conn_label)
        f.addWidget(self.all_clips_box)
        f.addWidget(self.conn_rest)
        f.addStretch(1)
        f.addSpacing(10)  # the same gap as between the boxes on the right, once the clip name is shortened
        self.chk_all =QCheckBox("Apply to all clips in the Timeline", objectName="allclips")
        self.chk_all.setToolTip("Unticked: scenes, layer keys and node labels change the clip under the playhead "
                                "only.\nTicked: every enabled clip in the timeline (a scene or layer key set to "
                                "“This clip only” still changes just that clip).\nTimeline nodes belong to the "
                                "timeline either way.")
        self.chk_all.toggled.connect(self._all_clips_toggled)  # unticked at every launch
        f.addWidget(self.chk_all)  # on the right, next to Preset: it stays put as the clip name changes
        f.addSpacing(15)
        self.conn_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        # status line: its own row, above the footer
        self.status = QLabel("", objectName="flash")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setContentsMargins(self.conn_dot.width() + f.spacing(), 5, 0, 0)  # under "DR …", past the dot
        bottom = QVBoxLayout()  # status and footer: the status 5px lower, its gap to the footer 5px shorter
        bottom.setSpacing(v.spacing() - 5)
        bottom.addWidget(self.status)
        v.addLayout(bottom)
        if self.cfg["script_shortcut"] and not self.demo:  # (re)written each launch: points at this copy, in
            with contextlib.suppress(Exception):           # case the app was moved, renamed or packaged
                set_script_shortcut(True)
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
        f.addSpacing(15)
        self.btn_settings = QPushButton("Settings")
        self.btn_settings.clicked.connect(self._settings_menu)
        f.addWidget(self.btn_settings)
        bottom.addLayout(f)
        # at launch: the preset the scenes match is selected (blue); none → 1
        self._pick_preset(next((s for s in self.cfg["presets"] if self._matches_preset(s)), "1"))

        self.setCentralWidget(root)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self.flash(""))
        self.flash("")

    def _layer_name(self, n: int) -> str:
        return self.cfg["layer_names"].get(str(n)) or f"Layer {n}"

    def _open_slot(self) -> str | None:
        """The row of the graph open in Resolve's node editor, from its dropdown's text: "Timeline",
        "Clip - <layer name>"... (None: not on the Color page, or not readable)."""
        text = (self._open_view or "").strip()
        low = text.lower()
        if not text:
            return None
        if low.startswith("timeline"):
            return "timeline"
        if not low.startswith("clip"):
            if "clip" in low and "pre" in low:
                return "preclip"
            if "clip" in low and "post" in low:
                return "postclip"
            return None
        bare = lambda t: re.sub(r"[^0-9a-z]", "", t.lower())  # Resolve shows "--- L1" as "-- -  L1"
        rest = bare(text[4:])
        if not rest:
            return clip_slot(1)
        for n in range(1, MAX_LAYERS + 1):
            if rest in (bare(self._layer_name(n)), bare(f"Layer {n}"), bare(f"L{n}")):
                return clip_slot(n)
        return None

    def _mark_open_row(self):
        """The row label of the graph open in Resolve turns gold, like "Apply to all clips"."""
        slot = self._open_slot()
        for row, lab in self._row_labels.items():
            if bool(lab.property("open")) != (row == slot):
                lab.setProperty("open", row == slot)
                repolish(lab)

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
        self._layers = max(layers, 1, *saved)
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
        self._row_labels = {}

        def temp(w):
            self._grid_widgets.append(w)
            return w

        def label(text, layer=False):
            lab = temp(QLabel(text, objectName="rowlabel"))
            lab.setProperty("layer", layer)
            lab.setFixedHeight(24)
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
            else:
                lab = label(slot_title(row))
            self._row_labels[row] = lab
            self.grid.addWidget(lab, r, 0)
            for i, t in enumerate(self.cfg["toggles"]):
                p = temp(NodePicker(i, row))
                p.set_data(self._nodes.get(row), self._reasons.get(row, "Not connected"), t["sel"].get(row, []))
                p.changed.connect(self._selection_changed)
                p.menu_open.connect(self._pick_menu)
                self.pickers[(i, row)] = p
                self.grid.addWidget(p, r, i + 1)
            r += 1
        self.grid.addWidget(temp(Divider()), r, 0, 1, NUM_TOGGLES + 1)
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
        self.grid.addWidget(label("Scene Shortcut"), r, 0)
        for i in range(NUM_TOGGLES):
            self.grid.addWidget(self.hotkey_btns[i], r, i + 1)
        r += 1
        # Layer State
        self.grid.addWidget(label("Layer State"), r, 0, top_right)
        holder = temp(FlowHolder())
        flow = holder.flow
        for key in self._slot_order():
            lk = self._layer_key(key)
            layer = slot_layer(key)
            tip = f"Whole {self._layer_name(layer) if layer else slot_title(key)} on / off"
            tip += "\nTurning it back ON leaves OFF node labels and nodes picked in OFF scenes off"
            pill = LayerPill(key, self._layer_key_title(key), tip, holder)
            pill.hk.set_hotkey(lk.get("hotkey"))
            pill.hk.changed.connect(lambda hk, key=key: self._set_layer_hotkey(key, hk))
            pill.picked.connect(self._pick_layer)
            pill.set_state(lk["on"])
            self.pills[key] = pill
            flow.addWidget(pill)
        self.grid.addWidget(holder, r, 1, 1, NUM_TOGGLES)
        r += 1
        self.grid.addWidget(temp(Divider()), r, 0, 1, NUM_TOGGLES + 1)
        r += 1
        # Node labels
        tags_label = label("Node Label")
        tags_label.setFixedHeight(27)  # centred on the first row of chips (field border + margin + half a chip)
        self.grid.addWidget(tags_label, r, 0, top_right)
        self.grid.addWidget(self.tags_field, r, 1, 1, NUM_TOGGLES)
        self._refresh_tags()
        self._mark_clashes()
        self._mark_open_row()
        QTimer.singleShot(0, self._fit_height)
        QTimer.singleShot(80, self._fit_height)  # again once new cells are styled

    def _refresh_clicked(self):
        self.setUpdatesEnabled(False)  # one repaint at the end, no blink in between
        try:
            self._refresh()
        finally:
            self.setUpdatesEnabled(True)

    def _refresh(self):
        if self.bridge.resolve is not None and self._visible_tags():
            self._sync_labels(self._visible_tags())
            self.save()
            self._refresh_tags()
        self.rkeys.load(force=True)
        self._mark_clashes()
        self._keys_hint()
        if self.bridge.resolve:
            self.refresh_context(True)
            self._drop_missing()
        else:
            self.try_connect()

    def _drop_missing(self):
        """Refresh: a pick whose node was renamed in Resolve takes the new name (same node number); picks whose
        node is gone from the clip's node tree are forgotten (where the tree could be read)."""
        renamed, dropped = [], 0
        for t in self.cfg["toggles"]:
            for slot, sel in list(t["sel"].items()):
                nodes = self._nodes.get(slot)
                if nodes is None:
                    continue
                labels = {n[0]: n[1] for n in nodes}  # node number -> its label now
                keep = []
                for pick in sel:
                    if not pick.get("all") and not pick.get("label") and labels.get(pick.get("index")):
                        new = labels[pick["index"]]  # the node got a name in Resolve
                        renamed.append(f"{pick_name(pick)} → {new}")
                        pick = {"index": pick["index"], "label": new}
                    if not (pick.get("all") or match_nodes(nodes, [pick])):
                        if pick.get("index") not in labels:  # the node is gone
                            dropped += 1
                            continue
                        new = labels[pick["index"]]  # same node number, renamed in Resolve
                        renamed.append(f"{pick_name(pick)} → {pick_name({'index': pick['index'], 'label': new})}")
                        pick = {"index": pick["index"], "label": new}
                    if pick not in keep:
                        keep.append(pick)
                if not keep:
                    del t["sel"][slot]
                elif keep != sel:
                    t["sel"][slot] = keep
        if renamed or dropped:
            self.save()
            self._show_nodes(self._rows_for(self._layers))  # the cells take the new names, in place
            self.refresh_states()
            self._refresh_tags()
            parts = [f"renamed {', '.join(renamed)}"] if renamed else []
            if dropped:
                parts.append(f"removed {dropped} pick{'s' if dropped != 1 else ''} no longer on this clip")
            self.flash("Refresh: " + "; ".join(parts), 8000)

    def _apply_layer_settings(self, d):
        """From the watcher: Project Settings' layer names / Post Group while it's open (held), then "closed"
        (applied: nothing changes while it's open). Also the graph open in Resolve's node editor ({"view": text})."""
        if isinstance(d, dict) and "view" in d:
            self._open_view = d["view"]
            self._mark_open_row()
            return
        if d != "closed":
            self._pending_layers = d
            return
        d, self._pending_layers = self._pending_layers, None
        if not d:
            return
        old = self.cfg["layer_names"]
        # a field caught empty while you retype it keeps its last name (Resolve shows "Layer N" for an empty one)
        names = {k: v.strip() or old.get(k, "") for k, v in d["names"].items()}
        names = {k: v for k, v in names.items() if v}
        post = self.cfg["post_layer"] if d["post"] is None else d["post"]
        if names == self.cfg["layer_names"] and post == self.cfg["post_layer"]:
            return
        self.cfg["layer_names"] = names
        self.cfg["post_layer"] = post
        self.save()
        self._rows = []  # rebuild so rows and layer pills show the new names / order
        self._show_nodes(self._rows_for(self._layers))

    def _keys_hint(self):
        st = self.rkeys.status
        if st == "saved":
            self.flash(f"Resolve's current keyboard layout isn't on disk: checking clashes against your saved "
                       f"preset “{self.rkeys.source}” (shortcuts folder)", 12000)
        elif st == "builtin":
            self.flash("Resolve is on a built-in keyboard layout: save it as a custom preset "
                       "(Keyboard Customization › Save As) to get shortcut clash warnings", 12000)
        elif st in ("missing", "error"):
            self.flash("Couldn't read Resolve's keyboard shortcuts, so clashes can't be checked", 8000)

    def _mark_clashes(self):
        for b in self.hotkey_btns + [p.hk for p in self.pills.values()] + [c.hk for c in self.tags_field.chips]:
            b.set_clash(self.rkeys.clash(b.hotkey))

    def _confirm_key(self, hk, who: str) -> bool:
        """True = keep the key. Asks first when Resolve already uses it."""
        self.rkeys.load()
        hits = self.rkeys.clash(hk)
        if not hits:
            return True
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
        return self._ask("Shortcut already used by Resolve",
                         f"{gold(hk['text'])} is also Resolve's {', '.join(f'“{gold(h)}”' for h in hits[:3])}"
                         f"{html.escape(more)}.",
                         f"If you give it to {html.escape(who)}, both will fire when you press it.",
                         "Use anyway", "Pick another")

    def _ask(self, title: str, text: str, info: str, yes: str, no: str) -> bool:
        """The app's alert: sign, rich text (gold() for keys and names), two buttons. True = yes."""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setStyleSheet("QLabel { font-size: 12px; }")  # a step up from the window's 11px
        box.setIconPixmap(QIcon(str(ALERT_ICON)).pixmap(QSize(32, 32), self.devicePixelRatioF()))
        QTimer.singleShot(0, lambda: alert_title_bar(box))  # once it's on screen

        def para(t: str) -> str:  # a little more room between the lines of a wrapped sentence
            return f'<p style="line-height:125%; margin:0">{t}</p>'

        box.setTextFormat(Qt.RichText)
        box.setText(para(text))
        box.setInformativeText(para(info))
        keep = box.addButton(yes, QMessageBox.AcceptRole)
        box.addButton(no, QMessageBox.RejectRole)
        lay = box.layout()
        lay.setContentsMargins(18, 13, 18, 12)  # room between the border and everything in it (the sign's top)
        lay.setVerticalSpacing(4)  # the two lines of text closer together
        lay.setHorizontalSpacing(2)  # the sign close to the text
        if icon := box.findChild(QLabel, "qt_msgboxex_icon_label"):
            icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            lay.setAlignment(icon, Qt.AlignTop)
        if text := box.findChild(QLabel, "qt_msgbox_label"):
            text.setContentsMargins(0, 3, 0, 0)  # the text starts 3px below the sign's top
        if buttons := box.findChild(QDialogButtonBox):
            buttons.setContentsMargins(0, 8, 0, 0)  # a clear gap above the buttons (the line height adds some)
        box.exec()
        return box.clickedButton() is keep

    def _fit_height(self):
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)  # widgets that just changed size (a pill) first
        lay = self.centralWidget().layout()
        lay.activate()
        gw = self.grid_frame.width() if self._sized else WINDOW_W - 28
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
            self.resize(WINDOW_W, self.height())

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
        self._sync_preset()  # every change comes through here: blue / grey preset number

    def _bind(self):
        b = [(i, t.get("hotkey")) for i, t in enumerate(self.cfg["toggles"])]
        b += [(k, v.get("hotkey")) for k, v in self.cfg["layer_keys"].items()]
        b += [(label_key(t), t.get("hotkey")) for t in self._visible_tags()]  # this project's node labels
        if b != getattr(self, "_bound", None):  # (runs on every Node Label refresh)
            self._bound = b
            self.hotkeys.set_bindings(b)

    def refresh_states(self):
        for i, t in enumerate(self.cfg["toggles"]):
            configured = has_selection(t)
            self.state_btns[i].set_state(t["on"], configured)
            self.one_marks[i].setVisible(bool(t.get("one_clip")))
        for key, pill in self.pills.items():
            lk = self.cfg["layer_keys"].get(key, {})
            on = lk.get("on")
            # a mismatch always pairs with something: a scene that picks nodes on this layer, or a Node Label on it
            scenes = [] if on is None else [t for t in self.cfg["toggles"] if t["on"] is not None and t["sel"].get(key)]
            on_layer = {str(n[1] or "").lower() for n in self._nodes.get(key) or []}
            tags = [] if on is None else [t for t in self._visible_tags()
                                          if t["on"] is not None and t["name"].lower() in on_layer]
            clash = [t["name"] for t in scenes if t["on"] != on] + \
                    [f"Node Label “{t['name']}”" for t in tags if t["on"] != on]
            if (scenes or tags) and lk.get("before"):  # what HOLD will put back: nodes that were the other way
                names = {n[0]: n[1] for n in self._nodes.get(key) or []}
                other = [pick_name({"index": int(i), "label": names.get(int(i))})
                         for i, was in lk["before"]["nodes"].items() if was != on]
                if other:
                    clash.append(f"{', '.join(other)} (was {'OFF' if on else 'ON'} before)")
            pill.set_state(on, clash)

    # ---------------------------------------------------------------- tags
    def _visible_tags(self) -> list[dict]:
        pk = self._proj_key or "_"
        return [t for t in self.cfg["tags"] if t["scope"] == "F" or t.get("project") == pk]

    def _label_clashes(self) -> set[str]:
        """Node Labels whose ON/OFF disagrees with a scene (not HOLD) that picks a node with that label, or with the
        Layer State of a layer that has a node with that label."""
        out = set()
        for tag in self._visible_tags():
            if tag["on"] is None:  # HOLD: nothing to compare yet
                continue
            name = tag["name"].lower()
            if any(lk.get("on") is not None and lk["on"] != tag["on"]
                   and name in {str(n[1] or "").lower() for n in self._nodes.get(key) or []}
                   for key, lk in self.cfg["layer_keys"].items()):
                out.add(name)
                continue
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
        self._mark_clashes()  # the chips are new: their key boxes too
        self._bind()  # another project's labels, or a label's key changed
        # red dot on every scene cell that picks a node whose Node Label, or whose layer's Layer State, is set
        # the other way
        labels = {t["name"].lower(): t for t in self._visible_tags() if t["on"] is not None}
        for (i, slot), picker in self.pickers.items():
            t = self.cfg["toggles"][i]
            why = []
            if t["on"] is not None and t["sel"].get(slot):
                for sp in t["sel"][slot]:
                    tag = labels.get(str(sp.get("label") or "").lower())
                    if tag and tag["on"] != t["on"] and f"Node Label “{tag['name']}”" not in why:
                        why.append(f"Node Label “{tag['name']}”")
                lk = self.cfg["layer_keys"].get(slot, {})
                layer_on = lk.get("on")
                mixed = layer_on is not None and any(was != layer_on for was in
                                                     (lk.get("before") or {}).get("nodes", {}).values())
                if layer_on is not None and layer_on != t["on"]:
                    why.append(f"Layer State {self._layer_key_title(slot)}")
                elif mixed:  # agrees with the scene, but overrides nodes that were the other way
                    why.append(f"Layer State {self._layer_key_title(slot)} ({'ON' if layer_on else 'OFF'} "
                               f"over nodes that were {'OFF' if layer_on else 'ON'})")
            picker.set_label_clash(why)
        QTimer.singleShot(0, self._fit_height)

    def _add_tag(self, name: str):
        if any(t["name"].lower() == name.lower() for t in self._visible_tags()):
            self.flash(f"“{name}” is already in Node Label")
        else:
            tag = {"name": name, "on": None, "scope": "P", "project": self._proj_key or "_"}  # HOLD
            self.cfg["tags"].append(tag)
            self._sync_labels([tag], quiet_found=True, keep_state=True)
            self.save()
        self.tags_field.typing = True
        self._refresh_tags()

    def _sync_labels(self, tags: list[dict], quiet_found=False, keep_state=False):
        """Set label chips to the real state of those nodes on the open clip (and timeline).
        HOLD labels (and keep_state) only get checked for existence — they stay grey until ON / OFF is picked."""
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
            self.flash(f"No node labelled {', '.join(f'“{m}”' for m in missing)} on this clip")
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

    def _layer_snapshot(self, key: str) -> dict | None:
        """The layer's real node states on the clip under the playhead (the timeline's for "timeline"), read from
        Resolve: {"clip": its id, "nodes": {number: on}}. None where they can't be read (pre- / post-group)."""
        if key in ("preclip", "postclip") or self.bridge.resolve is None:
            return None
        try:
            proj, tl, item = self.bridge.context()
            states = (self.bridge.graph_states(proj, tl) or {}).get(key) if proj and tl else None
            clip = "timeline" if key == "timeline" else (_safe(item.GetUniqueId) if item else None)
        except Exception:
            return None
        return {"clip": clip, "nodes": {str(i): on for i, on in states.items()}} if states and clip else None

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
        for t in self._visible_tags():
            if label_key(t) != skip and same(t.get("hotkey")):
                return ("label", t)
        return None

    def _clear_owner(self, owner, hk, new_name):
        kind, who = owner
        if kind == "scene":
            old = self.cfg["toggles"][who]["name"]
            self.cfg["toggles"][who]["hotkey"] = None
            self.hotkey_btns[who].set_hotkey(None)
        elif kind == "label":  # its chip is rebuilt by the caller's _refresh_tags
            old = f"“{who['name']}”"
            who["hotkey"] = None
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

    def _set_label_hotkey(self, tag: dict, hk):
        """A Node Label chip's key box: same checks as a layer key (Resolve clash, a key used elsewhere moves)."""
        name = f"“{tag['name']}”"
        if hk and not self._confirm_key(hk, name):
            self._refresh_tags()  # back to its old key
            return
        if hk and (owner := self._key_owner(hk, label_key(tag))):
            self._clear_owner(owner, hk, name)
        tag["hotkey"] = hk
        self.save()
        self._refresh_tags()  # rebuilds the chips (new widths), re-marks clashes and rebinds

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
        self._layer_key(key)["hotkey"] = hk
        self._mark_clashes()
        self._bind()
        self.save()
        QTimer.singleShot(0, self._fit_height)  # the pill changed width: its row may wrap onto a second line, or back

    # ---------------------------------------------------------------- undo / redo of node picks
    def _picks(self) -> list[dict]:
        return [copy.deepcopy(t["sel"]) for t in self.cfg["toggles"]]

    def _pick_menu(self, opening: bool):
        """A node menu opened / closed: everything it changed is one undo step; no undo while it's open."""
        if opening:
            self._picks_before = self._picks()
        elif self._picks() != self._picks_before:
            self._undo.append(self._picks_before)
            self._redo.clear()
        self._menu_up = opening
        self._sync_undo()

    def _sync_undo(self):
        for b, stack, word in ((self.btn_undo, self._undo, "Undo"), (self.btn_redo, self._redo, "Redo")):
            b.setEnabled(bool(stack) and not self._menu_up)
            b.setToolTip(f"{word} the last node pick change ({len(stack)} of {UNDO_STEPS} step"
                         f"{'s' if len(stack) != 1 else ''})" if stack else f"Nothing to {word.lower()}")

    def _restore_picks(self, frm, to, word: str):
        if self._menu_up or not frm:
            return
        to.append(self._picks())
        for t, sel in zip(self.cfg["toggles"], frm.pop(), strict=True):
            if sel != t["sel"]:
                t["sel"] = sel
                t["on"] = None  # changed picks: their state is unknown until the scene fires, as after a pick
        self.save()
        self._rows = []
        self._show_nodes(self._rows_for(self._layers))
        self.refresh_states()
        self._refresh_tags()
        self._sync_undo()
        self.flash(f"{word}: node picks")

    def undo(self):
        self._restore_picks(self._undo, self._redo, "Undo")

    def redo(self):
        self._restore_picks(self._redo, self._undo, "Redo")

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

    def _pick_state(self, i: int, state: bool | None):
        if state is not None:
            self.run_toggle(i, state)
        elif self.cfg["toggles"][i]["on"] is not None:  # HOLD: its nodes back to how they were before ON / OFF
            t = self.cfg["toggles"][i]
            before = t.pop("before", None)
            n = self._restore(before, t["name"])
            t["on"] = None
            self.save()
            self.refresh_states()
            self._refresh_tags()
            if n:
                self.flash(f"{t['name']} → HOLD: back to how it was before ({n} node{'s' if n != 1 else ''})")
            elif before:
                self.flash(f"{t['name']} → HOLD: its nodes are left as they are (another clip, or Resolve didn't "
                           f"switch them)")

    def _state_menu(self, i: int, gpos):
        t = self.cfg["toggles"][i]
        m = QMenu(self)
        a = m.addAction("This clip only")
        a.setCheckable(True)
        a.setChecked(bool(t.get("one_clip")))
        a.toggled.connect(lambda on: self._set_one_clip(i, on))
        m.exec(gpos)

    def _set_one_clip(self, i: int, on: bool):
        self.cfg["toggles"][i]["one_clip"] = on
        self.save()
        self.refresh_states()

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
            hwnd, dwm = int(self.winId()), ctypes.windll.dwmapi
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            edge = ctypes.c_uint(colorref(BORDER))
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(edge), 4)  # DWMWA_BORDER_COLOR; Windows 10 ignores it
            text = ctypes.c_uint(colorref(TITLE_TEXT))
            dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)  # DWMWA_TEXT_COLOR; Windows 10 ignores it
            # WTA_OPTIONS: WTNCA_NOSYSMENU — no system menu, but the icon slot stays (Windows strips leading
            # spaces, so this is what insets the title): a faint circle in it, a shade lighter than the bar
            no_menu = (ctypes.c_uint * 2)(0x4, 0x6)
            ctypes.windll.uxtheme.SetWindowThemeAttribute(hwnd, 1, no_menu, ctypes.sizeof(no_menu))  # WTA_NONCLIENT
            self._title_dot()
        self.setWindowOpacity(0.0)
        self.show()
        QTimer.singleShot(160, lambda: self.setWindowOpacity(1.0))
        if IS_WIN:
            QTimer.singleShot(200, self._title_dot)  # Qt puts its own icon back while showing

    def _title_dot(self):
        """Windows: the title bar's icon slot shows a faint circle, not the app logo (the taskbar keeps the logo)."""
        import ctypes
        if not hasattr(self, "_dot_icon"):
            dot = QImage(32, 32, QImage.Format_ARGB32)
            dot.fill(Qt.transparent)
            p = QPainter(dot)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(TITLE_BG).lighter(135))
            p.drawEllipse(QRectF(7, 7, 18, 18))
            p.end()
            self._dot_icon = int(dot.toHICON())
            # moving to a screen with other scaling: Qt puts its own icon back
            self.windowHandle().screenChanged.connect(lambda _s: QTimer.singleShot(0, self._title_dot))
        u32 = ctypes.windll.user32
        u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        u32.SendMessageW(int(self.winId()), 0x80, 0, self._dot_icon)  # WM_SETICON, ICON_SMALL

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

    def _settings_menu(self):
        """Settings: a menu that opens upwards from its button, right edges aligned, styled like the node lists."""
        if time.monotonic() - getattr(self, "_settings_closed", 0) < 0.3:
            return  # this click on the button just closed the menu: leave it closed
        m = StickyMenu(self)
        m.setToolTipsVisible(True)
        rel = self._release
        if rel and rel["newer"]:
            a = DotAction("Update Release", QColor(ACCENT), m)
            a.setToolTip(f"{APP_NAME} {rel['version']} is out (you have {APP_VERSION})")
            a.triggered.connect(self._update_release)
        elif rel:
            a = DotAction("Running Latest Release", CONN_COLOR, m)
            a.setToolTip(f"{APP_NAME} {APP_VERSION} is the latest release")
        else:
            a = DotAction("Check for Updates", QColor("#1f1f1f"), m)
            a.setToolTip("Couldn't reach GitHub at start: check again")
            a.triggered.connect(lambda: self._check_updates(report=True))
        m.addAction(a)
        m.addSeparator()
        items = [("Script Shortcut", self.cfg["script_shortcut"], self._set_script_shortcut,
                  f"Add _{APP_NAME} to Resolve's Workspace › Scripts menu, a second way to open {APP_NAME}"),
                 ("Keys only in Resolve", self.cfg["focus_only"], self._set_focus_only,
                  "Shortcuts work only while Resolve (or this window) is in front" if IS_WIN else "Windows only")]
        for text, on, slot, tip in items:
            a = QAction(text, m, checkable=True)
            a.setChecked(on)
            a.setToolTip(tip)
            a.setEnabled(IS_WIN or slot != self._set_focus_only)
            a.toggled.connect(slot)
            m.addAction(a)
        b = self.btn_settings
        size = m.sizeHint()
        m.exec(b.mapToGlobal(QPoint(b.width() - size.width(), -size.height() - 2)))
        self._settings_closed = time.monotonic()
        m.deleteLater()

    def _check_updates(self, report: bool = False):
        """Ask GitHub for the latest release, in the background (at every start; again from Settings)."""
        self._report_update = report

        def run():
            try:
                rel = latest_release()
            except Exception as e:
                print(f"Couldn't check for updates: {e}", file=sys.stderr)
                rel = None
            self.update_checked.emit(rel)

        threading.Thread(target=run, daemon=True).start()

    def _on_update_checked(self, rel):
        self._release = rel
        if rel and rel["newer"]:
            self.flash(f"{APP_NAME} {rel['version']} is out: Settings › Update Release", 8000)
        elif self._report_update:
            self.flash(f"{APP_NAME} {APP_VERSION} is the latest release" if rel else
                       "Couldn't reach GitHub to check for updates")

    def _update_release(self):
        """Download only the files that changed in the latest release, put them in place, restart."""
        rel = self._release
        why = can_self_update()
        if why == "packaged":  # a packaged app can't swap one file: the release page has the new build
            QDesktopServices.openUrl(QUrl(rel["url"]))
            return
        if why == "git":
            self.flash("This copy is a git working copy: update it with git pull", 8000)
            return
        if not self._ask("Update Release", f"Update {APP_NAME} to {gold(rel['version'])}?",
                         f"Only the files that changed are downloaded; {html.escape(APP_NAME)} then restarts.",
                         "Update", "Not now"):
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            changed = update_files(rel["tag"])
            pip_ok = "requirements.txt" not in changed or install_requirements()
        except Exception as e:
            self.flash(f"Update failed, nothing changed: {e}", 10000)
            return
        finally:
            QApplication.restoreOverrideCursor()
        if not pip_ok:
            self.flash("Updated, but a new package didn't install: run pip install -r requirements.txt", 15000)
            return
        self._restart()

    def _restart(self):
        """Start this app again (it waits for this one to close) and quit."""
        with contextlib.suppress(Exception):
            self.hotkeys.stop()
            save_config(self.cfg, self.demo)
            if self.server:
                self.server.close()
        kw = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WIN
              else {"start_new_session": True})
        subprocess.Popen(_launch_cmd("--restarted", *(["--demo"] if self.demo else [])), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        os._exit(0)

    def _set_script_shortcut(self, on: bool):
        self.cfg["script_shortcut"] = on
        self.save()
        if self.demo:
            return
        try:
            set_script_shortcut(on)
            self.flash(f"Workspace › Scripts › _{APP_NAME} added in Resolve" if on
                       else f"_{APP_NAME} removed from Resolve's Scripts menu")
        except Exception as e:
            self.flash(f"Couldn't change the Script Shortcut: {e}")

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

    def _set_focus_only(self, on: bool):
        self.cfg["focus_only"] = on
        self.hotkeys.set_focus_only(on)
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
        self.conn_rest.setText("")
        self._has_clip = False
        self.all_clips_box.hide()
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
        if self.standby:  # come back by itself the next time Resolve starts
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
        (in standby it starts hidden, hotkeys only)."""
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

    def open_main(self):
        if self._parked:
            return
        self._main_wanted = True
        if not self.isVisible():
            self.show_without_flash()
        self.raise_()
        self.activateWindow()
        self.refresh_states()

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
            # the saved layer count (it changes on Save only), every time: Resolve may apply it after its window closes
            saved_layers = _safe(proj.GetSetting, "nodeStackLayers") if proj else None
            pname, tname = (proj.GetName() if proj else None), (tl.GetName() if tl else None)
            sig = (
                saved_layers,
                pname,
                tname,
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
        try:  # Resolve's Project Settings › Node Stack Layers, as saved, is the reference for the rows
            layers = int(saved_layers or 0)
        except (TypeError, ValueError):
            layers = 0
        layers = layers or clip_layers or 1
        parts = [v] if (v := self.bridge.version()) else []
        if not proj:
            parts.append("no project open")
        elif not tl:
            parts.append("no timeline open")
        rest = ""
        if item:
            gname = _safe(grp.GetName) if grp else None
            parts.append(item.GetName())  # last, so a shortened line still ends with the clip name's end
            rest = f"({gname})" if gname else ""
        elif tl:
            parts.append("no clip under playhead")
        self._has_clip = bool(item)
        self.conn_label.setText("  ·  ".join(parts))
        self.conn_rest.setText(rest)
        self._sync_all_clips_box()
        self.conn_label.setToolTip(f"{pname or ''}  /  {tname or ''}")

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
        self.refresh_states()  # the pills' mismatches follow this clip's nodes and labels
        if self._label_clashes() != self.tags_field.clash:
            self._refresh_tags()

    def _demo_next_clip(self):
        self.bridge.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline().advance()
        self.refresh_context()

    # ---------------------------------------------------------------- actions
    def _recording(self) -> bool:
        btns = self.hotkey_btns + [p.hk for p in self.pills.values()] + [c.hk for c in self.tags_field.chips]
        return any(b.recording or time.monotonic() - b.stopped_at < 0.4 for b in btns)

    def on_hotkey(self, key):
        if self._recording():
            return  # this key press was the one being recorded
        if QApplication.activeWindow() is self and isinstance(QApplication.focusWidget(), QLineEdit):
            return  # typing a scene name or a tag
        if isinstance(key, int):
            self.run_toggle(key)
        elif key.startswith("label:"):
            tag = next((t for t in self._visible_tags() if label_key(t) == key), None)
            if tag is None:
                return
            if tag["on"] is None:
                self.flash(f"“{tag['name']}” is on HOLD — pick ON or OFF to use its shortcut")
            else:
                self.run_label(tag)  # ON → OFF, OFF → ON
        else:
            self.run_layer(key)

    def _sync_all_clips_box(self):
        self.all_clips_box.setVisible(self.chk_all.isChecked() and self._has_clip)

    def _all_clips_toggled(self, on: bool):
        """Ticking / unticking "Apply to all clips" re-applies every ON / OFF scene in the new scope, as another
        click on its state; scenes on HOLD are left alone."""
        self._sync_all_clips_box()
        done = [t["name"] for i, t in enumerate(self.cfg["toggles"])
                if t["on"] is not None and has_selection(t) and self.run_toggle(i, t["on"]) is not False]
        if done:
            where = "every enabled clip" if on else "this clip"
            self.flash(f"Apply to all clips {'on' if on else 'off'}: {', '.join(done)} re-applied on {where}")

    def _sweep(self, title: str, word: str, one_clip: bool, per_item, per_tl):
        """Run per_item over the clip under the playhead (one_clip, or "Apply to all clips" unticked) or every
        enabled clip, then per_tl once. Returns (nodes changed, where) or None on failure."""
        one_clip = one_clip or not self.chk_all.isChecked()
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
        if state is None and t["on"] is None:  # HOLD: the shortcut leaves the nodes alone
            self.flash(f"{t['name']} is on HOLD — pick ON or OFF to use it")
            return
        new_state = (t["on"] is False) if state is None else state  # ON → OFF, OFF → ON
        word = "ON" if new_state else "OFF"
        before = self._snapshot(lambda slot: t["sel"].get(slot)) if t["on"] is None else None  # for HOLD
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
            return False
        if t["on"] is None:
            t["before"] = before
        t["on"] = new_state
        self.save()
        self.refresh_states()
        self._refresh_tags()  # Node Labels turn red if they now disagree with this scene
        self._report(t["name"], word, res)
        return True

    def _layer_key(self, key: str) -> dict:
        """A layer key's settings, made on first use: no shortcut, HOLD, every clip."""
        return self.cfg["layer_keys"].setdefault(key, {"hotkey": None, "on": None})

    def _pick_layer(self, key: str, state):
        """A layer key's dot or name: ON / OFF switch the whole layer over the scenes; HOLD puts it back the way
        the scenes and node labels have it (its shortcut then does nothing)."""
        if state is not None:
            self.run_layer(key, state)
        elif self._layer_key(key)["on"] is not None:
            self.run_layer(key, restore=True)

    def run_layer(self, key: str, state: bool | None = None, restore: bool = False):
        """ON / OFF: every node on the layer, over the scenes and node labels; leaving HOLD first remembers how the
        layer really is on the clip under the playhead (read from Resolve). restore: HOLD — that clip back exactly
        as remembered; elsewhere (other clips, pre- / post-group) every node as the scenes and node labels have
        it: off when picked in an OFF scene or labelled with an OFF node label, else on."""
        if not SLOT_RE.match(key):
            return
        lk = self._layer_key(key)
        title = self._layer_key_title(key)
        if state is None and lk["on"] is None and not restore:  # HOLD: the shortcut leaves the nodes alone
            self.flash(f"{title} is on HOLD — pick ON or OFF to use it")
            return
        new_state = None if restore else (not lk["on"]) if state is None else state
        word = "HOLD" if restore else "ON" if new_state else "OFF"
        if lk["on"] is None and not restore:  # leaving HOLD: remember how the layer is, for HOLD to put back
            lk["before"] = self._layer_snapshot(key)
        before = lk.get("before") or {}
        stay_off = self._keep_off(key) if restore else None
        ALL = [{"all": True}]
        b = self.bridge

        def act(graph, item_id=None) -> int:
            if not restore:
                return b.apply(graph, ALL, new_state)
            if graph is not None and item_id and item_id == before.get("clip"):  # as it really was
                return sum(1 for i, on in before["nodes"].items() if graph.SetNodeEnabled(int(i), on))
            n = b.apply(graph, ALL, True, stay_off)  # elsewhere: on, but for the nodes that should stay off…
            if stay_off:
                n += b.apply(graph, ALL, False, lambda node: not stay_off(node))  # …which go off
            return n

        per_item = per_tl = None
        if key == "timeline":
            per_tl = lambda tl: act(_safe(tl.GetNodeGraph), "timeline")
        elif key in ("preclip", "postclip"):
            getter = "GetPreClipNodeGraph" if key == "preclip" else "GetPostClipNodeGraph"

            def per_item(it, g, proj):
                grp = b.group_once(it, g)
                return act(_safe(getattr(grp, getter))) if grp else 0
        else:
            layer = slot_layer(key)
            per_item = lambda it, g, proj: act(_safe(it.GetNodeGraph, layer), _safe(it.GetUniqueId))
        res = self._sweep(title, word, False, per_item, per_tl)
        if res is None:
            return
        if not res[0]:  # Resolve switched nothing (no nodes, or a dialog open in Resolve): keep state and memory
            self.flash(f"{title}: Resolve didn't switch any node — close any open dialog in Resolve and try again")
            return
        lk["on"] = new_state
        if restore:
            lk.pop("before", None)
        self.save()
        self.refresh_states()
        self._refresh_tags()  # the scene cells' red dots follow the layer
        if restore:
            n, where = res
            how = "how it was before" if before else "the scenes' and node labels' states"
            self.flash(f"{title} → HOLD: back to {how} ({n} node{'s' if n != 1 else ''}{' ' + where if where else ''})")
        else:
            self._report(title, word, res)

    def run_label(self, tag: dict, state: bool | None = None):
        """Switch every node with this label ON / OFF: all layers, groups and the timeline, this clip or every one.
        state None = flip (HOLD → OFF)."""
        new_state = (tag["on"] is False) if state is None else state
        word = "ON" if new_state else "OFF"
        before = self._label_snapshot(tag) if tag["on"] is None else None  # leaving HOLD: for HOLD to put back
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
            if tag["on"] is None:
                tag["before"] = before
            tag["on"] = new_state
            self.save()
            self._refresh_tags()
            self.refresh_states()  # the Layer State pills it pairs with
            self.flash(f"“{tag['name']}” → {word}  ({n} node{'s' if n != 1 else ''} {where})")
        else:  # nothing matched: leave the label's state alone
            self.flash(f"No nodes labelled “{tag['name']}” found on enabled clips — check the spelling in Resolve")

    def _snapshot(self, picks) -> dict | None:
        """Real on/off of the nodes picks(slot) chooses on the clip under the playhead (its layers and the timeline;
        pre- / post-group can't be read): {"clip": its id, "nodes": {slot: {number: on}}}. None if unreadable."""
        if self.bridge.resolve is None:
            return None
        try:
            proj, tl, item = self.bridge.context()
            states = self.bridge.graph_states(proj, tl) if proj and tl and item else None
            clip = _safe(item.GetUniqueId) if item else None
        except Exception:
            return None
        nodes = {}
        for slot, st in (states or {}).items():
            sel = picks(slot)
            chosen = set(match_nodes(self._nodes.get(slot) or [], sel)) if sel else set()
            hit = {str(i): on for i, on in st.items() if i in chosen}
            if hit:
                nodes[slot] = hit
        return {"clip": clip, "nodes": nodes} if nodes and clip else None

    def _restore(self, before: dict | None, what: str) -> int:
        """Put a _snapshot back, on the clip it was read from (elsewhere nothing changes). Returns nodes set."""
        n = 0
        if before and self.bridge.resolve is not None:
            try:
                proj, tl, item = self.bridge.context()
                if item and _safe(item.GetUniqueId) == before["clip"]:
                    for slot, st in before["nodes"].items():
                        g = _safe(tl.GetNodeGraph) if slot == "timeline" else _safe(item.GetNodeGraph, slot_layer(slot))
                        if g is not None:
                            n += sum(1 for i, on in st.items() if g.SetNodeEnabled(int(i), on))
            except Exception as e:
                print(f"Couldn't restore {what}: {e}", file=sys.stderr)
        return n

    def _label_snapshot(self, tag: dict) -> dict | None:
        return self._snapshot(lambda slot: [{"label": tag["name"], "ci": True}])

    def hold_label(self, tag: dict):
        """Node Label back to HOLD: on the clip it was read from, its nodes go back to how they were before ON / OFF;
        elsewhere they're left as they are."""
        before = tag.pop("before", None)
        n = self._restore(before, f"“{tag['name']}”")
        tag["on"] = None
        self.save()
        self._refresh_tags()
        self.refresh_states()
        if n:
            self.flash(f"“{tag['name']}” → HOLD: back to how it was before ({n} node{'s' if n != 1 else ''})")
        elif before:
            self.flash(f"“{tag['name']}” → HOLD: its nodes are left as they are (another clip, or Resolve didn't "
                       f"switch them)")

    def _scenes(self) -> list[dict]:
        """The scenes as a preset holds them: names, keys, node picks; not their ON/OFF."""
        return [clean_toggle(i, t) for i, t in enumerate(self.cfg["toggles"])]

    def _matches_preset(self, slot: str) -> bool:
        data = self.cfg["presets"].get(slot)
        return bool(data) and self._scenes() == \
            [clean_toggle(i, data[i] if i < len(data) else {}) for i in range(NUM_TOGGLES)]

    def _pick_preset(self, slot: str):
        """Select slot for Save / Load: its number is blue while the scenes match what it holds, dark (FIELD)
        once anything differs; the other numbers stay grey."""
        self.preset_slot = slot
        match = self._matches_preset(slot)
        for b in self.seg_btns:
            chosen = b.text() == slot
            b.setChecked(match and chosen)
            b.setProperty("sel", chosen)
            repolish(b)
        has = bool(self.cfg["presets"].get(slot))
        self.btn_load.setEnabled(has)
        self.btn_load.setToolTip(f"Load preset {slot}" if has else f"Preset {slot} is empty")

    def _sync_preset(self):
        if not self.seg_btns:
            return
        scenes = self._scenes()
        if scenes != self._scenes_seen:  # a scene firing (ON / OFF) changes nothing here
            self._scenes_seen = scenes
            self._pick_preset(self.preset_slot)

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

    def closeEvent(self, e):  # the window's X quits Fuse Box (minimise it to keep the hotkeys out of the way)
        self.hotkeys.stop()
        self.save()
        super().closeEvent(e)
        QApplication.quit()


def colorref(colour: str) -> int:
    """A Windows COLORREF (0x00BBGGRR) for a "#rrggbb" colour."""
    c = QColor(colour)
    return c.red() | c.green() << 8 | c.blue() << 16


def gold(t: str) -> str:
    """Rich text for an alert: a key, a command or a name, in the alerts' gold."""
    return f'<span style="color:{ALERT_GOLD}">{html.escape(t)}</span>'


def alert_title_bar(w: QWidget) -> None:
    """An alert's title bar: no icon (or system menu), WTNCA_NODRAWICON | WTNCA_NOSYSMENU, and ALERT_TITLE /
    ALERT_TITLE_TEXT as its colours (Windows 11; Windows 10 keeps its own)."""
    if IS_WIN:
        import ctypes
        hwnd = int(w.winId())
        flags = (ctypes.c_uint * 2)(0x6, 0x6)  # (flags, mask)
        ctypes.windll.uxtheme.SetWindowThemeAttribute(hwnd, 1, flags, ctypes.sizeof(flags))  # WTA_NONCLIENT
        for attr, colour in ((35, ALERT_TITLE), (36, ALERT_TITLE_TEXT)):  # DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_uint(colorref(colour))), 4)


def load_fonts():
    """Register the bundled fonts (fonts/*.ttf) for this run; nothing is installed on the system."""
    for f in sorted(FONTS_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))


class AppStyle(QProxyStyle):
    """Fusion, with tooltips only after resting on an item for 2 s, every time, and rounded see-through menus
    and tooltips."""

    def polish(self, arg):
        if isinstance(arg, QMenu) or (isinstance(arg, QWidget) and arg.inherits("QTipLabel")):  # see-through
            # corners, so the style sheet's border-radius shows
            arg.setAttribute(Qt.WA_TranslucentBackground)
            arg.setWindowFlag(Qt.FramelessWindowHint)
            arg.setWindowFlag(Qt.NoDropShadowWindowHint)
        return super().polish(arg)

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return 2000
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:  # else, after one tooltip, the next ones pop up at once
            return 0
        return super().styleHint(hint, option, widget, returnData)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="use a fake Resolve to try the UI")
    ap.add_argument("--standby", action="store_true", help="stay hidden until Resolve runs")
    ap.add_argument("--after", type=int, help=argparse.SUPPRESS)  # pid of a Resolve that is shutting down
    ap.add_argument("--hotkeys", type=int, help=argparse.SUPPRESS)  # run the hotkey process for this app pid
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)  # after an update: wait for the old one
    args = ap.parse_args()
    if args.hotkeys is not None:
        return hotkey_process(args.hotkeys)
    if args.demo:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle(AppStyle("Fusion"))
    pal = app.palette()  # dark from the very first paint, before the stylesheet kicks in
    for role in (QPalette.Window, QPalette.Base, QPalette.Button):
        pal.setColor(role, QColor(BG))
    app.setPalette(pal)
    load_fonts()
    app.setStyleSheet(STYLE)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = f"{AUTOSTART_NAME}-{os.environ.get('USERNAME') or os.environ.get('USER') or 'user'}" + ("-demo" if args.demo else "")
    sock = QLocalSocket()
    sock.connectToServer(name)
    for _ in range(40 if args.restarted else 0):  # after an update: the old copy is still closing, wait for it
        if not sock.waitForConnected(300):
            break
        sock.abort()
        time.sleep(0.25)
        sock.connectToServer(name)
    if sock.waitForConnected(300):  # already running: bring that window forward instead of opening another
        sock.write(b"show")
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(name)
    app.setQuitOnLastWindowClosed(False)  # standby and Resolve's project switches hide the window: keep running
    w = MainWindow(args.demo, standby=args.standby, ignore_pid=args.after)
    w.server = QLocalServer()
    w.server.listen(name)
    w.server.newConnection.connect(lambda: (w.server.nextPendingConnection(), w.bring_to_front()))
    if not w._parked:
        w.show_without_flash()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
