// Artifact-relative path derivation, junk exclusion, and path validation
// for the upload wizard (spec §5.2). Pure functions — unit-testable without
// React.

export interface SelectedUploadFile {
  file: File;
  path: string;
}

// Directory segments whose presence anywhere in the relative path excludes
// the file (spec §5.2).
const EXCLUDED_SEGMENTS = new Set(['.git', '__pycache__', '.ipynb_checkpoints']);

// Basenames excluded wherever they appear (spec §5.2).
const EXCLUDED_BASENAMES = new Set(['.gitattributes', '.gitignore', '.DS_Store', 'Thumbs.db', 'desktop.ini']);

// Suffixes excluded wherever they appear (spec §5.2).
const EXCLUDED_SUFFIXES = ['.pyc', '.tmp', '.part', '.lock'];

// Mirrors the Data Repository's _NAME_RE / version rule (spec §4.2).
export const ARTIFACT_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,254}$/;
export const ARTIFACT_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

/**
 * Strip the chosen folder's own name from a webkitRelativePath.
 * `my-model/config.json` -> `config.json` (spec §5.2).
 */
export function stripRootSegment(webkitRelativePath: string): string {
  const parts = webkitRelativePath.split('/');
  return parts.slice(1).join('/');
}

/** Apply the exclusion list from spec §5.2 to a relative path. */
export function isExcludedPath(relPath: string): boolean {
  const segments = relPath.split('/');
  const basename = segments[segments.length - 1];
  if (segments.some((segment) => EXCLUDED_SEGMENTS.has(segment))) return true;
  if (EXCLUDED_BASENAMES.has(basename)) return true;
  return EXCLUDED_SUFFIXES.some((suffix) => basename.endsWith(suffix));
}

/**
 * Validate a relative path against the artifact layout contract (spec §2.3).
 * Returns an error message, or null when the path is valid.
 */
export function validateArtifactPath(relPath: string): string | null {
  if (!relPath) return 'Path must not be empty';
  if (relPath.startsWith('/')) return 'Path must be relative';
  if (/^[A-Za-z]:/.test(relPath)) return 'Path must not start with a drive letter';
  const segments = relPath.split('/');
  if (segments.some((segment) => segment === '.' || segment === '..')) {
    return "Path must not contain '.' or '..' segments";
  }
  return null;
}

/**
 * Derive the upload list from a directory-picker FileList: strip the root
 * segment, apply exclusions, and validate every surviving path.
 */
export function deriveUploadPaths(files: File[]): SelectedUploadFile[] {
  const result: SelectedUploadFile[] = [];
  for (const file of files) {
    const raw = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
    const rel = stripRootSegment(raw);
    if (isExcludedPath(rel)) continue;
    if (validateArtifactPath(rel) !== null) continue; // impossible from a FileList
    result.push({ file, path: rel });
  }
  return result;
}

/** Model warnings: no config.json and no *.gguf means nothing can serve it. */
export function modelWarnings(files: SelectedUploadFile[]): string[] {
  const warnings: string[] = [];
  const hasConfig = files.some((entry) => entry.path === 'config.json');
  const hasGguf = files.some((entry) => entry.path.endsWith('.gguf'));
  if (!hasConfig && !hasGguf) {
    warnings.push('Solar Host will not be able to serve this artifact: no config.json and no .gguf files.');
  }
  return warnings;
}

/** Dataset warnings: no file matches the declared format. */
export function datasetWarnings(files: SelectedUploadFile[], format: string): string[] {
  if (!format) return [];
  const lower = format.toLowerCase();
  const matches = files.some((entry) => {
    const name = entry.path.toLowerCase();
    if (lower === 'parquet') return name.endsWith('.parquet');
    if (lower === 'hdf5') return name.endsWith('.h5') || name.endsWith('.hdf5');
    if (lower === 'json') return name.endsWith('.json');
    return name.endsWith(`.${lower}`);
  });
  return matches ? [] : [`No file matches the declared ${format} format.`];
}
