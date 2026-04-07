#!/usr/bin/env node
/**
 * GitNexus Claude Code Hook
 *
 * PreToolUse  — intercepts Grep/Glob/Bash searches and augments
 *               with graph context from the GitNexus index.
 * PostToolUse — detects stale index after git mutations and notifies
 *               the agent to reindex.
 *
 * NOTE: SessionStart hooks are broken on Windows (Claude Code bug).
 * Session context is injected via CLAUDE.md / skills instead.
 */

const path = require('path');
const {
  createBundledCliRunner,
  createClaudeHandlers,
  runHookMain,
} = require('../shared/gitnexus-hook-core.cjs');

const runGitNexusCli = createBundledCliRunner({
  injectedCliPath: '__GITNEXUS_CLI_PATH__',
  fallbackCliPath: path.resolve(__dirname, '..', '..', 'dist', 'cli', 'index.js'),
});

runHookMain(createClaudeHandlers(runGitNexusCli));
