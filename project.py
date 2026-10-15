"""What the prompts say about the reviewed project: the only project-specific prompt text.

It comes from `MR_AGENT_PROJECT`, set in the project's CI: a few lines on its layout and on
the shared state that two adjacent releases must both keep working with. Unset, the prompts
describe no project.
"""

import os

PROJECT = os.environ.get("MR_AGENT_PROJECT", "").strip()
