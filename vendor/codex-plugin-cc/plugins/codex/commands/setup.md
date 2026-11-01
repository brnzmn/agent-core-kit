---
description: Check whether the local Codex CLI is ready and optionally toggle the stop-time review gate
argument-hint: '[--enable-review-gate|--disable-review-gate]'
disable-model-invocation: true
allowed-tools: Bash(node:*), AskUserQuestion
---

Run:

```bash
node "${CLAUDE_PLUGIN_ROOT}/scripts/codex-companion.mjs" setup --json $ARGUMENTS
```

If the result says Codex is unavailable:
- Do not install it. Codex is installed and updated outside this plugin; say so with the setup output.

Output rules:
- Present the final setup output to the user.
- If Codex is installed but not authenticated, preserve the guidance to run `!codex login`.
