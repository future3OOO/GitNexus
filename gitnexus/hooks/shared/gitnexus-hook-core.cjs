const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const CLI_PATH_PLACEHOLDER = '__GITNEXUS_CLI_PATH__';

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

function extractBashSearchPattern(command) {
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

function extractSearchPattern(toolName, toolInput) {
  if (toolName === 'Grep') {
    return toolInput.pattern || null;
  }

  if (toolName === 'Glob') {
    const raw = toolInput.pattern || '';
    const match = raw.match(/[*\/]([a-zA-Z][a-zA-Z0-9_-]{2,})/);
    return match ? match[1] : null;
  }

  if (toolName === 'Bash') {
    return extractBashSearchPattern(toolInput.command || '');
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

function spawnCliProcess(cliPath, args, cwd, timeout) {
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

function createBundledCliRunner(options) {
  const { fallbackCliPath, injectedCliPath } = options;

  return function runGitNexusCli(args, cwd, timeout) {
    let cliPath = process.env.GITNEXUS_HOOK_CLI_PATH || injectedCliPath || '';
    if (!cliPath || cliPath === CLI_PATH_PLACEHOLDER) {
      cliPath = fallbackCliPath;
    }
    if (cliPath && !fs.existsSync(cliPath)) {
      try {
        cliPath = require.resolve('gitnexus/dist/cli/index.js');
      } catch {
        cliPath = '';
      }
    }
    return spawnCliProcess(cliPath, args, cwd, timeout);
  };
}

function createBinaryCliRunner() {
  return function runGitNexusCli(args, cwd, timeout) {
    const overrideCliPath = process.env.GITNEXUS_HOOK_CLI_PATH || '';
    if (overrideCliPath) {
      return spawnCliProcess(overrideCliPath, args, cwd, timeout);
    }

    const isWin = process.platform === 'win32';
    let useDirectBinary = false;
    try {
      const which = spawnSync(isWin ? 'where' : 'which', ['gitnexus'], {
        encoding: 'utf-8',
        timeout: 3000,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      useDirectBinary = which.status === 0;
    } catch {
      /* not on PATH */
    }

    if (useDirectBinary) {
      return spawnSync(isWin ? 'gitnexus.cmd' : 'gitnexus', args, {
        encoding: 'utf-8',
        timeout,
        cwd,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    }

    return spawnCliProcess('', args, cwd, timeout);
  };
}

function sendHookResponse(hookEventName, message) {
  console.log(
    JSON.stringify({
      hookSpecificOutput: { hookEventName, additionalContext: message },
    }),
  );
}

function getSearchAugmentation(toolName, toolInput, cwd, runGitNexusCli) {
  const searchPattern = extractSearchPattern(toolName, toolInput);
  if (!searchPattern || searchPattern.length < 3) return '';

  let result = '';
  try {
    const child = runGitNexusCli(['augment', '--', searchPattern], cwd, 7000);
    if (!child.error && child.status === 0) {
      result = child.stderr || '';
    }
  } catch {
    /* graceful failure */
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

  if (currentHead === lastCommit) return '';

  const analyzeCmd = `npx gitnexus analyze${hadEmbeddings ? ' --embeddings' : ''}`;
  return (
    `GitNexus index is stale (last indexed: ${lastCommit ? lastCommit.slice(0, 7) : 'never'}). ` +
    `Run \`${analyzeCmd}\` to update the knowledge graph.`
  );
}

function createClaudeHandlers(runGitNexusCli) {
  return {
    PreToolUse(input) {
      const cwd = input.cwd || process.cwd();
      if (!path.isAbsolute(cwd)) return;
      if (!findGitNexusDir(cwd)) return;

      const toolName = input.tool_name || '';
      if (toolName !== 'Grep' && toolName !== 'Glob' && toolName !== 'Bash') return;

      const result = getSearchAugmentation(toolName, input.tool_input || {}, cwd, runGitNexusCli);
      if (result) {
        sendHookResponse('PreToolUse', result);
      }
    },

    PostToolUse(input) {
      if ((input.tool_name || '') !== 'Bash') return;

      const command = (input.tool_input || {}).command || '';
      const cwd = input.cwd || process.cwd();
      if (!path.isAbsolute(cwd)) return;

      const gitNexusDir = findGitNexusDir(cwd);
      if (!gitNexusDir) return;

      const warning = getStaleIndexWarning(command, cwd, gitNexusDir, input.tool_output || {});
      if (warning) {
        sendHookResponse('PostToolUse', warning);
      }
    },
  };
}

function createCodexHandlers(runGitNexusCli) {
  return {
    PostToolUse(input) {
      if ((input.tool_name || '') !== 'Bash') return;

      const command = (input.tool_input || {}).command || '';
      const cwd = input.cwd || process.cwd();
      if (!path.isAbsolute(cwd)) return;

      const gitNexusDir = findGitNexusDir(cwd);
      if (!gitNexusDir) return;

      const bashResult = extractBashResult(input);
      const messages = [
        getSearchAugmentation('Bash', { command }, cwd, runGitNexusCli),
        getStaleIndexWarning(command, cwd, gitNexusDir, bashResult),
      ].filter(Boolean);

      if (messages.length > 0) {
        sendHookResponse('PostToolUse', messages.join('\n\n'));
      }
    },
  };
}

function runHookMain(handlers) {
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

module.exports = {
  CLI_PATH_PLACEHOLDER,
  createBinaryCliRunner,
  createBundledCliRunner,
  createClaudeHandlers,
  createCodexHandlers,
  extractBashResult,
  extractSearchPattern,
  findGitNexusDir,
  getSearchAugmentation,
  getStaleIndexWarning,
  isSuccessfulBashResult,
  runHookMain,
};
