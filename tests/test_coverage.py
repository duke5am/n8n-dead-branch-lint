"""Coverage guards: every registered rule must be reachable from a real input.

The kitchen-sink document below is a JSON *array* export holding one small
workflow per problem.  If a rule is added to the registry and no fixture
triggers it, :meth:`TestRuleCoverage.test_every_registered_rule_fires` fails.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from helpers import connections_of, lint, node, rules_of, workflow  # noqa: E402

from n8nlint.rules import RULES, RULES_BY_ID, Finding  # noqa: E402


def _webhook(name: str = "Start", path: str = "hook", **extra: object) -> dict:
    return node(
        name,
        "n8n-nodes-base.webhook",
        (-200, 0),
        {"httpMethod": "POST", "path": path, "options": {}},
        **extra,
    )


def kitchen_sink() -> list[dict]:
    """One deliberately broken workflow per rule."""

    docs: list[dict] = []

    # invalid-workflow: valid JSON, not a workflow
    docs.append({"name": "not a workflow", "id": "sink-invalid", "foo": "bar"})

    # empty-workflow
    docs.append({"name": "empty", "id": "sink-empty", "nodes": [], "connections": {}})

    # node-missing-fields
    docs.append(
        workflow(
            [_webhook(), node("Broken", None, (0, 0))],
            connections_of({"Start": [["Broken"]]}),
            name="missing-fields",
            id="sink-missing-fields",
        )
    )

    # duplicate-node-name
    docs.append(
        workflow(
            [_webhook(), node("Twin", position=(0, 0)), node("Twin", position=(0, 200))],
            connections_of({"Start": [["Twin"]]}),
            name="duplicate-name",
            id="sink-duplicate-name",
        )
    )

    # dangling-connection
    docs.append(
        workflow(
            [_webhook(), node("Keep", position=(0, 0))],
            connections_of({"Start": [["Keep", "Ghost"]]}),
            name="dangling",
            id="sink-dangling",
        )
    )

    # unreachable-node
    docs.append(
        workflow(
            [_webhook(), node("Keep", position=(0, 0)),
             node("Orphan", position=(0, 240))],
            connections_of({"Start": [["Keep"]]}),
            name="unreachable",
            id="sink-unreachable",
        )
    )

    # no-trigger
    docs.append(
        workflow(
            [node("A", position=(0, 0)), node("B", position=(200, 0))],
            connections_of({"A": [["B"]]}),
            name="no-trigger",
            id="sink-no-trigger",
        )
    )

    # manual-trigger-only
    docs.append(
        workflow(
            [
                node("Start", "n8n-nodes-base.manualTrigger", (-200, 0)),
                node("Work", position=(0, 0)),
            ],
            connections_of({"Start": [["Work"]]}),
            name="manual-only",
            id="sink-manual-only",
        )
    )

    # disabled-node-in-path
    docs.append(
        workflow(
            [
                _webhook(),
                node("Off", position=(0, 0), disabled=True),
                node("After", position=(200, 0)),
            ],
            connections_of({"Start": [["Off"]], "Off": [["After"]]}),
            name="disabled-in-path",
            id="sink-disabled",
        )
    )

    # continue-on-fail-unchecked
    docs.append(
        workflow(
            [
                _webhook(),
                node("Risky", "n8n-nodes-base.httpRequest", (0, 0),
                     {"url": "https://api.example.com/x"}, continueOnFail=True),
                node("Consume", position=(200, 0)),
            ],
            connections_of({"Start": [["Risky"]], "Risky": [["Consume"]]}),
            name="continue-on-fail",
            id="sink-continue-on-fail",
        )
    )

    # missing-upstream-field
    docs.append(
        workflow(
            [
                _webhook(),
                node("Shape", "n8n-nodes-base.set", (0, 0),
                     {"mode": "manual", "includeOtherFields": False,
                      "assignments": {"assignments": [
                          {"name": "order_id", "value": "1", "type": "string"}]},
                      "options": {}}),
                node("Use", "n8n-nodes-base.httpRequest", (200, 0),
                     {"url": "https://api.example.com/x",
                      "jsonBody": "={{ $json.nope }}"}),
            ],
            connections_of({"Start": [["Shape"]], "Shape": [["Use"]]}),
            name="missing-field",
            id="sink-missing-field",
        )
    )

    # credential-reference
    docs.append(
        workflow(
            [
                _webhook(),
                node("Call", "n8n-nodes-base.slack", (0, 0), {"select": "channel"},
                     credentials={"slackApi": {"name": "Slack Bot"}}),
            ],
            connections_of({"Start": [["Call"]]}),
            name="credential",
            id="sink-credential",
        )
    )

    # hardcoded-secret
    docs.append(
        workflow(
            [
                _webhook(),
                node("Call", "n8n-nodes-base.httpRequest", (0, 0),
                     {"url": "https://api.example.com/x?api_key=n8n-lint-fixture-key-0001"}),
            ],
            connections_of({"Start": [["Call"]]}),
            name="secret",
            id="sink-secret",
        )
    )

    # subworkflow-id-not-in-file
    docs.append(
        workflow(
            [
                _webhook(),
                node("Call Child", "n8n-nodes-base.executeWorkflow", (0, 0),
                     {"workflowId": {"__rl": True, "value": "wf-missing", "mode": "list"}}),
            ],
            connections_of({"Start": [["Call Child"]]}),
            name="subworkflow",
            id="sink-subworkflow",
        )
    )

    # unintended-cycle
    docs.append(
        workflow(
            [
                _webhook(),
                node("A", position=(0, 0)),
                node("B", position=(200, 0)),
            ],
            connections_of({"Start": [["A"]], "A": [["B"]], "B": [["A"]]}),
            name="cycle",
            id="sink-cycle",
        )
    )

    # duplicate-webhook-path
    docs.append(
        workflow(
            [
                _webhook("Hook A", "orders"),
                _webhook("Hook B", "orders"),
                node("Sink", position=(200, 0)),
            ],
            connections_of({"Hook A": [["Sink"]], "Hook B": [["Sink"]]}),
            name="duplicate-webhook",
            id="sink-duplicate-webhook",
        )
    )

    # pinned-data
    pinned = workflow(
        [_webhook(), node("Work", position=(0, 0))],
        connections_of({"Start": [["Work"]]}),
        name="pinned",
        id="sink-pinned",
    )
    pinned["pinData"] = {"Work": [{"json": {"a": 1}}]}
    docs.append(pinned)

    # overlapping-nodes
    docs.append(
        workflow(
            [_webhook(), node("Twin A", position=(0, 0)), node("Twin B", position=(0, 0))],
            connections_of({"Start": [["Twin A", "Twin B"]]}),
            name="overlap",
            id="sink-overlap",
        )
    )

    return docs


class TestRuleCoverage(unittest.TestCase):
    def setUp(self) -> None:
        self.findings: list[Finding] = lint(kitchen_sink(), "kitchen-sink.json")
        self.fired = rules_of(self.findings)

    def test_sink_is_a_multi_workflow_export(self) -> None:
        self.assertEqual(len(kitchen_sink()), 18)

    def test_every_registered_rule_fires(self) -> None:
        missing = sorted({r.id for r in RULES} - self.fired)
        self.assertEqual(
            missing,
            [],
            f"these registered rules were never triggered by any fixture: {missing}",
        )

    def test_no_unknown_rule_ids_are_produced(self) -> None:
        self.assertEqual(sorted(self.fired - set(RULES_BY_ID)), [])

    def test_findings_are_sorted_most_severe_first(self) -> None:
        from n8nlint.model import SEVERITY_RANK

        ranks = [SEVERITY_RANK[f.severity] for f in self.findings]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_every_finding_has_an_explanation_and_a_fix(self) -> None:
        for finding in self.findings:
            with self.subTest(rule=finding.rule):
                self.assertIn(finding.severity, ("high", "medium", "low"))
                self.assertGreater(len(finding.message), 40)
                self.assertGreater(len(finding.fix), 20)

    def test_multi_workflow_findings_name_their_workflow(self) -> None:
        named = [f for f in self.findings if f.workflow]
        self.assertEqual(len(named), len(self.findings))
        self.assertIn("not a workflow", {f.workflow for f in self.findings})


class TestRegistryIntegrity(unittest.TestCase):
    def test_rule_ids_are_unique(self) -> None:
        ids = [rule.id for rule in RULES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rule_ids_are_lowercase_kebab_case(self) -> None:
        import re

        for rule in RULES:
            with self.subTest(rule=rule.id):
                self.assertRegex(rule.id, re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$"))

    def test_severities_are_valid(self) -> None:
        for rule in RULES:
            with self.subTest(rule=rule.id):
                self.assertIn(rule.severity, ("high", "medium", "low"))

    def test_every_rule_documents_why_it_matters(self) -> None:
        for rule in RULES:
            with self.subTest(rule=rule.id):
                self.assertTrue(rule.title.strip())
                self.assertTrue(rule.why.strip())

    def test_registry_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(RULES), 18)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
