import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))
if ROOT_DIR.exists():
    sys.path.insert(0, str(ROOT_DIR))
