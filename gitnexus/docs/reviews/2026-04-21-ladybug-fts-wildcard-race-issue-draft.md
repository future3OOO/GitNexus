# Draft Upstream Issue: FTS Wildcard Query Shares `unordered_map` Across Concurrent `VertexCompute` Copies

## Summary

Regex/wildcard FTS matching appears to share mutable state across concurrent Ladybug `VertexCompute` worker copies. In a GitNexus workload this produced native SIGSEGV/core earlier in investigation. GitNexus now contains a product-side fail-closed workaround, but current upstream source still exposes the same shared-state race in the wildcard path.

## Versions / Source

- Ladybug release checkout used for symbolization: `d701392a3337d9c6e905148e28409694ac733ee0` (`0.15.2`)
- Ladybug current upstream `main` as of 2026-04-21: `86f6fb9333e240f6aceccedf20a07c8268bae6b2`
- extensions current upstream `main` as of 2026-04-21: `160c616cdb0688fd00fdcb4686bb269801a374d4`

## Exact Code Pointers

Extensions:

- `fts/src/function/query_fts_index.cpp:247`
- `fts/src/function/query_fts_index.cpp:249`
- `fts/src/function/query_fts_index.cpp:252`
- `fts/src/function/query_fts_index.cpp:275`
- `fts/src/function/query_fts_index.cpp:280`

Ladybug core:

- `src/function/gds/gds_task.cpp:124`
- `src/function/gds/gds_task.cpp:127`
- `src/function/gds/gds_task.cpp:133`
- `src/function/gds/gds_utils.cpp:139`
- `src/function/gds/gds_utils.cpp:149`

## Suspected Defect

`MatchTermsVertexCompute` stores a shared reference:

```cpp
std::unordered_map<offset_t, uint64_t>& resDfs;
```

and `copy()` returns:

```cpp
return std::make_unique<MatchTermsVertexCompute>(resDfs, queryTerms);
```

Ladybug then does:

```cpp
auto localVc = info.vc.copy();
```

per worker in `VertexComputeTask::run()`, and schedules the work concurrently from `runVertexCompute(...)`.

That means multiple workers can execute:

```cpp
resDfs[nodeIds[i].offset] = dfs[i];
```

against the same `std::unordered_map` without synchronization.

## Observed Runtime Evidence

- Native frames previously captured during crash included:
  - `lbug::fts_extension::MatchTermsVertexCompute::vertexCompute(...)`
  - `lbug::function::VertexComputeTask::run()`
  - `lbug::common::TaskScheduler::runWorkerThread()`
  - `std::__detail::_Map_base<...>::operator[]`
- The failure reproduced on the wildcard/regex-heavy path that uses `MatchTermsVertexCompute` and `GDSUtils::runVertexCompute(...)`.
- Sequential literal/non-wildcard paths do not use the same concurrent matching branch.

## Repro Shape

Workload-level repro from GitNexus:

1. Use a repo with a Ladybug-backed GitNexus index.
2. Run multiple concurrent `augment` calls with a regex-heavy pattern, for example:

```bash
node dist/cli/index.js augment -- 'run_tapi_action|/api/.*/actions'
```

3. Launch the command concurrently several times.

Observed:

- Earlier investigation reproduced SIGSEGV/core on this path.
- After the GitNexus-side fail-closed mitigation, the product no longer routes wildcard patterns into this path, but the upstream source remains unchanged on `main`.

## Expected

- Wildcard FTS matching should be safe under concurrent `runVertexCompute(...)`.
- No worker copy should mutate shared unsynchronized state.

## Proposed Immediate Fix

Guard shared `resDfs` writes with synchronization and make `copy()` share the same mutex alongside the shared result map.

Prepared patch shape:

1. `MatchTermsVertexCompute` accepts a shared `std::mutex`.
2. The initial instance creates the mutex.
3. `copy()` passes the same mutex to worker copies.
4. Every `resDfs[nodeIds[i].offset] = dfs[i];` write is covered by `std::lock_guard`.

Why this is the immediate fix:

- matches the demonstrated race exactly
- avoids changing the existing `VertexCompute` / `runVertexCompute(...)` API
- is small enough for upstream review while preserving correctness

## Follow-up Optimization

If upstream wants to recover more parallelism, the better long-term design is thread-local accumulation plus a deterministic merge after `runVertexCompute(...)` completes. I did not choose that for the immediate patch because the current `VertexCompute` API does not expose a merge/finalize hook for worker-local state.

## Remaining Proof Gap

This report has source-level proof plus workload-level reproduction, but not yet a rebuilt patched extension in the current environment because the current WSL image has no compiler toolchain (`gcc`/`g++`/`clang` absent). The attached patch is therefore source-reviewed only in this environment and should be validated upstream with the same concurrent wildcard workload as the acceptance proof.
