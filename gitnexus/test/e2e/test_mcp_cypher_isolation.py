"""Attacks on the raw cypher tool through the real MCP stdio server and CLI.

A raw Cypher query with a variable-length path and an ALL(relationships) predicate
segfaults LadybugDB (claude-skills#197). These tests seed a real store with the
synthetic graph that reproduces it and drive the real server from outside, so a
dead server is observable instead of killing the test runner.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]
ENGINE = PACKAGE / "node_modules" / "@ladybugdb" / "core" / "index.mjs"
TSX = PACKAGE / "node_modules" / ".bin" / "tsx"
SOURCE_ENTRY = [str(TSX), str(PACKAGE / "src" / "cli" / "index.ts")]
DIST_ENTRY = ["node", str(PACKAGE / "dist" / "cli" / "index.js")]
REPO = "crash-graph"
CRASH = ("MATCH p=(caller)-[:CodeRelation*1..5]->(target:Function {name: 'target'}) "
         "WHERE ALL(r IN relationships(p) WHERE r.type = 'CALLS') "
         "RETURN DISTINCT caller.name AS caller ORDER BY caller LIMIT 100")
COUNT = "MATCH (n:Function) RETURN count(n) AS n"
RUNAWAY = ("MATCH (a:Function),(b:Function),(c:Function),(d:Function),(e:Function) "
           "WHERE a.startLine + b.startLine + c.startLine + d.startLine + e.startLine = -1 RETURN count(*) AS n")
SEED_SCRIPT = """
const lbug = (await import(process.argv[1])).default;
const { SCHEMA_QUERIES } = await import(process.argv[2]);
const [store, n, m] = [process.argv[3], Number(process.argv[4]), Number(process.argv[5])];
let seed = 7;
const rand = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
const db = new lbug.Database(store, 0, false, false);
const conn = new lbug.Connection(db);
const run = async (q) => { const r = await conn.query(q); return r.getAll ? await r.getAll() : r; };
for (const q of SCHEMA_QUERIES) { try { await run(q); } catch (e) { if (!String(e).includes('already exists')) throw e; } }
for (let i = 0; i < n; i++) await run(`CREATE (:Function {id: 'f${i}', name: '${i === 0 ? 'target' : 'f' + i}', filePath: 'a.py', startLine: ${i}, endLine: ${i + 1}, isExported: false, content: '', description: ''})`);
const types = ['CALLS', 'CALLS', 'CALLS', 'IMPORTS', 'DEFINES'];
for (let e = 0; e < m; e++) { const a = Math.floor(rand() * n), b = Math.floor(rand() * n); const t = types[Math.floor(rand() * types.length)];
  await run(`MATCH (a:Function {id:'f${a}'}),(b:Function {id:'f${b}'}) CREATE (a)-[:CodeRelation {type:'${t}', confidence: 1.0, reason: 'seed', step: 0}]->(b)`); }
try { await run('LOAD EXTENSION fts'); await run("CALL CREATE_FTS_INDEX('Function', 'function_fts', ['name'])"); console.log('seeded fts'); } catch (e) { console.log('seeded'); }
"""
QUERY_SCRIPT = """
const lbug = (await import(process.argv[1])).default;
const db = new lbug.Database(process.argv[2], 0, false, true);
const conn = new lbug.Connection(db);
const r = await conn.query(process.argv[3]);
console.log(JSON.stringify({ rows: (await r.getAll()).length }));
"""

HOME: Path | None = None


def runner_pids() -> list[str]:
    """Runner children of this module's store only: the store path is in the runner's argv, so
    concurrent suites elsewhere on the machine do not contaminate the cleanup assertions."""
    pattern = "gitnexus-cypher-runner.*" + re.escape(str(HOME / "repo" / ".gitnexus" / "lbug"))
    return subprocess.run(["pgrep", "-f", pattern], text=True, capture_output=True).stdout.split()


def node_module(script: str, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["node", "--input-type=module", "-e", script, "--", str(ENGINE), *args],
                          cwd=PACKAGE, text=True, capture_output=True, timeout=timeout)


CRASH_SIGNAL: int | None = None
FTS_INDEXED = False


def setUpModule() -> None:
    global HOME
    HOME = Path(tempfile.mkdtemp(prefix="gitnexus-mcp-attack-"))
    repo = HOME / "repo"
    storage = repo / ".gitnexus"
    storage.mkdir(parents=True)
    # The engine resolves LOAD EXTENSION under $HOME/.lbdb/extension; give the isolated HOME the installed copy.
    installed = Path.home() / ".lbdb" / "extension"
    if installed.is_dir():
        shutil.copytree(installed, HOME / ".lbdb" / "extension")
    seeded = node_module(SEED_SCRIPT, str(PACKAGE / "dist" / "core" / "lbug" / "schema.js"), str(storage / "lbug"), "300", "1500")
    if seeded.returncode != 0:
        raise RuntimeError(f"seeding failed: {seeded.stderr[-800:]}")
    (storage / "meta.json").write_text(json.dumps({"indexedAt": "2026-09-05T00:00:00.000Z", "lastCommit": "seed"}), encoding="utf-8")
    (HOME / ".gitnexus").mkdir()
    (HOME / ".gitnexus" / "registry.json").write_text(json.dumps([{
        "name": REPO, "path": str(repo), "storagePath": str(storage),
        "indexedAt": "2026-09-05T00:00:00.000Z", "lastCommit": "seed", "stats": {"files": 1, "nodes": 300},
    }]), encoding="utf-8")
    global CRASH_SIGNAL, FTS_INDEXED
    FTS_INDEXED = 'seeded fts' in seeded.stdout
    probe = node_module(QUERY_SCRIPT, str(HOME / "repo" / ".gitnexus" / "lbug"), CRASH)
    CRASH_SIGNAL = probe.returncode if probe.returncode in (-11, 139) else None



def tearDownModule() -> None:
    if HOME is not None:
        shutil.rmtree(HOME, ignore_errors=True)


class McpClient:
    """Newline-delimited JSON-RPC over the real server's stdio."""

    def __init__(self, entry: list[str], home: Path, extra_env: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            [*entry, "mcp"], cwd=PACKAGE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env={**os.environ, "HOME": str(home), **(extra_env or {})})
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.next_id = 0
        self.responses: dict[int, dict] = {}
        threading.Thread(target=self._pump, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "attack", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def send(self, method: str, params: dict) -> int:
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self.next_id

    def receive(self, request_id: int, timeout: float = 120.0) -> dict | None:
        """The response for request_id, or None when the server's stdout closes first."""
        if request_id in self.responses:
            return self.responses.pop(request_id)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
            if message.get("id") is not None:
                self.responses[message["id"]] = message

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict | None:
        return self.receive(self.send(method, params), timeout)

    def call(self, tool: str, arguments: dict, timeout: float = 120.0) -> dict | None:
        """The tool's decoded payload ({markdown,row_count} or {error}), or None when the server died."""
        response = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)
        if response is None:
            return None
        text = response["result"]["content"][0]["text"]
        body = text.split("\n\n---\n**Next:**")[0]
        try:
            return json.loads(body)
        except ValueError:
            return {"error": text}

    def alive(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


class McpCypherIsolationTests(unittest.TestCase):
    def client(self, entry: list[str] = SOURCE_ENTRY, extra_env: dict[str, str] | None = None) -> McpClient:
        client = McpClient(entry, HOME, extra_env)
        self.addCleanup(client.close)
        return client

    def require_crash(self) -> None:
        if CRASH_SIGNAL is None:
            self.skipTest("the installed engine no longer crashes on the reference query, so there is no crash to isolate")

    def test_mcp_connection_survives_engine_crash(self) -> None:
        marker = "ENGINE_CRASH_KILLED_MCP_SERVER"
        self.require_crash()
        client = self.client()
        crashed = client.call("cypher", {"repo": REPO, "query": CRASH})
        self.assertIsNotNone(crashed, marker + " (no response; server stdout closed)")
        self.assertTrue(client.alive(), marker + " (process exited)")
        after = client.call("cypher", {"repo": REPO, "query": COUNT})
        self.assertIsNotNone(after, marker + " (no response after the crash)")
        self.assertIn("300", after.get("markdown", ""), marker + f": {after}")

    def test_crash_error_names_signal(self) -> None:
        marker = "CRASH_ERROR_UNNAMED"
        self.require_crash()
        crashed = self.client().call("cypher", {"repo": REPO, "query": CRASH})
        self.assertIsNotNone(crashed, marker + " (no response)")
        if "timed out" in str(crashed.get("error", "")):
            self.skipTest("the engine hung instead of crashing on this run; the timeout reply already shows the server survived")
        self.assertIn("SIGSEGV", str(crashed.get("error", "")), marker + f": {crashed}")

    def test_other_tools_answer_after_crash(self) -> None:
        marker = "OTHER_TOOLS_DEAD_AFTER_CRASH"
        self.require_crash()
        client = self.client()
        client.call("cypher", {"repo": REPO, "query": CRASH})
        context = client.request("tools/call", {"name": "context", "arguments": {"repo": REPO, "name": "target"}})
        impact = client.request("tools/call", {"name": "impact", "arguments": {"repo": REPO, "target": "target", "direction": "upstream"}})
        self.assertIsNotNone(context, marker + " (context: no response)")
        self.assertIsNotNone(impact, marker + " (impact: no response)")
        self.assertEqual((context["result"].get("isError", False), impact["result"].get("isError", False)), (False, False), marker)

    def test_cli_cypher_returns_on_crash(self) -> None:
        marker = "CLI_CYPHER_DIED_WITH_ENGINE"
        self.require_crash()
        result = subprocess.run([*SOURCE_ENTRY, "cypher", "-r", REPO, CRASH], cwd=PACKAGE, text=True, capture_output=True,
                                env={**os.environ, "HOME": str(HOME)}, timeout=300)
        self.assertNotIn(result.returncode, (-11, 139), marker + f": exit {result.returncode}")
        self.assertIn("error", result.stdout.lower(), marker + f": {result.stdout[:300]} {result.stderr[-300:]}")

    def test_dist_entry_survives_engine_crash(self) -> None:
        marker = "DIST_ENTRY_DIED_WITH_ENGINE"
        self.require_crash()
        client = self.client(DIST_ENTRY)
        crashed = client.call("cypher", {"repo": REPO, "query": CRASH})
        self.assertIsNotNone(crashed, marker + " (no response; server stdout closed)")
        after = client.call("cypher", {"repo": REPO, "query": COUNT})
        self.assertIsNotNone(after, marker + " (no response after the crash)")
        self.assertIn("300", after.get("markdown", ""), marker + f": {after}")

    def test_engine_error_stays_ordinary(self) -> None:
        marker = "ENGINE_ERROR_REPORTED_AS_CRASH"
        result = self.client().call("cypher", {"repo": REPO, "query": "MATCH (n RETURN n"})
        self.assertIsNotNone(result, marker + " (no response)")
        error = str(result.get("error", ""))
        self.assertTrue(error and "SIGSEGV" not in error and "crash" not in error.lower(), marker + f": {result}")

    def test_write_forms_refused(self) -> None:
        marker = "WRITE_FORM_REACHED_STORE"
        client = self.client()
        before = client.call("cypher", {"repo": REPO, "query": COUNT})
        forms = [
            "CREATE (:Function {id: 'w1', name: 'w1', filePath: 'w', startLine: 0, endLine: 0, isExported: false, content: '', description: ''})",
            "MATCH (n:Function) RETURN count(n); CREATE (:Function {id: 'w2', name: 'w2', filePath: 'w', startLine: 0, endLine: 0, isExported: false, content: '', description: ''})",
            "COPY Function FROM '/dev/null'",
        ]
        outcomes = [client.call("cypher", {"repo": REPO, "query": form}) for form in forms]
        after = client.call("cypher", {"repo": REPO, "query": COUNT})
        self.assertEqual([("error" in (o or {})) for o in outcomes], [True] * len(forms), marker + f": {outcomes}")
        self.assertEqual(after, before, marker)

    def test_overlapping_calls_return(self) -> None:
        marker = "OVERLAPPING_CALL_FAILED"
        first = self.client()
        second = self.client()
        ids = [first.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": COUNT}}) for _ in range(2)]
        other = second.call("cypher", {"repo": REPO, "query": COUNT})
        responses = [first.receive(request_id) for request_id in ids]
        self.assertTrue(all(r is not None and "300" in r["result"]["content"][0]["text"] for r in responses), marker + f": {responses}")
        self.assertIn("300", (other or {}).get("markdown", ""), marker + f": {other}")

    def test_result_rendering_unchanged(self) -> None:
        marker = "RESULT_RENDERING_DRIFTED"
        query = ("MATCH p=(a:Function {id: 'f0'})-[r:CodeRelation]->(b:Function) "
                 "RETURN 9007199254740993 AS big, a.startLine AS line, null AS nothing, [1, 2] AS items, {k: 1} AS mapping, a, r, p "
                 "ORDER BY b.id LIMIT 1")
        result = self.client().call("cypher", {"repo": REPO, "query": query})
        golden = Path(__file__).with_name("cypher_render.golden.json")
        self.assertIsNotNone(result, marker + " (no response)")
        self.assertEqual(result, json.loads(golden.read_text(encoding="utf-8")), marker)

    def test_dist_cli_cypher_returns_on_crash(self) -> None:
        marker = "DIST_CLI_CYPHER_DIED_WITH_ENGINE"
        self.require_crash()
        result = subprocess.run([*DIST_ENTRY, "cypher", "-r", REPO, CRASH], cwd=PACKAGE, text=True, capture_output=True,
                                env={**os.environ, "HOME": str(HOME)}, timeout=300)
        self.assertNotIn(result.returncode, (-11, 139), marker + f": exit {result.returncode}")
        self.assertIn("error", result.stdout.lower(), marker + f": {result.stdout[:300]} {result.stderr[-300:]}")

    def test_large_result_arrives_complete(self) -> None:
        marker = "LARGE_RESULT_TRUNCATED"
        client = self.client()
        result = client.call("cypher", {"repo": REPO, "query": "MATCH (a:Function)-[r:CodeRelation]->(b:Function) RETURN a, r, b"}, timeout=180)
        self.assertEqual((result or {}).get("row_count"), 1500, marker + f": {str(result)[:300]}")
        self.assertGreater(len(result["markdown"]), 65536, marker + " (result smaller than one pipe buffer)")
        self.assertIn("300", (client.call("cypher", {"repo": REPO, "query": COUNT}) or {}).get("markdown", ""), marker + " (server unresponsive after the large result)")

    def test_server_exit_reaps_runner(self) -> None:
        marker = "RUNNER_ORPHANED_ON_SERVER_EXIT"
        client = self.client()
        client.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": RUNAWAY}})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not runner_pids():
            time.sleep(0.2)
        self.assertTrue(runner_pids(), "runner never started for the runaway query")
        client.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and runner_pids():
            time.sleep(0.2)
        self.assertEqual(runner_pids(), [], marker + f": runner children {runner_pids()} after the server exited with {client.process.returncode}")

    def test_cli_termination_reaps_runner(self) -> None:
        marker = "RUNNER_ORPHANED_ON_CLI_EXIT"
        cli = subprocess.Popen([*SOURCE_ENTRY, "cypher", "-r", REPO, RUNAWAY], cwd=PACKAGE, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "HOME": str(HOME)})
        self.addCleanup(cli.kill)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not runner_pids():
            time.sleep(0.2)
        self.assertTrue(runner_pids(), "runner never started for the runaway query")
        cli.terminate()
        cli.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and runner_pids():
            time.sleep(0.2)
        self.assertEqual(runner_pids(), [], marker + f": runner children {runner_pids()} after the CLI exited with {cli.returncode}")

    def test_attacks_run_from_the_durable_gate(self) -> None:
        marker = "ATTACKS_NOT_ON_A_DURABLE_GATE"
        run = subprocess.run(["npm", "run", "--silent", "test:mcp-isolation", "--", "-k", "no_such_attack"], cwd=PACKAGE, text=True, capture_output=True, timeout=300)
        self.assertIn("Ran 0 tests", run.stdout + run.stderr, marker + f": rc={run.returncode} {(run.stdout + run.stderr)[-300:]}")
        workflow = (PACKAGE.parent / ".github" / "workflows" / "ci-tests.yml").read_text(encoding="utf-8")
        self.assertIn("npm run test:mcp-isolation", workflow, marker + " (ci-tests.yml never runs the module)")

    def test_fts_call_answers_through_the_child(self) -> None:
        marker = "FTS_UNAVAILABLE_IN_CHILD"
        if not FTS_INDEXED:
            self.skipTest("the seed could not build an FTS index (extension unavailable)")
        result = self.client().call("cypher", {"repo": REPO, "query": "CALL QUERY_FTS_INDEX('Function', 'function_fts', 'target') RETURN node.name AS name"})
        self.assertIn("target", (result or {}).get("markdown", ""), marker + f": {result}")

    def test_large_query_then_disconnect_keeps_the_server_clean(self) -> None:
        marker = "LARGE_QUERY_DISCONNECT_CRASHED_SERVER"
        client = self.client()
        client.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": "/* " + "x" * (4 * 1024 * 1024) + " */ " + COUNT}})
        client.close()
        self.assertEqual(client.process.returncode, 0, marker + f": server exited {client.process.returncode}: {client.process.stderr.read()[-400:] if client.process.stderr else ''}")
        self.assertEqual(runner_pids(), [], marker + f": runner children {runner_pids()}")

    def test_concurrent_raw_queries_share_the_pool_limit(self) -> None:
        marker = "RUNNERS_EXCEED_POOL_LIMIT"
        client = self.client()
        slow = "MATCH (a:Function),(b:Function),(c:Function) WHERE a.startLine + b.startLine + c.startLine = -1 RETURN count(*) AS n"
        peak = [0]
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                peak[0] = max(peak[0], len(runner_pids()))
                time.sleep(0.05)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        ids = [client.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": slow}}) for _ in range(9)]
        responses = [client.receive(request_id, timeout=120) for request_id in ids]
        stop.set()
        sampler.join()
        self.assertTrue(all(r is not None and '"row_count": 1' in r["result"]["content"][0]["text"] for r in responses), marker + " (a reply was missing or wrong)")
        self.assertLessEqual(peak[0], 8, marker + f": {peak[0]} runners observed at once, pool limit is 8")

    def test_engine_error_is_prompt_under_warn_rejections(self) -> None:
        marker = "ENGINE_ERROR_BECAME_TIMEOUT"
        client = self.client(extra_env={"NODE_OPTIONS": "--unhandled-rejections=warn"})
        started = time.monotonic()
        result = client.call("cypher", {"repo": REPO, "query": "MATCH (n:Nope RETURN n"})
        elapsed = time.monotonic() - started
        error = str((result or {}).get("error", ""))
        self.assertIn("Parser exception", error, marker + f": {result}")
        self.assertLess(elapsed, 10, marker + f": took {elapsed:.1f}s")

    def test_timeout_reaps_runner_child(self) -> None:
        marker = "TIMEOUT_LEAKED_CHILD"
        client = self.client()
        result = client.call("cypher", {"repo": REPO, "query": RUNAWAY}, timeout=90)
        self.assertIsNotNone(result, marker + " (no response)")
        self.assertIn("timed out", str(result.get("error", "")).lower(), marker + f": {result}")
        self.assertEqual(runner_pids(), [], marker + f": runner children {runner_pids()}")
        after = client.call("cypher", {"repo": REPO, "query": COUNT})
        self.assertIn("300", (after or {}).get("markdown", ""), marker + f": {after}")


if __name__ == "__main__":
    unittest.main()
