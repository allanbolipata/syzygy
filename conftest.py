import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def step(msg: str) -> None:
    """Print a labeled step notice for test visibility."""
    print(f"\n  [STEP] {msg}")
