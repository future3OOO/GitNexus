export interface EditorSupportRow {
  editor: string;
  mcp: 'Yes' | '—';
  skills: 'Yes' | '—';
  hooks: string;
  support: string;
}

export const editorSupportRows: readonly EditorSupportRow[] = [
  {
    editor: '**Claude Code**',
    mcp: 'Yes',
    skills: 'Yes',
    hooks: 'Yes (PreToolUse + PostToolUse)',
    support: '**Full**',
  },
  {
    editor: '**Cursor**',
    mcp: 'Yes',
    skills: 'Yes',
    hooks: '—',
    support: 'MCP + Skills',
  },
  {
    editor: '**Codex**',
    mcp: 'Yes',
    skills: 'Yes',
    hooks: 'Yes (PostToolUse on Bash)',
    support: 'MCP + Skills + Hooks',
  },
  {
    editor: '**Windsurf**',
    mcp: 'Yes',
    skills: '—',
    hooks: '—',
    support: 'MCP',
  },
  {
    editor: '**OpenCode**',
    mcp: 'Yes',
    skills: 'Yes',
    hooks: '—',
    support: 'MCP + Skills',
  },
] as const;

export const analyzeBehaviorLines = [
  'Plain `npx gitnexus analyze` indexes or refreshes the graph in `.gitnexus/` only.',
  'Use `npx gitnexus analyze --ai-context` to generate repo-local `AGENTS.md` / `CLAUDE.md` context and bundled `.claude/skills`.',
  'Use `npx gitnexus analyze --skills` to generate repo-specific skills; this also materializes the repo-local AI context.',
  'Use `npx gitnexus setup` once for global MCP wiring, global skills, and editor hook installation.',
] as const;

export const codexHookContractLine =
  'Current Codex hooks can enrich Bash-based search commands (for example `rg` / `grep`) and warn when git mutations make the index stale.';

export const supportDepthNote =
  '**Claude Code** still has the deepest hook surface today because it can enrich native `Grep` / `Glob` / `Bash` searches pre-tool. **Codex** now gets global hooks too, but current Codex runtime only emits `PreToolUse` / `PostToolUse` for `Bash`.';

export function renderEditorSupportTable(): string {
  const lines = [
    '| Editor | MCP | Skills | Hooks (auto-augment) | Support |',
    '|--------|-----|--------|---------------------|---------|',
    ...editorSupportRows.map(
      (row) => `| ${row.editor} | ${row.mcp} | ${row.skills} | ${row.hooks} | ${row.support} |`,
    ),
  ];
  return lines.join('\n');
}

export function renderAnalyzeBehaviorSummary(): string {
  return analyzeBehaviorLines.map((line) => `- ${line}`).join('\n');
}
