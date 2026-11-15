"""Layer B: the runner's per-test deadline fails loudly instead of hanging.

The sleeping module runs in a child interpreter, through the runner's real
entry point and discovery: doing it here would stall the suite whenever the
deadline mechanism regresses. Each child gets a 0.05 s cap and a throwaway test
module; this process only checks its exit status and output.
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CAP = 0.05
CHILD_LIMIT = 20

CHILD = """
import sys

sys.path.insert(0, {headless!r})
import run

run.HERE = {case_dir!r}
sys.exit(run.main())
"""

HANGING_TEST = """
import time
import unittest

class Hung(unittest.TestCase):
    def test_hangs(self):
        time.sleep(10)
"""

HANGING_FIXTURE = """
import time
import unittest

class Hung(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(10)

    def test_never_runs(self):
        pass
"""

IGNORED_SIGNAL = """
import signal
import time
import unittest

class Hung(unittest.TestCase):
    def test_blocks_the_signal(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        time.sleep(10)
"""


class RunnerTimeoutTests(unittest.TestCase):
    def run_module(self, source):
        with tempfile.TemporaryDirectory(prefix="stageloom-runner-") as case_dir:
            module = os.path.join(case_dir, "test_hang.py")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent(source))
            env = dict(os.environ)
            env["STAGELOOM_TEST_TIMEOUT"] = str(CAP)
            env["STAGELOOM_TEST_PATTERN"] = "test_*.py"
            child = CHILD.format(headless=HERE, case_dir=case_dir)
            return subprocess.run(
                [sys.executable, "-c", child],
                capture_output=True,
                text=True,
                timeout=CHILD_LIMIT,
                env=env,
            )

    def assert_timed_out(self, completed, *needles):
        output = completed.stdout + completed.stderr
        self.assertNotEqual(completed.returncode, 0, output)
        self.assertIn("TIMEOUT", output, output)
        for needle in needles:
            self.assertIn(needle, output, output)

    def test_a_hung_test_fails_with_its_id(self):
        self.assert_timed_out(
            self.run_module(HANGING_TEST), "test_hang.Hung.test_hangs"
        )

    def test_a_hung_class_fixture_fails_with_its_id(self):
        self.assert_timed_out(
            self.run_module(HANGING_FIXTURE), "setUpClass (test_hang.Hung)"
        )

    @unittest.skipUnless(
        hasattr(signal, "pthread_sigmask") and hasattr(signal, "SIGALRM"),
        "the hard watchdog backstop needs POSIX signals to be blocked",
    )
    def test_the_hard_watchdog_catches_a_test_that_ignores_the_signal(self):
        self.assert_timed_out(
            self.run_module(IGNORED_SIGNAL),
            "test_hang.Hung.test_blocks_the_signal",
            "hard watchdog",
        )
