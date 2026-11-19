"""Tests of the persistent boot-loop guard state (power/guard.py)."""
import json
import os
import tempfile
import unittest

from power.guard import FILE_NAME, Guard


class GuardFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "state")
        self.path = os.path.join(self.dir, FILE_NAME)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, content):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(content)

    def test_missing_file_gives_defaults_without_a_problem(self):
        g = Guard(self.dir)
        self.assertEqual(g.load(), [])
        self.assertEqual((g.count, g.passive, g.last_shutdown), (0, False, None))

    def test_save_creates_the_directory_and_round_trips(self):
        g = Guard(self.dir)
        g.update({"count": 2, "passive": False, "last_shutdown": {"uptime_s": 31.0, "early": True}})
        self.assertEqual(g.save(), "")
        self.assertEqual(os.listdir(self.dir), [FILE_NAME])      # no temp file left behind
        g2 = Guard(self.dir)
        self.assertEqual(g2.load(), [])
        self.assertEqual((g2.count, g2.passive), (2, False))
        self.assertEqual(g2.last_shutdown, {"uptime_s": 31.0, "early": True})

    def test_update_without_last_shutdown_keeps_it(self):
        g = Guard(self.dir)
        g.update({"count": 1, "passive": False, "last_shutdown": {"early": True}})
        g.update({"count": 0, "passive": False})
        self.assertEqual(g.last_shutdown, {"early": True})

    def test_corrupt_files_give_defaults_and_a_problem(self):
        cases = [b"", b"\x00\xff\xfe garbage", b"{", b'{"count": 2, "passive": fal', b"[]", b"null",
                 b"3", b'"text"', b'{"count": 2}', b'{"passive": true}',
                 b'{"count": "2", "passive": false}', b'{"count": -1, "passive": false}',
                 b'{"count": 2.5, "passive": false}', b'{"count": true, "passive": false}',
                 b'{"count": 2, "passive": "yes"}', b'{"count": 2, "passive": 1}',
                 b'{"count": 1e99, "passive": false}', b'{"count": 2, "passive": false, "last_shutdown": 5}',
                 b"[" * 60000, b"\xff" * 100000]
        for content in cases:
            self.write(content)
            g = Guard(self.dir)
            problems = g.load()
            self.assertEqual(len(problems), 1, content[:40])
            self.assertEqual((g.count, g.passive, g.last_shutdown), (0, False, None), content[:40])
            self.assertEqual(g.save(), "", content[:40])          # and it can be rewritten
            self.assertEqual(Guard(self.dir).load(), [], content[:40])

    def test_extra_fields_are_tolerated(self):
        self.write(b'{"version": 9, "count": 1, "passive": true, "future": [1, 2]}')
        g = Guard(self.dir)
        self.assertEqual(g.load(), [])
        self.assertEqual((g.count, g.passive), (1, True))

    def test_a_directory_in_place_of_the_file(self):
        os.makedirs(self.path)
        g = Guard(self.dir)
        self.assertEqual(len(g.load()), 1)
        self.assertNotEqual(g.save(), "")          # reported, not raised

    def test_unwritable_state_dir_is_reported_not_raised(self):
        with open(os.path.join(self.tmp.name, "afile"), "w") as f:
            f.write("x")
        g = Guard(os.path.join(self.tmp.name, "afile", "state"))   # parent is a regular file
        self.assertEqual(len(g.load()), 1)
        self.assertEqual((g.count, g.passive), (0, False))
        self.assertNotEqual(g.save(), "")

    def test_save_is_atomic_against_an_interrupted_write(self):
        g = Guard(self.dir)
        g.update({"count": 1, "passive": False})
        g.save()
        # A crash while writing leaves only the temp file touched.
        with open(self.path + ".tmp", "w") as f:
            f.write('{"count": 2, "pas')
        g2 = Guard(self.dir)
        self.assertEqual(g2.load(), [])
        self.assertEqual(g2.count, 1)
        g2.update({"count": 2, "passive": False})
        self.assertEqual(g2.save(), "")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["count"], 2)


if __name__ == "__main__":
    unittest.main()
