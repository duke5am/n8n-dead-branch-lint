"""End-to-end tests for the command line interface.

Every test runs the real ``n8n_workflow_lint.py`` in a subprocess, so the
documented exit codes are tested rather than assumed.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import (  # noqa: E402
    BAD_EXAMPLE,
    GOOD_EXAMPLE,
    ROOT,
    Scratch,
    connections_of,
    node,
    run_cli,
    workflow,
)

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

MALFORMED = '{"nodes": [{"name": "A",  "connections": {}}\n'


def low_only_workflow() -> dict:
    """A workflow whose only finding is cosmetic (`overlapping-nodes`)."""

    return workflow(
        [
            node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "hook"}),
            node("Twin A", position=(0, 0)),
            node("Twin B", position=(0, 0)),
        ],
        connections_of({"Start": [["Twin A", "Twin B"]]}),
    )


class TestExitCodes(unittest.TestCase):
    def test_clean_example_exits_zero(self) -> None:
        result = run_cli(GOOD_EXAMPLE)
        self.assertEqual(result.code, EXIT_CLEAN)
        self.assertIn("clean", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_bad_example_exits_one(self) -> None:
        result = run_cli(BAD_EXAMPLE)
        self.assertEqual(result.code, EXIT_FINDINGS)
        self.assertIn("findings", result.stdout)

    def test_missing_path_exits_two(self) -> None:
        result = run_cli(os.path.join(ROOT, "does-not-exist.json"))
        self.assertEqual(result.code, EXIT_USAGE)
        self.assertIn("no such file or directory", result.stderr)

    def test_no_arguments_exits_two(self) -> None:
        result = run_cli()
        self.assertEqual(result.code, EXIT_USAGE)
        self.assertIn("at least one file", result.stderr)

    def test_unknown_severity_exits_two(self) -> None:
        result = run_cli(GOOD_EXAMPLE, "--severity", "critical")
        self.assertEqual(result.code, EXIT_USAGE)
        self.assertIn("invalid choice", result.stderr)

    def test_version_exits_zero(self) -> None:
        result = run_cli("--version")
        self.assertEqual(result.code, EXIT_CLEAN)
        self.assertIn("n8n-workflow-lint", result.stdout)

    def test_list_rules_exits_zero(self) -> None:
        result = run_cli("--list-rules")
        self.assertEqual(result.code, EXIT_CLEAN)


class TestMalformedInput(unittest.TestCase):
    def test_malformed_json_exits_two(self) -> None:
        with Scratch("malformed") as scratch:
            path = scratch.write("broken.json", MALFORMED)
            result = run_cli(path)
            self.assertEqual(result.code, EXIT_USAGE)
            self.assertIn("not valid JSON", result.stderr)

    def test_malformed_json_in_a_directory_exits_two(self) -> None:
        with Scratch("malformed-dir") as scratch:
            scratch.write("ok.json", low_only_workflow())
            scratch.write("broken.json", MALFORMED)
            result = run_cli(scratch.path)
            self.assertEqual(result.code, EXIT_USAGE)
            self.assertIn("not valid JSON", result.stderr)

    def test_parse_error_beats_findings_in_the_exit_code(self) -> None:
        with Scratch("mixed") as scratch:
            scratch.write("bad.json", {"nodes": [], "connections": {}})
            scratch.write("broken.json", "{not json")
            result = run_cli(scratch.path)
            self.assertEqual(result.code, EXIT_USAGE)

    def test_empty_workflow_is_a_finding_not_a_parse_error(self) -> None:
        with Scratch("empty") as scratch:
            path = scratch.write("empty.json", {"nodes": [], "connections": {}})
            result = run_cli(path)
            self.assertEqual(result.code, EXIT_FINDINGS)
            self.assertIn("empty-workflow", result.stdout)

    def test_valid_json_that_is_not_a_workflow_is_reported_when_named(self) -> None:
        with Scratch("notworkflow") as scratch:
            path = scratch.write("package.json", {"name": "not-a-workflow"})
            result = run_cli(path)
            self.assertEqual(result.code, EXIT_FINDINGS)
            self.assertIn("invalid-workflow", result.stdout)

    def test_unreadable_directory_without_json_exits_two(self) -> None:
        with Scratch("no-json") as scratch:
            scratch.write("notes.txt", "hello")
            result = run_cli(scratch.path)
            self.assertEqual(result.code, EXIT_USAGE)
            self.assertIn("no .json files", result.stderr)


class TestSeverityThreshold(unittest.TestCase):
    def test_low_only_file_fails_at_the_default_threshold(self) -> None:
        with Scratch("low-only") as scratch:
            path = scratch.write("low.json", low_only_workflow())
            result = run_cli(path)
            self.assertEqual(result.code, EXIT_FINDINGS)
            self.assertIn("overlapping-nodes", result.stdout)

    def test_low_only_file_passes_at_the_medium_threshold(self) -> None:
        with Scratch("low-only-medium") as scratch:
            path = scratch.write("low.json", low_only_workflow())
            result = run_cli(path, "--severity", "medium")
            self.assertEqual(result.code, EXIT_CLEAN)
            self.assertNotIn("overlapping-nodes", result.stdout)

    def test_high_threshold_hides_lower_severities(self) -> None:
        default = run_cli(BAD_EXAMPLE, "--json")
        high = run_cli(BAD_EXAMPLE, "--json", "--severity", "high")
        default_payload = json.loads(default.stdout)
        high_payload = json.loads(high.stdout)
        self.assertEqual(high_payload["summary"]["high"], default_payload["summary"]["high"])
        self.assertEqual(high_payload["summary"]["medium"], 0)
        self.assertEqual(high_payload["summary"]["low"], 0)
        self.assertLess(high_payload["summary"]["findings"], default_payload["summary"]["findings"])

    def test_clean_file_passes_at_every_threshold(self) -> None:
        for threshold in ("high", "medium", "low"):
            with self.subTest(threshold=threshold):
                result = run_cli(GOOD_EXAMPLE, "--severity", threshold)
                self.assertEqual(result.code, EXIT_CLEAN)


class TestOutputModes(unittest.TestCase):
    def test_json_output_is_machine_readable(self) -> None:
        result = run_cli(BAD_EXAMPLE, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tool"], "n8n-workflow-lint")
        self.assertEqual(payload["threshold"], "low")
        self.assertEqual(payload["summary"]["findings"], len(payload["findings"]))
        self.assertEqual(
            payload["summary"]["findings"],
            payload["summary"]["high"]
            + payload["summary"]["medium"]
            + payload["summary"]["low"],
        )
        first = payload["findings"][0]
        for key in ("rule", "severity", "message", "fix", "location", "file"):
            with self.subTest(key=key):
                self.assertIn(key, first)
        self.assertEqual(first["file"], BAD_EXAMPLE)

    def test_json_output_on_a_clean_file_has_no_findings(self) -> None:
        payload = json.loads(run_cli(GOOD_EXAMPLE, "--json").stdout)
        self.assertEqual(payload["findings"], [])
        self.assertEqual(payload["summary"]["findings"], 0)
        self.assertEqual(payload["errors"], [])

    def test_quiet_mode_is_silent_when_clean(self) -> None:
        result = run_cli(GOOD_EXAMPLE, "--quiet")
        self.assertEqual(result.code, EXIT_CLEAN)
        self.assertEqual(result.stdout, "")

    def test_quiet_mode_prints_one_line_per_finding(self) -> None:
        normal = json.loads(run_cli(BAD_EXAMPLE, "--json").stdout)
        result = run_cli(BAD_EXAMPLE, "--quiet")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), normal["summary"]["findings"])
        self.assertEqual(result.code, EXIT_FINDINGS)
        self.assertTrue(lines[0].startswith(BAD_EXAMPLE + ": "))
        self.assertIn(" :: ", lines[0])

    def test_every_finding_carries_a_fix_and_a_reason(self) -> None:
        payload = json.loads(run_cli(BAD_EXAMPLE, "--json").stdout)
        for finding in payload["findings"]:
            with self.subTest(rule=finding["rule"]):
                self.assertTrue(finding["message"].strip())
                self.assertTrue(finding["fix"].strip())
                self.assertIn(finding["severity"], ("high", "medium", "low"))

    def test_text_output_names_each_file(self) -> None:
        result = run_cli(BAD_EXAMPLE)
        self.assertIn(BAD_EXAMPLE + ":", result.stdout)
        self.assertIn("findings (", result.stdout)

    def test_list_rules_covers_the_registry(self) -> None:
        from n8nlint.rules import RULES

        result = run_cli("--list-rules")
        for rule in RULES:
            with self.subTest(rule=rule.id):
                self.assertIn(rule.id, result.stdout)
        self.assertIn("--severity high|medium|low", result.stdout)

    def test_output_is_deterministic(self) -> None:
        first = run_cli(BAD_EXAMPLE, "--json").stdout
        second = run_cli(BAD_EXAMPLE, "--json").stdout
        self.assertEqual(first, second)


class TestInputPaths(unittest.TestCase):
    def test_directory_scan_finds_every_json_file(self) -> None:
        with Scratch("scan") as scratch:
            scratch.write("a.json", low_only_workflow())
            scratch.write("nested/b.json", low_only_workflow())
            result = run_cli(scratch.path, "--json")
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["files"]), 2)
            self.assertEqual(result.code, EXIT_FINDINGS)

    def test_directory_scan_skips_json_that_is_not_a_workflow(self) -> None:
        with Scratch("scan-skip") as scratch:
            scratch.write("good.json", low_only_workflow())
            scratch.write("meta.json", {"generator": "something else"})
            result = run_cli(scratch.path, "--json")
            payload = json.loads(result.stdout)
            self.assertEqual(len(payload["files"]), 1)
            self.assertEqual(payload["files"][0]["path"].endswith("good.json"), True)

    def test_directory_scan_of_a_clean_directory_exits_zero(self) -> None:
        with Scratch("scan-clean") as scratch:
            with open(GOOD_EXAMPLE, "r", encoding="utf-8") as handle:
                scratch.write("good.json", handle.read())
            scratch.write("notes.json", {"not": "a workflow"})
            result = run_cli(scratch.path, "--quiet")
            self.assertEqual(result.code, EXIT_CLEAN)
            self.assertEqual(result.stdout, "")

    def test_several_explicit_files_are_all_linted(self) -> None:
        result = run_cli(GOOD_EXAMPLE, BAD_EXAMPLE, "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["files"]), 2)
        self.assertEqual(result.code, EXIT_FINDINGS)

    def test_relative_paths_work_from_another_directory(self) -> None:
        result = run_cli(
            os.path.join("examples", "good.json"), cwd=ROOT
        )
        self.assertEqual(result.code, EXIT_CLEAN)


class TestEntryPoint(unittest.TestCase):
    def test_script_runs_without_installation(self) -> None:
        # The shim must work from any working directory, with no env vars.
        result = run_cli(GOOD_EXAMPLE, cwd=os.path.dirname(ROOT))
        self.assertEqual(result.code, EXIT_CLEAN)

    def test_module_entry_point_works(self) -> None:
        import subprocess

        completed = subprocess.run(
            [sys.executable, "-m", "n8nlint.cli", GOOD_EXAMPLE],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=120,
        )
        self.assertEqual(completed.returncode, EXIT_CLEAN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
