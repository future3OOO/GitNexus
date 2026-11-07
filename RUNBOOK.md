# Runbook — GitNexus

Short, copy-paste operations for **local development**, **MCP**, and **CI**. Commands assume a Unix shell; on Windows use Git Bash or equivalent paths.

## Prerequisites

- **Node.js** ≥ 20 (`gitnexus-web/package.json` `engines`).  
- **Git** (analyze requires a git repository).  
- From repo root, install and build the CLI package:

```bash
cd gitnexus
npm install
npm run build
```

Use `npx gitnexus …` from any path after global/published install, or `node dist/cli/index.js …` when developing from `gitnexus/` with a local build.

---

## Index out of date / “stale” tools

**Symptom:** MCP or resources warn the index is behind `HEAD`, or results don’t reflect recent commits.

**Fix (from the target repo root):**

```bash
npx gitnexus analyze
```

**Force full rebuild** (same commit but suspect corruption or changed ignore rules):

```bash
npx gitnexus analyze --force
```

**Check status:**

```bash
npx gitnexus status
```

**List what MCP knows about:**

```bash
npx gitnexus list
```

---

## Embeddings

**First time with vectors** (slower, more disk/RAM):

```bash
npx gitnexus analyze --embeddings
```

**Important:** If you already had embeddings, **always** pass `--embeddings` on later analyzes, or they can be dropped. See `stats.embeddings` in `.gitnexus/meta.json` (0 means none).

**Large repos:** Analyze may skip or limit embedding work when node counts are very high; watch CLI output.

---

## MCP: no repos / empty tools

**Symptom:** `GitNexus: No indexed repos yet` on stderr when starting MCP.

**Fix:** In each project you want indexed:

```bash
cd /path/to/repo
npx gitnexus analyze
```

Restart the editor MCP session if needed. The server **refreshes the registry lazily**; new analyzes are picked up without necessarily reinstalling MCP.

**Symptom:** Wrong repo when multiple are indexed — pass `repo` on tools or use `list_repos` first.

---

## MCP: idle servers holding memory

**Symptom:** each agent thread keeps a `gitnexus mcp` process that holds 300–700 MB after its first query, until the thread ends.

**Fix:** run the server through `gitnexus-mcp-idle`. It forwards stdio unchanged, stops the server after the idle period with no request open, and starts it again on the next request. An idle thread then holds the launcher only (under 1 MB). Workers must exit on stdin EOF and restore session state beyond the replayed initialize handshake. The first call after idle reopens the index. With Rust/rustup and a C compiler installed, build from the repository root on Linux x86-64:

```bash
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl --manifest-path gitnexus-mcp-idle/Cargo.toml
```

Codex (`~/.codex/config.toml`), with a 30 s idle period:

```toml
[mcp_servers.gitnexus]
command = "/path/to/GitNexus/gitnexus-mcp-idle/target/x86_64-unknown-linux-musl/release/gitnexus-mcp-idle"
args = ["30", "node", "/path/to/GitNexus/gitnexus/dist/cli/index.js", "mcp"]
```

FFF needs the included `gitnexus-mcp-idle/fff-0.7.1.patch`. It targets **dmtrKovalenko/fff.nvim**, `crates/fff-mcp`, at commit `e8dd50ce5a6857f2dc7f827746163a4b1040ba9c` (0.7.1). It saves the existing bounded cursor store before replying. Restored workers wait for the full index; the first worker keeps upstream startup behavior. Search and ranking code stay unchanged.

From that FFF checkout, with the Rust musl target, musl C compiler, libclang and Zig 0.16.0 (used by the `zlob` dependency) installed:

```bash
git apply /path/to/GitNexus/gitnexus-mcp-idle/fff-0.7.1.patch
cargo rustc --locked --profile ci -p fff-mcp --target x86_64-unknown-linux-musl -- -C target-feature=+crt-static
```

The FFF launcher holds an anonymous file for cursor state across workers. The file disappears when the thread closes. Keep update checks disabled, as in this config:

```toml
[mcp_servers.fff]
command = "/bin/sh"
args = ["-c", '''set -eu
state=$(mktemp)
exec 3<>"$state"
rm -- "$state"
export FFF_MCP_CURSOR_STATE=/proc/self/fd/3
exec "$@"
''', "fff-idle", "/path/to/GitNexus/gitnexus-mcp-idle/target/x86_64-unknown-linux-musl/release/gitnexus-mcp-idle", "30", "/path/to/fff/target/x86_64-unknown-linux-musl/ci/fff-mcp", "--no-update-check"]

[mcp_servers.fff.env]
MIMALLOC_ARENA_EAGER_COMMIT = "0"
```

---

## Clean slate (corrupt or huge `.gitnexus`)

**Current repo only** (prompts for confirmation):

```bash
npx gitnexus clean
```

**Skip confirmation:**

```bash
npx gitnexus clean --force
```

**All registered repos:**

```bash
npx gitnexus clean --all --force
```

Then re-run `npx gitnexus analyze` (and `--embeddings` if you need vectors).

---

## Local bridge for the web UI

```bash
cd gitnexus
npx gitnexus serve
# default http://127.0.0.1:4747 — see serve --help for port/host
```

Use when the browser UI should talk to **local** indexed repos instead of WASM-only mode.

---

## CLI equivalents of MCP tools

Useful for debugging without an editor:

```bash
cd gitnexus
npx gitnexus query "authentication flow" --repo MyRepo
npx gitnexus context SomeSymbol --repo MyRepo
npx gitnexus impact SomeSymbol --direction upstream --repo MyRepo
npx gitnexus cypher "MATCH (n) RETURN count(n) LIMIT 1" --repo MyRepo
```

---

## CI failures (contributors)

Orchestrator: `.github/workflows/ci.yml`.

| Job | Typical local repro |
|-----|---------------------|
| **quality** | `cd gitnexus && npx tsc --noEmit` |
| **unit-tests** | `cd gitnexus && npx vitest run test/unit` |
| **integration** | `cd gitnexus && npx vitest run test/integration` (see workflow matrix for groups) |
| **e2e** | Triggered when `gitnexus-web/` changes; `cd gitnexus-web && E2E=1 npx playwright test` (requires `gitnexus serve` + `npm run dev`) |

**Note:** Pushes that touch only certain markdown paths may be skipped by `paths-ignore` in CI — see workflow file for exact patterns.

---

## Memory / analyze crashes

Analyze re-execs Node with a **large old-space heap** when needed (`analyze.ts`). If you still OOM on huge repos, close other processes, avoid `--embeddings` for a first pass, or analyze a smaller path if supported by your workflow.

---

## LadybugDB / lock errors

Only one process should open a repo’s `.gitnexus/lbug` store at a time. If MCP and a second `analyze` run conflict, stop one process, then retry `analyze` or restart MCP.

---

## Where to dig deeper

- Architecture overview: [ARCHITECTURE.md](ARCHITECTURE.md)  
- Agent safety rules: [GUARDRAILS.md](GUARDRAILS.md)  
- Tests: [TESTING.md](TESTING.md)
