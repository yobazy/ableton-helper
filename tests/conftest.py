import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# lom.py is imported directly: the Remote Script package's __init__ needs Live's _Framework.
sys.path.insert(0, str(ROOT / "remote_script" / "AbletonHelper"))
sys.path.insert(0, str(Path(__file__).parent))
