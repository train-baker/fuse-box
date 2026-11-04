"""
A fake DaVinci Resolve scripting object, used by `fuse_box.py --demo`.

It mimics just the parts of the Resolve API that Fuse Box calls, so the UI
can be tried without Resolve running. Node enable/disable calls are printed
to the console.
"""


class _Graph:
    def __init__(self, owner, nodes):
        self.owner = owner
        self.nodes = [{"label": label, "tools": tools, "enabled": True} for label, tools in nodes]

    def GetNumNodes(self):
        return len(self.nodes)

    def GetNodeLabel(self, i):
        return self.nodes[i - 1]["label"]

    def GetToolsInNode(self, i):
        return list(self.nodes[i - 1]["tools"])

    def SetNodeEnabled(self, i, on):
        if not 1 <= i <= len(self.nodes):
            return False
        self.nodes[i - 1]["enabled"] = bool(on)
        label = self.nodes[i - 1]["label"] or "(no label)"
        print(f"[mock] {self.owner}: node {i} {label!r} -> {'ON' if on else 'OFF'}")
        return True


class _Group:
    def __init__(self, name, pre, post):
        self.name = name
        self.pre = _Graph(f"group {name} / pre-clip", pre)
        self.post = _Graph(f"group {name} / post-clip", post)

    def GetName(self):
        return self.name

    def GetPreClipNodeGraph(self):
        return self.pre

    def GetPostClipNodeGraph(self):
        return self.post


class _Item:
    _next = 1000

    def __init__(self, name, nodes, group=None, extra_layers=()):
        _Item._next += 1
        self.uid = str(_Item._next)
        self.name = name
        self.layers = [_Graph(f"clip {name} L1", nodes)]
        for n, layer_nodes in enumerate(extra_layers, 2):
            self.layers.append(_Graph(f"clip {name} L{n}", layer_nodes))
        self.group = group

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetNodeGraph(self, layer=1):
        return self.layers[layer - 1] if 1 <= layer <= len(self.layers) else None

    def GetColorGroup(self):
        return self.group

    def GetClipEnabled(self):
        return True


class _Timeline:
    def __init__(self):
        interview = _Group("Interview", [("CST In", ["Color Space Transform"]), ("Exposure", [])],
                           [("Look", ["Custom Curves"]), ("Skin Tone", ["Qualifier"])])
        broll = _Group("B-Roll", [("CST In", ["Color Space Transform"])], [("Look", [])])
        self.items = [
            _Item("A001_C003_Interview", [("Balance", []), ("Skin", ["Qualifier", "Window"]),
                                          ("Vignette", ["Window"]), ("", ["Blur"])], interview,
                  extra_layers=[[("Grain", ["Film Grain"]), ("Halation", ["Halation"])],
                                [("Glow", ["Glow"]), ("Beauty", ["Beauty"])]]),
            _Item("A001_C007_Interview", [("Balance", []), ("Skin", ["Qualifier"]), ("Vignette", ["Window"])], interview,
                  extra_layers=[[("Grain", ["Film Grain"])]]),
            _Item("B002_C011_Street", [("Balance", []), ("Sky", ["Qualifier"])], broll),
            _Item("B002_C015_Market", [("Balance", []), ("Denoise", ["Noise Reduction"])], None),
        ]
        self.current = 0
        self.graph = _Graph("timeline", [("Halation", ["Halation"]), ("Film Grain", ["Film Grain"]),
                                         ("Output LUT", ["LUT"])])

    def GetName(self):
        return "Edit v3 — Color"

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 0

    def GetItemListInTrack(self, kind, idx):
        return list(self.items)

    def GetCurrentVideoItem(self):
        return self.items[self.current]

    def GetNodeGraph(self):
        return self.graph

    def advance(self):
        self.current = (self.current + 1) % len(self.items)


class _Project:
    def __init__(self):
        self.timeline = _Timeline()

    def GetName(self):
        return "Demo Project"

    def GetUniqueId(self):
        return "demo-project"

    def GetSetting(self, key):
        return "3" if key == "nodeStackLayers" else ""

    def GetCurrentTimeline(self):
        return self.timeline


class _ProjectManager:
    def __init__(self):
        self.project = _Project()

    def GetCurrentProject(self):
        return self.project


class MockResolve:
    def __init__(self):
        self.pm = _ProjectManager()

    def GetVersionString(self):
        return "20.3.2 (mock)"

    def GetProductName(self):
        return "DaVinci Resolve Studio (mock)"

    def GetProjectManager(self):
        return self.pm
