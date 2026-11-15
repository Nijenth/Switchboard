"""Run a script, and exit as soon as the parent process closes our stdin.

The harness starts the checkpoint fixture through this wrapper so a hard-killed
harness cannot leave the fixture running: the pipe closes with the parent.
The wrapped script itself is unchanged.
"""

import os
import runpy
import sys
from pathlib import Path
from threading import Thread


def _exit_when_parent_leaves() -> None:
    sys.stdin.buffer.read()
    os._exit(0)


if __name__ == "__main__":
    Thread(target=_exit_when_parent_leaves, daemon=True).start()
    script = Path(sys.argv[1]).resolve()
    # Match running the script directly: its own folder comes first on the import path.
    sys.path[0] = str(script.parent)
    runpy.run_path(str(script), run_name="__main__")
