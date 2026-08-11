"""Import this extra the way the engine does, not the way Core packages work.

`Registry._scan_layer` inserts an overlay's `tools/` dir into sys.path and
imports each file directly, so once installed the module is `shelly`, not
`src.tools.shelly`. Tests import it under that same name to exercise the real
load path rather than a Core-only one that would pass here and fail live.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
