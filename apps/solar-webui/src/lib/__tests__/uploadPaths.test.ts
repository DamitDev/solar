import { describe, expect, it } from 'vitest';
import {
  ARTIFACT_NAME_RE,
  ARTIFACT_VERSION_RE,
  datasetWarnings,
  deriveUploadPaths,
  isExcludedPath,
  modelWarnings,
  stripRootSegment,
  validateArtifactPath,
} from '@/lib/uploadPaths';

function fakeFile(name: string, webkitRelativePath: string): File {
  return Object.assign(new File(['x'], name), { webkitRelativePath });
}

describe('stripRootSegment', () => {
  it('strips the root folder segment', () => {
    expect(stripRootSegment('my-model/config.json')).toBe('config.json');
    expect(stripRootSegment('my-model/weights/shard-1.bin')).toBe('weights/shard-1.bin');
    expect(stripRootSegment('flat.txt')).toBe('');
  });
});

describe('isExcludedPath', () => {
  it('excludes junk files from spec §5.2', () => {
    expect(isExcludedPath('.git/config')).toBe(true);
    expect(isExcludedPath('model/.git/HEAD')).toBe(true);
    expect(isExcludedPath('.gitattributes')).toBe(true);
    expect(isExcludedPath('nested/.gitignore')).toBe(true);
    expect(isExcludedPath('.DS_Store')).toBe(true);
    expect(isExcludedPath('Thumbs.db')).toBe(true);
    expect(isExcludedPath('desktop.ini')).toBe(true);
    expect(isExcludedPath('__pycache__/model.cpython-312.pyc')).toBe(true);
    expect(isExcludedPath('train/__pycache__/x.pyc')).toBe(true);
    expect(isExcludedPath('weights/part-1.pyc')).toBe(true);
    expect(isExcludedPath('download.part')).toBe(true);
    expect(isExcludedPath('file.tmp')).toBe(true);
    expect(isExcludedPath('checkpoint.lock')).toBe(true);
    expect(isExcludedPath('.ipynb_checkpoints/checkpoint.ipynb')).toBe(true);
  });

  it('keeps real artifact files', () => {
    expect(isExcludedPath('config.json')).toBe(false);
    expect(isExcludedPath('weights/shard-1.bin')).toBe(false);
    expect(isExcludedPath('model-Q4_K_M.gguf')).toBe(false);
    expect(isExcludedPath('tokenizer.json')).toBe(false);
  });
});

describe('validateArtifactPath', () => {
  it('rejects traversal and absolute paths (spec §2.3)', () => {
    expect(validateArtifactPath('../escape')).toMatch(/\.\./);
    expect(validateArtifactPath('a/../../b')).toMatch(/\.\./);
    expect(validateArtifactPath('/abs/path')).toMatch(/relative/);
    expect(validateArtifactPath('C:/windows')).toMatch(/drive letter/);
    expect(validateArtifactPath('')).toMatch(/empty/);
  });

  it('accepts relative nested paths', () => {
    expect(validateArtifactPath('weights/shard-1.bin')).toBeNull();
    expect(validateArtifactPath('config.json')).toBeNull();
  });
});

describe('deriveUploadPaths', () => {
  it('strips the root segment and applies exclusions', () => {
    const files = [
      fakeFile('config.json', 'my-model/config.json'),
      fakeFile('shard-1.bin', 'my-model/weights/shard-1.bin'),
      fakeFile('.DS_Store', 'my-model/.DS_Store'),
      fakeFile('x.pyc', 'my-model/__pycache__/x.pyc'),
      fakeFile('notes.txt', 'my-model/.git/notes.txt'),
    ];
    const derived = deriveUploadPaths(files);
    expect(derived.map((entry) => entry.path).sort()).toEqual(['config.json', 'weights/shard-1.bin']);
  });

  it('preserves nested paths', () => {
    const files = [fakeFile('shard-1.bin', 'my-model/weights/shard-1.bin')];
    const derived = deriveUploadPaths(files);
    expect(derived[0].path).toBe('weights/shard-1.bin');
  });
});

describe('modelWarnings', () => {
  it('warns when neither config.json nor a .gguf is present', () => {
    const warnings = modelWarnings([{ file: fakeFile('a.bin', 'm/a.bin'), path: 'a.bin' }]);
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toMatch(/Solar Host will not be able to serve/);
  });

  it('does not warn for a config.json model', () => {
    expect(modelWarnings([{ file: fakeFile('c.json', 'm/config.json'), path: 'config.json' }])).toEqual([]);
  });

  it('does not warn for a gguf model', () => {
    expect(modelWarnings([{ file: fakeFile('m.gguf', 'm/model.gguf'), path: 'model.gguf' }])).toEqual([]);
  });
});

describe('datasetWarnings', () => {
  it('warns when no file matches the declared format', () => {
    const files = [{ file: fakeFile('a.csv', 'm/a.csv'), path: 'a.csv' }];
    expect(datasetWarnings(files, 'parquet')).toHaveLength(1);
  });

  it('accepts matching extensions per format', () => {
    const parquet = [{ file: fakeFile('a.parquet', 'm/a.parquet'), path: 'a.parquet' }];
    const hdf5 = [{ file: fakeFile('a.h5', 'm/a.h5'), path: 'a.h5' }];
    const hdf5b = [{ file: fakeFile('a.hdf5', 'm/a.hdf5'), path: 'a.hdf5' }];
    const json = [{ file: fakeFile('a.json', 'm/a.json'), path: 'a.json' }];
    expect(datasetWarnings(parquet, 'parquet')).toEqual([]);
    expect(datasetWarnings(hdf5, 'hdf5')).toEqual([]);
    expect(datasetWarnings(hdf5b, 'hdf5')).toEqual([]);
    expect(datasetWarnings(json, 'json')).toEqual([]);
  });
});

describe('artifact name/version patterns', () => {
  it('mirrors the Data Repository rules (spec §4.2)', () => {
    expect(ARTIFACT_NAME_RE.test('my-model')).toBe(true);
    expect(ARTIFACT_NAME_RE.test('my.model_v2')).toBe(true);
    expect(ARTIFACT_NAME_RE.test('BadName')).toBe(false);
    expect(ARTIFACT_NAME_RE.test('-leading')).toBe(false);

    expect(ARTIFACT_VERSION_RE.test('v1')).toBe(true);
    expect(ARTIFACT_VERSION_RE.test('1.0.0-beta')).toBe(true);
    expect(ARTIFACT_VERSION_RE.test('latest')).toBe(true); // regex alone; reserved check is separate
    expect(ARTIFACT_VERSION_RE.test('-bad')).toBe(false);
  });
});
