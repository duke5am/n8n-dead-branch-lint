"""n8n-workflow-lint -- a static linter for exported n8n workflow JSON.

Pure standard library.  Nothing here executes a workflow, contacts an n8n
instance, or evaluates an expression: the whole tool is a static reader of
the JSON that n8n writes when you export a workflow.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
