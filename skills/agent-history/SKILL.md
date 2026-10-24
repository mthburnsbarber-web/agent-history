---
name: agent-history
description: Search earlier Claude Code and Codex sessions on this machine. Use when the user refers to past work ("what did we decide about…", "the session where we fixed…", "last week you said…", "find the command we used to…"), asks whether something was already done, or wants an old conversation restored. Requires the agent-history command.
---

# agent-history

Past sessions are searchable with the `agent-history` command. Check before answering from memory.

1. Refresh the index first: `agent-history index --quiet`
2. Search with words the user would have used. Narrow with filters:
   `agent-history search "rate limiter" --project api --since 2026-09-01 --limit 10 --json`
   - `--role user` finds what the user asked; `--role tool` finds commands that were run and their output.
   - FTS5 syntax works: `"exact phrase"`, `deploy OR release`, `migrat*`.
3. Read the matching session in order before drawing conclusions: `agent-history show <session id prefix>` (add `--tools` to see commands and results).
4. Quote what the transcript says, with its date and session id. A past assistant message saying something was done is a claim, not proof; check the current code or system before repeating it as fact.
5. To bring back a transcript Claude Code has deleted: `agent-history restore <session> <new file>`.

Results are redacted. Never try to recover a `[REDACTED]` value from the raw snapshots.
If the command is missing, tell the user: `pipx install git+https://github.com/mthburnsbarber-web/agent-history`.
