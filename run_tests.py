#!/usr/bin/env python3
"""Run the whole test suite: ``python3 run_tests.py [-v]``.

Equivalent to ``python3 -m unittest discover -s tests -v``.  The runner is
here because the verbose discovery command is easy to mis-type, and because
it also reports how many assertions actually ran -- a test file full of
``assertTrue(True)`` would otherwise look as good as a real one.

Exit code: 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import functools
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")

#: Assertion helpers that other helpers call internally; counting them too
#: would report the same assertion two or three times.
_INTERNAL_ASSERTIONS = frozenset(
    {
        "assertMultiLineEqual",
        "assertSequenceEqual",
        "assertListEqual",
        "assertTupleEqual",
        "assertSetEqual",
        "assertDictEqual",
        "assertAlmostEqual",
        "assertNotAlmostEqual",
        "assertRaisesRegex",
        "assertWarnsRegex",
    }
)


def _install_assertion_counter() -> dict[str, int]:
    """Wrap every public ``assert*`` on TestCase so calls can be counted."""

    counter = {"count": 0}
    for name in dir(unittest.TestCase):
        if not name.startswith("assert") or name in _INTERNAL_ASSERTIONS:
            continue
        original = getattr(unittest.TestCase, name, None)
        if not callable(original):
            continue

        def make(orig):  # noqa: ANN001, ANN202 - local factory
            @functools.wraps(orig)
            def wrapper(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
                counter["count"] += 1
                return orig(self, *args, **kwargs)

            return wrapper

        setattr(unittest.TestCase, name, make(original))
    return counter


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbosity = 1
    if "-v" in argv or "--verbose" in argv:
        verbosity = 2
        argv = [a for a in argv if a not in ("-v", "--verbose")]
    if argv:
        sys.stderr.write(f"run_tests.py: unexpected argument(s): {argv}\n")
        return 2

    for path in (HERE, TESTS):
        if path not in sys.path:
            sys.path.insert(0, path)

    counter = _install_assertion_counter()

    loader = unittest.TestLoader()
    suite = loader.discover(TESTS)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)

    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(
        f"\nran {result.testsRun} tests ({counter['count']} assertions): "
        f"{passed} passed, {len(result.failures)} failed, "
        f"{len(result.errors)} errored, {len(result.skipped)} skipped"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
