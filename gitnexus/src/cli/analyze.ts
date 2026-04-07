/**
 * Analyze Command
 *
 * Indexes a repository and stores the knowledge graph in .gitnexus/
 *
 * Delegates core analysis to the shared runFullAnalysis orchestrator.
 * This CLI wrapper handles: heap management, progress bar, SIGINT,
 * optional repo-local AI context generation (--ai-context / --skills),
 * summary output, and process.exit().
 */

import path from 'path';
import { execFileSync } from 'child_process';
import v8 from 'v8';
import cliProgress from 'cli-progress';
import { closeLbug } from '../core/lbug/lbug-adapter.js';
import { getStoragePaths, getGlobalRegistryPath } from '../storage/repo-manager.js';
import { getGitRoot, hasGitDir } from '../storage/git.js';
import { runFullAnalysis, type AnalyzeResult as CoreAnalyzeResult } from '../core/run-analyze.js';
import type { GeneratedSkillInfo } from './skill-gen.js';
import fs from 'fs/promises';

const HEAP_MB = 8192;
const HEAP_FLAG = `--max-old-space-size=${HEAP_MB}`;

/** Re-exec the process with an 8GB heap if we're currently below that. */
function ensureHeap(): boolean {
  const nodeOpts = process.env.NODE_OPTIONS || '';
  if (nodeOpts.includes('--max-old-space-size')) return false;

  const v8Heap = v8.getHeapStatistics().heap_size_limit;
  if (v8Heap >= HEAP_MB * 1024 * 1024 * 0.9) return false;

  try {
    execFileSync(process.execPath, [HEAP_FLAG, ...process.argv.slice(1)], {
      stdio: 'inherit',
      env: { ...process.env, NODE_OPTIONS: `${nodeOpts} ${HEAP_FLAG}`.trim() },
    });
  } catch (e: any) {
    process.exitCode = e.status ?? 1;
  }
  return true;
}

export interface AnalyzeOptions {
  force?: boolean;
  embeddings?: boolean;
  /** Generate repo-local AGENTS.md / CLAUDE.md context and bundled .claude skills. */
  aiContext?: boolean;
  skills?: boolean;
  verbose?: boolean;
  /** Skip AGENTS.md and CLAUDE.md gitnexus block updates. */
  skipAgentsMd?: boolean;
  /** Index the folder even when no .git directory is present. */
  skipGit?: boolean;
}

function buildAIContextStats(result: {
  stats: CoreAnalyzeResult['stats'];
  pipelineResult?: CoreAnalyzeResult['pipelineResult'];
}) {
  let aggregatedClusterCount: number | undefined;
  const communityResult = result.pipelineResult?.communityResult;
  if (communityResult?.communities) {
    const groups = new Map<string, number>();
    for (const community of communityResult.communities) {
      const label = community.heuristicLabel || community.label || 'Unknown';
      groups.set(label, (groups.get(label) || 0) + community.symbolCount);
    }
    aggregatedClusterCount = Array.from(groups.values()).filter((count) => count >= 5).length;
  }

  const stats = result.stats;
  return {
    files: stats.files ?? 0,
    nodes: stats.nodes ?? 0,
    edges: stats.edges ?? 0,
    communities: stats.communities,
    clusters: aggregatedClusterCount,
    processes: stats.processes,
  };
}

async function materializeAIContext(
  repoPath: string,
  result: Pick<CoreAnalyzeResult, 'repoName' | 'stats' | 'pipelineResult'>,
  options?: AnalyzeOptions,
  generatedSkills?: GeneratedSkillInfo[],
) {
  const { generateAIContextFiles } = await import('./ai-context.js');
  const { storagePath } = getStoragePaths(repoPath);
  await generateAIContextFiles(
    repoPath,
    storagePath,
    result.repoName,
    buildAIContextStats(result),
    generatedSkills,
    { skipAgentsMd: options?.skipAgentsMd },
  );
}

export const analyzeCommand = async (inputPath?: string, options?: AnalyzeOptions) => {
  if (ensureHeap()) return;

  if (options?.verbose) {
    process.env.GITNEXUS_VERBOSE = '1';
  }

  console.log('\n  GitNexus Analyzer\n');

  let repoPath: string;
  if (inputPath) {
    repoPath = path.resolve(inputPath);
  } else {
    const gitRoot = getGitRoot(process.cwd());
    if (!gitRoot) {
      if (!options?.skipGit) {
        console.log(
          '  Not inside a git repository.\n  Tip: pass --skip-git to index any folder without a .git directory.\n',
        );
        process.exitCode = 1;
        return;
      }
      // --skip-git: fall back to cwd as the root
      repoPath = path.resolve(process.cwd());
    } else {
      repoPath = gitRoot;
    }
  }

  const repoHasGit = hasGitDir(repoPath);
  if (!repoHasGit && !options?.skipGit) {
    console.log(
      '  Not a git repository.\n  Tip: pass --skip-git to index any folder without a .git directory.\n',
    );
    process.exitCode = 1;
    return;
  }
  if (!repoHasGit) {
    console.log(
      '  Warning: no .git directory found \u2014 commit-tracking and incremental updates disabled.\n',
    );
  }

  // KuzuDB migration cleanup is handled by runFullAnalysis internally.
  // Note: repo-local AI context generation is handled here after runFullAnalysis.

  if (process.env.GITNEXUS_NO_GITIGNORE) {
    console.log(
      '  GITNEXUS_NO_GITIGNORE is set — skipping .gitignore (still reading .gitnexusignore)\n',
    );
  }

  // ── CLI progress bar setup ─────────────────────────────────────────
  const bar = new cliProgress.SingleBar(
    {
      format: '  {bar} {percentage}% | {phase}',
      barCompleteChar: '\u2588',
      barIncompleteChar: '\u2591',
      hideCursor: true,
      barGlue: '',
      autopadding: true,
      clearOnComplete: false,
      stopOnComplete: false,
    },
    cliProgress.Presets.shades_grey,
  );

  bar.start(100, 0, { phase: 'Initializing...' });

  // Graceful SIGINT handling
  let aborted = false;
  const sigintHandler = () => {
    if (aborted) process.exit(1);
    aborted = true;
    bar.stop();
    console.log('\n  Interrupted — cleaning up...');
    closeLbug()
      .catch(() => {})
      .finally(() => process.exit(130));
  };
  process.on('SIGINT', sigintHandler);

  // Route console output through bar.log() to prevent progress bar corruption
  const origLog = console.log.bind(console);
  const origWarn = console.warn.bind(console);
  const origError = console.error.bind(console);
  const barLog = (...args: any[]) => {
    process.stdout.write('\x1b[2K\r');
    origLog(args.map((a) => (typeof a === 'string' ? a : String(a))).join(' '));
  };
  console.log = barLog;
  console.warn = barLog;
  console.error = barLog;

  // Track elapsed time per phase
  let lastPhaseLabel = 'Initializing...';
  let phaseStart = Date.now();

  const updateBar = (value: number, phaseLabel: string) => {
    if (phaseLabel !== lastPhaseLabel) {
      lastPhaseLabel = phaseLabel;
      phaseStart = Date.now();
    }
    const elapsed = Math.round((Date.now() - phaseStart) / 1000);
    const display = elapsed >= 3 ? `${phaseLabel} (${elapsed}s)` : phaseLabel;
    bar.update(value, { phase: display });
  };

  const elapsedTimer = setInterval(() => {
    const elapsed = Math.round((Date.now() - phaseStart) / 1000);
    if (elapsed >= 3) {
      bar.update({ phase: `${lastPhaseLabel} (${elapsed}s)` });
    }
  }, 1000);

  const t0 = Date.now();

  // ── Run shared analysis orchestrator ───────────────────────────────
  try {
    const result = await runFullAnalysis(
      repoPath,
      {
        force: options?.force || options?.skills,
        embeddings: options?.embeddings,
        skipGit: options?.skipGit,
      },
      {
        onProgress: (_phase, percent, message) => {
          updateBar(percent, message);
        },
        onLog: barLog,
      },
    );

    if (result.alreadyUpToDate && !options?.aiContext && !options?.skills) {
      clearInterval(elapsedTimer);
      process.removeListener('SIGINT', sigintHandler);
      console.log = origLog;
      console.warn = origWarn;
      console.error = origError;
      bar.stop();
      console.log('  Already up to date\n');
      // Safe to return without process.exit(0) — the early-return path in
      // runFullAnalysis never opens LadybugDB, so no native handles prevent exit.
      return;
    }

    if (options?.aiContext && !options.skills) {
      updateBar(99, 'Generating AI context files...');
      try {
        await materializeAIContext(repoPath, result, options);
      } catch {
        /* best-effort */
      }
    }

    // Skill generation (CLI-only, uses pipeline result from analysis)
    if (options?.skills && result.pipelineResult) {
      updateBar(99, 'Generating skill files...');
      try {
        const { generateSkillFiles } = await import('./skill-gen.js');
        const skillResult = await generateSkillFiles(
          repoPath,
          result.repoName,
          result.pipelineResult,
        );
        if (skillResult.skills.length > 0) {
          barLog(`  Generated ${skillResult.skills.length} skill files`);
        }
        await materializeAIContext(repoPath, result, options, skillResult.skills);
      } catch {
        /* best-effort */
      }
    }

    const totalTime = ((Date.now() - t0) / 1000).toFixed(1);

    clearInterval(elapsedTimer);
    process.removeListener('SIGINT', sigintHandler);

    console.log = origLog;
    console.warn = origWarn;
    console.error = origError;

    bar.update(100, { phase: 'Done' });
    bar.stop();

    // ── Summary ────────────────────────────────────────────────────
    const s = result.stats;
    console.log(`\n  Repository indexed successfully (${totalTime}s)\n`);
    console.log(
      `  ${(s.nodes ?? 0).toLocaleString()} nodes | ${(s.edges ?? 0).toLocaleString()} edges | ${s.communities ?? 0} clusters | ${s.processes ?? 0} flows`,
    );
    console.log(`  ${repoPath}`);

    try {
      await fs.access(getGlobalRegistryPath());
    } catch {
      console.log('\n  Tip: Run `gitnexus setup` to configure MCP for your editor.');
    }

    console.log('');
  } catch (err: any) {
    clearInterval(elapsedTimer);
    process.removeListener('SIGINT', sigintHandler);
    console.log = origLog;
    console.warn = origWarn;
    console.error = origError;
    bar.stop();
    console.error(`\n  Analysis failed: ${err.message}\n`);
    process.exitCode = 1;
    return;
  }

  // LadybugDB's native module holds open handles that prevent Node from exiting.
  // ONNX Runtime also registers native atexit hooks that segfault on some
  // platforms (#38, #40). Force-exit to ensure clean termination.
  process.exit(0);
};
