"""Layer B runner: discover and run the Blender-side tests under ``blender -b``.

    scripts/test-blender

``STAGELOOM_TEST_PATTERN=test_stage_index.py`` discovers one module, which is
how a standalone module is checked. ``STAGELOOM_TEST_TIMEOUT`` overrides the
per-test cap in seconds (default 120). Exits non-zero when a test fails or
exceeds the cap, so CI and the shell both see it.
"""

import os
import signal
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
# One owner for the test import path (W0a). REPO/blender makes `import
# stageloom` resolve to the working tree, including before `addon_enable`;
# REPO makes the `blender.stageloom` imports work; VENDOR supplies websockets
# in the tree. The shipped add-on never manipulates sys.path (Add-on
# Guidelines, G2): this is test infrastructure.
for _path in (
    os.path.join(REPO, "blender", "stageloom", "vendor"),
    os.path.join(REPO, "blender"),
    REPO,
):
    if _path not in sys.path:
        sys.path.insert(0, _path)

DEFAULT_TIMEOUT = 120.0
# A test stuck in C code cannot be interrupted by a Python signal. The
# watchdog waits one extra second for the SIGALRM path to record its error,
# then reports the test id and terminates the process.
WATCHDOG_GRACE = 1.0


def _timeout_from_environment():
    raw = os.environ.get("STAGELOOM_TEST_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        seconds = float(raw)
    except ValueError:
        raise SystemExit(
            "STAGELOOM_TEST_TIMEOUT must be a number of seconds, got %r" % raw
        )
    if not 0.0 < seconds < float("inf"):
        raise SystemExit(
            "STAGELOOM_TEST_TIMEOUT must be finite and positive, got %r" % raw
        )
    return seconds


class TestTimeout(TimeoutError):
    """The per-test alarm fired.

    A TimeoutError subclass so a test or fixture that catches TimeoutError sees
    the familiar type, while the result can tell this apart from a real socket
    timeout and print the runner's message.
    """


class TestDeadline:
    """One owner for the per-test deadline.

    SIGALRM raises TestTimeout inside the running test, so unittest records an
    error and the rest of the suite still runs. A daemon thread is the backstop
    for a test stuck in C code, where the handler cannot run: it prints the test
    id and exits non-zero. That backstop is also the whole mechanism on Windows,
    which has no SIGALRM; the process still fails within the cap plus grace.
    """

    def __init__(self, seconds):
        self.seconds = seconds
        self._test_id = None
        self._deadline = None
        self._stop = False
        self._condition = threading.Condition()
        self._watchdog = None
        self._use_alarm = hasattr(signal, "SIGALRM") and hasattr(
            signal, "setitimer"
        )
        if self._use_alarm:
            signal.signal(signal.SIGALRM, self._raise_timeout)

    def arm(self, test_id):
        with self._condition:
            self._test_id = test_id
            self._deadline = time.monotonic() + self.seconds + WATCHDOG_GRACE
            if self._watchdog is None:
                self._watchdog = threading.Thread(
                    target=self._watch,
                    name="stageloom-test-deadline",
                    daemon=True,
                )
                self._watchdog.start()
            else:
                self._condition.notify()
        if self._use_alarm:
            signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def disarm(self):
        if self._use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
        with self._condition:
            self._test_id = None
            self._deadline = None

    def close(self):
        if self._use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
        with self._condition:
            self._stop = True
            self._deadline = None
            self._condition.notify()
        if self._watchdog is not None:
            self._watchdog.join(timeout=1.0)

    def _raise_timeout(self, _signum, _frame):
        raise TestTimeout(
            "%s exceeded the %.3gs per-test cap (STAGELOOM_TEST_TIMEOUT)"
            % (self._test_id or "the running test", self.seconds)
        )

    def _watch(self):
        while True:
            with self._condition:
                while not self._stop and self._deadline is None:
                    self._condition.wait()
                if self._stop:
                    return
                remaining = self._deadline - time.monotonic()
                if remaining > 0.0:
                    self._condition.wait(remaining)
                    continue
                test_id = self._test_id
                self._deadline = None
            self._hard_exit(test_id)

    def _hard_exit(self, test_id):
        # Bypass buffered stderr: another thread may hold its lock, and the
        # point is to terminate even when normal reporting is stuck. A GUI
        # Blender on Windows may have no usable fd 2; the exit code still wins.
        try:
            os.write(
                2,
                (
                    "TIMEOUT: %s exceeded the %.3gs per-test cap and did not "
                    "return to Python (hard watchdog)\n" % (test_id, self.seconds)
                ).encode("utf-8", "replace"),
            )
        except OSError:
            pass
        os._exit(1)


DEADLINE = TestDeadline(_timeout_from_environment())


class _TimeoutResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        DEADLINE.arm(test.id())

    def stopTest(self, test):
        DEADLINE.disarm()
        super().stopTest(test)

    def addError(self, test, err):
        if isinstance(err[1], TestTimeout):
            # The failure is recorded; stop the backstop before printing so a
            # slow stream cannot turn a reported timeout into a hard exit.
            DEADLINE.disarm()
            self.stream.writeln(
                "TIMEOUT: %s exceeded the %.3gs per-test cap "
                "(STAGELOOM_TEST_TIMEOUT)" % (test.id(), DEADLINE.seconds)
            )
            self.stream.flush()
        super().addError(test, err)


class _TimeoutSuite(unittest.TestSuite):
    """Arm the same deadline for class fixtures.

    unittest runs setUpClass before TestCase.run calls result.startTest, and the
    fixtures are where a server start or a scene reset can block. The private
    hooks are the only place unittest offers around them.
    """

    def _handleClassSetUp(self, test, result):
        DEADLINE.arm("setUpClass (%s)" % test.id())
        try:
            return super()._handleClassSetUp(test, result)
        finally:
            DEADLINE.disarm()

    def _tearDownPreviousClass(self, test, result):
        previous = getattr(result, "_previousTestClass", None)
        if previous is None or (test is not None and test.__class__ is previous):
            return super()._tearDownPreviousClass(test, result)
        DEADLINE.arm("tearDownClass (%s)" % unittest.util.strclass(previous))
        try:
            return super()._tearDownPreviousClass(test, result)
        finally:
            DEADLINE.disarm()


class _TimeoutLoader(unittest.TestLoader):
    suiteClass = _TimeoutSuite


def main():
    pattern = os.environ.get("STAGELOOM_TEST_PATTERN", "test_*.py")
    try:
        suite = _TimeoutLoader().discover(
            HERE, pattern=pattern, top_level_dir=HERE
        )
        result = unittest.TextTestRunner(
            verbosity=2, resultclass=_TimeoutResult
        ).run(suite)
        return 0 if result.wasSuccessful() else 1
    finally:
        DEADLINE.close()


if __name__ == "__main__":
    sys.exit(main())
