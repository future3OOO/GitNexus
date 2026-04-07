import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  renderAnalyzeBehaviorSummary,
  renderEditorSupportTable,
  supportDepthNote,
} from '../../src/cli/integration-contract.js';

const PACKAGE_README = path.resolve(__dirname, '..', '..', 'README.md');
const ROOT_README = path.resolve(__dirname, '..', '..', '..', 'README.md');

function readFile(filePath: string): string {
  return fs.readFileSync(filePath, 'utf-8');
}

function extractMarkedBlock(content: string, name: string): string {
  const start = `<!-- gitnexus:${name}:start -->`;
  const end = `<!-- gitnexus:${name}:end -->`;
  const startIndex = content.indexOf(start);
  const endIndex = content.indexOf(end);

  expect(startIndex).toBeGreaterThanOrEqual(0);
  expect(endIndex).toBeGreaterThan(startIndex);

  return content
    .slice(startIndex + start.length, endIndex)
    .trim()
    .replace(/\r\n/g, '\n');
}

describe('integration contract documentation', () => {
  it('keeps the packaged README analyze behavior block aligned with the contract', () => {
    const content = readFile(PACKAGE_README);
    expect(extractMarkedBlock(content, 'analyze-behavior')).toBe(renderAnalyzeBehaviorSummary());
  });

  it('keeps the root README analyze behavior block aligned with the contract', () => {
    const content = readFile(ROOT_README);
    expect(extractMarkedBlock(content, 'analyze-behavior')).toBe(renderAnalyzeBehaviorSummary());
  });

  it('keeps the packaged README editor support table aligned with the contract', () => {
    const content = readFile(PACKAGE_README);
    expect(extractMarkedBlock(content, 'editor-support')).toBe(renderEditorSupportTable());
    expect(content).toContain(supportDepthNote);
  });

  it('keeps the root README editor support table aligned with the contract', () => {
    const content = readFile(ROOT_README);
    expect(extractMarkedBlock(content, 'editor-support')).toBe(renderEditorSupportTable());
    expect(content).toContain(supportDepthNote);
  });
});
