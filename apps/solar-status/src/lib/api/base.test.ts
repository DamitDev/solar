import { describe, expect, it } from 'vitest';
import { apiBase, apiUrl, resolveApiBase } from './base';

describe('resolveApiBase', () => {
  it('resolves an absolute VITE_API_BASE directly, ignoring the mount anchor (dev mode)', () => {
    expect(resolveApiBase('http://localhost:8000', '/', 'http://localhost:5174').href).toBe(
      'http://localhost:8000/'
    );
  });

  it('ignores the mount anchor for an absolute VITE_API_BASE even when embedded', () => {
    expect(resolveApiBase('http://localhost:8000', '/status', 'http://control.example').href).toBe(
      'http://localhost:8000/'
    );
  });

  it('resolves the ../api default against the mount anchor (embedded mode)', () => {
    expect(resolveApiBase(undefined, '/status', 'http://control.example').href).toBe(
      'http://control.example/api'
    );
  });

  it('resolves the ../api default with or without a trailing slash on the mount base', () => {
    expect(resolveApiBase(undefined, '/status/', 'http://control.example').href).toBe(
      'http://control.example/api'
    );
    expect(resolveApiBase(undefined, '/status', 'http://control.example').href).toBe(
      'http://control.example/api'
    );
  });

  it('anchors the mount point at location.origin, not at the active client-side route', () => {
    history.replaceState(null, '', '/status/deeply/nested/route');

    expect(resolveApiBase(undefined, '/status', location.origin).pathname).toBe('/api');
  });
});

describe('apiBase', () => {
  it('is resolved once at startup to same-origin /api and stays stable at any route depth', () => {
    history.replaceState(null, '', '/status/another/deep/route');

    expect(apiBase.href).toBe(`${location.origin}/api`);
  });
});

describe('apiUrl', () => {
  it('appends endpoint paths to the resolved base', () => {
    expect(apiUrl('status', new URL('http://control.example/api')).href).toBe(
      'http://control.example/api/status'
    );
  });

  it('keeps endpoint paths on the API origin for an absolute root base (dev mode)', () => {
    expect(apiUrl('status', new URL('http://localhost:8000')).href).toBe(
      'http://localhost:8000/status'
    );
  });

  it('tolerates a leading slash on the endpoint path and a trailing slash on the base', () => {
    expect(apiUrl('/status', new URL('http://control.example/api/')).href).toBe(
      'http://control.example/api/status'
    );
  });
});
