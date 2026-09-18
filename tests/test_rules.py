"""One test class per rule, plus the negative control.

Every fixture is built in memory, so a test never depends on another test's
file and nothing is written outside the project.
"""

from __future__ import annotations

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
    connections_of,
    lint,
    lint_file,
    messages_for,
    node,
    only,
    rules_of,
    simple_workflow,
    workflow,
)

from n8nlint.fields import (  # noqa: E402
    describe_output,
    extract_json_literal_keys,
    is_field_reset,
    is_passthrough,
)
from n8nlint.model import (  # noqa: E402
    SEVERITY_RANK,
    Node,
    is_trigger_type,
    parse_text,
    short_type,
)
from n8nlint.expressions import find_json_reads  # noqa: E402
from n8nlint.secrets import is_placeholder, scan_parameters  # noqa: E402


# --------------------------------------------------------------------------
# negative control
# --------------------------------------------------------------------------


class TestNegativeControl(unittest.TestCase):
    """A clean workflow must produce *zero* findings, not "few"."""

    def test_shipped_good_example_is_completely_clean(self) -> None:
        findings = lint_file(GOOD_EXAMPLE)
        self.assertEqual(
            findings,
            [],
            "examples/good.json must produce no findings at the default "
            f"threshold, but produced: {[f.rule for f in findings]}",
        )

    def test_minimal_clean_workflow_is_clean(self) -> None:
        self.assertEqual(lint(simple_workflow()), [])

    def test_clean_workflow_with_two_exits_is_clean(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "hook"}),
                node("Check", "n8n-nodes-base.if", (0, 0), {"conditions": {}}),
                node("Yes", "n8n-nodes-base.noOp", (200, -100)),
                node("No", "n8n-nodes-base.noOp", (200, 100)),
            ],
            connections_of(
                {
                    "Start": [["Check"]],
                    "Check": [["Yes"], ["No"]],
                }
            ),
        )
        self.assertEqual(lint(doc), [])

    def test_clean_set_chain_is_clean(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "hook"}),
                node(
                    "Shape",
                    "n8n-nodes-base.set",
                    (0, 0),
                    {
                        "mode": "manual",
                        "includeOtherFields": False,
                        "assignments": {
                            "assignments": [
                                {"name": "email", "value": "x", "type": "string"}
                            ]
                        },
                        "options": {},
                    },
                ),
                node(
                    "Use",
                    "n8n-nodes-base.httpRequest",
                    (200, 0),
                    {"url": "https://api.example.com/send", "jsonBody": "={{ $json.email }}"},
                ),
            ],
            connections_of({"Start": [["Shape"]], "Shape": [["Use"]]}),
        )
        self.assertEqual(lint(doc), [])


# --------------------------------------------------------------------------
# structural rules
# --------------------------------------------------------------------------


class TestInvalidWorkflow(unittest.TestCase):
    def test_non_workflow_object_fires(self) -> None:
        findings = lint({"hello": "world"})
        self.assertIn("invalid-workflow", rules_of(findings))

    def test_nodes_not_a_list_fires(self) -> None:
        findings = lint({"nodes": "nope", "connections": {}})
        self.assertIn("invalid-workflow", rules_of(findings))
        self.assertIn("not a list", messages_for(findings, "invalid-workflow"))

    def test_valid_minimal_object_does_not_fire(self) -> None:
        findings = lint(simple_workflow())
        self.assertNotIn("invalid-workflow", rules_of(findings))

    def test_severity_is_high(self) -> None:
        findings = only(lint({"hello": "world"}), "invalid-workflow")
        self.assertEqual(findings[0].severity, "high")


class TestEmptyWorkflow(unittest.TestCase):
    def test_empty_nodes_array_fires(self) -> None:
        findings = lint({"nodes": [], "connections": {}})
        self.assertIn("empty-workflow", rules_of(findings))

    def test_empty_workflow_does_not_also_report_no_trigger(self) -> None:
        findings = lint({"nodes": [], "connections": {}})
        self.assertNotIn("no-trigger", rules_of(findings))

    def test_empty_workflow_is_not_invalid(self) -> None:
        findings = lint({"nodes": [], "connections": {}})
        self.assertNotIn("invalid-workflow", rules_of(findings))

    def test_populated_workflow_does_not_fire(self) -> None:
        self.assertNotIn("empty-workflow", rules_of(lint(simple_workflow())))


class TestNodeMissingFields(unittest.TestCase):
    def test_node_without_name_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}),
                node(None, "n8n-nodes-base.noOp", (200, 0)),
            ],
            connections_of({"Start": [[""]]}),
        )
        findings = only(lint(doc), "node-missing-fields")
        self.assertTrue(findings)
        self.assertIn("`name`", findings[0].message)

    def test_node_without_type_fires(self) -> None:
        doc = workflow(
            [node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}),
             node("Orphan", None, (200, 0))],
            connections_of({"Start": [["Orphan"]]}),
        )
        findings = only(lint(doc), "node-missing-fields")
        self.assertTrue(findings)
        self.assertIn("`type`", findings[0].message)

    def test_well_formed_nodes_do_not_fire(self) -> None:
        self.assertNotIn("node-missing-fields", rules_of(lint(simple_workflow())))


class TestDuplicateNodeName(unittest.TestCase):
    def test_duplicate_names_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}),
                node("Twin", "n8n-nodes-base.noOp", (200, 0)),
                node("Twin", "n8n-nodes-base.noOp", (200, 100)),
            ],
            connections_of({"Start": [["Twin"]]}),
        )
        findings = only(lint(doc), "duplicate-node-name")
        self.assertEqual(len(findings), 1)
        self.assertIn('"Twin"', findings[0].message)

    def test_unique_names_do_not_fire(self) -> None:
        self.assertNotIn("duplicate-node-name", rules_of(lint(simple_workflow())))


# --------------------------------------------------------------------------
# graph rules
# --------------------------------------------------------------------------


class TestDanglingConnection(unittest.TestCase):
    def test_missing_target_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}),
                node("Keep", "n8n-nodes-base.noOp", (200, 0)),
            ],
            connections_of({"Start": [["Keep", "Ghost"]]}),
        )
        findings = only(lint(doc), "dangling-connection")
        self.assertEqual(len(findings), 1)
        self.assertIn('"Ghost"', findings[0].message)

    def test_missing_source_fires(self) -> None:
        doc = workflow(
            [node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"})],
            connections_of({"Deleted Node": [["Start"]]}),
        )
        findings = only(lint(doc), "dangling-connection")
        self.assertEqual(len(findings), 1)
        self.assertIn("Deleted Node", findings[0].message)

    def test_complete_graph_does_not_fire(self) -> None:
        self.assertNotIn("dangling-connection", rules_of(lint(simple_workflow())))

    def test_severity_is_high(self) -> None:
        doc = simple_workflow()
        doc["connections"] = connections_of({"Start": [["Keep", "Ghost"]]})
        findings = only(lint(doc), "dangling-connection")
        self.assertEqual(findings[0].severity, "high")


class TestUnreachableNode(unittest.TestCase):
    def test_orphan_node_fires(self) -> None:
        findings = only(lint(simple_workflow([node("Orphan", position=(0, 200))])),
                        "unreachable-node")
        self.assertEqual(len(findings), 1)
        self.assertIn('"Orphan"', findings[0].message)

    def test_connected_nodes_do_not_fire(self) -> None:
        self.assertNotIn("unreachable-node", rules_of(lint(simple_workflow())))

    def test_unreachable_is_suppressed_when_there_is_no_trigger(self) -> None:
        # `no-trigger` already explains the situation; listing every node as
        # unreachable on top of it would be noise.
        doc = workflow(
            [node("A", position=(0, 0)), node("B", position=(200, 0))],
            connections_of({"A": [["B"]]}),
        )
        findings = lint(doc)
        self.assertIn("no-trigger", rules_of(findings))
        self.assertNotIn("unreachable-node", rules_of(findings))

    def test_node_reachable_only_through_a_disabled_node(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Off", position=(0, 0), disabled=True),
                node("After", position=(200, 0)),
            ],
            connections_of({"Start": [["Off"]], "Off": [["After"]]}),
        )
        self.assertNotIn("unreachable-node", rules_of(lint(doc)))


class TestNoTrigger(unittest.TestCase):
    def test_workflow_without_trigger_fires(self) -> None:
        doc = workflow(
            [node("A", position=(0, 0)), node("B", position=(200, 0))],
            connections_of({"A": [["B"]]}),
        )
        findings = only(lint(doc), "no-trigger")
        self.assertEqual(len(findings), 1)
        self.assertIn("can therefore only be started by hand", findings[0].message)

    def test_workflow_with_webhook_does_not_fire(self) -> None:
        self.assertNotIn("no-trigger", rules_of(lint(simple_workflow())))

    def test_all_triggers_disabled_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}, disabled=True),
                node("Work", position=(200, 0)),
            ],
            connections_of({"Start": [["Work"]]}),
        )
        findings = only(lint(doc), "no-trigger")
        self.assertEqual(len(findings), 1)
        self.assertIn("every one of them is disabled", findings[0].message)

    def test_manual_trigger_counts_as_a_trigger(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.manualTrigger", (0, 0)),
                node("Work", position=(200, 0)),
            ],
            connections_of({"Start": [["Work"]]}),
        )
        self.assertNotIn("no-trigger", rules_of(lint(doc)))

    def test_execute_workflow_trigger_counts_as_a_trigger(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.executeWorkflowTrigger", (0, 0)),
                node("Work", position=(200, 0)),
            ],
            connections_of({"Start": [["Work"]]}),
        )
        self.assertNotIn("no-trigger", rules_of(lint(doc)))

    def test_severity_is_high(self) -> None:
        doc = workflow([node("A", position=(0, 0))], {})
        findings = only(lint(doc), "no-trigger")
        self.assertEqual(findings[0].severity, "high")


class TestManualTriggerOnly(unittest.TestCase):
    def test_manual_only_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.manualTrigger", (0, 0)),
                node("Work", position=(200, 0)),
            ],
            connections_of({"Start": [["Work"]]}),
        )
        findings = only(lint(doc), "manual-trigger-only")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "low")

    def test_manual_plus_schedule_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.manualTrigger", (0, 0)),
                node("Clock", "n8n-nodes-base.scheduleTrigger", (0, 200)),
                node("Work", position=(200, 0)),
            ],
            connections_of({"Start": [["Work"]], "Clock": [["Work"]]}),
        )
        self.assertNotIn("manual-trigger-only", rules_of(lint(doc)))

    def test_clean_workflow_does_not_fire(self) -> None:
        self.assertNotIn("manual-trigger-only", rules_of(lint(simple_workflow())))


class TestDisabledNodeInPath(unittest.TestCase):
    def test_disabled_middle_node_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Off", position=(0, 0), disabled=True),
                node("After", position=(200, 0)),
            ],
            connections_of({"Start": [["Off"]], "Off": [["After"]]}),
        )
        findings = only(lint(doc), "disabled-node-in-path")
        self.assertEqual(len(findings), 1)
        self.assertIn("receives data from", findings[0].message)
        self.assertIn("feeds", findings[0].message)

    def test_disabled_node_with_no_edges_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Lonely", position=(0, 200), disabled=True),
            ],
            connections_of({"Start": []}),
        )
        self.assertNotIn("disabled-node-in-path", rules_of(lint(doc)))

    def test_enabled_node_does_not_fire(self) -> None:
        self.assertNotIn("disabled-node-in-path", rules_of(lint(simple_workflow())))

    def test_severity_is_high(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Off", position=(0, 0), disabled=True),
                node("After", position=(200, 0)),
            ],
            connections_of({"Start": [["Off"]], "Off": [["After"]]}),
        )
        self.assertEqual(only(lint(doc), "disabled-node-in-path")[0].severity, "high")


class TestUnintendedCycle(unittest.TestCase):
    def _cycle_doc(self, middle_type: str = "n8n-nodes-base.noOp") -> dict:
        return workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("A", position=(0, 0)),
                node("B", middle_type, (200, 0)),
                node("C", position=(400, 0)),
            ],
            connections_of(
                {
                    "Start": [["A"]],
                    "A": [["B"]],
                    "B": [["C"]],
                    "C": [["A"]],
                }
            ),
        )

    def test_plain_cycle_fires(self) -> None:
        findings = only(lint(self._cycle_doc()), "unintended-cycle")
        self.assertEqual(len(findings), 1)
        self.assertIn("cannot prove intent", findings[0].message)
        self.assertIn('"A"', findings[0].message)
        self.assertIn('"C"', findings[0].message)

    def test_split_in_batches_cycle_does_not_fire(self) -> None:
        doc = self._cycle_doc("n8n-nodes-base.splitInBatches")
        self.assertNotIn("unintended-cycle", rules_of(lint(doc)))

    def test_wait_node_cycle_does_not_fire(self) -> None:
        doc = self._cycle_doc("n8n-nodes-base.wait")
        self.assertNotIn("unintended-cycle", rules_of(lint(doc)))

    def test_no_cycle_does_not_fire(self) -> None:
        self.assertNotIn("unintended-cycle", rules_of(lint(simple_workflow())))

    def test_diamond_without_a_cycle_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("A", position=(0, 0)),
                node("B", position=(200, -100)),
                node("C", position=(200, 100)),
                node("D", "n8n-nodes-base.merge", (400, 0)),
            ],
            connections_of(
                {
                    "Start": [["A"]],
                    "A": [["B", "C"]],
                    "B": [["D"]],
                    "C": [["D"]],
                }
            ),
        )
        self.assertNotIn("unintended-cycle", rules_of(lint(doc)))


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------


class TestContinueOnFail(unittest.TestCase):
    def _doc(self, **extra: object) -> dict:
        return workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Risky", "n8n-nodes-base.httpRequest", (0, 0),
                     {"url": "https://api.example.com/x"}, **extra),
                node("Consume", position=(200, 0)),
            ],
            connections_of({"Start": [["Risky"]], "Risky": [["Consume"]]}),
        )

    def test_continue_on_fail_true_fires(self) -> None:
        findings = only(lint(self._doc(continueOnFail=True)),
                        "continue-on-fail-unchecked")
        self.assertEqual(len(findings), 1)
        self.assertIn("continueOnFail: true", findings[0].message)
        self.assertEqual(findings[0].severity, "medium")

    def test_on_error_continue_regular_output_fires(self) -> None:
        findings = only(lint(self._doc(onError="continueRegularOutput")),
                        "continue-on-fail-unchecked")
        self.assertEqual(len(findings), 1)

    def test_on_error_continue_error_output_does_not_fire(self) -> None:
        doc = self._doc(onError="continueErrorOutput")
        self.assertNotIn("continue-on-fail-unchecked", rules_of(lint(doc)))

    def test_downstream_error_check_suppresses(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Risky", "n8n-nodes-base.httpRequest", (0, 0),
                     {"url": "https://api.example.com/x"}, continueOnFail=True),
                node("Guard", "n8n-nodes-base.if", (200, 0),
                     {"conditions": {"conditions": [
                         {"leftValue": "={{ $json.error }}", "rightValue": ""}]}}),
            ],
            connections_of({"Start": [["Risky"]], "Risky": [["Guard"]]}),
        )
        self.assertNotIn("continue-on-fail-unchecked", rules_of(lint(doc)))

    def test_terminal_node_with_continue_on_fail_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Risky", "n8n-nodes-base.httpRequest", (0, 0),
                     {"url": "https://api.example.com/x"}, continueOnFail=True),
            ],
            connections_of({"Start": [["Risky"]]}),
        )
        self.assertNotIn("continue-on-fail-unchecked", rules_of(lint(doc)))

    def test_without_the_flag_nothing_fires(self) -> None:
        self.assertNotIn("continue-on-fail-unchecked", rules_of(lint(self._doc())))


class TestMissingUpstreamField(unittest.TestCase):
    def _chain(self, read_field: str, middle: dict | None = None) -> dict:
        shape = node(
            "Shape",
            "n8n-nodes-base.set",
            (0, 0),
            {
                "mode": "manual",
                "includeOtherFields": False,
                "assignments": {
                    "assignments": [
                        {"name": "order_id", "value": "1", "type": "string"},
                        {"name": "amount", "value": "2", "type": "number"},
                    ]
                },
                "options": {},
            },
        )
        nodes = [
            node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
            shape,
        ]
        conns = {"Start": [["Shape"]]}
        if middle is not None:
            nodes.append(middle)
            conns["Shape"] = [[middle["name"]]]
            conns[middle["name"]] = [["Use"]]
        else:
            conns["Shape"] = [["Use"]]
        nodes.append(
            node(
                "Use",
                "n8n-nodes-base.httpRequest",
                (400, 0),
                {
                    "url": "https://api.example.com/send",
                    "jsonBody": "={{ JSON.stringify({ v: $json.%s }) }}" % read_field,
                },
            )
        )
        return workflow(nodes, connections_of(conns))

    def test_field_that_exists_does_not_fire(self) -> None:
        self.assertNotIn("missing-upstream-field", rules_of(lint(self._chain("order_id"))))

    def test_field_that_does_not_exist_fires(self) -> None:
        findings = only(lint(self._chain("customer_email")), "missing-upstream-field")
        self.assertEqual(len(findings), 1)

    def test_message_says_it_is_a_warning_and_why(self) -> None:
        findings = only(lint(self._chain("customer_email")), "missing-upstream-field")
        message = findings[0].message
        self.assertIn("warning, not an error", message)
        self.assertIn("statically", message)
        self.assertEqual(findings[0].severity, "medium")

    def test_message_lists_what_is_available(self) -> None:
        findings = only(lint(self._chain("customer_email")), "missing-upstream-field")
        self.assertIn("order_id", findings[0].message)
        self.assertIn("amount", findings[0].message)

    def test_opaque_node_upstream_suppresses_the_finding(self) -> None:
        opaque = node("Fetch", "n8n-nodes-base.httpRequest", (200, 0),
                      {"url": "https://api.example.com/fetch"})
        findings = lint(self._chain("customer_email", middle=opaque))
        self.assertNotIn("missing-upstream-field", rules_of(findings))

    def test_set_without_the_include_other_fields_flag_is_not_judged(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Shape", "n8n-nodes-base.set", (0, 0),
                     {"values": {"string": [{"name": "a", "value": "1"}]}}),
                node("Use", "n8n-nodes-base.httpRequest", (200, 0),
                     {"url": "https://api.example.com/x",
                      "jsonBody": "={{ $json.zzz }}"}),
            ],
            connections_of({"Start": [["Shape"]], "Shape": [["Use"]]}),
        )
        self.assertNotIn("missing-upstream-field", rules_of(lint(doc)))

    def test_code_node_literal_fields_are_understood(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Build", "n8n-nodes-base.code", (0, 0),
                     {"mode": "runOnceForAllItems",
                      "jsCode": "return [{ json: { alpha: 1, beta: 2 } }];"}),
                node("Use", "n8n-nodes-base.httpRequest", (200, 0),
                     {"url": "https://api.example.com/x",
                      "jsonBody": "={{ $json.gamma }}"}),
            ],
            connections_of({"Start": [["Build"]], "Build": [["Use"]]}),
        )
        findings = only(lint(doc), "missing-upstream-field")
        self.assertEqual(len(findings), 1)
        self.assertIn("alpha", findings[0].message)

    def test_code_node_with_a_spread_is_treated_as_unknown(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Build", "n8n-nodes-base.code", (0, 0),
                     {"mode": "runOnceForAllItems",
                      "jsCode": "return [{ json: { ...item.json, alpha: 1 } }];"}),
                node("Use", "n8n-nodes-base.httpRequest", (200, 0),
                     {"url": "https://api.example.com/x",
                      "jsonBody": "={{ $json.gamma }}"}),
            ],
            connections_of({"Start": [["Build"]], "Build": [["Use"]]}),
        )
        self.assertNotIn("missing-upstream-field", rules_of(lint(doc)))

    def test_passthrough_node_is_walked_through(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Shape", "n8n-nodes-base.set", (0, 0),
                     {"mode": "manual", "includeOtherFields": False,
                      "assignments": {"assignments": [
                          {"name": "a", "value": "1", "type": "string"}]},
                      "options": {}}),
                node("Gate", "n8n-nodes-base.if", (200, 0), {"conditions": {}}),
                node("Use", "n8n-nodes-base.httpRequest", (400, 0),
                     {"url": "https://api.example.com/x",
                      "jsonBody": "={{ $json.zzz }}"}),
            ],
            connections_of({"Start": [["Shape"]], "Shape": [["Gate"]],
                            "Gate": [["Use"]]}),
        )
        findings = only(lint(doc), "missing-upstream-field")
        self.assertEqual(len(findings), 1)


# --------------------------------------------------------------------------
# credentials and secrets
# --------------------------------------------------------------------------


class TestCredentialReference(unittest.TestCase):
    def _doc(self, credentials: dict) -> dict:
        return workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Work", "n8n-nodes-base.slack", (0, 0),
                     {"select": "channel"}, credentials=credentials),
            ],
            connections_of({"Start": [["Work"]]}),
        )

    def test_name_only_reference_fires(self) -> None:
        findings = only(lint(self._doc({"slackApi": {"name": "Slack Bot"}})),
                        "credential-reference")
        self.assertEqual(len(findings), 1)
        self.assertIn("works on my machine", findings[0].message)
        self.assertEqual(findings[0].severity, "medium")

    def test_complete_reference_does_not_fire(self) -> None:
        doc = self._doc({"slackApi": {"id": "1", "name": "Slack Bot"}})
        self.assertNotIn("credential-reference", rules_of(lint(doc)))

    def test_empty_reference_fires(self) -> None:
        findings = only(lint(self._doc({"slackApi": {}})), "credential-reference")
        self.assertEqual(len(findings), 1)
        self.assertIn("empty", findings[0].message)

    def test_id_without_name_fires(self) -> None:
        findings = only(lint(self._doc({"slackApi": {"id": "7"}})),
                        "credential-reference")
        self.assertEqual(len(findings), 1)
        self.assertIn("no name", findings[0].message)

    def test_same_name_two_ids_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("A", "n8n-nodes-base.slack", (0, 0), {"select": "channel"},
                     credentials={"slackApi": {"id": "1", "name": "Slack Bot"}}),
                node("B", "n8n-nodes-base.slack", (200, 0), {"select": "channel"},
                     credentials={"slackApi": {"id": "9", "name": "Slack Bot"}}),
            ],
            connections_of({"Start": [["A"]], "A": [["B"]]}),
        )
        findings = only(lint(doc), "credential-reference")
        self.assertEqual(len(findings), 1)
        self.assertIn('"Slack Bot"', findings[0].message)
        self.assertIn("different ids", findings[0].message)

    def test_same_name_same_id_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("A", "n8n-nodes-base.slack", (0, 0), {"select": "channel"},
                     credentials={"slackApi": {"id": "1", "name": "Slack Bot"}}),
                node("B", "n8n-nodes-base.slack", (200, 0), {"select": "channel"},
                     credentials={"slackApi": {"id": "1", "name": "Slack Bot"}}),
            ],
            connections_of({"Start": [["A"]], "A": [["B"]]}),
        )
        self.assertNotIn("credential-reference", rules_of(lint(doc)))

    def test_no_credentials_does_not_fire(self) -> None:
        self.assertNotIn("credential-reference", rules_of(lint(simple_workflow())))


class TestHardcodedSecret(unittest.TestCase):
    def _doc(self, parameters: dict) -> dict:
        return workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                node("Call", "n8n-nodes-base.httpRequest", (0, 0), parameters),
            ],
            connections_of({"Start": [["Call"]]}),
        )

    def test_bearer_token_in_a_header_fires(self) -> None:
        doc = self._doc({
            "url": "https://api.example.com/v1",
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "Bearer n8n-lint-fixture-token-0001"},
            ]},
        })
        findings = only(lint(doc), "hardcoded-secret")
        self.assertGreaterEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")

    def test_api_key_query_parameter_fires(self) -> None:
        doc = self._doc({"url": "https://api.example.com/v1?api_key=n8n-lint-fixture-key-0001"})
        findings = only(lint(doc), "hardcoded-secret")
        self.assertEqual(len(findings), 1)
        self.assertIn("secret-shaped", findings[0].message)

    def test_x_api_key_header_fires(self) -> None:
        doc = self._doc({
            "url": "https://api.example.com/v1",
            "headerParameters": {"parameters": [
                {"name": "X-Api-Key", "value": "n8n-lint-fixture-key-0002"},
            ]},
        })
        findings = only(lint(doc), "hardcoded-secret")
        self.assertEqual(len(findings), 1)
        self.assertIn("X-Api-Key", findings[0].message)

    def test_slack_webhook_url_fires(self) -> None:
        # Assembled at runtime so no webhook-shaped literal sits in the repo:
        # a fake-but-well-formed URL in a source file trips secret scanners
        # on push, which is exactly the problem this rule exists to catch.
        fake_hook = (
            "https://hooks.slack.com"
            + "/services/"
            + "T00000000/B00000000/"
            + "0" * 24
        )
        doc = self._doc({"url": fake_hook})
        findings = only(lint(doc), "hardcoded-secret")
        self.assertEqual(len(findings), 1)
        self.assertIn("Slack", findings[0].message)

    def test_changeme_placeholder_is_not_reported(self) -> None:
        doc = self._doc({
            "url": "https://api.example.com/v1",
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "Bearer CHANGEME"},
            ]},
        })
        self.assertNotIn("hardcoded-secret", rules_of(lint(doc)))

    def test_environment_expression_is_not_reported(self) -> None:
        doc = self._doc({
            "url": "https://api.example.com/v1",
            "headerParameters": {"parameters": [
                {"name": "Authorization", "value": "=Bearer {{ $env.API_TOKEN }}"},
            ]},
        })
        self.assertNotIn("hardcoded-secret", rules_of(lint(doc)))

    def test_clean_request_does_not_fire(self) -> None:
        doc = self._doc({"url": "https://api.example.com/v1"})
        self.assertNotIn("hardcoded-secret", rules_of(lint(doc)))

    def test_redaction_hides_the_value(self) -> None:
        doc = self._doc({"url": "https://api.example.com/v1?api_key=n8n-lint-fixture-key-0001"})
        message = only(lint(doc), "hardcoded-secret")[0].message
        self.assertNotIn("n8n-lint-fixture-key-0001", message)
        self.assertIn("n8n-li", message)

    def test_raw_scanner_exempts_placeholders(self) -> None:
        self.assertTrue(is_placeholder("Bearer CHANGEME"))
        self.assertTrue(is_placeholder("{{ $env.TOKEN }}"))
        self.assertTrue(is_placeholder("<your-token>"))
        self.assertFalse(is_placeholder("n8n-lint-fixture-token-0001"))

    def test_raw_scanner_finds_a_bearer_header(self) -> None:
        hits = scan_parameters({"headers": [{"name": "Authorization",
                                             "value": "Bearer n8n-lint-fixture-token-0001"}]})
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].path, "parameters.headers[0].value")


# --------------------------------------------------------------------------
# sub-workflows
# --------------------------------------------------------------------------


class TestSubworkflowId(unittest.TestCase):
    def _caller(self, workflow_id: object, **extra: object) -> dict:
        return node(
            "Call Child",
            "n8n-nodes-base.executeWorkflow",
            (200, 0),
            {"workflowId": workflow_id, **extra},
        )

    def test_reference_to_a_missing_workflow_id_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({"__rl": True, "value": "wf-999", "mode": "list"}),
            ],
            connections_of({"Start": [["Call Child"]]}),
            id="wf-host",
        )
        findings = only(lint(doc), "subworkflow-id-not-in-file")
        self.assertEqual(len(findings), 1)
        self.assertIn("wf-999", findings[0].message)
        self.assertEqual(findings[0].severity, "medium")

    def test_reference_to_a_present_workflow_id_does_not_fire(self) -> None:
        child = workflow(
            [node("Child Work", position=(0, 0))],
            {},
            name="Child",
            id="wf-child",
        )
        parent = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({"__rl": True, "value": "wf-child", "mode": "list"}),
            ],
            connections_of({"Start": [["Call Child"]]}),
            name="Parent",
            id="wf-parent",
        )
        findings = lint([parent, child])
        self.assertNotIn("subworkflow-id-not-in-file", rules_of(findings))

    def test_missing_id_in_a_multi_workflow_file_fires(self) -> None:
        child = workflow([node("Child Work", position=(0, 0))], {}, name="Child", id="wf-child")
        parent = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({"__rl": True, "value": "wf-gone", "mode": "list"}),
            ],
            connections_of({"Start": [["Call Child"]]}),
            name="Parent",
            id="wf-parent",
        )
        findings = only(lint([parent, child]), "subworkflow-id-not-in-file")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].workflow, "Parent")

    def test_single_workflow_download_without_ids_is_not_judged(self) -> None:
        # A single-workflow export carries no workflow ids at all, so the
        # tool has no ground truth and stays silent.  Documented limitation.
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({"__rl": True, "value": "whatever", "mode": "list"}),
            ],
            connections_of({"Start": [["Call Child"]]}),
        )
        self.assertNotIn("subworkflow-id-not-in-file", rules_of(lint(doc)))

    def test_expression_id_is_not_judged(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller("={{ $json.workflow_id }}"),
            ],
            connections_of({"Start": [["Call Child"]]}),
            id="wf-parent",
        )
        self.assertNotIn("subworkflow-id-not-in-file", rules_of(lint(doc)))

    def test_inlined_workflow_parameter_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({}, source="parameter",
                             workflowJson={"nodes": [], "connections": {}}),
            ],
            connections_of({"Start": [["Call Child"]]}),
        )
        self.assertNotIn("subworkflow-id-not-in-file", rules_of(lint(doc)))

    def test_empty_inlined_workflow_fires(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (-200, 0), {"path": "h"}),
                self._caller({}, source="parameter"),
            ],
            connections_of({"Start": [["Call Child"]]}),
        )
        findings = only(lint(doc), "subworkflow-id-not-in-file")
        self.assertEqual(len(findings), 1)
        self.assertIn("workflowJson", findings[0].message)


# --------------------------------------------------------------------------
# webhooks, pinning, layout
# --------------------------------------------------------------------------


class TestDuplicateWebhookPath(unittest.TestCase):
    def _doc(self, first: dict, second: dict) -> dict:
        return workflow(
            [
                node("Hook A", "n8n-nodes-base.webhook", (0, 0), first),
                node("Hook B", "n8n-nodes-base.webhook", (0, 200), second),
                node("Sink", position=(200, 0)),
            ],
            connections_of({"Hook A": [["Sink"]], "Hook B": [["Sink"]]}),
        )

    def test_same_path_and_method_fires(self) -> None:
        doc = self._doc({"httpMethod": "POST", "path": "orders", "options": {}},
                        {"httpMethod": "POST", "path": "orders", "options": {}})
        findings = only(lint(doc), "duplicate-webhook-path")
        self.assertEqual(len(findings), 1)
        self.assertIn("POST /orders", findings[0].message)
        self.assertIn('"Hook A"', findings[0].message)
        self.assertEqual(findings[0].severity, "high")

    def test_same_path_different_method_does_not_fire(self) -> None:
        doc = self._doc({"httpMethod": "POST", "path": "orders", "options": {}},
                        {"httpMethod": "GET", "path": "orders", "options": {}})
        self.assertNotIn("duplicate-webhook-path", rules_of(lint(doc)))

    def test_leading_and_trailing_slashes_are_normalised(self) -> None:
        doc = self._doc({"httpMethod": "POST", "path": "/orders/", "options": {}},
                        {"httpMethod": "POST", "path": "orders", "options": {}})
        self.assertIn("duplicate-webhook-path", rules_of(lint(doc)))

    def test_disabled_duplicate_does_not_fire(self) -> None:
        doc = workflow(
            [
                node("Hook A", "n8n-nodes-base.webhook", (0, 0),
                     {"httpMethod": "POST", "path": "orders", "options": {}}),
                node("Hook B", "n8n-nodes-base.webhook", (0, 200),
                     {"httpMethod": "POST", "path": "orders", "options": {}},
                     disabled=True),
                node("Sink", position=(200, 0)),
            ],
            connections_of({"Hook A": [["Sink"]], "Hook B": [["Sink"]]}),
        )
        self.assertNotIn("duplicate-webhook-path", rules_of(lint(doc)))

    def test_different_paths_do_not_fire(self) -> None:
        doc = self._doc({"httpMethod": "POST", "path": "orders", "options": {}},
                        {"httpMethod": "POST", "path": "refunds", "options": {}})
        self.assertNotIn("duplicate-webhook-path", rules_of(lint(doc)))

    def test_expression_path_is_not_judged(self) -> None:
        doc = self._doc({"httpMethod": "POST", "path": "={{ $json.p }}", "options": {}},
                        {"httpMethod": "POST", "path": "={{ $json.p }}", "options": {}})
        self.assertNotIn("duplicate-webhook-path", rules_of(lint(doc)))

    def test_missing_method_defaults_to_get(self) -> None:
        doc = self._doc({"path": "orders", "options": {}},
                        {"httpMethod": "GET", "path": "orders", "options": {}})
        self.assertIn("duplicate-webhook-path", rules_of(lint(doc)))


class TestPinnedData(unittest.TestCase):
    def test_populated_pin_data_fires(self) -> None:
        doc = simple_workflow()
        doc["pinData"] = {"Work": [{"json": {"a": 1}}]}
        findings = only(lint(doc), "pinned-data")
        self.assertEqual(len(findings), 1)
        self.assertIn('"Work"', findings[0].message)
        self.assertEqual(findings[0].severity, "medium")

    def test_empty_pin_data_does_not_fire(self) -> None:
        doc = simple_workflow()
        doc["pinData"] = {}
        self.assertNotIn("pinned-data", rules_of(lint(doc)))

    def test_absent_pin_data_does_not_fire(self) -> None:
        self.assertNotIn("pinned-data", rules_of(lint(simple_workflow())))

    def test_pin_entry_with_no_items_does_not_fire(self) -> None:
        doc = simple_workflow()
        doc["pinData"] = {"Work": []}
        self.assertNotIn("pinned-data", rules_of(lint(doc)))


class TestOverlappingNodes(unittest.TestCase):
    def test_identical_positions_fire(self) -> None:
        doc = simple_workflow([
            node("Twin A", position=(200, 0)),
            node("Twin B", position=(200, 0)),
        ])
        doc["connections"]["Start"] = {
            "main": [[{"node": "Twin A", "type": "main", "index": 0},
                      {"node": "Twin B", "type": "main", "index": 0}]]
        }
        findings = only(lint(doc), "overlapping-nodes")
        self.assertEqual(len(findings), 1)
        self.assertIn("cosmetic", findings[0].message)
        self.assertEqual(findings[0].severity, "low")

    def test_different_positions_do_not_fire(self) -> None:
        self.assertNotIn("overlapping-nodes", rules_of(lint(simple_workflow())))

    def test_missing_position_does_not_crash_or_fire(self) -> None:
        doc = workflow(
            [
                node("Start", "n8n-nodes-base.webhook", (0, 0), {"path": "h"}),
                node("NoPos", position=None),
            ],
            connections_of({"Start": [["NoPos"]]}),
        )
        self.assertNotIn("overlapping-nodes", rules_of(lint(doc)))


# --------------------------------------------------------------------------
# supporting modules
# --------------------------------------------------------------------------


class TestTriggerDetection(unittest.TestCase):
    def test_trigger_like_types(self) -> None:
        for type_name in (
            "n8n-nodes-base.webhook",
            "n8n-nodes-base.scheduleTrigger",
            "n8n-nodes-base.manualTrigger",
            "n8n-nodes-base.errorTrigger",
            "n8n-nodes-base.executeWorkflowTrigger",
            "n8n-nodes-base.formTrigger",
            "n8n-nodes-base.emailReadImap",
            "n8n-nodes-base.cron",
            "n8n-nodes-base.interval",
            "@n8n/n8n-nodes-langchain.chatTrigger",
        ):
            with self.subTest(type_name=type_name):
                self.assertTrue(is_trigger_type(type_name))

    def test_non_trigger_types(self) -> None:
        for type_name in (
            "n8n-nodes-base.httpRequest",
            "n8n-nodes-base.set",
            "n8n-nodes-base.respondToWebhook",
            "n8n-nodes-base.noOp",
            "n8n-nodes-base.splitInBatches",
        ):
            with self.subTest(type_name=type_name):
                self.assertFalse(is_trigger_type(type_name))

    def test_short_type_strips_every_prefix_shape(self) -> None:
        self.assertEqual(short_type("n8n-nodes-base.splitInBatches"), "splitInBatches")
        self.assertEqual(
            short_type("@n8n/n8n-nodes-langchain.toolWorkflow"), "toolWorkflow"
        )
        self.assertEqual(short_type("n8n-nodes-mypkg.myNode"), "myNode")
        self.assertEqual(short_type("noDots"), "noDots")
        self.assertEqual(short_type(None), "")


class TestExpressionReads(unittest.TestCase):
    def test_dot_and_bracket_forms(self) -> None:
        self.assertEqual(find_json_reads("{{ $json.email }}"), {"email"})
        self.assertEqual(find_json_reads('{{ $json["email"] }}'), {"email"})
        self.assertEqual(find_json_reads("{{ $json['email'] }}"), {"email"})

    def test_item_json_form(self) -> None:
        self.assertEqual(find_json_reads("return item.json.order_id;"), {"order_id"})

    def test_nested_access_reads_only_the_first_segment(self) -> None:
        self.assertEqual(find_json_reads("{{ $json.customer.email }}"), {"customer"})

    def test_builtin_fields_are_ignored(self) -> None:
        self.assertEqual(find_json_reads("{{ $json.error }}"), set())
        self.assertEqual(find_json_reads("{{ $json.binary }}"), set())

    def test_jsonata_and_other_helpers_do_not_match(self) -> None:
        self.assertEqual(find_json_reads("{{ $jsonata('a') }}"), set())

    def test_plain_text_yields_nothing(self) -> None:
        self.assertEqual(find_json_reads("no expressions here"), set())

    def test_unrelated_json_local_does_not_match(self) -> None:
        self.assertEqual(find_json_reads("const x = response.json.data;"), set())


class TestOutputShape(unittest.TestCase):
    def _node(self, parameters: dict, type_name: str = "n8n-nodes-base.set") -> Node:
        return Node(index=0, raw={}, name="n", type=type_name, parameters=parameters)

    def test_set_v34_assignments(self) -> None:
        shape = describe_output(self._node({
            "mode": "manual",
            "includeOtherFields": False,
            "assignments": {"assignments": [{"name": "a"}, {"name": "b"}]},
        }))
        self.assertTrue(shape.is_known)
        self.assertEqual(shape.fields, frozenset({"a", "b"}))
        self.assertTrue(shape.resets)

    def test_set_v1_values_layout(self) -> None:
        shape = describe_output(self._node({
            "includeOtherFields": False,
            "values": {"string": [{"name": "a"}], "number": [{"name": "b"}]},
        }))
        self.assertEqual(shape.fields, frozenset({"a", "b"}))

    def test_set_fields_values_layout(self) -> None:
        shape = describe_output(self._node({
            "includeOtherFields": False,
            "fields": {"values": [{"name": "only"}]},
        }))
        self.assertEqual(shape.fields, frozenset({"only"}))

    def test_set_raw_json_mode(self) -> None:
        shape = describe_output(self._node({"mode": "raw", "jsonOutput": '{"k": 1}'}))
        self.assertTrue(shape.is_known)
        self.assertEqual(shape.fields, frozenset({"k"}))
        self.assertTrue(shape.resets)

    def test_set_raw_json_that_is_broken_is_unknown(self) -> None:
        shape = describe_output(self._node({"mode": "raw", "jsonOutput": "{oops"}))
        self.assertFalse(shape.is_known)

    def test_set_with_include_other_fields_is_unknown(self) -> None:
        shape = describe_output(self._node({
            "includeOtherFields": True,
            "assignments": {"assignments": [{"name": "a"}]},
        }))
        self.assertFalse(shape.is_known)

    def test_set_without_the_flag_is_unknown(self) -> None:
        shape = describe_output(self._node({
            "assignments": {"assignments": [{"name": "a"}]},
        }))
        self.assertFalse(shape.is_known)

    def test_unknown_node_types(self) -> None:
        shape = describe_output(self._node({}, "n8n-nodes-base.httpRequest"))
        self.assertFalse(shape.is_known)
        self.assertIn("httpRequest", shape.reason)

    def test_code_node_is_understood_from_literals(self) -> None:
        shape = describe_output(self._node(
            {"mode": "runOnceForAllItems",
             "jsCode": "return [{ json: { a: 1, b: 2 } }];"},
            "n8n-nodes-base.code",
        ))
        self.assertTrue(shape.is_known)
        self.assertEqual(shape.fields, frozenset({"a", "b"}))

    def test_code_node_that_returns_items_is_unknown(self) -> None:
        shape = describe_output(self._node(
            {"mode": "runOnceForAllItems", "jsCode": "return items;"},
            "n8n-nodes-base.code",
        ))
        self.assertFalse(shape.is_known)

    def test_code_node_with_a_spread_is_unknown(self) -> None:
        shape = describe_output(self._node(
            {"mode": "runOnceForAllItems",
             "jsCode": "return [{ json: { ...item.json, a: 1 } }];"},
            "n8n-nodes-base.code",
        ))
        self.assertFalse(shape.is_known)

    def test_code_node_with_a_variable_body_is_unknown(self) -> None:
        shape = describe_output(self._node(
            {"mode": "runOnceForAllItems", "jsCode": "const o = {}; return [{ json: o }];"},
            "n8n-nodes-base.code",
        ))
        self.assertFalse(shape.is_known)

    def test_literal_key_extraction_handles_nesting(self) -> None:
        keys, complete = extract_json_literal_keys(
            "return [{ json: { a: { deep: 1 }, 'b-c': 2 } }];"
        )
        self.assertTrue(complete)
        self.assertEqual(keys, {"a", "b-c"})

    def test_literal_key_extraction_reports_incompleteness(self) -> None:
        _, complete = extract_json_literal_keys("return [{ json: dynamic }];")
        self.assertFalse(complete)

    def test_is_field_reset_only_for_evidenced_nodes(self) -> None:
        self.assertTrue(is_field_reset(self._node({
            "includeOtherFields": False,
            "assignments": {"assignments": [{"name": "a"}]},
        })))
        self.assertTrue(is_field_reset(self._node({"keepOnlySet": True,
                                                  "values": {"string": [{"name": "a"}]}})))
        self.assertFalse(is_field_reset(self._node({
            "assignments": {"assignments": [{"name": "a"}]},
        })))
        self.assertFalse(is_field_reset(self._node({}, "n8n-nodes-base.if")))

    def test_passthrough_types(self) -> None:
        for type_name in ("if", "switch", "filter", "merge", "noOp",
                          "splitInBatches", "loopOverItems", "wait"):
            with self.subTest(type_name=type_name):
                self.assertTrue(
                    is_passthrough(self._node({}, f"n8n-nodes-base.{type_name}"))
                )
        self.assertFalse(
            is_passthrough(self._node({}, "n8n-nodes-base.httpRequest"))
        )


class TestSeverityFilter(unittest.TestCase):
    def test_thresholds(self) -> None:
        from n8nlint.rules import filter_findings

        findings = lint_file(BAD_EXAMPLE)
        high = filter_findings(findings, "high")
        medium = filter_findings(findings, "medium")
        low = filter_findings(findings, "low")
        self.assertTrue(all(f.severity == "high" for f in high))
        self.assertTrue(all(SEVERITY_RANK[f.severity] >= 2 for f in medium))
        self.assertTrue(all(SEVERITY_RANK[f.severity] >= 1 for f in low))
        self.assertGreater(len(low), len(medium))
        self.assertGreater(len(medium), len(high))

    def test_clean_file_stays_clean_at_every_threshold(self) -> None:
        from n8nlint.rules import filter_findings

        findings = lint_file(GOOD_EXAMPLE)
        for threshold in ("high", "medium", "low"):
            with self.subTest(threshold=threshold):
                self.assertEqual(filter_findings(findings, threshold), [])


class TestShippedExamples(unittest.TestCase):
    def test_bad_example_fires_the_expected_rules(self) -> None:
        fired = rules_of(lint_file(BAD_EXAMPLE))
        for rule in (
            "dangling-connection",
            "disabled-node-in-path",
            "duplicate-webhook-path",
            "hardcoded-secret",
            "unreachable-node",
            "continue-on-fail-unchecked",
            "credential-reference",
            "missing-upstream-field",
            "pinned-data",
            "unintended-cycle",
            "overlapping-nodes",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, fired)

    def test_bad_example_contains_no_real_looking_credentials(self) -> None:
        with open(BAD_EXAMPLE, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("n8n-lint-fixture", text)
        for forbidden in ("AKIA", "ghp_", "sk-live", "xoxb-", "BEGIN RSA"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_both_examples_are_valid_json_workflows(self) -> None:
        for path in (GOOD_EXAMPLE, BAD_EXAMPLE):
            with self.subTest(path=path):
                parsed = parse_text(
                    open(path, "r", encoding="utf-8").read(), path
                )
                self.assertTrue(parsed.is_workflow_shaped)
                self.assertEqual(len(parsed.workflows), 1)
                self.assertTrue(parsed.workflows[0].nodes)
                self.assertTrue(parsed.workflows[0].connections)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
