from __future__ import annotations

import inspect
import subprocess
import sys
import tomllib
import unittest
from importlib import metadata
from pathlib import Path

from etoro_agent import __version__, v2_runtime


class V2ReleaseSurfaceTests(unittest.TestCase):
    def test_package_and_documented_versions_match_release_metadata(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        expected = project["project"]["version"]
        self.assertEqual(__version__, expected)
        self.assertEqual(metadata.version("etoro-demo-agent"), expected)
        self.assertIn(
            f"V2 status — {expected} canonical runtime",
            (root / "V2_STATUS.md").read_text(encoding="utf-8").splitlines()[0],
        )

    def test_installed_distribution_exposes_only_canonical_cli(self) -> None:
        distribution = metadata.distribution("etoro-demo-agent")
        scripts = {
            item.name: item.value
            for item in distribution.entry_points
            if item.group == "console_scripts"
        }
        self.assertEqual(scripts, {"etoro-v2": "etoro_agent.v2_runtime:main"})

    def test_cli_has_no_sqlite_writer_or_state_mutation(self) -> None:
        source = inspect.getsource(v2_runtime)
        for forbidden in (
            "executor-once",
            "executor-worker",
            "set-state",
            "RuntimeStoreV2",
            "etoro-agent",
        ):
            self.assertNotIn(forbidden, source)
        output = subprocess.run(
            [sys.executable, "-m", "etoro_agent.v2_runtime", "--help"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("validate-config", output)
        self.assertIn("release-info", output)
        self.assertNotIn("executor-once", output)
        self.assertNotIn("executor-worker", output)
        self.assertNotIn("set-state", output)

    def test_legacy_runtime_and_units_are_absent_from_current_tree(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = root / "src/etoro_agent"
        forbidden_modules = {
            "agent.py",
            "cli.py",
            "config.py",
            "engine.py",
            "etoro_api_v2.py",
            "execution.py",
            "executor_service_v2.py",
            "market.py",
            "mcp.py",
            "risk.py",
            "strategy.py",
        }
        self.assertFalse(forbidden_modules & {item.name for item in package.glob("*.py")})
        units = root / "ops/systemd"
        forbidden_units = {
            "etoro-backup.service",
            "etoro-backup.timer",
            "etoro-dashboard.service",
            "etoro-demo-executor.service",
            "etoro-news-scanner.service",
            "etoro-shadow.service",
        }
        self.assertFalse(forbidden_units & {item.name for item in units.iterdir()})


if __name__ == "__main__":
    unittest.main()
