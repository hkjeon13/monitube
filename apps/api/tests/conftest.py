"""Test-only import path for the adjacent worker scaffold in this monorepo."""

from __future__ import annotations

import sys
from pathlib import Path


WORKER_ROOT = Path(__file__).resolve().parents[2] / "worker"
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))
