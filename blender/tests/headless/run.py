"""Layer B runner: discover and run the Blender-side tests under ``blender -b``.

    scripts/test-blender

Exits non-zero when a test fails, so CI and the shell both see it.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

loader = unittest.TestLoader()
suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
result = unittest.TextTestRunner(verbosity=2).run(suite)

if not result.wasSuccessful():
    sys.exit(1)
