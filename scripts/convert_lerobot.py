# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "av==15.1.0",
#   "h5py==3.16.0",
#   "numpy==2.2.6",
#   "pandas==2.3.3",
#   "pyarrow==25.0.0",
# ]
# ///
"""一键离线转换：uv run --script scripts/convert_lerobot.py 来源 --output 新目录。"""
import sys
from pathlib import Path

# Allow the script environment to import only our lightweight conversion modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from yam_abc_reproduce.hil.batch_convert import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
