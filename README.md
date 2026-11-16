# Fuse Box

Switch DaVinci Resolve color nodes on and off with your own keyboard shortcuts.

Fuse Box sits next to Resolve. You pick nodes into up to four **scenes** (for example "No grain", "Skin pass", "Day for night"), give each scene a key, and press it to switch those nodes ON or OFF without hunting through the node tree. It's for comparing looks quickly in a grading session, or for showing a client A/B versions.

## Install

Download the latest build from [Releases](../../releases):

- **macOS** (Apple silicon): open `FuseBox-…-macos-arm64.dmg` and drag **Fuse Box** onto **Applications**. The app isn't signed, so the first time, right-click it in Applications and choose **Open**. For global shortcuts, allow Fuse Box under System Settings › Privacy & Security › **Accessibility** and **Input Monitoring**.
- **Windows**: run `FuseBox-…-windows-x64.exe`. SmartScreen may warn about an unknown publisher: **More info › Run anyway**.
- **Linux** (x64): `tar -xzf FuseBox-…-linux-x64.tar.gz`, then `./fusebox`. Shortcuts need an X11 session.

In Resolve, set **Preferences › System › General › External scripting using** to **Local**. Resolve Studio 19.1 or later is needed; the free version doesn't allow external scripting.

### From source

Python 3.10+ (64-bit):

```bash
pip install -r requirements.txt
python fuse_box.py
```

`python fuse_box.py --demo` opens it with a pretend Resolve, to try the interface without Resolve running. `--standby` waits in the background and opens when Resolve starts.

## Use

- **Scenes**: each column is a scene. Click a cell to pick nodes from Pre-Group, each clip layer (L1, L2 …), Post-Group or Timeline, or a whole layer at once. Click and drag over the list to tick several nodes.
- **State**: a scene is HOLD (grey, left alone), ON or OFF. Click a dot to set it; after that its shortcut flips between ON and OFF. On HOLD the shortcut does nothing. Going back to HOLD puts the scene's nodes back the way they were before ON or OFF (on the clip under the playhead; elsewhere they're left as they are).
- **Layer State**: ON or OFF switches a whole layer or group, over what the scenes set; each has its own shortcut. HOLD puts the layer back the way it was before ON or OFF (read from Resolve for the clip under the playhead; on other clips and Pre-/Post-Group, the way the scenes and node labels have it). A layer turns red when a scene sets nodes on it the other way.
- **Node Label**: type a node label you use in Resolve (e.g. `NR`). Its dots, or its own shortcut, switch every node with that label, on every layer. HOLD puts those nodes back the way they were before ON or OFF (on the clip under the playhead; elsewhere they're left as they are).
- **Apply to all clips in the Timeline**: unticked, everything acts on the clip under the playhead; ticked, on every enabled clip. Right-click a scene key for **This clip only**.
- **Presets 1–3**: Save and Load sets of scenes. The selected number is blue while the scenes match that preset.
- **Undo / Redo** (8 steps) for node picks, **Refresh** to re-read the node tree, **Clean Plate** to reset all scenes.
- **Settings** (bottom right): at every start Fuse Box checks GitHub for a newer release. **Update Release** (yellow dot) downloads only the files that changed and restarts; with the packaged app it opens the release page instead. **Running Latest Release** (green dot) means you're up to date. **Script Shortcut** adds **_Fuse Box** to Resolve's Workspace › Scripts menu; **Keys only in Resolve** (Windows) makes the shortcuts work only while Resolve or Fuse Box is in front. Both are on by default.

Fuse Box warns when a key you assign is already a Resolve shortcut (it reads your active custom keyboard preset).

## Known limits

- Resolve's scripting can switch a node on or off but can't report whether it is on. Fuse Box remembers what it set, so if you flip nodes by hand in Resolve, click the scene's dot to bring it back in step.
- Resolve's scripting doesn't expose node selection, so nodes are picked in Fuse Box, not in Resolve's node editor.
- Layer names are read from Resolve's Project Settings window (Windows only), and are applied when that window closes.

## Credits

Inter font by The Inter Project Authors, under the SIL Open Font License 1.1 (`fonts/OFL-Inter.txt`).
