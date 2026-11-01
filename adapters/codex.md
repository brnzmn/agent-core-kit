
## Codex
- This file is generated from {{KIT}} (local/AGENTS.md, adapters/codex.md, local/codex.md). Direct edits are overwritten on the next sync; change the source instead.
- Codex reads instruction files only from the git root down, so each repo's AGENTS.md has to stand on its own.
- When {{USER}} corrects you or you hit a mistake worth not repeating, log it: `python3 {{KIT}}/bin/learn.py add "<what went wrong> -> <rule that prevents it>"`. {{USER}} reviews these in Claude Code with /learn review.
- Approval and sandbox settings in ~/.codex/config.toml are the real guardrails. Prose here doesn't replace them.
- Write every update and agent-to-agent message in normally spaced prose; concise means fewer words, never words, numbers or identifiers run together.
