# agent-history

Keep and search every Claude Code and Codex conversation on your machine.

```
$ agent-history index
3 transcript files: 3 indexed, 0 unchanged.
15 messages from 3 sources. Store: ~/.agent-history (0.0s)

$ agent-history search "rate limiter"
2026-09-02 04:00  codex     01a1c0de  user             ~/code/api
    Why is the [rate] [limiter] returning 429 for every request?

$ agent-history search login --tool claude --limit 2
2026-09-01 06:00  claude    7f3c2a10  tool/tool_result ~/code/shop
    src/[Login].tsx:12: onClick={handleSubmitSafari}

2026-09-01 06:00  claude    7f3c2a10  user             ~/code/shop
    The [login] button does nothing on Safari

$ agent-history sessions
2026-09-02 04:02  codex     01a1c0de      6 msgs  ~/code/api
    Why is the rate limiter returning 429 for every request?
2026-09-01 06:02  claude    7f3c2a10      5 msgs  ~/code/shop
    Fix the Safari login button
2025-09-01 05:46  claude    0ld5e551      1 msg   ~/code/shop
    deploy the shop to staging
```

## Why

Claude Code keeps each session's full transcript in `~/.claude/projects/<project>/<session>.jsonl`, and [deletes it after 30 days by default](https://code.claude.com/docs/en/claude-directory#cleaned-up-automatically). That transcript is the only record of what you asked, what the agent ran, and what it said it did. Codex keeps its own rollouts in `~/.codex/sessions`. Neither tool lets you search across all of it.

`agent-history index` copies each transcript's exact bytes into a private store before the cleanup gets to it, and builds a full-text index. When the source is deleted, the conversation stays searchable and can be restored byte for byte:

```
$ rm ~/.claude/projects/-Users-you-code-shop/7f3c2a10-1111-4c4c-9a9a-000000000001.jsonl   # what the 30-day cleanup does
$ agent-history index
2 transcript files: 0 indexed, 2 unchanged.
15 messages from 3 sources, 1 kept after their source was deleted. Store: ~/.agent-history (0.0s)

$ agent-history sessions --tool claude
2026-09-01 06:02  claude    7f3c2a10      5 msgs  ~/code/shop  [source deleted, kept here]
    Fix the Safari login button
2025-09-01 05:46  claude    0ld5e551      1 msg   ~/code/shop
    deploy the shop to staging
$ agent-history restore 7f3c2a10 shop-session.jsonl
Restored ~/.claude/projects/-Users-you-code-shop/7f3c2a10-1111-4c4c-9a9a-000000000001.jsonl
      to shop-session.jsonl
  sha256 ccb55c6a14bc8f7e723fba6eda12f85bec58a432b136bdd5e0ea4854ff97cdfb (verified)
```

Transcripts are plain text, and anything a tool printed is in them, including keys. The index never stores them:

```
$ agent-history show 7f3c2a10 --role user
── USER  2026-09-01 06:00
The login button does nothing on Safari

── USER  2026-09-01 06:02
Use key [REDACTED] and [REDACTED], password=[REDACTED], db postgres://app:[REDACTED]@db.internal/app
```

## Install

Python 3.9 or newer. No dependencies.

```
pipx install git+https://github.com/mthburnsbarber-web/agent-history
```

or `uv tool install git+https://github.com/mthburnsbarber-web/agent-history`, or download the one file and run it: `python3 agent_history.py index`.

## Usage

| Command | Does |
|---|---|
| `agent-history index` | Snapshot and index every transcript it finds. Incremental: unchanged files are skipped by size and modification time. Run it as often as you like. |
| `agent-history search <query>` | Full-text search, newest first. SQLite FTS5 syntax works: `"exact phrase"`, `deploy OR release`, `migrat*`. Words FTS5 would reject, such as `wow-ui` or `src/app.ts`, are quoted for you. |
| `agent-history sessions` | Sessions, newest first, with their title (the one you set in Claude Code, or the first prompt), project and message count. |
| `agent-history show <session>` | One conversation in order. A unique prefix of the id is enough. `--tools` adds tool calls and results, `--full` stops long messages being shortened. |
| `agent-history restore <session or path> <file>` | Writes the transcript's exact original bytes to a new file and verifies the SHA-256. It never overwrites. |
| `agent-history import <conversations.json>` | Indexes an official Claude.ai or ChatGPT data export, so web chats are searchable beside your agent sessions. |
| `agent-history stats` | Sessions and messages per tool, transcripts kept after deletion, store size. |

Filters for `search`, `sessions` and `show`: `--tool claude|codex|claude.ai|chatgpt`, `--project <text>`, `--since 2026-09-01`, `--role user|assistant|tool`, `--session <prefix>`, `--limit n`, `--json` (one JSON object per line).

`index` options: `--source <file or folder>` adds transcripts from somewhere else (`--tool codex` if they are Codex rollouts), `--no-snapshots` indexes without keeping raw copies, `--force` re-reads everything.

Exit codes: `0` found something, `1` nothing found or an error, `2` usage error.

### What it reads

| Source | Path |
|---|---|
| Claude Code sessions and subagents | `~/.claude/projects/**/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects`) |
| Claude Code prompt history | `~/.claude/history.jsonl`, which the cleanup leaves alone, so it can remember sessions whose transcripts are long gone |
| Claude desktop agent sessions | `~/Library/Application Support/Claude/local-agent-mode-sessions` on macOS |
| Codex sessions | `~/.codex/sessions` and `~/.codex/archived_sessions` (or `$CODEX_HOME`) |
| Codex prompt history | `~/.codex/history.jsonl` |

It indexes what you and the agent said, tool calls (the command or arguments) and tool results, capped at 20,000 characters each in the index; the snapshot keeps everything. It skips thinking blocks, reasoning items and images. Text that the tools inject into your turn, such as `AGENTS.md` instructions, environment blocks and system reminders, is indexed as `system` and hidden from search unless you pass `--all-roles`. Codex writes some messages twice; the copies are paired one to one, so a message you really sent twice still shows twice.

## Keep it running

Index on a schedule so nothing ages out. Unchanged files are skipped, so running it often is cheap.

cron (macOS and Linux), hourly:

```
0 * * * * agent-history index --quiet
```

Or have Claude Code index at the start of every session, in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [{ "type": "command", "command": "agent-history index --quiet >/dev/null 2>&1 &" }]
      }
    ]
  }
}
```

## For your agent

[`skills/agent-history/SKILL.md`](skills/agent-history/SKILL.md) is a Claude Code skill that teaches the agent to look up earlier sessions when you say things like "what did we decide about the rate limiter" or "the session where we fixed Safari". Install it with:

```
mkdir -p ~/.claude/skills/agent-history && curl -o ~/.claude/skills/agent-history/SKILL.md https://raw.githubusercontent.com/mthburnsbarber-web/agent-history/main/skills/agent-history/SKILL.md
```

## Privacy

Everything stays on your machine. Nothing is uploaded and there is no telemetry.

- The store is `~/.agent-history` (or `$AGENT_HISTORY_HOME`), created with owner-only permissions (`0700` folder, `0600` files).
- The search index holds redacted text. API keys (Anthropic, OpenAI, GitHub, AWS, Google, Slack, Stripe), bearer tokens, JWTs, private keys, `password=` style assignments, passwords in URLs and signed URLs are replaced with `[REDACTED]` before they are written. Results are redacted again on the way out.
- The snapshots are exact copies of the originals, so they contain whatever the originals contain. That is the point of a snapshot. If you don't want raw copies, use `--no-snapshots`. Don't sync the store to shared or cloud storage.
- Source transcripts are only read, never changed. `restore` refuses to write over an existing file.

## What it doesn't do

- It doesn't recover transcripts that were deleted before the first `index`. The prompt history files often still have your side of those sessions; `sessions` lists them.
- It doesn't index cloud-only sessions (Claude Code on the web, Codex cloud tasks). Use `import` for official Claude.ai and ChatGPT exports.
- Redaction matches known secret shapes. It will not catch a password with no label in plain prose. Treat the index as sensitive anyway.
- It reads each transcript into memory to snapshot it. A multi-gigabyte single transcript needs that much memory.

## License

MIT
