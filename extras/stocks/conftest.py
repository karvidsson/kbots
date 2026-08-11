"""Put this dir on sys.path so `import stocks` resolves the way _scan_layer
loads installed extras (see extras/shelly/conftest.py for the rationale)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
