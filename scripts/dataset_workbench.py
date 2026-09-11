# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "av==15.1.0", "h5py==3.16.0", "numpy==2.2.6",
#   "pandas==2.3.3", "pyarrow==25.0.0", "pillow==12.3.0",
#   "fastapi==0.140.0", "uvicorn==0.51.0",
# ]
# ///
"""Portable workbench entrypoint: no camera SDK, Torch, or robot installation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yam_abc_reproduce.dataset_workbench.web import main

if __name__ == "__main__":
    main()
