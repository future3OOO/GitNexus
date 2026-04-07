#!/usr/bin/env node
/**
 * GitNexus Codex Hook
 *
 * Codex currently exposes Bash hook events only.
 * This hook augments Bash search commands with graph context and detects
 * stale GitNexus indexes after successful git mutations.
 */

const path = require('path');
const {
  createBundledCliRunner,
  createCodexHandlers,
  runHookMain,
} = require('../shared/gitnexus-hook-core.cjs');

const runGitNexusCli = createBundledCliRunner({
  injectedCliPath: '__GITNEXUS_CLI_PATH__',
  fallbackCliPath: path.resolve(__dirname, '..', '..', 'dist', 'cli', 'index.js'),
});

runHookMain(createCodexHandlers(runGitNexusCli));
