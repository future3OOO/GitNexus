---
title: "feat: harden GitNexus analyze/setup/hook integration for clean review workflows"
type: feat
status: active
date: 2026-04-08
owner: codex
origin: transcript-driven architecture review + GitNexus blast-radius walk
---

# GitNexus Integration Hardening Plan

## Objective

Fix the current integration drift between GitNexus indexing, repo-local AI context generation, global editor setup, and Codex/Claude hooks so that:

1. `gitnexus analyze` can be used safely in clean review worktrees without polluting tracked files.
2. Codex hooks stay useful and low-latency without regressing Claude behavior.
3. Setup, docs, and shipped hook/runtime behavior are generated from one coherent contract instead of multiple drifting implementations.

## Governing Execution Boundary

- **Owning repo:** `/home/prop_/projects/GitNexus`
- **Owning package:** `/home/prop_/projects/GitNexus/gitnexus`
- **Live PR:** `#1`
- **PR URL:** `https://github.com/future3OOO/GitNexus/pull/1`
- **Target branch:** `codex/add-global-codex-hooks`
- **Base branch:** `main`
- **Live PR head / local HEAD at plan creation:** `75cd61a37616d51291c32779b088abb023520ac1`
- **Checkout mode:** branch-attached

This plan governs the current PR branch. Do not silently fork new implementation work onto a second branch while this plan is active.

## Scope

### In scope

- `gitnexus/src/core/run-analyze.ts`
- `gitnexus/src/cli/analyze.ts`
- `gitnexus/src/cli/ai-context.ts`
- `gitnexus/src/cli/setup.ts`
- `gitnexus/hooks/codex/gitnexus-hook.cjs`
- `gitnexus/hooks/claude/gitnexus-hook.cjs`
- `gitnexus-claude-plugin/hooks/gitnexus-hook.js`
- affected tests in `gitnexus/test/unit` and `gitnexus/test/integration`
- `gitnexus/README.md` and root `README.md` where behavior/support claims drift from reality

### Explicitly out of scope

- `gitnexus-web/`
- `eval/`
- unrelated root-file dirt currently present in:
  - `AGENTS.md`
  - `CLAUDE.md`

Those two root-file modifications are analyzer side effects from prior work and are not owned by this pass unless a later slice explicitly takes them on.

## Delivery Map

### P1 — Make indexing safe in review worktrees

**Owner slice:** indexing lifecycle and repo-local AI context materialization

**Goal:** decouple graph indexing from repo mutation so a plain `gitnexus analyze` does not rewrite tracked repo files or install repo-local skills by default.

**Primary files:**
- `gitnexus/src/core/run-analyze.ts`
- `gitnexus/src/cli/analyze.ts`
- `gitnexus/src/cli/ai-context.ts`
- tests covering analyze + ai-context behavior

**Expected outcome:**
- plain analyze is graph-only
- repo-local AI context generation becomes explicit
- review workflows no longer need cleanup after reindex

### P2 — Unify hook runtime behavior and strengthen Codex proof

**Owner slice:** Claude/Codex/plugin hook runtime and shared hook semantics

**Goal:** remove hook drift and prove Codex behavior end-to-end while keeping Claude behavior unchanged.

**Primary files:**
- `gitnexus/hooks/codex/gitnexus-hook.cjs`
- `gitnexus/hooks/claude/gitnexus-hook.cjs`
- `gitnexus-claude-plugin/hooks/gitnexus-hook.js`
- hook unit/integration tests

**Expected outcome:**
- one shared hook core with thin runtime-specific adapters
- Codex stays Codex-only
- Claude keeps current pre-tool behavior
- hook behavior is proven with e2e fixtures, not just source inspection

### P3 — Unify setup/docs/integration contract

**Owner slice:** editor setup, shipped instructions, and support matrix

**Goal:** remove setup/docs drift and ensure the product teaches exactly the behavior it ships.

**Primary files:**
- `gitnexus/src/cli/setup.ts`
- `gitnexus/README.md`
- root `README.md`
- any shared integration registry/templates needed by setup + docs

**Expected outcome:**
- one support matrix / integration contract
- idempotent global setup
- no contradictory Codex/Claude support claims

## Commit Structure

1. `plan: add integration hardening governing artifact`
2. `feat: make analyze graph-only by default`
3. `refactor: unify hook runtimes and add codex e2e proof`
4. `docs: align setup and integration contract`

If P2 requires a small preparatory shared-core extraction commit, it may be split once, but keep total new commits for this plan at `<= 5`.

## PR Structure

Stay on the current PR branch unless implementation complexity forces a regroup.

- **PR #1 remains the owner PR** for the current scope.
- If P1 lands cleanly but P2/P3 materially expand PR review scope, regroup before stacking another dependent PR.
- Do not exceed active dependent stack depth `1` from this branch without an explicit regroup decision.

## Verification Gates

### P1 gate

- `cd gitnexus && ./node_modules/.bin/tsc --noEmit`
- targeted unit/integration tests for analyze + ai-context
- proof that a plain analyze on a clean fixture repo leaves tracked files untouched
- proof that explicit AI-context generation still works when requested

### P2 gate

- `cd gitnexus && ./node_modules/.bin/tsc --noEmit`
- targeted hook unit tests
- hook e2e for Claude + Codex + plugin surfaces
- proof that Codex augmentation and stale detection both work
- proof that Claude behavior stays unchanged

### P3 gate

- `cd gitnexus && ./node_modules/.bin/tsc --noEmit`
- targeted setup tests
- repeated setup idempotency proof
- docs checked against actual shipped behavior

### Final gate

- `cd gitnexus && npm test`
- `cd gitnexus && npm run test:integration`
- `cd gitnexus && npm run build`
- `gitnexus_detect_changes({repo:"GitNexus",scope:"all"})`

## Execution Checklist

- [x] Map the current architecture with GitNexus before planning.
- [x] Verify the live PR head and local checkout state.
- [x] Write the governing plan artifact before tracked code edits.
- [x] P1 preflight against the live branch + this artifact.
- [x] Implement P1.
- [x] Run P1 verification and update this checklist.
- [ ] P2 preflight against the updated branch + this artifact.
- [ ] Implement P2.
- [ ] Run P2 verification and update this checklist.
- [ ] P3 preflight against the updated branch + this artifact.
- [ ] Implement P3.
- [ ] Run P3 verification and update this checklist.
- [ ] Run final repo gates.
- [ ] Run `gitnexus_detect_changes({repo:"GitNexus",scope:"all"})`.
- [ ] Commit and push the implementation.
- [ ] Update the PR description / review threads only after the pushed branch is verified.

## Consolidation Trigger

Stop and regroup if any of the following happen:

- P1 requires a breaking CLI contract change larger than the current PR can reasonably carry.
- P2 reveals the standalone Claude plugin must version independently from the main package.
- the same file is being rewritten in both P2 and P3 in incompatible ways.
- review surface on PR #1 becomes too broad to reason about as one owner slice.

## Stop Condition

Do not call this work complete until:

- plain `gitnexus analyze` is safe for review worktrees,
- Codex hooks are proven end-to-end and remain isolated from Claude semantics,
- setup/docs/runtime behavior agree,
- and the final branch passes the repo quality gate plus `gitnexus_detect_changes`.
