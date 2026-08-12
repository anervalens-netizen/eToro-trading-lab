from __future__ import annotations

import os
import sys
import unittest

from etoro_agent.bounded_subprocess_v2 import SubprocessOutputLimitError, run_bounded


class BoundedSubprocessV2Tests(unittest.TestCase):
    def test_output_is_capped_and_process_group_is_terminated(self) -> None:
        with self.assertRaises(SubprocessOutputLimitError):
            run_bounded(
                (sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"),
                input_text=None,
                timeout=5,
                max_output_bytes=1024,
                env=os.environ,
            )

    def test_small_fixed_argv_command_returns_bounded_output(self) -> None:
        result = run_bounded(
            (sys.executable, "-c", "print('ok')"),
            input_text=None,
            timeout=5,
            max_output_bytes=1024,
            env=os.environ,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
