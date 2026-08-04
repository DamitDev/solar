/**
 * API error unwrapping helpers.
 *
 * Solar Control validation errors may arrive in two shapes (spec
 * deployment-intent.md §12.6):
 *   - flat:  { "detail": "..." }
 *   - structured: { "detail": "Invalid intent", "errors": [{ "field": ..., "message": ... }] }
 * `extractApiError` normalizes both into an ApiErrorDetail.
 */

export interface ApiErrorDetail {
  message: string;
  errors?: Array<{ field: string; message: string }>;
}

export function extractApiError(err: any): ApiErrorDetail {
  const data = err?.response?.data;
  const detail = data?.detail;
  if (typeof detail === 'string') return { message: detail };
  if (detail && typeof detail === 'object') {
    return { message: detail.detail ?? 'Invalid request', errors: detail.errors };
  }
  return { message: err?.message ?? 'Request failed' };
}
