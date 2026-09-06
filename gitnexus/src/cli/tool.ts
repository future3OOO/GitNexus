/**
 * Direct CLI Tool Commands
 *
 * Exposes GitNexus tools (query, context, impact, cypher) as direct CLI commands.
 * Bypasses MCP entirely — invokes LocalBackend directly for minimal overhead.
 *
 * Usage:
 *   gitnexus query "authentication flow"
 *   gitnexus context --name "validateUser"
 *   gitnexus impact --target "AuthService" --direction upstream
 *   gitnexus cypher "MATCH (n:Function) RETURN n.name LIMIT 10"
 *
 * Note: Output goes to stdout via fs.writeSync(fd 1), bypassing LadybugDB's
 * native module which captures the Node.js process.stdout stream during init.
 * See the output() function for details (#324).
 */

import { writeSync } from 'node:fs';
import { LocalBackend } from '../mcp/local/local-backend.js';

let _backend: LocalBackend | null = null;

async function getBackend(): Promise<LocalBackend> {
  if (_backend) return _backend;
  const backend = new LocalBackend();
  if (!(await backend.init())) {
    // Thrown, not printed: every command answers with its own JSON error and exit 1.
    throw new Error('No indexed repositories found. Run: gitnexus analyze');
  }
  _backend = backend;
  return _backend;
}

/**
 * Write tool output to stdout using low-level fd write.
 *
 * LadybugDB's native module captures Node.js process.stdout during init,
 * but the underlying OS file descriptor 1 (stdout) remains intact.
 * By using fs.writeSync(1, ...) we bypass the Node.js stream layer
 * and write directly to the real stdout fd (#324).
 *
 * Falls back to stderr if the fd write fails (e.g., broken pipe).
 *
 * A structured `{ error }` result exits 1: the JSON is the answer, the exit
 * status is the verdict, so scripts and hooks can branch on it. The status is
 * set, not forced, so a stderr fallback on an asynchronous pipe still flushes.
 */
function output(data: any): void {
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  try {
    writeSync(1, text + '\n');
  } catch (err: any) {
    if (err?.code === 'EPIPE') {
      // Consumer closed the pipe (e.g., `gitnexus cypher ... | head -1`)
      // Exit cleanly per Unix convention
      process.exit(0);
    }
    // Fallback: stderr (previous behavior, works on all platforms)
    process.stderr.write(text + '\n');
  }
  if (data && typeof data === 'object' && 'error' in data) process.exitCode = 1;
}

/**
 * Run one tool call and hand its result to output(). A thrown failure — a bad
 * repo selector, a transport fault — is an answer, not a stack trace.
 */
async function call(name: string, args: Record<string, unknown>): Promise<void> {
  try {
    const backend = await getBackend();
    output(await backend.callTool(name, args));
  } catch (err: unknown) {
    output({
      error: (err instanceof Error ? err.message : String(err)) || `${name} failed unexpectedly`,
    });
  }
}

export async function queryCommand(
  queryText: string,
  options?: {
    repo?: string;
    context?: string;
    goal?: string;
    limit?: string;
    content?: boolean;
  },
): Promise<void> {
  if (!queryText?.trim()) {
    console.error('Usage: gitnexus query <search_query>');
    process.exit(1);
  }

  await call('query', {
    query: queryText,
    task_context: options?.context,
    goal: options?.goal,
    limit: options?.limit ? parseInt(options.limit) : undefined,
    include_content: options?.content ?? false,
    repo: options?.repo,
  });
}

export async function contextCommand(
  name: string,
  options?: {
    repo?: string;
    file?: string;
    uid?: string;
    content?: boolean;
  },
): Promise<void> {
  if (!name?.trim() && !options?.uid) {
    console.error('Usage: gitnexus context <symbol_name> [--uid <uid>] [--file <path>]');
    process.exit(1);
  }

  await call('context', {
    name: name || undefined,
    uid: options?.uid,
    file_path: options?.file,
    include_content: options?.content ?? false,
    repo: options?.repo,
  });
}

export async function impactCommand(
  target: string | undefined,
  options?: {
    direction?: string;
    repo?: string;
    depth?: string;
    includeTests?: boolean;
    uid?: string;
  },
): Promise<void> {
  if (!target?.trim() && !options?.uid?.trim()) {
    console.error(
      'Usage: gitnexus impact <symbol_name> [--uid <uid>] [--direction upstream|downstream]',
    );
    process.exit(1);
  }

  await call('impact', {
    target: target || undefined,
    uid: options?.uid,
    direction: options?.direction || 'upstream',
    maxDepth: options?.depth ? parseInt(options.depth, 10) : undefined,
    includeTests: options?.includeTests ?? false,
    repo: options?.repo,
  });
}

export async function detectChangesCommand(options?: {
  repo?: string;
  scope?: string;
  baseRef?: string;
  worktree?: string;
}): Promise<void> {
  await call('detect_changes', {
    scope: options?.scope,
    base_ref: options?.baseRef,
    worktree: options?.worktree,
    repo: options?.repo,
  });
}

export async function cypherCommand(
  query: string,
  options?: {
    repo?: string;
  },
): Promise<void> {
  if (!query?.trim()) {
    console.error('Usage: gitnexus cypher <cypher_query>');
    process.exit(1);
  }

  await call('cypher', { query, repo: options?.repo });
}
