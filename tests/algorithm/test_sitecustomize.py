"""Exercise the real interpreter startup without loading application dependencies."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SiteCustomizeTest(unittest.TestCase):
    def test_tracker_skips_business_imports_but_application_keeps_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parents[2] / "algorithm" / "sitecustomize.py"
            (root / "sitecustomize.py").write_bytes(source.read_bytes())
            package = root / "lazymind" / "common" / "database"
            package.mkdir(parents=True)
            for folder in [package, package.parent, package.parent.parent]:
                (folder / "__init__.py").touch()
            (package / "sqlite_proxy.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "Path(os.environ['HOOK_MARKER']).write_text('imported')\n"
                "def install_lazyllm_sqlite_proxy():\n"
                "    Path(os.environ['HOOK_MARKER']).write_text('installed')\n"
            )
            marker = root / "hook-marker"
            env = {**os.environ, "PYTHONPATH": str(root), "HOOK_MARKER": str(marker),
                   "LAZYMIND_DATABASE_URL": "sqliteproxy://test"}
            tracker_script = (
                "from pathlib import Path\n"
                "import os\n"
                "from multiprocessing import resource_tracker\n"
                "Path(os.environ['HOOK_MARKER']).unlink()\n"
                "resource_tracker._resource_tracker.ensure_running()\n"
                "resource_tracker._resource_tracker._stop()\n"
            )
            tracker = subprocess.run(
                [sys.executable, "-B", "-c", tracker_script],
                env=env, cwd=root, capture_output=True, timeout=10,
            )
            self.assertEqual(tracker.returncode, 0, tracker.stderr.decode())
            self.assertFalse(marker.exists(), "tracker imported business code")
            application = subprocess.run(
                [sys.executable, "-B", "-c", "pass"], env=env, cwd=root,
                capture_output=True, timeout=10,
            )
            self.assertEqual(application.returncode, 0, application.stderr.decode())
            self.assertEqual(marker.read_text(), "installed")


if __name__ == "__main__":
    unittest.main()
