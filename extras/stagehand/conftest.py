import sys
from pathlib import Path

# The extra is imported by filename when installed into the overlay's tools/
# dir; mirror that here so `import stagehand_browser` resolves in tests.
sys.path.insert(0, str(Path(__file__).parent))
