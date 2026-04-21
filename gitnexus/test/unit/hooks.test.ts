/**
 * Regression Tests: Claude Code Hooks
 *
 * Tests the hook scripts (gitnexus-hook.cjs and gitnexus-hook.js) that run
 * as PreToolUse and PostToolUse hooks in Claude Code.
 *
 * Covers:
 * - extractPattern: pattern extraction from Grep/Glob/Bash tool inputs
 * - findGitNexusDir: .gitnexus directory discovery
 * - handlePostToolUse: staleness detection after git mutations
 * - cwd validation: rejects relative paths (defense-in-depth)
 * - shell injection: verifies no shell: true in spawnSync calls
 * - dispatch map: correct handler routing
 * - cross-platform: Windows .cmd extension handling
 *
 * Since the hooks are CJS scripts that call main() on load, we test them
 * by spawning them as child processes with controlled stdin JSON.
 */
import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { runHook, runHookWithEnv, parseHookOutput } from '../utils/hook-test-helpers.js';

// ─── Paths to both hook variants ────────────────────────────────────

const CJS_HOOK = path.resolve(__dirname, '..', '..', 'hooks', 'claude', 'gitnexus-hook.cjs');
const CODEX_HOOK = path.resolve(__dirname, '..', '..', 'hooks', 'codex', 'gitnexus-hook.cjs');
const PLUGIN_HOOK = path.resolve(
  __dirname,
  '..',
  '..',
  '..',
  'gitnexus-claude-plugin',
  'hooks',
  'gitnexus-hook.js',
);

// ─── Test fixtures: temporary .gitnexus directory ───────────────────

let tmpDir: string;
let gitNexusDir: string;
let fakeCliPath: string;
let augmentLockDir: string;
let augmentLockOwnerPath: string;

beforeAll(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gitnexus-hook-test-'));
  gitNexusDir = path.join(tmpDir, '.gitnexus');
  fs.mkdirSync(gitNexusDir, { recursive: true });

  // Initialize a bare git repo so git rev-parse HEAD works
  spawnSync('git', ['init'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.email', 'test@test.com'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['config', 'user.name', 'Test'], { cwd: tmpDir, stdio: 'pipe' });
  fs.writeFileSync(path.join(tmpDir, 'dummy.txt'), 'hello');
  spawnSync('git', ['add', '.'], { cwd: tmpDir, stdio: 'pipe' });
  spawnSync('git', ['commit', '-m', 'init'], { cwd: tmpDir, stdio: 'pipe' });
  augmentLockDir = path.join(gitNexusDir, 'codex-augment.lock');
  augmentLockOwnerPath = path.join(augmentLockDir, 'owner.json');

  fakeCliPath = path.join(tmpDir, 'fake-gitnexus-cli.cjs');
  fs.writeFileSync(
    fakeCliPath,
    [
      '#!/usr/bin/env node',
      "if (process.argv[2] === 'augment') {",
      "  process.stderr.write('FAKE_AUGMENT_OUTPUT');",
      '}',
      '',
    ].join('\n'),
    'utf-8',
  );
});

afterAll(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

// ─── Helper to get HEAD commit hash ─────────────────────────────────

function getHeadCommit(): string {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], {
    cwd: tmpDir,
    encoding: 'utf-8',
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  return (result.stdout || '').trim();
}

// ─── Both hook files should exist ───────────────────────────────────

describe('Hook files exist', () => {
  it('CJS hook exists', () => {
    expect(fs.existsSync(CJS_HOOK)).toBe(true);
  });

  it('Codex hook exists', () => {
    expect(fs.existsSync(CODEX_HOOK)).toBe(true);
  });

  it('Plugin hook exists', () => {
    expect(fs.existsSync(PLUGIN_HOOK)).toBe(true);
  });
});

// ─── Source code regression: no shell: true ──────────────────────────

describe('Shell injection regression', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook has no shell: true in spawnSync calls`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      // Match spawnSync calls with shell option set to true or a variable
      // Allowed: comments mentioning shell: true, string literals
      const lines = source.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        // Skip comments and string literals
        if (line.trim().startsWith('//') || line.trim().startsWith('*')) continue;
        // Check for shell: true or shell: isWin in actual code
        if (/shell:\s*(true|isWin)/.test(line)) {
          throw new Error(`${label} hook line ${i + 1} has shell injection risk: ${line.trim()}`);
        }
      }
    });
  }
});

// ─── Source code regression: .cmd extensions for Windows ─────────────

describe('Windows .cmd extension handling', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook uses .cmd extensions for Windows npx`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('npx.cmd');
    });
  }

  it('Plugin hook uses .cmd extension for Windows gitnexus binary', () => {
    const source = fs.readFileSync(PLUGIN_HOOK, 'utf-8');
    expect(source).toContain('gitnexus.cmd');
  });
});

// ─── Source code regression: cwd validation ─────────────────────────

describe('cwd validation guards', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook validates cwd is absolute path`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      const cwdChecks = (source.match(/path\.isAbsolute\(cwd\)/g) || []).length;
      // Should have at least 2 checks (one in PreToolUse, one in PostToolUse)
      expect(cwdChecks).toBeGreaterThanOrEqual(2);
    });
  }
});

// ─── Source code regression: sendHookResponse used consistently ──────

describe('sendHookResponse consistency', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook uses sendHookResponse in both handlers`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      const calls = (source.match(/sendHookResponse\(/g) || []).length;
      // At least 3: definition + PreToolUse call + PostToolUse call
      expect(calls).toBeGreaterThanOrEqual(3);
    });

    it(`${label} hook does not inline hookSpecificOutput JSON in handlers`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      // Count inline hookSpecificOutput usage (should only be in sendHookResponse definition)
      const inlineCount = (source.match(/hookSpecificOutput/g) || []).length;
      // Exactly 1 occurrence: inside the sendHookResponse function body
      expect(inlineCount).toBe(1);
    });
  }
});

// ─── Source code regression: dispatch map pattern ────────────────────

describe('Dispatch map pattern', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook uses dispatch map instead of if/else`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('const handlers = {');
      expect(source).toContain('PreToolUse: handlePreToolUse');
      expect(source).toContain('PostToolUse: handlePostToolUse');
      // Should NOT have if/else dispatch in main()
      expect(source).not.toMatch(/if\s*\(hookEvent\s*===\s*'PreToolUse'\)/);
    });
  }
});

describe('Codex hook isolation', () => {
  it('Claude hook remains Claude-only and does not parse Codex tool_response payloads', () => {
    const source = fs.readFileSync(CJS_HOOK, 'utf-8');
    expect(source).toContain('GitNexus Claude Code Hook');
    expect(source).not.toContain('tool_response');
  });

  it('Codex hook is PostToolUse-only and parses tool_response payloads', () => {
    const source = fs.readFileSync(CODEX_HOOK, 'utf-8');
    expect(source).toContain('GitNexus Codex Hook');
    expect(source).toContain('tool_response');
    expect(source).toContain('const handlers = {');
    expect(source).toContain('PostToolUse: handlePostToolUse');
    expect(source).not.toContain('PreToolUse: handlePreToolUse');
  });

  it('Codex hook has no shell: true in spawnSync calls', () => {
    const source = fs.readFileSync(CODEX_HOOK, 'utf-8');
    const lines = source.split('\n');
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.trim().startsWith('//') || line.trim().startsWith('*')) continue;
      if (/shell:\s*(true|isWin)/.test(line)) {
        throw new Error(`Codex hook line ${i + 1} has shell injection risk: ${line.trim()}`);
      }
    }
  });

  it('Codex hook uses .cmd extension for Windows npx', () => {
    const source = fs.readFileSync(CODEX_HOOK, 'utf-8');
    expect(source).toContain('npx.cmd');
  });
});

// ─── Source code regression: debug error truncation ──────────────────

describe('Debug error message truncation', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook truncates error messages to 200 chars`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('.slice(0, 200)');
    });
  }
});

// ─── extractPattern regression (via source analysis) ────────────────

describe('extractPattern coverage', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook extracts pattern from Grep tool input`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain("toolName === 'Grep'");
      expect(source).toContain('toolInput.pattern');
    });

    it(`${label} hook extracts pattern from Glob tool input`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain("toolName === 'Glob'");
    });

    it(`${label} hook extracts pattern from Bash grep/rg commands`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toMatch(/\\brg\\b.*\\bgrep\\b/);
    });

    it(`${label} hook rejects patterns shorter than 3 chars`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('cleaned.length >= 3');
    });
  }
});

// ─── PostToolUse: git mutation regex coverage ───────────────────────

describe('Git mutation regex', () => {
  const GIT_REGEX = /\\bgit\\s\+\(commit\|merge\|rebase\|cherry-pick\|pull\)/;

  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label} hook detects git commit`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('commit');
    });

    it(`${label} hook detects git merge`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('merge');
    });

    it(`${label} hook detects git rebase`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('rebase');
    });

    it(`${label} hook detects git cherry-pick`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      expect(source).toContain('cherry-pick');
    });

    it(`${label} hook detects git pull`, () => {
      const source = fs.readFileSync(hookPath, 'utf-8');
      // 'pull' in the regex alternation
      expect(source).toMatch(/commit\|merge\|rebase\|cherry-pick\|pull/);
    });
  }
});

// ─── Integration: PostToolUse staleness detection ───────────────────

describe('PostToolUse staleness detection (integration)', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label}: emits stale notification when HEAD differs from meta`, () => {
      // Write meta.json with a different commit
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
      expect(output!.hookEventName).toBe('PostToolUse');
      expect(output!.additionalContext).toContain('stale');
      expect(output!.additionalContext).toContain('aaaaaaa');
    });

    it(`${label}: silent when HEAD matches meta lastCommit`, () => {
      const head = getHeadCommit();
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: head, stats: {} }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: silent when tool is not Bash`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Grep',
        tool_input: { command: 'git commit -m "test"' },
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: silent when command is not a git mutation`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git status' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: silent when exit code is non-zero`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "fail"' },
        tool_output: { exit_code: 1 },
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: includes --embeddings in suggestion when meta had embeddings`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'deadbeef', stats: { embeddings: 42 } }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git merge feature' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
      expect(output!.additionalContext).toContain('--embeddings');
    });

    it(`${label}: omits --embeddings when meta had no embeddings`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'deadbeef', stats: { embeddings: 0 } }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
      expect(output!.additionalContext).not.toContain('--embeddings');
    });

    it(`${label}: detects git rebase as a mutation`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'oldcommit', stats: {} }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git rebase main' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
      expect(output!.additionalContext).toContain('stale');
    });

    it(`${label}: detects git cherry-pick as a mutation`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'oldcommit', stats: {} }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git cherry-pick abc123' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
    });

    it(`${label}: detects git pull as a mutation`, () => {
      fs.writeFileSync(
        path.join(gitNexusDir, 'meta.json'),
        JSON.stringify({ lastCommit: 'oldcommit', stats: {} }),
      );

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git pull origin main' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
    });
  }
});

describe('PostToolUse Codex payload support (integration)', () => {
  it('Codex hook parses Codex tool_response JSON strings', () => {
    fs.writeFileSync(
      path.join(gitNexusDir, 'meta.json'),
      JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
    );

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ exitCode: 0 }),
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.hookEventName).toBe('PostToolUse');
    expect(output!.additionalContext).toContain('stale');
  });

  it('Codex hook ignores failed Codex tool_response payloads', () => {
    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "fail"' },
      tool_response: JSON.stringify({ exitCode: 1 }),
      cwd: tmpDir,
    });

    expect(result.stdout.trim()).toBe('');
  });

  it('Codex hook ignores unrecognised Codex tool_response objects', () => {
    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "fail"' },
      tool_response: JSON.stringify({ status: 'failed' }),
      cwd: tmpDir,
    });

    expect(result.stdout.trim()).toBe('');
  });

  it('Codex hook accepts object-shaped tool responses with exit_code', () => {
    fs.writeFileSync(
      path.join(gitNexusDir, 'meta.json'),
      JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
    );

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: { exit_code: 0 },
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('stale');
  });

  it('Codex hook still emits stale warnings for mixed search + git commands', () => {
    fs.writeFileSync(
      path.join(gitNexusDir, 'meta.json'),
      JSON.stringify({ lastCommit: 'aaaaaaa0000000000000000000000000deadbeef', stats: {} }),
    );

    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'rg dummy && git commit -m "test"' },
      tool_response: JSON.stringify({ exitCode: 0 }),
      cwd: tmpDir,
    });

    const output = parseHookOutput(result.stdout);
    expect(output).not.toBeNull();
    expect(output!.additionalContext).toContain('stale');
  });
});

describe('Codex search augmentation safety', () => {
  it('Codex hook augments plain literal rg patterns', () => {
    const result = runHookWithEnv(
      CODEX_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'rg landlord src' },
        tool_response: JSON.stringify({ exitCode: 0 }),
        cwd: tmpDir,
      },
      { GITNEXUS_CLI_PATH: fakeCliPath },
    );

    const output = parseHookOutput(result.stdout);
    expect(result.status).toBe(0);
    expect(output).not.toBeNull();
    expect(output!.hookEventName).toBe('PostToolUse');
    expect(output!.additionalContext).toContain('FAKE_AUGMENT_OUTPUT');
  });

  it('Codex hook skips regex-heavy rg patterns that would drive wildcard FTS', () => {
    const result = runHookWithEnv(
      CODEX_HOOK,
      {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: "rg 'run_tapi_action|/api/.*/actions' src" },
        tool_response: JSON.stringify({ exitCode: 0 }),
        cwd: tmpDir,
      },
      { GITNEXUS_CLI_PATH: fakeCliPath },
    );

    expect(result.status).toBe(0);
    expect(result.stdout.trim()).toBe('');
  });

  it('Codex hook skips augmentation while the recorded lock owner is still alive', () => {
    fs.mkdirSync(augmentLockDir, { recursive: true });
    fs.writeFileSync(
      augmentLockOwnerPath,
      JSON.stringify({ pid: process.pid, createdAt: Date.now() }),
      'utf-8',
    );

    try {
      const result = runHookWithEnv(
        CODEX_HOOK,
        {
          hook_event_name: 'PostToolUse',
          tool_name: 'Bash',
          tool_input: { command: 'rg landlord src' },
          tool_response: JSON.stringify({ exitCode: 0 }),
          cwd: tmpDir,
        },
        { GITNEXUS_CLI_PATH: fakeCliPath },
      );

      expect(result.status).toBe(0);
      expect(result.stdout.trim()).toBe('');
    } finally {
      fs.rmSync(augmentLockDir, { recursive: true, force: true });
    }
  });

  it('Codex hook clears abandoned augment locks owned by dead processes', () => {
    fs.mkdirSync(augmentLockDir, { recursive: true });
    fs.writeFileSync(
      augmentLockOwnerPath,
      JSON.stringify({ pid: 999999, createdAt: Date.now() - 30000 }),
      'utf-8',
    );

    try {
      const result = runHookWithEnv(
        CODEX_HOOK,
        {
          hook_event_name: 'PostToolUse',
          tool_name: 'Bash',
          tool_input: { command: 'rg landlord src' },
          tool_response: JSON.stringify({ exitCode: 0 }),
          cwd: tmpDir,
        },
        { GITNEXUS_CLI_PATH: fakeCliPath },
      );

      const output = parseHookOutput(result.stdout);
      expect(result.status).toBe(0);
      expect(output).not.toBeNull();
      expect(output!.additionalContext).toContain('FAKE_AUGMENT_OUTPUT');
    } finally {
      fs.rmSync(augmentLockDir, { recursive: true, force: true });
    }
  });
});

describe('Codex cwd validation (integration)', () => {
  it('Codex hook is silent when cwd is relative', () => {
    const result = runHook(CODEX_HOOK, {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'git commit -m "test"' },
      tool_response: JSON.stringify({ exitCode: 0 }),
      cwd: 'relative/path',
    });
    expect(result.stdout.trim()).toBe('');
  });
});

// ─── Integration: cwd validation rejects relative paths ─────────────

describe('cwd validation (integration)', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label}: PostToolUse silent when cwd is relative`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: 'relative/path',
      });
      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: PreToolUse silent when cwd is relative`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'PreToolUse',
        tool_name: 'Grep',
        tool_input: { pattern: 'validateUser' },
        cwd: 'relative/path',
      });
      expect(result.stdout.trim()).toBe('');
    });
  }
});

// ─── Integration: dispatch map routes correctly ─────────────────────

describe('Dispatch map routing (integration)', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label}: unknown hook_event_name produces no output`, () => {
      const result = runHook(hookPath, {
        hook_event_name: 'UnknownEvent',
        tool_name: 'Bash',
        tool_input: { command: 'echo hello' },
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
      expect(result.status).toBe(0);
    });

    it(`${label}: empty hook_event_name produces no output`, () => {
      const result = runHook(hookPath, {
        hook_event_name: '',
        tool_name: 'Bash',
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
      expect(result.status).toBe(0);
    });

    it(`${label}: missing hook_event_name produces no output`, () => {
      const result = runHook(hookPath, {
        tool_name: 'Bash',
        cwd: tmpDir,
      });
      expect(result.stdout.trim()).toBe('');
      expect(result.status).toBe(0);
    });

    it(`${label}: invalid JSON input exits cleanly`, () => {
      const result = spawnSync(process.execPath, [hookPath], {
        input: 'not json at all',
        encoding: 'utf-8',
        timeout: 10000,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      expect(result.status).toBe(0);
      expect(result.stdout.trim()).toBe('');
    });

    it(`${label}: empty stdin exits cleanly`, () => {
      const result = spawnSync(process.execPath, [hookPath], {
        input: '',
        encoding: 'utf-8',
        timeout: 10000,
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      expect(result.status).toBe(0);
    });
  }
});

// ─── Integration: PostToolUse with missing meta.json ────────────────

describe('PostToolUse with missing/corrupt meta.json', () => {
  for (const [label, hookPath] of [
    ['CJS', CJS_HOOK],
    ['Plugin', PLUGIN_HOOK],
  ] as const) {
    it(`${label}: emits stale when meta.json does not exist`, () => {
      const metaPath = path.join(gitNexusDir, 'meta.json');
      const hadMeta = fs.existsSync(metaPath);
      if (hadMeta) fs.unlinkSync(metaPath);

      try {
        const result = runHook(hookPath, {
          hook_event_name: 'PostToolUse',
          tool_name: 'Bash',
          tool_input: { command: 'git commit -m "test"' },
          tool_output: { exit_code: 0 },
          cwd: tmpDir,
        });

        const output = parseHookOutput(result.stdout);
        expect(output).not.toBeNull();
        expect(output!.additionalContext).toContain('never');
      } finally {
        // Restore meta.json for subsequent tests
        fs.writeFileSync(metaPath, JSON.stringify({ lastCommit: 'old', stats: {} }));
      }
    });

    it(`${label}: emits stale when meta.json is corrupt`, () => {
      const metaPath = path.join(gitNexusDir, 'meta.json');
      fs.writeFileSync(metaPath, 'not valid json!!!');

      const result = runHook(hookPath, {
        hook_event_name: 'PostToolUse',
        tool_name: 'Bash',
        tool_input: { command: 'git commit -m "test"' },
        tool_output: { exit_code: 0 },
        cwd: tmpDir,
      });

      const output = parseHookOutput(result.stdout);
      expect(output).not.toBeNull();
      expect(output!.additionalContext).toContain('never');

      // Restore
      fs.writeFileSync(metaPath, JSON.stringify({ lastCommit: 'old', stats: {} }));
    });
  }
});
