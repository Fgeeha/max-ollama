# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MAX-Ollama — an async bot for the [MAX](https://max.ru) messenger (built on `maxapi`) that proxies chat to a local Ollama server. Python 3.12, `uv`-managed, SQLAlchemy async ORM + Alembic, streamed responses via message edits. See `README.md` for the user-facing command list and `.env` configuration reference.

## Commands

| Task | Command |
|---|---|
| Install deps | `make install` (`uv sync`) |
| Run locally | `make run` (`uv run python -m bot.main`) |
| Lint | `make lint` (`ruff check src/` + `mypy src/`) |
| Format | `make format` (`black src/` + `ruff check --fix src/`) |
| Test | `make test` (`uv run pytest --cov=src/bot --cov-report=term-missing`) |
| Single test | `uv run pytest src/tests/test_context.py::test_name -v` |
| Full pre-commit check | `make check` (lint + test) |
| New migration | `make migration m="description"` (alembic autogenerate) |
| Apply migrations manually | `make migrate` (alembic upgrade head; normally automatic at startup) |
| Docker | `make build`, `make up-local` / `make down-local`, `make logs`, `make shell` |

CI (`.github/workflows/ci.yml`) runs `ruff check src/` and `pytest -q` with dummy `MAX_BOT_TOKEN`/`ADMIN_ID` env vars on push/PR to `Master`, then builds and pushes a Docker image to GHCR on `Master` and `v*` tags.

pytest is configured with `pythonpath = ["src"]` (`pyproject.toml`), so tests import as `from bot...`, not `from src.bot...`; `asyncio_mode = "auto"` means async tests need no marker.

## Architecture

- **Shared runtime, no per-update context.** MAX handlers only receive the update object — there is no request-scoped context object to carry state through. `bot/runtime.py` holds module-level singletons (`bot`, `dp`, `ollama_client`) that handlers import directly. `Dispatcher(use_create_task=True)` processes updates concurrently; without it, one slow generation would block the whole polling loop.

- **Handler registration order is dispatch priority.** MAX routes an update to the *first* handler that matches. `bot/handlers/__init__.py` imports submodules in a fixed order — commands, then image attachments, then the plain-text catch-all in `chat` — marked `# isort: skip_file` so an import sorter can't reorder it. A test pins this order; don't let it drift.

- **Two event shapes, one interface.** MAX delivers `MessageCreated` and `MessageCallback` (button presses), which store the sender and reply target differently. `bot/utils/events.py` normalizes both behind `AnyEvent` / `event_user_id` / `answer` / `answer_html` so handlers and decorators don't special-case event type.

- **Decorator stack for access control** (`bot/decorators.py`), applied in `bot/handlers/*`: `@admin_only`, `@authorized_only`, `@rate_limited`. Admin bypasses both authorization and rate limiting; order in the source matters, since the decorator closest to the function runs first.

- **Streaming replies via message edits.** `OllamaClient.chat_stream` (`bot/utils/ollama.py`) streams tokens from Ollama; `chat.py` sends the first reply early (~50 chars) and appends via `bot.edit_message`, throttled to avoid MAX's edit rate limit. One in-flight generation per user is tracked in `chat.py`'s `_generating: dict[user_id, Task]` — a second message from the same user is rejected rather than interleaved, and `/stop` cancels the tracked task.

- **Token-budgeted context, not character-budgeted.** `ConversationContext` (`bot/utils/context.py`) keeps an in-memory LRU cache (max 500 users, evicted oldest-first) backed by the `conversations` table as source of truth. Each turn drops the oldest messages until `estimate_messages_tokens(...)` fits `MAX_CONTEXT_TOKENS`, but never below the last 2 messages. `/clear` doesn't delete rows — it writes a marker (`settings` table, key `context_reset:<user_id>`) so `/history` still shows everything while the model only sees what's after the marker.

- **Migrations run automatically at startup.** `bot/database/migrate.py` inspects the DB: if tables exist but there's no Alembic revision (a DB created by the old `Base.metadata.create_all` path), it stamps the baseline revision first, then always runs `alembic upgrade head`. `make migrate` / `make migration` are for authoring and applying revisions manually, not required for normal operation.

- **`get_session()` (`bot/database/connection.py`) auto-commits/rolls back.** It commits on a clean exit from the `async with` block and rolls back on exception, so handlers should not call `session.commit()` themselves except to flush mid-block before further work in the same session.

- **Tests hit a real SQLite DB, not mocks.** The `db` fixture (`src/tests/conftest.py`) points the real connection module at a temp-file SQLite DB via `monkeypatch` and runs actual migrations. An autouse `clean_context` fixture resets `ConversationContext`'s class-level cache between tests since it's shared process-wide state.

# Ruflo — Claude Code Configuration

## Rules

- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary — prefer editing existing files
- NEVER create documentation files unless explicitly requested
- NEVER save working files or tests to root — use `/src`, `/tests`, `/docs`, `/config`, `/scripts`
- ALWAYS read a file before editing it
- NEVER commit secrets, credentials, or .env files
- NEVER add a `Co-Authored-By` trailer to user commits unless this project's `.claude/settings.json` has `attribution.commit` set (#2078). The Claude Code Bash tool may suggest one in its default commit-message template — ignore it. `Co-Authored-By` is semantic authorship attribution under git/GitHub convention; the tool is the facilitator, not a co-author.
- Keep files under 500 lines
- Validate input at system boundaries

## Ruflo Capability Brain & Implementation Loop

Ruflo is the coordination ledger and policy decision point. Claude Code is the
executor: after a Ruflo coordination call, continue implementing the task.

When it is registered, call
`guidance_brain({ mode: "recommend", task: "..." })` before complex Ruflo
work. Use its live registry instead of guessing tool names. Treat
`registered`, `configured`, `reachable`, `healthy`, and `authorized`
as separate facts. If the brain is unavailable, continue with the compatible
`guidance_recommend` tool, CLI discovery, and repository instructions.

Follow the returned loop:

1. Recall memory and ADR constraints.
2. Inspect source, runtime, dependencies, policy, and health.
3. Route to the smallest capable topology, agents, skills, and tools.
4. Plan acceptance criteria, safety envelope, ownership, and validation.
5. Execute in isolated scopes; the coding agent performs the work.
6. Test focused, regression, and failure paths.
7. Validate types, security, policy, compatibility, and artifacts.
8. Benchmark a source-bound candidate against a source-bound baseline.
9. Optimize measured bottlenecks without weakening safety.
10. Bind claims and evidence to exact source/build receipts.
11. Reconcile concurrent handoffs and disclose limitations.
12. Publish only through a separately authorized release gate.

### Concurrency and authority

- Never allow two writers in one worktree; give each writing agent an isolated
  worktree and explicit file ownership.
- Read-only research may run concurrently and report findings to the owner.
- Only the integration owner edits shared manifests and lockfiles or reconciles
  overlapping changes.
- A child may drop capabilities but cannot add tools, network, secrets, spend,
  concurrency, namespaces, or delegation depth.
- A lease or claim coordinates ownership; it does not authorize a side effect.
- Darwin, Flywheel, MetaHarness, memory, and neural systems may propose or
  evaluate candidates but cannot self-promote or expand their SafetyEnvelope.
- Bind tests, benchmarks, policy decisions, and release evidence to an exact
  commit or immutable dirty-worktree snapshot.

## Agent Comms (SendMessage-First Coordination)

Named agents coordinate via `SendMessage`, not polling or shared state.

```
Lead (you) ←→ architect ←→ developer ←→ tester ←→ reviewer
              (named agents message each other directly)
```

### Spawning a Coordinated Team

```javascript
// ALL agents in ONE message, each knows WHO to message next
Agent({ prompt: "Research the codebase. SendMessage findings to 'architect'.",
  subagent_type: "researcher", name: "researcher", run_in_background: true })
Agent({ prompt: "Wait for 'researcher'. Design solution. SendMessage to 'coder'.",
  subagent_type: "system-architect", name: "architect", run_in_background: true })
Agent({ prompt: "Wait for 'architect'. Implement it. SendMessage to 'tester'.",
  subagent_type: "coder", name: "coder", run_in_background: true })
Agent({ prompt: "Wait for 'coder'. Write tests. SendMessage results to 'reviewer'.",
  subagent_type: "tester", name: "tester", run_in_background: true })
Agent({ prompt: "Wait for 'tester'. Review code quality and security.",
  subagent_type: "reviewer", name: "reviewer", run_in_background: true })

// Kick off the pipeline
SendMessage({ to: "researcher", summary: "Start", message: "[task context]" })
```

### Patterns

| Pattern | Flow | Use When |
|---------|------|----------|
| **Pipeline** | A → B → C → D | Sequential dependencies (feature dev) |
| **Fan-out** | Lead → A, B, C → Lead | Independent parallel work (research) |
| **Supervisor** | Lead ↔ workers | Ongoing coordination (complex refactor) |

### Rules

- ALWAYS name agents — `name: "role"` makes them addressable
- ALWAYS include comms instructions in prompts — who to message, what to send
- Spawn ALL agents in ONE message with `run_in_background: true`
- After spawning, continue independent local work; wait only when a dependency
  genuinely blocks progress
- Do not poll repeatedly — agents message back or complete automatically
- Give every writing agent an isolated worktree and a non-overlapping file scope

## Swarm & Routing

### Config
- **Topology**: hierarchical-mesh (anti-drift)
- **Max Agents**: 15
- **Memory**: hybrid
- **HNSW**: Enabled
- **Neural**: Enabled

```bash
npx @claude-flow/cli@latest swarm init --topology hierarchical --max-agents 8 --strategy specialized
```

### Agent Routing

| Task | Agents | Topology |
|------|--------|----------|
| Bug Fix | researcher, coder, tester | hierarchical |
| Feature | architect, coder, tester, reviewer | hierarchical |
| Refactor | architect, coder, reviewer | hierarchical |
| Performance | perf-engineer, coder | hierarchical |
| Security | security-architect, auditor | hierarchical |

### When to Swarm
- **YES**: 3+ files, new features, cross-module refactoring, API changes, security, performance
- **NO**: single file edits, 1-2 line fixes, docs updates, config changes, questions

### 3-Tier Model Routing

| Tier | Handler | Use Cases |
|------|---------|-----------|
| 1 | Agent Booster (WASM) | Simple transforms — skip LLM, use Edit directly |
| 2 | Haiku | Simple tasks, low complexity |
| 3 | Sonnet/Opus | Architecture, security, complex reasoning |

## Memory & Learning

### Before Any Task
```bash
npx @claude-flow/cli@latest memory search --query "[task keywords]" --namespace patterns
npx @claude-flow/cli@latest hooks route --task "[task description]"
```

### After Success
```bash
npx @claude-flow/cli@latest memory store --namespace patterns --key "[name]" --value "[what worked]"
npx @claude-flow/cli@latest hooks post-task --task-id "[id]" --success true --store-results true
```

### MCP Tools (use `ToolSearch("keyword")` to discover)

| Category | Key Tools |
|----------|-----------|
| **Memory** | `memory_store`, `memory_search`, `memory_search_unified` |
| **Bridge** | `memory_import_claude`, `memory_bridge_status` |
| **Swarm** | `swarm_init`, `swarm_status`, `swarm_health` |
| **Agents** | `agent_spawn`, `agent_list`, `agent_status` |
| **Hooks** | `hooks_route`, `hooks_post-task`, `hooks_worker-dispatch` |
| **Security** | `aidefence_scan`, `aidefence_is_safe`, `aidefence_has_pii` |
| **Hive-Mind** | `hive-mind_init`, `hive-mind_consensus`, `hive-mind_spawn` |

### Background Workers

| Worker | When |
|--------|------|
| `audit` | After security changes |
| `optimize` | After performance work |
| `testgaps` | After adding features |
| `map` | Every 5+ file changes |
| `document` | After API changes |

```bash
npx @claude-flow/cli@latest hooks worker dispatch --trigger audit
```

## Agents

**Core**: `coder`, `reviewer`, `tester`, `planner`, `researcher`
**Architecture**: `system-architect`, `backend-dev`, `mobile-dev`
**Security**: `security-architect`, `security-auditor`
**Performance**: `performance-engineer`, `perf-analyzer`
**Coordination**: `hierarchical-coordinator`, `mesh-coordinator`, `adaptive-coordinator`
**GitHub**: `pr-manager`, `code-review-swarm`, `issue-tracker`, `release-manager`

Any string works as a custom agent type.

## Build & Test

- ALWAYS run tests after code changes
- ALWAYS verify build succeeds before committing

```bash
npm run build && npm test
```

## CLI Quick Reference

```bash
npx @claude-flow/cli@latest init --wizard           # Setup
npx @claude-flow/cli@latest swarm init --v3-mode     # Start swarm
npx @claude-flow/cli@latest memory search --query "" # Vector search
npx @claude-flow/cli@latest hooks route --task ""    # Route to agent
npx @claude-flow/cli@latest doctor --fix             # Diagnostics
npx @claude-flow/cli@latest security scan            # Security scan
npx @claude-flow/cli@latest performance benchmark    # Benchmarks
```

26 commands, 140+ subcommands. Use `--help` on any command for details.

## Setup

```bash
claude mcp add claude-flow -- npx -y ruflo@latest mcp start
npx ruflo@latest doctor --fix
```

> The background `daemon` is optional. It runs interval workers that each spawn
> a headless `claude` session, so it consumes tokens continuously. Start it only
> if you want those sweeps: `npx ruflo@latest daemon start` (self-stops after 12h
> by default; `--ttl 0` to disable, `daemon status --all` to audit running daemons).

**Agent tool** handles execution (agents, files, code, git). **MCP tools** handle coordination (swarm, memory, hooks). **CLI** is the same via Bash.
