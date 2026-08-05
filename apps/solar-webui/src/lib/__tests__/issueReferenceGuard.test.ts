/**
 * Repo guard: user-facing component strings must not leak internal issue
 * references (S-041, S-040, ...). Code comments keep the IDs as the
 * traceability link to training-platform-project; rendered strings must
 * not. The guard strips comments first, so only string literals and JSX
 * text are checked.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const COMPONENTS_DIR = join(process.cwd(), 'src', 'components');
const ISSUE_REF = /\b[SDNU]-\d{3}\b/;

function stripComments(source: string): string {
  let out = source.replace(/\/\/[^\n]*/g, '');
  out = out.replace(/\/\*[\s\S]*?\*\//g, '');
  return out;
}

function collectSourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      files.push(...collectSourceFiles(full));
    } else if (entry.endsWith('.tsx') || entry.endsWith('.ts')) {
      files.push(full);
    }
  }
  return files;
}

describe('user-facing issue references', () => {
  it('no rendered string in src/components/** carries an [SDNU]-NNN reference', () => {
    const offenders = collectSourceFiles(COMPONENTS_DIR).filter((file) =>
      ISSUE_REF.test(stripComments(readFileSync(file, 'utf-8'))),
    );
    expect(offenders).toEqual([]);
  });
});
