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
EDGES = "MATCH (a:Function)-[r:CodeRelation]->(b:Function) RETURN a, r, b"
MAX_RESPONSE_BYTES = 16384
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
for (let i = 0; i < n; i++) await run(`CREATE (:Function {id: 'f${i}', name: '${i === 0 ? 'target' : 'f' + i}', filePath: 'a.py', startLine: ${i}, endLine: ${i + 1}, isExported: false, content: '${i === 1 ? 'c'.repeat(20000) : ''}', description: ''})`);
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
        try:
            return json.loads(text)
        except ValueError:
            return {"error": text}

    def text(self, tool: str, arguments: dict, timeout: float = 120.0) -> str:
        """The tool reply exactly as the agent sees it."""
        response = self.request("tools/call", {"name": tool, "arguments": arguments}, timeout)
        assert response is not None, f"{tool}: the server gave no answer"
        return response["result"]["content"][0]["text"]

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

    def test_crash_reply_names_the_trigger_and_the_alternative(self) -> None:
        # claude-skills#197: a caller who reads only the crash reply must learn the known
        # trigger and where to go instead, or its next move is another crashed child.
        marker = "CRASH_REPLY_LACKS_GUIDANCE"
        self.require_crash()
        crashed = self.client().call("cypher", {"repo": REPO, "query": CRASH})
        self.assertIsNotNone(crashed, marker + " (no response)")
        error = str(crashed.get("error", ""))
        if "timed out" in error:
            self.skipTest("the engine hung instead of crashing on this run; the timeout reply already shows the server survived")
        self.assertIn("relationships(p)", error, marker + f": {error}")
        self.assertIn("impact", error, marker + f": {error}")
        # The signal branch fires for crashes of any origin, so the trigger is offered as a
        # possibility conditional on the caller's query, never asserted as this crash's cause.
        self.assertIn("if your query has that shape", error, f"CRASH_REPLY_ASSERTS_CAUSE_UNCONDITIONALLY: {error}")
        self.assertNotIn("Known trigger:", error, f"CRASH_REPLY_ASSERTS_CAUSE_UNCONDITIONALLY: {error}")

    def test_cypher_description_names_the_trigger_and_the_alternative(self) -> None:
        marker = "CYPHER_DESCRIPTION_LACKS_GUIDANCE"
        listed = self.client().request("tools/list", {})
        self.assertIsNotNone(listed, marker + " (no response)")
        [cypher] = [tool for tool in listed["result"]["tools"] if tool["name"] == "cypher"]
        self.assertIn("relationships(p)", cypher["description"], marker + f": {cypher['description'][:300]}")
        self.assertIn("impact", cypher["description"], marker + f": {cypher['description'][:300]}")

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

    def results_dir(self) -> Path:
        results = HOME / ".gitnexus" / "results"
        shutil.rmtree(results, ignore_errors=True)
        return results

    def bounded(self, text: str, marker: str) -> tuple[dict, Path]:
        """The JSON summary and the retained file of a bounded reply; fails with marker when the reply floods or does not parse."""
        self.assertLessEqual(len(text.encode()), MAX_RESPONSE_BYTES, marker + f": {len(text.encode())} bytes reached the agent")
        try:
            reply = json.loads(text)
        except ValueError:
            self.fail(marker + f": the bounded reply is not valid JSON: {text[-200:]!r}")
        path = Path(reply.get("complete_result", ""))
        self.assertTrue(path.is_file(), marker + f": no retained file named in {text[:300]!r}")
        self.assertEqual(reply["complete_bytes"], path.stat().st_size, marker + ": complete_bytes misstates the retained size")
        return reply, path

    def distinct_large(self, client: McpClient, k: int) -> str:
        return client.text("cypher", {"repo": REPO, "query": f"MATCH (a:Function)-[r:CodeRelation]->(b:Function) RETURN a.id, b.id, {k} AS k"})

    def test_large_cypher_result_is_bounded_and_retained(self) -> None:
        marker = "LARGE_RESULT_FLOODED"
        self.results_dir().mkdir(mode=0o755)  # a results directory left by an earlier run must be made private too
        client = self.client()
        reply, path = self.bounded(client.text("cypher", {"repo": REPO, "query": EDGES}, timeout=180), marker)
        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(reply["row_count"], 1500, marker + ": the row count is not in the summary")
        self.assertEqual(reply["markdown"], {"omitted_bytes": len(full["markdown"].encode())}, marker + ": the table preview is still in the reply")
        self.assertEqual(full["row_count"], 1500, marker + ": retained result incomplete")
        self.assertGreater(len(full["markdown"]), 65536, marker + " (retained result smaller than one pipe buffer)")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700, "RETAINED_RESULT_WORLD_READABLE: a pre-existing results directory was left shared")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600, "RETAINED_RESULT_WORLD_READABLE")
        self.assertIn("300", (client.call("cypher", {"repo": REPO, "query": COUNT}) or {}).get("markdown", ""), marker + " (server unresponsive after the large result)")

    def test_large_impact_result_is_bounded_and_retained(self) -> None:
        marker = "LARGE_JSON_FLOODED"
        reply, path = self.bounded(self.client().text("impact", {"repo": REPO, "target": "target", "direction": "upstream"}), marker)
        self.assertEqual((reply["target"]["id"], "impactedCount" in reply, "risk" in reply), ("f0", True, True), marker + ": target, count or risk not in the summary")
        cli = subprocess.run([*DIST_ENTRY, "impact", "target", "-d", "upstream", "-r", REPO], cwd=PACKAGE, text=True, capture_output=True,
                             env={**os.environ, "HOME": str(HOME)}, timeout=300)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(cli.stdout), marker + ": retained result differs from the unbounded CLI result")

    def test_large_context_result_is_bounded_and_retained(self) -> None:
        marker = "LARGE_CONTEXT_FLOODED"
        reply, path = self.bounded(self.client().text("context", {"repo": REPO, "uid": "f1", "include_content": True}), marker)
        self.assertEqual((reply["symbol"]["uid"], reply["symbol"]["content"]), ("f1", {"omitted_bytes": 20000}), marker + f": {reply['symbol']}")
        cli = subprocess.run([*DIST_ENTRY, "context", "-u", "f1", "--content", "-r", REPO], cwd=PACKAGE, text=True, capture_output=True,
                             env={**os.environ, "HOME": str(HOME)}, timeout=300)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(cli.stdout), marker + ": retained result differs from the unbounded CLI result")

    def test_oversized_error_keeps_its_meaning(self) -> None:
        marker = "LARGE_ERROR_FLOODED"
        query = 'MATCH (n:Function) WHERE n.name = "' + "x" * 20000 + '" RETURN n.name ('
        reply, path = self.bounded(self.client().text("cypher", {"repo": REPO, "query": query}), marker)
        self.assertTrue(reply["error"].startswith("Parser exception"), marker + f": error meaning not visible: {reply['error'][:120]!r}")
        self.assertIn("Parser exception", json.loads(path.read_text(encoding="utf-8"))["error"], marker + ": retained error incomplete")

    def test_thrown_error_stays_bounded(self) -> None:
        marker = "THROWN_ERROR_FLOODED"
        response = self.client().request("tools/call", {"name": "x" * 20000, "arguments": {"repo": REPO}})
        self.assertIsNotNone(response, marker + " (no response)")
        text = response["result"]["content"][0]["text"]
        self.assertEqual((response["result"].get("isError"), text.startswith("Error: Unknown tool: x"), len(text.encode()) <= MAX_RESPONSE_BYTES), (True, True, True), marker + f": {len(text.encode())} bytes {text[:60]!r}")

    def test_small_result_is_the_plain_result(self) -> None:
        marker = "SMALL_RESULT_DECORATED"
        client = self.client()
        count = client.text("cypher", {"repo": REPO, "query": COUNT})
        impact = client.text("impact", {"repo": REPO, "uid": "f0", "direction": "upstream", "maxDepth": 1})
        for text in (count, impact):
            self.assertNotIn("Next:", text, marker + f": {text[-200:]!r}")
            self.assertNotIn("[gitnexus]", text, marker + f": {text[-200:]!r}")
        self.assertEqual(json.loads(count), {"row_count": 1, "markdown": "| n |\n| --- |\n| 300 |"}, marker + f": {count!r}")
        parsed = json.loads(impact)
        self.assertEqual((parsed["target"]["id"], "byDepth" in parsed), ("f0", True), marker + f": {impact[:200]!r}")

    def test_retention_failure_is_a_bounded_error(self) -> None:
        marker = "RETENTION_FAILURE_DUMPED"
        home = Path(tempfile.mkdtemp(prefix="gitnexus-mcp-noretain-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / ".gitnexus").mkdir()
        shutil.copy(HOME / ".gitnexus" / "registry.json", home / ".gitnexus" / "registry.json")
        (home / ".gitnexus" / "results").write_text("not a directory", encoding="utf-8")
        os.symlink(HOME / ".lbdb", home / ".lbdb")
        client = McpClient(SOURCE_ENTRY, home)
        self.addCleanup(client.close)
        response = client.request("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": EDGES}}, timeout=180)
        self.assertIsNotNone(response, marker + " (no response)")
        text = response["result"]["content"][0]["text"]
        self.assertTrue(response["result"].get("isError"), marker + f": not an error: {text[:200]!r}")
        self.assertLess(len(text.encode()), 1024, marker + f": {len(text.encode())} bytes")
        self.assertRegex(text, r"\d{5,} bytes", marker + f": size not named: {text!r}")
        self.assertIn(str(home / ".gitnexus" / "results"), text, marker + f": failing location not named: {text!r}")
        self.assertNotIn("| f", text, marker + ": table rows leaked into the error")

    def test_references_survive_a_second_server_sharing_the_home(self) -> None:
        # Every MCP process sharing the HOME prunes the same directory. A returned reference must survive
        # the other server's publications until 50 newer entries exist, whatever either server repeats.
        marker = "FLEET_REFERENCE_PRUNED"
        results = self.results_dir()
        first, second = self.client(DIST_ENTRY), self.client(DIST_ENTRY)
        query = "MATCH (a:Function)-[r:CodeRelation]->(b:Function) RETURN a.id, b.id, {} AS k"
        refs = [(k, self.bounded(self.distinct_large(first, k), marker)[1]) for k in range(700, 750)]
        k = 750
        for width in [4] * 20 + [24] * 3:
            oldest = min(refs, key=lambda entry: entry[1].stat().st_mtime if entry[1].exists() else 0)
            repeat = first.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": query.format(oldest[0])}})
            burst = [(k + i, second.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": query.format(k + i)}})) for i in range(width)]
            k += width
            for key, request_id, client in [(oldest[0], repeat, first)] + [(key, request_id, second) for key, request_id in burst]:
                response = client.receive(request_id)
                self.assertIsNotNone(response, marker + " (no response)")
                self.assertFalse(response["result"].get("isError"), marker + f": {response['result']['content'][0]['text'][:200]}")
                refs.append((key, self.bounded(response["result"]["content"][0]["text"], marker)[1]))
                # A path another process may be pruning is never rewritten: a repeat gets its own file.
                self.assertNotEqual(refs[-1][1], oldest[1], marker + ": a repeated result reused an existing path")
            missing = [entry for entry in refs[-(width + 1):] if not entry[1].is_file()]
            self.assertEqual(missing, [], marker + f": a reference returned this round was pruned: {missing[:3]}")
            refs = sorted(refs, key=lambda entry: entry[1].stat().st_mtime if entry[1].exists() else 0)[-50:]
        self.assertLessEqual(len(list(results.iterdir())), 50, marker + ": more than 50 entries after the last prune")

    def test_evicted_before_reply_is_a_bounded_error(self) -> None:
        # 50 entries newer than the call's own file at its prune (what a clock-skewed writer leaves) must not
        # yield a reference to a file the call itself just evicted.
        marker = "STALE_REFERENCE_RETURNED"
        results = self.results_dir()
        results.mkdir()
        future = time.time() + 3600
        for i in range(50):
            planted = results / f"cypher-future-{i}.json"
            planted.write_text("{}", encoding="utf-8")
            os.utime(planted, (future, future))
        client = self.client()
        response = client.request("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": EDGES}}, timeout=180)
        self.assertIsNotNone(response, marker + " (no response)")
        text = response["result"]["content"][0]["text"]
        self.assertEqual((response["result"].get("isError"), "complete_result" in text, "| f" in text), (True, False, False), marker + f": {text[:200]!r}")
        self.assertLess(len(text.encode()), 1024, marker + f": {len(text.encode())} bytes")
        self.assertRegex(text, r"\d{5,} bytes", marker + ": size not named")
        self.assertEqual(len(list(results.iterdir())), 50, marker + ": the evicted file or the planted entries changed")
        for planted in results.iterdir():
            planted.unlink()
        self.bounded(client.text("cypher", {"repo": REPO, "query": EDGES}, timeout=180), marker + " (after clearing)")

    def test_tied_timestamps_are_not_evicted_early(self) -> None:
        marker = "TIED_ENTRY_EVICTED_EARLY"
        results = self.results_dir()
        client = self.client()
        reference = self.bounded(self.distinct_large(client, 0), marker)[1]
        tied = [reference] + [results / f"cypher-tied-{i}.json" for i in range(10)]
        for entry in tied[1:]:
            entry.write_text("{}", encoding="utf-8")
        for entry in tied:
            os.utime(entry, (1000, 1000))
        for k in range(1, 50):
            self.distinct_large(client, k)
        self.assertEqual([entry for entry in tied if not entry.exists()], [], marker + ": a tied entry was evicted by 49 newer results")
        self.distinct_large(client, 50)
        self.assertEqual(([entry for entry in tied if entry.exists()], len(list(results.iterdir()))), ([], 50), marker + ": tied entries not evicted together by the 50th")

    def test_reference_survives_a_killed_server_and_its_residue(self) -> None:
        marker = "REFERENCE_LOST_ACROSS_RESTART"
        # SIGKILL once the reply's file appears: the residue (complete or partial) is an unreplied entry.
        # The reply can occasionally beat the kill; only an attempt that got no reply counts.
        for _ in range(5):
            results = self.results_dir()
            killed = self.client(DIST_ENTRY)
            reference = self.bounded(self.distinct_large(killed, 0), marker)[1]
            before = set(results.iterdir())
            interrupted = killed.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": EDGES}})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and set(results.iterdir()) == before:
                time.sleep(0.001)
            killed.process.kill()
            if killed.receive(interrupted, timeout=10) is None:
                break
        else:
            self.fail(marker + ": the reply beat the kill in 5 attempts")
        residue = set(results.iterdir()) - before
        self.assertEqual(len(residue), 1, marker + f": expected one residue entry, saw {residue}")
        fresh = self.client(DIST_ENTRY)
        published = [self.bounded(self.distinct_large(fresh, k), marker)[1] for k in range(1, 49)]
        self.assertTrue(reference.is_file(), marker + ": the reference did not survive 48 newer results plus the residue")
        published.append(self.bounded(self.distinct_large(fresh, 49), marker)[1])
        self.assertFalse(reference.exists(), marker + ": the 50th newer entry (48 + residue + 1) did not evict the reference")
        self.assertEqual([p for p in published if not p.is_file()], [], marker + ": a fresh server's reference is missing")
        self.assertEqual(residue & set(published), set(), marker + ": residue was returned as evidence")

    def test_overlapping_large_calls_each_retained(self) -> None:
        # Bursts make one call's write or prune race another call's prune listing.
        marker = "OVERLAP_RETENTION_BROKEN"
        client = self.client()
        query = "MATCH (a:Function)-[r:CodeRelation]->(b:Function) RETURN a.id, b.id, {} AS k"
        for first in range(200, 344, 24):
            ids = [(k, client.send("tools/call", {"name": "cypher", "arguments": {"repo": REPO, "query": query.format(k)}})) for k in range(first, first + 24)]
            for k, request_id in ids:
                response = client.receive(request_id)
                self.assertIsNotNone(response, marker + " (no response)")
                text = response["result"]["content"][0]["text"]
                self.assertFalse(response["result"].get("isError"), marker + f": k={k}: {text[:300]}")
                path = self.bounded(text, marker)[1]
                full = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual((full["row_count"], full["markdown"].endswith(f"| {k} |")), (1500, True), marker + f": retained result for k={k} incomplete")

    def test_tool_descriptions_target_one_question(self) -> None:
        marker = "DESCRIPTION_CHAINS_FOLLOWUPS"
        listed = self.client().request("tools/list", {})
        self.assertIsNotNone(listed, marker + " (no response)")
        tools = {tool["name"]: tool["description"] for tool in listed["result"]["tools"]}
        chained = [name for name, description in tools.items() if "AFTER THIS" in description or "schema first" in description]
        self.assertEqual(chained, [], marker + f": {chained}")
        self.assertNotIn("First step", tools["list_repos"], marker + ": list_repos is still a first step")
        self.assertIn("{id: ", tools["cypher"], marker + ": no exact-id example")
        self.assertIn("{type: 'CALLS'}", tools["cypher"], marker + ": no one-direction CALLS example")
        self.assertIn("relationships(p)", tools["cypher"], marker + ": crash guidance dropped")
        self.assertIn("impact", tools["cypher"], marker + ": crash alternative dropped")

    def test_cli_large_result_arrives_complete(self) -> None:
        marker = "CLI_RESULT_BOUNDED"
        # stdout goes to a file as the packet producer does: the CLI's fd-1 write stops at one pipe buffer (#28).
        with (HOME / "cli-edges.json").open("w", encoding="utf-8") as out:
            subprocess.run([*DIST_ENTRY, "cypher", "-r", REPO, EDGES], cwd=PACKAGE, stdout=out, stderr=subprocess.PIPE,
                           env={**os.environ, "HOME": str(HOME)}, timeout=300)
        text = (HOME / "cli-edges.json").read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertEqual((payload["row_count"], len(payload["markdown"]) > 65536, "[gitnexus]" in text), (1500, True, False), marker)

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

    def test_unknown_repo_reply_carries_no_catalogue(self) -> None:
        # X6R11 03:03:31Z: two invalid selectors returned 10,041 bytes each of unrelated registry names.
        marker = "NOT_FOUND_REPLY_LISTS_REGISTRY"
        result = self.client().request("tools/call", {"name": "cypher", "arguments": {"repo": "nope", "query": COUNT}})
        self.assertIsNotNone(result, marker + " (no response)")
        text = result["result"]["content"][0]["text"]
        self.assertIn('"nope"', text, marker + " (selector missing)")
        self.assertNotIn(REPO, text, marker + f": the registered name leaked: {text[:200]}")
        self.assertLess(len(text.encode("utf-8")), 500, marker + f": {len(text.encode('utf-8'))} bytes")

    def test_unknown_repo_reply_bounds_the_selector_echo(self) -> None:
        marker = "NOT_FOUND_REPLY_UNBOUNDED_SELECTOR_ECHO"
        result = self.client().request("tools/call", {"name": "cypher", "arguments": {"repo": "x" * 3000, "query": COUNT}})
        self.assertIsNotNone(result, marker + " (no response)")
        text = result["result"]["content"][0]["text"]
        self.assertIn("not found", text, marker + f": {text[:200]}")
        self.assertNotIn(REPO, text, marker + f": the registered name leaked: {text[:200]}")
        self.assertLess(len(text.encode("utf-8")), 500, marker + f": {len(text.encode('utf-8'))} bytes")

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*SOURCE_ENTRY, *args], cwd=PACKAGE, text=True, capture_output=True,
                              env={**os.environ, "HOME": str(HOME)}, timeout=300)

    def test_cli_unknown_repo_is_an_error_not_a_stack_trace(self) -> None:
        marker = "CLI_NOT_FOUND_DUMPS_STACK"
        result = self.cli("cypher", "-r", "nope", COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("nope", result.stdout, marker + f": {result.stdout[:200]}")
        self.assertNotIn("    at ", result.stdout + result.stderr, marker + " (stack trace printed)")
        combined = (result.stdout + result.stderr).encode("utf-8")
        self.assertLess(len(combined), 500, marker + f": {len(combined)} bytes")

    def test_cli_structured_error_exits_one(self) -> None:
        marker = "CLI_STRUCTURED_ERROR_EXITS_ZERO"
        result = self.cli("cypher", "-r", REPO, "THIS IS NOT CYPHER")
        self.assertIn("error", json.loads(result.stdout), marker + f": {result.stdout[:200]}")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")

    def test_cli_context_not_found_exits_one(self) -> None:
        marker = "CLI_CONTEXT_ERROR_EXITS_ZERO"
        result = self.cli("context", "nope", "-r", REPO)
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")

    def test_cli_valid_cypher_exits_zero(self) -> None:
        marker = "CLI_SUCCESS_EXIT_CHANGED"
        result = self.cli("cypher", "-r", REPO, COUNT)
        self.assertEqual(result.returncode, 0, marker + f": exit {result.returncode} {result.stderr[-300:]}")
        self.assertEqual(json.loads(result.stdout).get("row_count"), 1, marker + f": {result.stdout[:200]}")

    def test_cli_impact_unknown_target_exits_one(self) -> None:
        marker = "CLI_IMPACT_ERROR_EXITS_ZERO"
        result = self.cli("impact", "nope", "-r", REPO)
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")

    def test_cli_not_found_reply_bounds_serialized_echo(self) -> None:
        # JSON.stringify writes U+0001 as six ASCII bytes; the bound has to hold after that expansion.
        marker = "CLI_NOT_FOUND_SERIALIZED_ECHO_UNBOUNDED"
        result = self.cli("cypher", "-r", "\x01" * 120, COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("not found", result.stdout, marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

    def test_cli_not_found_reply_names_a_multibyte_selector_by_size(self) -> None:
        marker = "NOT_FOUND_MULTIBYTE_CUTOFF_UNBOUNDED"
        result = self.cli("cypher", "-r", "é" * 100, COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("selector of 200 bytes", result.stdout, marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

    def test_cli_query_unknown_repo_is_an_error_not_a_stack_trace(self) -> None:
        marker = "CLI_QUERY_NOT_FOUND_DUMPS_STACK"
        result = self.cli("query", "-r", "nope", "anything")
        self.assertNotIn("    at ", result.stdout + result.stderr, marker + " (stack trace printed)")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")

    def test_cli_context_unknown_repo_is_an_error_not_a_stack_trace(self) -> None:
        marker = "CLI_CONTEXT_NOT_FOUND_DUMPS_STACK"
        result = self.cli("context", "nope", "-r", "nope")
        self.assertNotIn("    at ", result.stdout + result.stderr, marker + " (stack trace printed)")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")

    def test_cli_impact_unknown_repo_is_an_error_not_a_stack_trace(self) -> None:
        marker = "CLI_IMPACT_NOT_FOUND_DUMPS_STACK"
        result = self.cli("impact", "target", "-r", "nope")
        self.assertNotIn("    at ", result.stdout + result.stderr, marker + " (stack trace printed)")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")

    def test_cli_impact_not_found_reply_does_not_echo_the_target(self) -> None:
        marker = "CLI_IMPACT_NOT_FOUND_ENVELOPE_UNBOUNDED"
        result = self.cli("impact", "a" * 600, "-r", "nope")
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("nope", str(json.loads(result.stdout).get("error")), marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

    def test_cli_query_success_exits_zero(self) -> None:
        marker = "CLI_QUERY_SUCCESS_EXIT_CHANGED"
        result = self.cli("query", "target", "-r", REPO)
        self.assertEqual(result.returncode, 0, marker + f": exit {result.returncode} {result.stderr[-300:]}")
        self.assertIn("definitions", json.loads(result.stdout), marker + f": {result.stdout[:200]}")

    def test_cli_context_success_exits_zero(self) -> None:
        marker = "CLI_CONTEXT_SUCCESS_EXIT_CHANGED"
        result = self.cli("context", "target", "-r", REPO)
        self.assertEqual(result.returncode, 0, marker + f": exit {result.returncode} {result.stderr[-300:]}")
        self.assertEqual(json.loads(result.stdout).get("status"), "found", marker + f": {result.stdout[:200]}")

    def test_cli_impact_success_exits_zero(self) -> None:
        marker = "CLI_IMPACT_SUCCESS_EXIT_CHANGED"
        result = self.cli("impact", "target", "-r", REPO)
        self.assertEqual(result.returncode, 0, marker + f": exit {result.returncode} {result.stderr[-300:]}")
        self.assertEqual(json.loads(result.stdout).get("target", {}).get("name"), "target", marker + f": {result.stdout[:200]}")

    def test_cli_not_found_reply_names_a_quote_selector_by_size(self) -> None:
        marker = "NOT_FOUND_QUOTE_CUTOFF_UNBOUNDED"
        result = self.cli("cypher", "-r", '"' * 61, COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("selector of 61 bytes", result.stdout, marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

    def cli_with_full_stdout(self, *args: str) -> subprocess.CompletedProcess[str]:
        # The emitter's own outgoing boundary: fd 1 refuses every write, so the fallback path runs.
        if not os.path.exists("/dev/full"):
            self.skipTest("/dev/full is not available on this platform")
        with open("/dev/full", "w") as full:
            return subprocess.run([*SOURCE_ENTRY, *args], cwd=PACKAGE, text=True, stdout=full, stderr=subprocess.PIPE,
                                  env={**os.environ, "HOME": str(HOME)}, timeout=300)

    def test_cli_success_survives_a_failed_stdout_write(self) -> None:
        marker = "CLI_STDOUT_FAILURE_LOSES_RESULT"
        result = self.cli_with_full_stdout("cypher", "-r", REPO, COUNT)
        self.assertEqual(result.returncode, 0, marker + f": exit {result.returncode} {result.stderr[-300:]}")
        self.assertEqual(json.loads(result.stderr).get("row_count"), 1, marker + f": {result.stderr[:200]}")

    def test_cli_error_survives_a_failed_stdout_write(self) -> None:
        marker = "CLI_STDOUT_FAILURE_LOSES_ERROR"
        result = self.cli_with_full_stdout("cypher", "-r", "nope", COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("not found", str(json.loads(result.stderr).get("error")), marker + f": {result.stderr[:200]}")

    def test_cli_not_found_reply_names_a_backslash_selector_by_size(self) -> None:
        marker = "NOT_FOUND_BACKSLASH_CUTOFF_UNBOUNDED"
        result = self.cli("cypher", "-r", "\\" * 61, COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn("selector of 61 bytes", result.stdout, marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

    def test_cli_not_found_reply_echoes_a_selector_that_fits(self) -> None:
        # Sixty quotes serialize to exactly the 122-byte budget: admitted, echoed escaped, still bounded.
        marker = "NOT_FOUND_ADMITTED_SELECTOR_DROPPED"
        result = self.cli("cypher", "-r", '"' * 60, COUNT)
        self.assertEqual(result.returncode, 1, marker + f": exit {result.returncode}")
        self.assertIn(json.dumps('"' * 60), json.loads(result.stdout).get("error", ""), marker + f": {result.stdout[:200]}")
        self.assertLess(len(result.stdout.encode("utf-8")), 500, marker + f": {len(result.stdout.encode('utf-8'))} bytes")

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
