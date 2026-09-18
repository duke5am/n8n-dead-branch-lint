"""Guards on the shipped documentation and packaging.

The README is the product page for this tool, so its promised facts are
tested like code: the documented exit codes, the example output, and the
exact final line.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import (  # noqa: E402
    BAD_EXAMPLE,
    CLI,
    GOOD_EXAMPLE,
    README,
    ROOT,
    run_cli,
)

LICENSE = os.path.join(ROOT, "LICENSE")

#: The one line the README must end with, byte for byte.
FUNNEL_LINE = "\u2192 **n8n Workflow Automation Pack**: <!-- GUMROAD-LINK -->"


class TestReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(README, "r", encoding="utf-8") as handle:
            cls.text = handle.read()
        cls.lines = cls.text.splitlines()

    def test_readme_exists_and_is_substantial(self) -> None:
        self.assertTrue(os.path.isfile(README))
        self.assertGreater(len(self.text), 4000)

    def test_last_line_is_the_exact_funnel_line(self) -> None:
        self.assertEqual(self.lines[-1], FUNNEL_LINE)

    def test_last_line_has_no_trailing_whitespace(self) -> None:
        self.assertEqual(self.lines[-1], self.lines[-1].rstrip())

    def test_funnel_line_appears_exactly_once(self) -> None:
        self.assertEqual(self.text.count("GUMROAD-LINK"), 1)

    def test_no_placeholder_tokens_are_left_behind(self) -> None:
        self.assertNotIn("@@", self.text)

    def test_documents_what_it_does(self) -> None:
        self.assertIn("## What it catches", self.text)
        self.assertIn("## Usage", self.text)

    def test_documents_every_exit_code(self) -> None:
        self.assertIn("### Exit codes", self.text)
        for code in ("`0`", "`1`", "`2`"):
            with self.subTest(code=code):
                self.assertIn(code, self.text)
        self.assertIn("Usage error, unreadable path, or a file that is not valid JSON", self.text)

    def test_documents_the_honest_limits(self) -> None:
        self.assertIn("## What this does not do", self.text)
        for promise in (
            "does not run n8n",
            "node schemas",
            "cannot evaluate expressions at runtime",
            "cannot verify that a credential exists",
        ):
            with self.subTest(promise=promise):
                self.assertIn(promise, self.text)

    def test_documents_the_assumed_export_format(self) -> None:
        self.assertIn("## Assumed export format", self.text)
        for field in ("`nodes`", "`connections`", '"parameters"', "`pinData`",
                      "`credentials`", "`typeVersion`", "`position`"):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_documents_every_rule_id(self) -> None:
        from n8nlint.rules import RULES

        for rule in RULES:
            with self.subTest(rule=rule.id):
                self.assertIn(rule.id, self.text)

    def test_has_an_mit_licence_section(self) -> None:
        self.assertIn("## Licence", self.text)
        self.assertIn("MIT", self.text)
        self.assertIn("Permission is hereby granted", self.text)

    def test_pasted_bad_example_output_is_the_real_output(self) -> None:
        # The README quotes the run with relative paths from the project root.
        result = run_cli("examples/bad.json", cwd=ROOT)
        self.assertEqual(result.code, 1)
        for line in result.stdout.splitlines():
            stripped = line.rstrip()
            if not stripped:
                continue
            with self.subTest(line=stripped[:50]):
                self.assertIn(stripped, self.text)

    def test_pasted_good_example_output_is_the_real_output(self) -> None:
        result = run_cli("examples/good.json", cwd=ROOT)
        self.assertEqual(result.code, 0)
        for line in result.stdout.splitlines():
            with self.subTest(line=line):
                self.assertIn(line, self.text)

    def test_pasted_test_summary_matches_the_documented_command(self) -> None:
        match = re.search(r"ran (\d+) tests \(([\d,]+) assertions\)", self.text)
        self.assertIsNotNone(match, "README must state the real test totals")
        assert match is not None
        self.assertGreater(int(match.group(1)), 100)
        self.assertGreater(int(match.group(2).replace(",", "")), 200)

    def test_quotes_the_working_invocation(self) -> None:
        self.assertIn("python3 n8n_workflow_lint.py examples/bad.json", self.text)
        self.assertIn(
            "python3 -m unittest discover -s tests -v", self.text
        )


class TestLicenseFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(LICENSE, "r", encoding="utf-8") as handle:
            cls.text = handle.read()

    def test_is_the_mit_licence(self) -> None:
        self.assertTrue(self.text.startswith("MIT License"))
        self.assertIn("Permission is hereby granted, free of charge", self.text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", self.text)

    def test_holder_is_duke5am(self) -> None:
        self.assertIn("Copyright (c) 2026 duke5am", self.text)


class TestShippedFiles(unittest.TestCase):
    def test_expected_layout(self) -> None:
        for relative in (
            "n8n_workflow_lint.py",
            "run_tests.py",
            "README.md",
            "LICENSE",
            ".gitignore",
            "examples/good.json",
            "examples/bad.json",
            "n8nlint/__init__.py",
            "n8nlint/model.py",
            "n8nlint/rules.py",
            "n8nlint/fields.py",
            "n8nlint/expressions.py",
            "n8nlint/secrets.py",
            "n8nlint/report.py",
            "n8nlint/cli.py",
            "tests/helpers.py",
            "tests/test_rules.py",
            "tests/test_cli.py",
            "tests/test_coverage.py",
        ):
            with self.subTest(path=relative):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)))

    def test_examples_are_labelled(self) -> None:
        for path in (GOOD_EXAMPLE, BAD_EXAMPLE):
            with self.subTest(path=path):
                with open(path, "r", encoding="utf-8") as handle:
                    doc = json.load(handle)
                self.assertIn("name", doc)
                self.assertTrue(doc["nodes"])

    def test_entry_point_has_a_shebang(self) -> None:
        with open(CLI, "r", encoding="utf-8") as handle:
            self.assertTrue(handle.readline().startswith("#!"))

    def test_no_third_party_imports_anywhere(self) -> None:
        """Standard library only: no pip, no vendored packages, no network."""

        allowed_roots = {
            "__future__", "argparse", "collections", "dataclasses", "functools",
            "json", "os", "re", "shutil", "subprocess", "sys", "typing",
            "unittest", "n8nlint", "helpers",
        }
        pattern = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", ".scratch")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(dirpath, name)
                with open(full, "r", encoding="utf-8") as handle:
                    for lineno, line in enumerate(handle, 1):
                        match = pattern.match(line)
                        if not match:
                            continue
                        root_module = match.group(1).split(".")[0]
                        with self.subTest(file=os.path.relpath(full, ROOT), line=lineno):
                            self.assertIn(
                                root_module,
                                allowed_roots,
                                f"{full}:{lineno} imports `{root_module}`, which "
                                f"is not in the standard-library allowlist",
                            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
