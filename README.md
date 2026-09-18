# n8n-workflow-lint

A **static** linter for exported n8n workflow JSON. Point it at a workflow
export and it tells you which mistakes will break the automation in
production, why each one matters, and how to fix it.

It is a single Python file plus a small package, standard library only,
nothing to install, no network, no n8n instance. It never runs your
workflow and never talks to n8n — it reads the JSON and reasons about it.

```
python3 n8n_workflow_lint.py examples/bad.json
```

```
examples/bad.json: 13 finding(s) (7 high, 5 medium, 1 low)
...
13 findings (7 high, 5 medium, 1 low) in 1 file
```

---

## What it catches

18 rules, each with a severity, the reason it matters and a concrete fix.

| Severity | Rule | What it means |
| --- | --- | --- |
| high | `invalid-workflow` | The file is JSON but not shaped like a workflow export |
| high | `node-missing-fields` | A node has no `name` or no `type` |
| high | `duplicate-node-name` | Two nodes share a name, so `connections` is ambiguous |
| high | `dangling-connection` | A connection points at a node that does not exist |
| high | `unreachable-node` | A node cannot be reached from any trigger — a silent no-op |
| high | `no-trigger` | No enabled trigger at all, so it can only be run by hand |
| high | `disabled-node-in-path` | A `disabled: true` node still wired into the flow |
| high | `hardcoded-secret` | A literal token, key, private key or webhook URL in the parameters |
| high | `duplicate-webhook-path` | Two enabled Webhook nodes share a path **and** method |
| medium | `empty-workflow` | An export with an empty `nodes` array |
| medium | `continue-on-fail-unchecked` | `continueOnFail` pushes error items down the happy path |
| medium | `missing-upstream-field` | An expression reads `$json.x` that no upstream node produces |
| medium | `credential-reference` | A credential reference that cannot be resolved |
| medium | `subworkflow-id-not-in-file` | `Execute Sub-workflow` points at a workflow id the file does not contain |
| medium | `unintended-cycle` | A connection cycle with no Split In Batches / Wait node |
| medium | `pinned-data` | The export ships pinned test data |
| low | `manual-trigger-only` | The only trigger is a Manual Trigger |
| low | `overlapping-nodes` | Two nodes sit on the exact same canvas position *(cosmetic)* |

`python3 n8n_workflow_lint.py --list-rules` prints the same list with the
full description of each rule.

---

## Usage

No install, no virtualenv, no dependencies. Python 3.9+ (developed and
tested on CPython 3.13.5).

```bash
# one export
python3 n8n_workflow_lint.py my-workflow.json

# several at once
python3 n8n_workflow_lint.py exports/*.json

# a whole directory, recursively (every *.json inside it)
python3 n8n_workflow_lint.py ./workflow-exports/

# only fail on things that actually break production
python3 n8n_workflow_lint.py my-workflow.json --severity high

# machine-readable, for CI annotations or an editor
python3 n8n_workflow_lint.py my-workflow.json --json

# one grep-able line per finding, and no output at all when clean
python3 n8n_workflow_lint.py my-workflow.json --quiet

# every rule, with what it catches and why it matters
python3 n8n_workflow_lint.py --list-rules
```

How to get an export: in the n8n editor open the workflow menu and choose
**Download**, or from the CLI:

```bash
n8n export:workflow --id=<workflow-id> --output=workflow.json
n8n export:workflow --all --output=all-workflows.json   # an array of workflows
```

### Options

| Flag | Meaning |
| --- | --- |
| `--json` | Emit a JSON report instead of text |
| `--severity {high,medium,low}` | Lowest severity to **report and to fail on**. Default `low` (everything) |
| `--quiet` | One line per finding, no banner and no summary; silent when clean |
| `--list-rules` | Print every rule and exit 0 |
| `--version` | Print the version and exit 0 |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Nothing at or above the severity threshold |
| `1` | At least one finding at or above the threshold |
| `2` | Usage error, unreadable path, or a file that is not valid JSON |

`2` always wins over `1`: if any file could not be parsed, the run exits `2`
even when other files produced findings, so a truncated export can never be
mistaken for a clean pass.

Because the default threshold is `low`, a cosmetic `overlapping-nodes`
finding also fails the run. For a CI gate that only breaks on real
breakage, use `--severity high` (or `--severity medium`).

---

## Example output

Both blocks below are the real, unedited output of the commands shown, run
against the two workflows in `examples/`.

### The messy example

`examples/bad.json` is a small, realistic "Stripe orders to Google Sheets"
workflow with eleven different problems planted in it.

```
$ python3 n8n_workflow_lint.py examples/bad.json; echo "exit=$?"
examples/bad.json: 13 finding(s) (7 high, 5 medium, 1 low)

  HIGH    dangling-connection          node "Normalise Order" · connections["Normalise Order"]
          `connections["Normalise Order"]` routes to a node called "Send
          Receipt", but `nodes` contains no node with that name. The branch
          stops here at runtime: everything downstream of "Normalise Order"
          never runs.
          fix: Point the connection at an existing node (check for a typo, a
          trailing space, or a node that was deleted) or remove the stale
          connection entry.

  HIGH    disabled-node-in-path        node "Log Failure" · nodes[5]
          "Log Failure" is disabled (`disabled: true`) but is still wired into
          the flow: it receives data from "Write To Sheets" and feeds "Notify
          Slack". A disabled node is skipped at runtime, and depending on your
          n8n version its input either passes straight through unchanged or
          produces no output at all -- so the nodes after it receive
          untransformed data or never run. Either way it is a silent behaviour
          change with no error in the execution log.
          fix: Re-enable the node, or delete it and rewire its neighbours
          directly to each other. If you are testing a replacement, do it on a
          copy of the workflow rather than by disabling a node in the live
          one.

  HIGH    duplicate-webhook-path       node "Stripe Webhook" · nodes[0], nodes[1]
          2 enabled Webhook nodes all listen on `POST /stripe-orders`: "Stripe
          Webhook", "Stripe Webhook Copy". An n8n instance registers one route
          per path and method, so at most one of these receives the request --
          the others silently never fire, and depending on the version
          activation can fail with a webhook-conflict error. (Assumed: a
          Webhook node with no explicit `httpMethod` defaults to GET.)
          fix: Give each Webhook node a distinct `path` (or a distinct
          `httpMethod` if they really are meant to share a URL), then
          re-export and re-register the production URL.

  HIGH    hardcoded-secret             node "Enrich Order" · nodes[3].parameters.headerParameters.parameters[0].value
          At `parameters.headerParameters.parameters[0].value` in "Enrich
          Order": a literal `Authorization: Bearer <token>` header value
          (value redacted as `n8n-li...****`). Anything inside a workflow
          export travels with the file into git, tickets, chat and backups, so
          the value is out of your control the moment the export is shared,
          and it cannot be rotated or scoped per environment.
          fix: Replace the literal with a credential: add an *Header Auth* /
          *Query Auth* / app credential in n8n, select it on the node, and let
          n8n inject the value. If you must keep it in the file, read it from
          the environment with `{{ $env.MY_TOKEN }}` and keep the value in
          n8n's environment, then rotate the exposed value.

  HIGH    hardcoded-secret             node "Enrich Order" · nodes[3].parameters.url
          At `parameters.url` in "Enrich Order": a secret-shaped `name=value`
          pair (value redacted as `n8n-li...****`). Anything inside a workflow
          export travels with the file into git, tickets, chat and backups, so
          the value is out of your control the moment the export is shared,
          and it cannot be rotated or scoped per environment.
          fix: Replace the literal with a credential: add an *Header Auth* /
          *Query Auth* / app credential in n8n, select it on the node, and let
          n8n inject the value. If you must keep it in the file, read it from
          the environment with `{{ $env.MY_TOKEN }}` and keep the value in
          n8n's environment, then rotate the exposed value.

  HIGH    unreachable-node             node "Archive Old Rows" · nodes[8]
          "Archive Old Rows" (n8n-nodes-base.httpRequest) cannot be reached
          from any trigger. No connection path leads into it, so it never
          executes -- a silent no-op that looks like part of the automation.
          fix: Connect it into the flow (check the `connections` block for a
          missing or mistyped source), or delete the node. If it is meant to
          be an independent entry point, give it its own trigger.

  HIGH    unreachable-node             node "Cleanup Duplicates" · nodes[9]
          "Cleanup Duplicates" (n8n-nodes-base.removeDuplicates) cannot be
          reached from any trigger. No connection path leads into it, so it
          never executes -- a silent no-op that looks like part of the
          automation.
          fix: Connect it into the flow (check the `connections` block for a
          missing or mistyped source), or delete the node. If it is meant to
          be an independent entry point, give it its own trigger.

  MEDIUM  continue-on-fail-unchecked   node "Enrich Order" · nodes[3]
          "Enrich Order" is configured to keep going when it fails
          (`continueOnFail: true`). n8n then emits an item that carries an
          `error` field and pushes it onto the *normal* output, so "Write To
          Sheets" receives that error item and treats it as real data -- an
          automation that reports success while doing nothing. None of the
          nodes directly after "Enrich Order" mentions `error`, so nothing
          here checks for it.
          fix: Either let the node fail loudly (`continueOnFail: false` /
          `onError: "stopWorkflow"`), or insert an IF node testing `{{
          $json.error }}` between "Enrich Order" and its consumers and route
          the failure somewhere you will see it (an alert channel, or an Error
          Trigger workflow).

  MEDIUM  credential-reference         node "Enrich Order" · nodes[3].credentials.httpHeaderAuth
          "Enrich Order" references the `httpHeaderAuth` credential by name
          only ("Enrichment API") -- there is no `id`. This file contains no
          credential definition for it, and n8n resolves credential references
          by id, not by name. A name-only reference is the usual cause of
          "works on my machine": on another instance the node binds to a
          different credential with the same name, or to nothing at all.
          fix: Re-select the credential in the node in the n8n editor so n8n
          writes an `{id, name}` pair, then re-export. Document the
          credentials the workflow expects in the workflow's `notes`.

  MEDIUM  missing-upstream-field       node "Enrich Order" · nodes[3].parameters.jsonBody
          "Enrich Order" reads `$json.customer_email` (at
          `parameters.jsonBody`), but every node that can still contribute
          fields at that point only produces: "order_id" (from "Normalise
          Order"). This is reported as a **warning, not an error**: field
          names are matched statically, n8n evaluates expressions at runtime,
          and this tool does not know your n8n version's node schemas or what
          an upstream HTTP/Code node really returns. It only fires when
          *every* producer upstream is statically knowable, which is why a
          workflow that touches an API will not produce this finding.
          fix: Add `customer_email` to the node that builds the item
          ("Normalise Order"), stop that node from dropping the other input
          fields, or fix the expression to read a field that is actually
          there.

  MEDIUM  pinned-data                  node "Normalise Order" · pinData
          This export ships pinned test data for 1 node(s): "Normalise Order".
          Pinned data is developer scaffolding, not part of the automation,
          but it travels with the file. The next person to import this
          workflow sees made-up values waiting in those nodes and can easily
          conclude the automation works. (Whether a pinned value can also
          reach a non-manual execution depends on your n8n version and how the
          run is started -- this tool cannot check that.)
          fix: Unpin the nodes in the editor (right-click the node -> *Unpin*)
          or delete the `pinData` key from the exported JSON before committing
          or sharing it.

  MEDIUM  unintended-cycle             connections
          The connections form a loop between "Notify Slack", "Retry Logic":
          following the outputs from any of them leads back to itself. n8n
          loops on purpose only through a Split In Batches / Loop Over Items
          node (or a Wait node for polling), and none of these nodes is one.
          The tool cannot prove intent from an export -- if you are closing
          the loop deliberately and rely on node-level error handling or
          `alwaysOutputData` to break out, this finding is expected and can be
          ignored.
          fix: Add the batching node the loop was meant to have (feed the loop
          output back into a Split In Batches / Loop Over Items node), or
          delete the connection that closes the cycle. Activate the workflow
          on a copy first and watch one execution.

  LOW     overlapping-nodes            node "Archive Old Rows" · nodes[8], nodes[9]
          2 nodes sit on exactly the same canvas position ([220, 420]):
          "Archive Old Rows", "Cleanup Duplicates". This is purely cosmetic --
          it changes nothing about how the workflow executes -- but stacked
          nodes are hard to select, easy to rewire by accident and usually a
          sign of a half-finished edit.
          fix: Drag the nodes apart in the n8n canvas and re-export. Nothing
          else depends on the position values.

13 findings (7 high, 5 medium, 1 low) in 1 file
exit=1
```

### The clean example

`examples/good.json` is the negative control: a well-formed
"invoice received" workflow — Webhook, Set, IF with two branches, HTTP
Request with a properly referenced credential. It must produce **zero**
findings, and it does.

```
$ python3 n8n_workflow_lint.py examples/good.json; echo "exit=$?"
examples/good.json: clean
0 findings (none) in 1 file
exit=0
```

A workflow with no findings at the default threshold exits `0`. That is
asserted by the test suite (`TestNegativeControl`), not just observed here.

### JSON output

```json
{
  "tool": "n8n-workflow-lint",
  "version": "1.0.0",
  "threshold": "low",
  "summary": {
    "findings": 13,
    "high": 7,
    "medium": 5,
    "low": 1,
    "files": 1,
    "parse_errors": 0
  },
  "files": [ { "path": "examples/bad.json", "workflows": 1, "findings": 13 } ],
  "errors": [],
  "findings": [
    {
      "rule": "dangling-connection",
      "severity": "high",
      "message": "`connections[\"Normalise Order\"]` routes to a node called ...",
      "fix": "Point the connection at an existing node ...",
      "node": "Normalise Order",
      "location": "connections[\"Normalise Order\"]",
      "workflow": null,
      "file": "examples/bad.json"
    }
  ]
}
```

---

## Assumed export format

Everything below was written from hand-built exports; the linter assumes
exactly this shape and nothing more. Fields it never reads are listed at
the end of this section.

### Top level

Either a **single workflow object** (what the editor's *Download* gives you):

```json
{
  "name": "My workflow",
  "nodes": [ ... ],
  "connections": { ... },
  "pinData": { ... },
  "settings": { "executionOrder": "v1" },
  "active": false,
  "versionId": "…",
  "meta": { "templateCredsSetupCompleted": false },
  "tags": []
}
```

or an **array of workflow objects** (what `n8n export:workflow --all`
gives you), where each element additionally carries an `id`:

```json
[ { "id": "wf-1", "name": "Caller", "nodes": [ ... ], "connections": { ... } },
  { "id": "wf-2", "name": "Child",  "nodes": [ ... ], "connections": { ... } } ]
```

Only `nodes` and `connections` are required for the linter to do anything.
An object with neither is reported as `invalid-workflow` when you name the
file explicitly; during a directory scan such a file is skipped and counted
in the summary as *not a workflow export*.

### `nodes` entries

```json
{
  "parameters": { },
  "id": "0f1a2b3c-…",
  "name": "Normalise Order",
  "type": "n8n-nodes-base.set",
  "typeVersion": 3.4,
  "position": [220, 0],
  "webhookId": "…",
  "credentials": { "httpHeaderAuth": { "id": "42", "name": "Accounting API" } },
  "disabled": false,
  "continueOnFail": false,
  "onError": "stopWorkflow",
  "alwaysOutputData": false,
  "notes": "",
  "notesInFlow": false
}
```

* `name` is the identity used by `connections` — the linter treats it as
  unique and required.
* `type` is the dotted type name: `n8n-nodes-base.set` for core nodes,
  `@n8n/n8n-nodes-langchain.agent` for LangChain nodes, anything else for
  community nodes. The linter only ever compares the part after the last
  dot.
* `credentials` holds **references only**. An n8n export never contains
  credential definitions, which is why the linter checks that a reference is
  internally complete rather than that the credential exists. See
  `credential-reference` in *What this does not do*.

### `connections`

```json
{
  "Source Node": {
    "main": [
      [ { "node": "Target A", "type": "main", "index": 0 } ],
      [ { "node": "Target B", "type": "main", "index": 0 } ]
    ]
  }
}
```

The outer array under `main` is the source node's **outputs, in order**
(an IF node has two); each inner array is the list of nodes that output
feeds. Other output types (`ai_tool`, `ai_languageModel`, `ai_memory`, …)
are recognised as connections too, but are excluded from cycle detection
and treated as undirected for reachability.

### `pinData`

```json
{ "Normalise Order": [ { "json": { "order_id": "TEST-ORDER-1" } } ] }
```

Any node whose entry is a non-empty list produces a `pinned-data` finding;
`pinData: {}` is the normal empty value and is ignored.

### Fields the linter ignores

`name`, `active`, `settings`, `meta`, `versionId`, `tags`, `createdAt`,
`updatedAt`, and any node field not listed above (`retryOnFail`,
`maxTries`, `notes`, `notesInFlow`, …). None of them affect a rule.

### Judgement calls and assumptions

These are the places where the format does not settle the question, so the
linter picks a rule and says so out loud. Where a guess could produce a
false positive, the linter stays silent instead of guessing.

1. **Trigger detection is a name heuristic, not a node catalogue.** A node
   counts as a trigger when its type's last dot-segment ends in `Trigger`
   (case-insensitive), or is one of `webhook`, `cron`, `interval`,
   `emailReadImap`, `rssFeedRead`. This covers every core trigger and every
   app trigger (`slackTrigger`, `scheduleTrigger`, `manualTrigger`,
   `executeWorkflowTrigger`, `errorTrigger`, …) without shipping a copy of
   n8n's node definitions. A community trigger whose name does not end in
   `Trigger` will be missed.
2. **Loop intent.** A connection cycle is reported *unless* it contains a
   node of type `splitInBatches`, `loopOverItems` or `wait`. The linter does
   not check which output of those nodes is used, and it cannot prove
   intent either way — the finding says so.
3. **Pass-through nodes.** `if`, `switch`, `filter`, `merge`, `noOp`,
   `splitInBatches`, `loopOverItems`, `wait`, `removeDuplicates`, `sort`,
   `limit`, `stopAndError` and `respondToWebhook` are assumed to forward
   their input items with the same fields (they may drop or reorder items,
   but not reshape them). This only affects the `missing-upstream-field`
   walk.
4. **Only `Set` and `Code` have a readable output shape.** Everything else
   — every trigger, every HTTP/app node — is treated as *unknown*, and any
   unknown node upstream of an expression read suppresses the finding. This
   is deliberately conservative: it is why a workflow that talks to an API
   will rarely produce `missing-upstream-field`.
5. **`includeOtherFields`.** A `Set` node only counts as *replacing* the
   item when the export states it: `includeOtherFields: false`, or (older
   layouts) `keepOnlySet: true`, or `mode: "raw"`. When the flag is absent
   the linter cannot tell what your n8n version defaults to, so it stays
   quiet and keeps walking upstream.
6. **`Code` nodes** are read only from literal `json: { … }` object
   literals, and only when *every* `json:` in the body is such a literal.
   A spread (`...item.json`), a variable, a helper call or `return items`
   makes the node unknown.
7. **`continueOnFail`.** `continueOnFail: true` and
   `onError: "continueRegularOutput"` are treated as "the error item goes
   to the normal output". `onError: "continueErrorOutput"` (error routed to
   a second output) is a different mechanism and is **not** covered.
   Whether a downstream node "assumes success" is decided by whether any
   node directly after it mentions `error` anywhere in its parameters.
8. **Webhook methods.** Two Webhooks with the same path but different
   `httpMethod` values are *not* reported, because that is a legitimate way
   to serve one URL. A Webhook with no `httpMethod` is assumed to be `GET`.
   Paths are compared with leading and trailing slashes stripped, and
   expression paths (`{{ … }}`) are skipped.
9. **Field names are matched exactly**, case-sensitively, and only the
   first segment of a read is considered: `$json.customer.email` is a read
   of `customer`. `json`, `binary`, `pairedItem`, `index` and `error` are
   treated as item properties rather than data fields.
10. **Sub-workflow ids** are only checked when the file actually declares
    workflow ids (an array export, or an object with an `id`). A
    single-workflow editor download carries none, so the linter has no
    ground truth and says nothing rather than guessing.
11. **Pinned data.** The finding describes pinned data as scaffolding that
    travels with the file. Whether a pinned value can also reach a
    non-manual execution depends on your n8n version and how the run is
    started; the linter does not claim to know, and the message says so.

---

## What this does not do

Being explicit about the limits, because a linter that oversells itself
gets ignored:

* **It does not run n8n and it cannot run n8n.** It never executes a
  workflow, never starts a container, never contacts an instance and never
  makes a network request. Everything it says comes from the JSON on disk.
  It cannot tell you whether a workflow *works*, only whether it is
  *internally consistent and free of the well-known footguns*.
* **It does not know your n8n version's node schemas.** There is no copy of
  the node catalogue in here. It does not know which parameters a given
  node requires, which credentials it needs, which outputs it has, or what
  its output items look like. That is why the trigger check is a name
  heuristic and why `missing-upstream-field` only fires when every producer
  upstream is statically knowable.
* **It cannot evaluate expressions at runtime.** `{{ … }}` is text to this
  tool. It does not run JavaScript, does not resolve `$node[...]`,
  `$items()`, `$env`, `$vars`, `$now` or `$workflow`, and does not know
  what a Webhook body or an HTTP response will contain. Reads through
  anything but `$json` / `item.json` are invisible to it.
* **It cannot verify that a credential exists.** An n8n export contains
  credential *references*, never definitions. The linter can only check
  that a reference is internally complete (has both an `id` and a `name`,
  and is used consistently) — not that the credential is present on the
  instance you are importing into.
* **It does not prove intent.** A cycle, a disabled node or a
  `continueOnFail` may all be deliberate. Those findings say so, and they
  are the ones to ignore when you know why they are there.
* **It is not a schema validator.** It does not check `typeVersion`
  validity, parameter names, operator strings or node-specific required
  fields.
* **It does not know which secrets are real.** The secret patterns match
  published credential shapes and `key=value` pairs. A secret in an unusual
  format will be missed, and the tool only ever prints a redacted prefix of
  what it found.
* **Findings are not proof.** Every rule is a heuristic over one export.
  Treat the output as a review checklist, not as a verdict. False negatives
  are expected; so is the occasional false positive on an exotic
  hand-edited export.

### Rules considered and deliberately not implemented

Two of the suggested checks were dropped rather than shipped as padding:

* **"workflow has no `notes`"** — a note is a documentation preference, not
  a production failure, and flagging every un-annotated workflow would
  bury the findings that matter.
* **"very large `Code` node body"** — a long Code node is a maintainability
  smell, and its right threshold is a matter of taste. It does not break a
  workflow, so it does not belong in a linter whose findings are supposed
  to be actionable.

`onError: "continueErrorOutput"` with an unconnected error output is a real
footgun, but detecting it correctly needs per-node output counts, which
needs node schemas this tool does not have. It is left out on purpose.

---

## Tests

```bash
python3 -m unittest discover -s tests -v     # the documented command
python3 run_tests.py                         # same suite, quieter output
python3 run_tests.py -v                      # same suite, verbose output
```

Current state on CPython 3.13.5:

```
ran 191 tests (871 assertions): 191 passed, 0 failed, 0 errored, 0 skipped
```

What the suite covers:

* **A negative control.** `examples/good.json` must produce *zero* findings
  at the default threshold — asserted with `assertEqual(findings, [])`, not
  "few". The same is asserted for three hand-built clean workflows.
* **Every rule**, including negative cases (a fixture that must *not* trip
  the rule) and the suppression rules: an opaque node upstream suppressing
  `missing-upstream-field`, a downstream `error` check suppressing
  `continue-on-fail-unchecked`, a `Split In Batches` cycle not being
  reported, a disabled or expression-path Webhook not counting towards
  `duplicate-webhook-path`.
* **A coverage guard.** `tests/test_coverage.py` lints a kitchen-sink
  export containing one broken workflow per rule and fails if any rule in
  the registry was never triggered, or if any rule id appears that is not
  in the registry.
* **Malformed input and edge cases**: broken JSON (exit `2`), valid JSON
  that is not a workflow, an empty `nodes` array, a missing `position`, a
  node with no name or no type, duplicate node names, a missing source key
  in `connections`, an unreachable node, a cycle.
* **The CLI end to end**, in a subprocess: every documented exit code
  (`0`, `1`, `2`), `--json`, `--quiet`, `--severity` at all three levels,
  `--list-rules`, `--version`, directory scanning, path handling, and that
  output is deterministic.
* The suite never writes to `/tmp` (which silently swallows writes in this
  environment) and never leaves files behind: scratch directories live
  under the project and are removed in teardown.

---

## Layout

```
n8n_workflow_lint.py      runnable entry point (no install needed)
n8nlint/
  model.py                parsing, node/connection model, graph traversal
  rules.py                the rule registry and every check
  fields.py               what fields a Set/Code node can be shown to emit
  expressions.py          finding $json reads inside parameters
  secrets.py              hardcoded-credential patterns
  report.py               text and JSON rendering
  cli.py                  argument parsing and exit codes
tests/                    unittest suite (no third-party packages)
examples/good.json        clean workflow (negative control)
examples/bad.json         workflow with eleven planted problems
```

---

## Licence

MIT. See [LICENSE](LICENSE).

```
MIT License

Copyright (c) 2026 duke5am

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

→ **n8n Workflow Automation Pack**: <!-- GUMROAD-LINK -->