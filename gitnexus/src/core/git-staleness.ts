/**
 * Git working tree vs index commit staleness (used by MCP resources, group status, etc.).
 * Lives in core/ so application code does not depend on the MCP package layer.
 */

import { execFileSync } from 'node:child_process';
import { getCurrentCommit } from '../storage/git.js';

export interface StalenessInfo {
  isStale: boolean;
  commitsBehind: number;
  hint?: string;
  currentCommit?: string;
  indexedCommit?: string;
}

/**
 * Check how many commits the index is behind HEAD (synchronous; uses git CLI).
 */
export function checkStaleness(
  repoPath: string,
  lastCommit: string,
  options?: { strict?: boolean },
): StalenessInfo {
  const indexedCommit = lastCommit || '';

  const stale = (currentCommit: string, commitsBehind = 0): StalenessInfo => ({
    isStale: true,
    commitsBehind,
    currentCommit,
    indexedCommit,
    hint: `GitNexus index is stale (indexed: ${indexedCommit ? indexedCommit.slice(0, 7) : 'unknown'}, current HEAD: ${currentCommit ? currentCommit.slice(0, 7) : 'unknown'}). Run \`gitnexus analyze\` to update the knowledge graph.`,
  });

  try {
    const currentCommit = getCurrentCommit(repoPath);
    if (!currentCommit) {
      return options?.strict ? stale('') : { isStale: false, commitsBehind: 0 };
    }

    if (!indexedCommit) {
      return options?.strict ? stale(currentCommit) : { isStale: false, commitsBehind: 0 };
    }

    if (currentCommit === indexedCommit) {
      return { isStale: false, commitsBehind: 0, currentCommit, indexedCommit };
    }

    let commitsBehind = 0;
    try {
      const result = execFileSync('git', ['rev-list', '--count', `${lastCommit}..HEAD`], {
        cwd: repoPath,
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      }).trim();
      commitsBehind = parseInt(result, 10) || 0;
    } catch {
      return options?.strict ? stale(currentCommit) : { isStale: false, commitsBehind: 0 };
    }

    if (commitsBehind > 0) {
      return {
        isStale: true,
        commitsBehind,
        currentCommit,
        indexedCommit,
        hint: `⚠️ Index is ${commitsBehind} commit${commitsBehind > 1 ? 's' : ''} behind HEAD. Run analyze tool to update.`,
      };
    }

    if (options?.strict) return stale(currentCommit);

    return { isStale: false, commitsBehind: 0 };
  } catch {
    if (options?.strict) {
      return stale('');
    }
    return { isStale: false, commitsBehind: 0 };
  }
}
