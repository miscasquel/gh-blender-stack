import sys
import unittest

from blender.gh_stack.core import PROTOCOL_VERSION


class CoreIsPureTests(unittest.TestCase):
    def test_protocol_version_is_2(self):
        self.assertEqual(PROTOCOL_VERSION, 2)

    def test_core_does_not_import_bpy(self):
        self.assertNotIn("bpy", sys.modules)


if __name__ == "__main__":
    unittest.main()
