# Codex WSL Crash Remediation Plan

Date: 2026-04-21
Owner: takeover agent
Checkout: `<review-worktree>/gitnexus`
Branch: `codex/add-global-codex-hooks`
Head: `e1ee2016aef3f24c4aea792563b036a783fbd53a`

## Goal

Keep Codex-on-WSL stable while closing the real native `augment` failure path and shipping a reviewable remediation split.

## Current Contract

- Do not re-enable WSL crash capture to `|/wsl-capture-crash ...` during investigation.
- Treat the Codex GitNexus hook as part of the product failure surface, not a local workaround.
- Prefer live runtime proofs over repo test status when Vitest is blocked by the unrelated `gitnexus-shared` resolution failure.

## Root Cause Summary

- The crashing process is GitNexus `augment`, not the Codex TUI.
- The strongest native root cause remains the LadybugDB FTS wildcard path in `MatchTermsVertexCompute`.
- `MatchTermsVertexCompute::copy()` shares `std::unordered_map<offset_t, uint64_t>& resDfs` across worker copies while Ladybug runs those copies concurrently through `VertexComputeTask::run()`.
- On 2026-04-21, current upstream Ladybug `main` was verified at `86f6fb9333e240f6aceccedf20a07c8268bae6b2` and current extensions `main` at `160c616cdb0688fd00fdcb4686bb269801a374d4`; both still contain the same shared-reference wildcard path.

## Containment State

Done:

- Regex-heavy/non-literal Codex search patterns are skipped in hook mode.
- Per-repo Codex hook locking exists and was tightened in this pass from TTL-only eviction to owner-aware lock cleanup.
- Direct CLI/native `augment` now applies the same fail-closed literal-pattern gate in `src/core/augmentation/engine.ts`, so regex-heavy wildcard patterns do not enter Ladybug FTS from the product path.
- Live installed hook at `~/.codex/hooks/gitnexus/gitnexus-hook.cjs` was kept in sync with the hardened logic while preserving the resolved local CLI path.
- `gitnexus-shared` package resolution for the touched Vitest/build surfaces was repaired, and targeted GitNexus tests/build are green again.
- WSL `core_pattern` is currently `core.%e.%p.%t`, not the giant Windows-host crash-capture pipe.

Still open:

- The upstream Ladybug/extensions native defect still needs to be fixed and reviewed upstream; GitNexus now contains the product-side containment.

## Recommended PR Split

PR A: GitNexus Codex hook containment

- Scope: `hooks/codex/gitnexus-hook.cjs`, `test/unit/hooks.test.ts`, `test/utils/hook-test-helpers.ts`, operator caveats for current hook behavior.
- Include: literal-only gate, owner-aware per-repo lock, runtime tests for live-owner skip and dead-owner recovery, docs clarifying silent skip/degrade behavior.
- Exclude: setup/install path policy changes.

PR B: GitNexus Codex setup/install reconciliation

- Scope: `src/cli/setup.ts`, `test/unit/setup-codex.test.ts`, `README.md`.
- Include: correct hook source/worktree ownership, hook dedupe semantics that survive path churn, explicit policy for `features.codex_hooks`, operator-facing docs.
- Open question to resolve in PR: whether `setup` should force-enable hooks or only install them when explicitly requested.

PR C: LadybugDB/extensions native fix

- Scope: `fts/src/function/query_fts_index.cpp` and any required Ladybug support/test files.
- Preferred implementation: thread-local match accumulation plus deterministic merge after `runVertexCompute`.
- Acceptable fallback: synchronization around `resDfs` only if benchmark cost is acceptable and simpler merge plumbing is unavailable.

PR D: GitNexus temporary fallback if upstream lead time is non-trivial

- Scope: GitNexus augmentation engine / BM25 path only.
- Goal: avoid wildcard FTS in hook-driven augmentation altogether, or degrade to a safe non-FTS path for unsupported patterns until PR C lands.
- Status: completed in this branch by failing closed in `augment()` for non-literal patterns.

## Verification Gates

Pass now:

- Live installed hook emits augmentation for a known literal symbol search: `rg setupCodex src/cli/setup.ts`.
- Live installed hook emits nothing for regex-heavy `rg 'run_tapi_action|/api/.*/actions' src`.
- Live installed hook still emits stale-index warnings on successful git mutation output.
- Direct CLI/native proof is green at the product boundary: concurrent `node dist/cli/index.js augment -- 'run_tapi_action|/api/.*/actions'` returns `0` with no stderr output and no new `core.*` file in the repo.
- Direct CLI/native proof is still green for literal search: `node dist/cli/index.js augment -- 'setupCodex'` returns augmentation on stderr.
- Targeted GitNexus verification is green: `test/unit/setup-codex.test.ts`, `test/unit/hooks.test.ts`, `test/integration/setup-skills.test.ts`, and `test/integration/augmentation.test.ts`; `npm run build` also passes.
- Isolated `gitnexus setup` fallback smoke with `codex` absent from `PATH` still writes `~/.codex/config.toml`, `~/.codex/hooks.json`, `~/.codex/AGENTS.md`, and the bundled Codex hook.
- No new WSL crash-capture pipe is configured.

Fail now:

- Upstream Ladybug/extensions patch is not locally rebuilt in this environment because the current WSL image lacks a compiler toolchain.

Blocked now:

- Full upstream native rebuild/proof remains blocked by missing compiler tooling in this WSL environment.

## Execution Checklist

- [x] Reconcile authoritative GitNexus worktree and live installed hook path.
- [x] Capture current root-cause evidence and upstream `main` status.
- [x] Harden current hook lock semantics so containment is not TTL-based.
- [x] Sync the live installed WSL hook with the hardened lock semantics.
- [x] Re-run live installed hook proofs.
- [x] Repair / isolate the `gitnexus-shared` Vitest environment break so touched branch tests can be trusted again.
- [x] Add GitNexus product-side fallback so direct `augment` skips regex-heavy wildcard queries safely.
- [ ] Prepare and publish PR A.
- [x] Decide PR B policy around forced `codex_hooks = true` and duplicate hook entry cleanup.
- [ ] Prepare upstream Ladybug issue/PR from the attached writeup.
- [x] Re-run direct CLI/native repro after upstream or fallback fix and require clean completion, no crash, and no hang.
