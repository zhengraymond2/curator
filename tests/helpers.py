from __future__ import annotations

import uuid
from pathlib import Path


TEST_ROOT = Path(__file__).resolve().parents[1] / "test" / "runtime"


def unique_case_dir(name: str) -> Path:
    case_dir = TEST_ROOT / f"{name}-{uuid.uuid4().hex[:8]}"
    case_dir.mkdir(parents=True, exist_ok=False)
    return case_dir

