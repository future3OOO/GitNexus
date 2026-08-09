#!/usr/bin/env node
/**
 * GitNexus Codex Hook
 *
 * Codex currently exposes Bash hook events only.
 * This hook augments Bash search commands with graph context and detects
 * stale GitNexus indexes after successful git mutations.
 */

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const AUGMENT_LOCK_DIR = 'codex-augment.lock';
const AUGMENT_LOCK_OWNER = 'owner.json';
const AUGMENT_LOCK_TTL_MS = 15000;
const SAFE_AUGMENT_PATTERN_RE = /^[A-Za-z0-9_:@./-]{3,}$/;

function readInput() {
  try {
    const data = fs.readFileSync(0, 'utf-8');
    return JSON.parse(data);
  } catch {
    return {};
  }
}

function findGitNexusDir(startDir) {
  let dir = startDir || process.cwd();
  for (let i = 0; i < 5; i++) {
    const candidate = path.join(dir, '.gitnexus');
    if (fs.existsSync(candidate)) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function extractPattern(command) {
  if (!/\brg\b|\bgrep\b/.test(command)) return null;

  const tokens = command.split(/\s+/);
  let foundCmd = false;
  let skipNext = false;
  const flagsWithValues = new Set([
    '-e',
    '-f',
    '-m',
    '-A',
    '-B',
    '-C',
    '-g',
    '--glob',
    '-t',
    '--type',
    '--include',
    '--exclude',
  ]);

  for (const token of tokens) {
    if (skipNext) {
      skipNext = false;
      continue;
    }
    if (!foundCmd) {
      if (/\brg$|\bgrep$/.test(token)) foundCmd = true;
      continue;
    }
    if (token.startsWith('-')) {
      if (flagsWithValues.has(token)) skipNext = true;
      continue;
    }
    const cleaned = token.replace(/['"]/g, '');
    return cleaned.length >= 3 ? cleaned : null;
  }

  return null;
}

function extractBashResult(input) {
  const response = input.tool_response ?? input.tool_output;
  if (response && typeof response === 'object') return response;
  if (typeof response !== 'string' || !response.trim()) return null;

  try {
    return JSON.parse(response);
  } catch {
    return null;
  }
}

function isSuccessfulBashResult(result) {
  if (!result || typeof result !== 'object') return false;
  if (typeof result.exit_code === 'number') return result.exit_code === 0;
  if (typeof result.exitCode === 'number') return result.exitCode === 0;
  return false;
}

function shouldAugmentPattern(pattern) {
  if (!pattern || pattern.length < 3) return false;
  // Hook-mode augmentation should stay on literal-ish tokens only. Regex-heavy rg
  // patterns drive Ladybug FTS wildcard execution, which is currently unstable.
  return SAFE_AUGMENT_PATTERN_RE.test(pattern);
}

function resolveCliPath() {
  const envCliPath = process.env.GITNEXUS_CLI_PATH || '';
  if (envCliPath) return envCliPath;
  let cliPath = path.resolve(__dirname, '..', '..', 'dist', 'cli', 'index.js');
  if (!fs.existsSync(cliPath)) {
    try {
      cliPath = require.resolve('gitnexus/dist/cli/index.js');
    } catch {
      cliPath = '';
    }
  }
  return cliPath;
}

function runGitNexusCli(cliPath, args, cwd, timeout) {
  const isWin = process.platform === 'win32';
  if (cliPath) {
    return spawnSync(process.execPath, [cliPath, ...args], {
      encoding: 'utf-8',
      timeout,
      cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  }
  return spawnSync(isWin ? 'npx.cmd' : 'npx', ['-y', 'gitnexus', ...args], {
    encoding: 'utf-8',
    timeout: timeout + 5000,
    cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
}

function sendHookResponse(hookEventName, message) {
  console.log(
    JSON.stringify({
      hookSpecificOutput: { hookEventName, additionalContext: message },
    }),
  );
}

function readAugmentLockOwner(lockDir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(lockDir, AUGMENT_LOCK_OWNER), 'utf-8'));
  } catch {
    return null;
  }
}

function isProcessAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err && err.code === 'EPERM';
  }
}

function removeAugmentLock(lockDir) {
  try {
    fs.rmSync(lockDir, { recursive: true, force: true });
  } catch {
    /* best effort cleanup */
  }
}

function shouldClearAugmentLock(lockDir, now) {
  const owner = readAugmentLockOwner(lockDir);
  if (owner && isProcessAlive(owner.pid)) return false;
  if (owner) return true;

  try {
    const stat = fs.statSync(lockDir);
    return now - stat.mtimeMs > AUGMENT_LOCK_TTL_MS;
  } catch {
    return false;
  }
}

function tryAcquireAugmentLock(gitNexusDir) {
  const lockDir = path.join(gitNexusDir, AUGMENT_LOCK_DIR);
  const now = Date.now();

  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      fs.mkdirSync(lockDir);
      fs.writeFileSync(
        path.join(lockDir, AUGMENT_LOCK_OWNER),
        JSON.stringify({ pid: process.pid, createdAt: now }),
        'utf-8',
      );
      return () => removeAugmentLock(lockDir);
    } catch (err) {
      if (!err || err.code !== 'EEXIST') return null;
      if (!shouldClearAugmentLock(lockDir, now)) return null;
      removeAugmentLock(lockDir);
    }
  }

  return null;
}

function getSearchAugmentation(command, cwd, gitNexusDir) {
  const searchPattern = extractPattern(command);
  if (!shouldAugmentPattern(searchPattern)) return '';

  const releaseLock = tryAcquireAugmentLock(gitNexusDir);
  if (!releaseLock) return '';

  const cliPath = resolveCliPath();
  let result = '';
  try {
    const child = runGitNexusCli(cliPath, ['augment', '--', searchPattern], cwd, 7000);
    if (!child.error && child.status === 0) {
      result = child.stderr || '';
    }
  } catch {
    /* graceful failure */
  } finally {
    releaseLock();
  }

  return result && result.trim() ? result.trim() : '';
}

function getStaleIndexWarning(command, cwd, gitNexusDir, bashResult) {
  if (!/\bgit\s+(commit|merge|rebase|cherry-pick|pull)(\s|$)/.test(command)) return '';
  if (!isSuccessfulBashResult(bashResult)) return '';

  let currentHead = '';
  try {
    const headResult = spawnSync('git', ['rev-parse', 'HEAD'], {
      encoding: 'utf-8',
      timeout: 3000,
      cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    currentHead = (headResult.stdout || '').trim();
  } catch {
    return '';
  }

  if (!currentHead) return '';

  let lastCommit = '';
  let hadEmbeddings = false;
  try {
    const meta = JSON.parse(fs.readFileSync(path.join(gitNexusDir, 'meta.json'), 'utf-8'));
    lastCommit = meta.lastCommit || '';
    hadEmbeddings = meta.stats && meta.stats.embeddings > 0;
  } catch {
    /* no meta — treat as stale */
  }

  if (currentHead && currentHead === lastCommit) return '';

  const analyzeCmd = `npx gitnexus analyze${hadEmbeddings ? ' --embeddings' : ''}`;
  return (
    `GitNexus index is stale (last indexed: ${lastCommit ? lastCommit.slice(0, 7) : 'never'}). ` +
    `Run \`${analyzeCmd}\` to update the knowledge graph.`
  );
}

function handlePostToolUse(input) {
  if ((input.tool_name || '') !== 'Bash') return;

  const command = (input.tool_input || {}).command || '';
  const cwd = input.cwd || process.cwd();
  if (!path.isAbsolute(cwd)) return;

  const gitNexusDir = findGitNexusDir(cwd);
  if (!gitNexusDir) return;

  const bashResult = extractBashResult(input);
  const messages = [
    getSearchAugmentation(command, cwd, gitNexusDir),
    getStaleIndexWarning(command, cwd, gitNexusDir, bashResult),
  ].filter(Boolean);

  if (messages.length > 0) {
    sendHookResponse('PostToolUse', messages.join('\n\n'));
  }
}

const handlers = {
  PostToolUse: handlePostToolUse,
};

function main() {
  try {
    const input = readInput();
    const handler = handlers[input.hook_event_name || ''];
    if (handler) handler(input);
  } catch (err) {
    if (process.env.GITNEXUS_DEBUG) {
      console.error('GitNexus hook error:', (err.message || '').slice(0, 200));
    }
  }
}

main();
