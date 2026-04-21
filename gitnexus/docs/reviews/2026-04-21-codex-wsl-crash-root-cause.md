# Root Cause Memo: Codex Crashes in WSL Were Triggering GitNexus `augment`

Date: 2026-04-21

## Executive Summary

The recurring WSL failures were not caused by Codex log growth. The large disk usage came from WSL crash dumps, and the crashing process was GitNexus `augment` running under Node. The strongest current native defect is a LadybugDB/extensions data race in the wildcard FTS path, and the Codex GitNexus hook widened the blast radius by invoking `augment` on regex-heavy Bash searches during normal Codex use.

## Evidence Chain

1. Disk growth source

- Windows host temp storage contained WSL crash dumps, including a roughly 477.8 GB Node crash dump.
- Codex TUI logs were only tens of MB.

2. Crashing process

- Crashed command: `/usr/bin/node /home/prop_/projects/GitNexus/gitnexus/dist/cli/index.js augment ...`
- Conclusion: GitNexus `augment` is the failing process, not the Codex UI.

3. Repro shape

- Concurrent `augment` calls against regex-heavy patterns reproduced the native failure path.
- Earlier investigation reproduced SIGSEGV/core under concurrency.
- This pass still reproduced unhealthy behavior on the same path: concurrent runs wedged, and a sequential run timed out after 15 seconds.

4. Symbolized native stack

- Relevant libraries:
  - `node_modules/@ladybugdb/core/lbugjs.node`
  - `~/.lbdb/extension/0.15.0/linux_amd64/fts/libfts.lbug_extension`
- Strongest frames previously captured:
  - `lbug::fts_extension::MatchTermsVertexCompute::vertexCompute(...)`
  - `lbug::function::VertexComputeTask::run()`
  - `lbug::common::TaskScheduler::runWorkerThread()`
  - `std::__detail::_Map_base<...>::operator[]`

5. Exact source path

- Extensions source: `/tmp/ladybug-ext/fts/src/function/query_fts_index.cpp`
- Ladybug source: `/tmp/ladybug-src/src/function/gds/gds_task.cpp`
- Ladybug source: `/tmp/ladybug-src/src/function/gds/gds_utils.cpp`

## Why The Shared `resDfs` Map Is The Strongest Root Cause

`MatchTermsVertexCompute` stores:

- `std::unordered_map<offset_t, uint64_t>& resDfs`
- `std::vector<VCQueryTerm>& queryTerms`

and `copy()` returns new worker instances that share those same references.

At the same time:

- `VertexComputeTask::run()` calls `auto localVc = info.vc.copy();`
- `GDSUtils::runVertexCompute(...)` schedules the task across worker threads

That means multiple worker copies can concurrently execute:

```cpp
resDfs[nodeIds[i].offset] = dfs[i];
```

against the same `std::unordered_map` without synchronization. The earlier crash frames through `std::__detail::_Map_base<...>::operator[]` are consistent with exactly that race.

## Current Upstream Status

Verified on 2026-04-21:

- Ladybug `main`: `86f6fb9333e240f6aceccedf20a07c8268bae6b2`
- extensions `main`: `160c616cdb0688fd00fdcb4686bb269801a374d4`

Both current upstream branches still contain:

- `VertexComputeTask::run()` using `info.vc.copy()`
- `MatchTermsVertexCompute::copy()` returning shared `resDfs, queryTerms`
- wildcard query matching that writes into the shared map

## Product-Level GitNexus Finding

The live Codex hook was sourced from the Codex hook copy in:

- `/home/prop_/projects/GitNexus-pr1-review/gitnexus/hooks/codex/gitnexus-hook.cjs`

not from the main checkout’s Claude hook. That matters because the Codex hook runs on Bash `PostToolUse` and was invoking `augment` for regex-heavy `rg` usage in normal Codex workflows, including delegated-agent searches.

This pass kept the hook containment in place and tightened it:

- literal-only augmentation gate remains
- per-repo lock now tracks owner PID instead of relying on time-only eviction
- live installed hook was synced with the hardened logic

## What Is Proven vs Not Yet Proven

Proven:

- the failing process is GitNexus `augment`
- the hook widened exposure to the native path
- current upstream source still matches the shared-reference race candidate
- hook-side containment can prevent the live Codex Bash path from invoking wildcard FTS on regex-heavy patterns

Not yet proven:

- a rebuilt patched Ladybug extension that eliminates the hang/crash
- a green repo Vitest path; current test runner is still blocked by unrelated `gitnexus-shared` package resolution

Reason for the remaining gap:

- this environment does not currently have `cmake`, so a local upstream rebuild/proof patch was not completed in this pass

## Recommended Next Action

Use `docs/reviews/2026-04-21-ladybug-fts-wildcard-race-issue-draft.md` as the upstream issue/PR seed, and do not treat direct CLI/native remediation as done until `augment` completes cleanly under the regex-heavy concurrent repro without crash or hang.
