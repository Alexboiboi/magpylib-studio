# sandbox

The folder the Extension Development Host opens when you press F5. Nothing in
here is used by the engine or the extension; it exists so the host always
starts in the *same* workspace.

That matters more than it sounds. VS Code gives an extension its storage per
workspace, and the studio keeps the script tab's `scene.py` there. A host
started with no folder open falls back to *global* storage, shared with every
other folder-less window — so the script tab could come up showing the scene
from whatever you were doing last, and two hosts would fight over one file.

Save scenes here while trying things out. They are ordinary `.py` or `.json`
files and nothing collects them.
