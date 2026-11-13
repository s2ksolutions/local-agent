# Local Agent 0.2.5 — Ollama auto-start

## Ollama auto-start (new in 0.2.5)

- In managed mode, the watchdog now starts an owned Ollama server automatically when none is running (`autoStart`, default on; `autoStartDelay`, default 15s of unreachable). Fresh installs default to managed mode; existing installs get `autoStart` added on upgrade — switch mode to `managed` in Runtime health → Watchdog settings to enable it.
- Ownership rules are unchanged: a reachable external server is never killed or taken over; the owned server binds the configured localhost endpoint; logs go to `ollama-managed.log` in the data directory.
- Auto-start attempts share the restart budget (`maxRestarts` within `restartWindow`) with exponential backoff, so a missing executable or occupied port cannot spawn processes forever. The manual Start/Restart buttons and their approval flow remain.

## Context and time management (new in 0.2.4)

- Context compaction: when the message history exceeds the context budget (`compactAfterChars`, or 3x the agent's context tokens by default), older conversation rounds are summarized — tool results become short excerpts that keep their evidence IDs, and oversized arguments are truncated. The task framing, a compaction note, the contract and the most recent rounds stay verbatim. An assistant tool-call message never loses its results, so the Ollama tool-call protocol stays intact across compaction. Evidence IDs remain valid for `verify_requirement` after compaction.
- Elapsed-time budget: runs are additionally bounded by wall-clock time (`maxElapsedSeconds`, default 3600). Exceeding it ends the run as `paused_budget`; Continue starts a fresh time window with the same saved state. `elapsed_ms` is recorded on every `model.started` event.
- Contract correction: a final answer is not accepted while required checklist items have no recorded evidence. The runtime sends at most two explicit corrections listing the pending items and how to record evidence, then completes as `completed_unverified` — a confident "everything passed" message alone can never produce `completed_verified`.

## Task contract, evidence and completion gate (new in 0.2.3)

- Agents propose a task checklist with the `submit_contract` tool; the application validates it (1–20 non-empty requirements) and records it on the run. Once recorded, requirements are fixed: later calls can only add items, never remove or weaken them.
- Every successful tool execution receives an evidence ID (`evidence_id` in the tool result) and is recorded in the run's evidence ledger with its arguments and result excerpt.
- `verify_requirement` marks a requirement satisfied only when it references evidence IDs actually recorded in this run — a textual "everything passed" claim is not evidence, and unknown evidence IDs are rejected as tool failures.
- The completion gate runs on recorded state, not the final message. With a contract: all required requirements satisfied with valid evidence -> `completed_verified`; otherwise `completed_unverified` with an explicit warning listing each unverified requirement. Runs without a contract keep the existing `completed` / `completed_with_warning` statuses.

## Write policy (new in 0.2.3)

- `POST /api/runs` accepts an optional `writePolicy` scoped to the run: per-file rules enforced before a write reaches disk.
  - `{"mode": "deny"}` — the file (e.g. a diagnostic harness) may not be modified at all.
  - `{"mode": "sections", "markers": [["<!-- APP START -->", "<!-- APP END -->"]]}` — only content inside the marker pairs may change; everything else, including the markers and their count, must remain byte-identical.
  - `{"validJson": true}` — the proposed content must parse as JSON (e.g. profile data).
  - `{"noTrailingNewline": true}` — exact-byte rule; a trailing newline is rejected.
- Rejected writes fail as tool errors and leave the file untouched; they are recorded separately from accepted repair cycles.

## Budget and continuation (new in 0.2.2)

- A "step" is one model request. Separate recorded counters replace the single step count: model requests, tool executions, repair cycles (accepted edits), and per-segment progress. They are shown on every `model.started` event and on pause events.
- Per-segment limit stays 12 model requests without browser access, 32 with it (configurable: `segmentRequests`). When a segment is exhausted but useful progress was recorded (any successful tool execution), the runtime automatically renews the segment and continues the same task — same messages, same tool-call history, no resubmission. Up to `maxSegments` renewals (default 8) within a total cap of `maxModelRequests` (default 8x the segment limit).
- Total-cap or no-progress exhaustion ends the run as `paused_budget` (not failed, not completed). The saved run keeps the original task, messages, tool-call history, results and pending calls. `POST /api/runs/{id}/continue` (also offered in the UI) resumes that exact run state with a fresh grant — it never resubmits the original prompt as a new task. An unexecuted pending tool call is dispatched first on resume, never silently dropped.
- Loop protection: the same action failing identically three times triggers an explicit change-approach instruction; a fourth identical failure stops the run. Six consecutive all-failure model rounds trigger a diagnose instruction; ten stop it. A segment with zero successful tool executions is not renewed.
- Repair cycles (accepted `write_file` edits) are counted separately and capped (`maxRepairCycles`, default 10).
- Generic non-validation runtime errors (not budget/loop stops, not ValueErrors) now get the same bounded two-attempt recovery retry as transport errors, so transient failures do not end a run that was making progress.
- Tool-call history integrity is unchanged: assistant tool calls and their results are always returned to the next model request; loop-correction instructions are only inserted after a completed tool result, never between a call and its result.

## Fixes in this package

- runtime.py explicitly exports groups. The actual import `from runtime import Runtime,validate_flow,TOOLS,groups` is checked before packaging and by the launcher.
- `/` serves static/index.html: the full unified workspace application, not a backend console.
- Sidebar: Workspace, Conversations, Chat, then Credentials / Diagnostics / Import-export / MCP servers / Memory / Models-resources / Prompts / Runtime health / Scheduler / Workflows, followed by Agent.
- Tool panels stay on the right; panels resize; workflow toolbar stays at the bottom; canvas SVG styling does not stretch node icons.

## Run

Extract into a NEW folder. Stop the earlier app occupying port 8765. Install Python 3.10+ and have Ollama available. Double-click launch.bat. First launch creates .venv and installs dependencies (internet required). Open http://127.0.0.1:8765; refresh if the browser opens before startup finishes. On Linux/macOS: sh launch.sh (native browser discovery and File Explorer are Windows-only).

Confirm your installed model tags using Models & resources. Defaults: Builder/Reviewer qwen3-coder:30b, Visual Analyst gemma3:4b. No models are automatically downloaded.

Data is in data/application.sqlite, data/projects and data/browser-profiles. LOCAL_AGENT_DATA overrides it. Keep old folders backed up. The 0.2.0 objects table is copied into the new records table without deleting it, if the same database is used; do not copy live databases. Older 0.1.0 state.sqlite uses a different schema and is not automatically migrated.

## Full interface versus exact prototype markup

The application includes all main sections and five right-hand panels of the unified design, connected to backend APIs. It is an API-adapted implementation of the approved layout, not a byte-identical copy of the old standalone localStorage prototype. Some prototype conveniences (box/multi-selection, cooperative pause, separate settings tabs, and every advanced context-menu action) are not yet at exact parity. Do not treat this as a claim of literal v17 source identity. No console replaces the application at the root URL.

## Implemented capabilities

SQLite-backed workspaces/agents/conversations/prompts/memory/workflows/schedules/artifacts/approvals/audit/checkpoints; real Ollama model/tool calls; image inputs; per-agent model allowlists and budgets; one shared generation+embedding queue; controlled model unload on switch and configurable idle retention; actual workflow step/DAG execution, model overrides and prompt bindings, upstream result handoffs and artifact outputs; agent-created workflow drafts; schedule proposals; exact-action approvals; managed Edge/Chrome tabs/read/screenshots/approved clicks; file editor with checkpoints/conflict-checked restores; OS-backed credential storage through keyring; explicit prototype-state import and workspace export.

Built-in services only, no external MCP dependency. Capability gates and browser/write grants are backend enforced. Agents cannot approve their own actions. Native process control needs managed ownership and exact approval. Existing external Ollama processes are never killed.

Vector memory: set vectorEnabled=true and configure an INSTALLED embeddingModel in Runtime policy (default embeddinggemma). Index each memory using its chip icon. Retrieval reports mode/errors; lexical+pin retrieval remains available. SQLite stores vectors as JSON and scans exact cosine similarity; this is not a large-corpus ANN database. Changing embedding models needs reindexing. Deleting a memory deletes its vector, but does not alter past exports/backups.

Scheduling: task/workflow, once/daily/weekdays/weekly/five-field cron, timezone, overlap skip or queue-one, missed-run skip or once. DST behavior follows croniter/timezone rules; verify your intended local times. Occurrences are claimed before dispatch to avoid duplicates after restart; a crash in the claim-to-run gap can skip an occurrence rather than duplicate it.

Recovery: transport/5xx inference errors have bounded safe retries; persisted states resume after restart when enabled. Pending side-effect markers become needs_reconciliation, never blindly replayed. Cancelled runs do not resume. Owned process restarts are limited and refused during active inference. Model progress is non-streaming with an HTTP read timeout, not token-by-token stall diagnosis. No independent GPU measurement or fallback guarantees are implied by settings.

File limits: 2 MB stored edits/checkpoints; 200 KB model read budget. File delete is supported, recursive folder delete is intentionally disabled. File rename makes two checkpoints, not an atomic multi-file transaction. Directory rename has no whole-directory rollback.

Workflow native tool arguments accept {{variable}}, {{upstream}} and {{previous}} placeholders; there is no arbitrary expression evaluator. Image IDs or attached image objects can supply vision inputs. Screenshot artifacts in upstream data can feed a screenshot vision node. Branching, loops, parallel node execution, and automatic screenshot capture between tasks are not implicit. Add explicit tools/tasks and review outputs.

Vault: secret values go to a supported OS keyring backend; insecure/plaintext fallback is refused. No secret-read API is exposed. External credential injection/OAuth providers are outside the built-in-only scope.

## Migration

Use Import / export, import your saved prototype state or exported workspace bundle, review counts, confirm. The backend creates a SQLite backup and appends imported workspaces; agents have browser/file writes disabled; workspace schedules pause; memory needs review. Prototype schedules/approval grants/credentials are not activated. file:// localStorage may be unavailable on localhost: use a JSON export. Importing configuration does not execute it.

## Local security and limits

Bound to localhost with Host/Origin/mutation-header checks, not multi-user authentication. Do not expose the port publicly. Trusted local clients can administer the app. Browser access includes authenticated/internal pages if present. Path checks are not hardened against malicious local filesystem races. Data, prompts, profiles, screenshots, traces, and checkpoints can contain private information. Protect/back up the data directory; no pruning or ordinary-record encryption.

## Verification

Quick syntax/import/packaging checks only, per request. Local module exports are actually imported to catch the reported groups error. Live Windows startup, GPU inference, browser, vault, cron/DST, and recovery are not end-to-end tested here. See BUILD_CHECKS.txt. API reference: /docs. If startup fails, send the exact traceback.
