#!/usr/bin/env node
/**
 * GitNexus Claude Code Plugin Hook
 *
 * PreToolUse  — intercepts Grep/Glob/Bash searches and augments
 *               with graph context from the GitNexus index.
 * PostToolUse — detects stale index after git mutations and notifies
 *               the agent to reindex.
 *
 * NOTE: SessionStart hooks are broken on Windows (Claude Code bug #23576).
 * Session context is injected via CLAUDE.md / skills instead.
 */

const {
  createBinaryCliRunner,
  createClaudeHandlers,
  runHookMain,
} = require('./shared/gitnexus-hook-core.cjs');

const runGitNexusCli = createBinaryCliRunner();

runHookMain(createClaudeHandlers(runGitNexusCli));
