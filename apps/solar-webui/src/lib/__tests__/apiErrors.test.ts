import { describe, expect, it } from 'vitest';
import { extractApiError } from '@/lib/apiErrors';

describe('extractApiError', () => {
  it('unwraps a flat string detail', () => {
    const err = { response: { data: { detail: 'Model not found' } } };
    expect(extractApiError(err)).toEqual({ message: 'Model not found' });
  });

  it('unwraps a structured detail with field errors (detail as object)', () => {
    const err = {
      response: {
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'replicas', message: 'must be >= 0' }],
          },
        },
      },
    };
    expect(extractApiError(err)).toEqual({
      message: 'Invalid intent',
      errors: [{ field: 'replicas', message: 'must be >= 0' }],
    });
  });

  it('falls back inside a structured detail without a message', () => {
    const err = {
      response: {
        data: { detail: { errors: [{ field: 'alias', message: 'required' }] } },
      },
    };
    expect(extractApiError(err).message).toBe('Invalid request');
    expect(extractApiError(err).errors).toEqual([{ field: 'alias', message: 'required' }]);
  });

  it('falls back to the error message when no response payload exists', () => {
    expect(extractApiError(new Error('Network Error'))).toEqual({ message: 'Network Error' });
  });

  it('falls back to a generic message for empty errors', () => {
    expect(extractApiError({})).toEqual({ message: 'Request failed' });
  });
});
