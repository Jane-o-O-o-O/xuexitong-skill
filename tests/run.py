"""Run portable skill regressions with all real network traffic disabled."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "xuexitong" / "scripts"))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
    with (
        tempfile.TemporaryDirectory() as directory,
        patch("chaoxing.cli.SESSION_FILE", str(Path(directory) / "session.json")),
        patch("chaoxing.session.SESSION_FILE", str(Path(directory) / "session.json")),
        patch(
            "requests.adapters.HTTPAdapter.send",
            side_effect=AssertionError("Real network is disabled"),
        ),
    ):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
