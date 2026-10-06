# AIMS Data Inventory — Gateway Request Logs (solar-control)

Source of truth: `apps/solar-control/app/database/logs.py` (`GatewayRequestLog`).
Scope: IT Sec finding #97 — retained log fields and their retention periods,
per the rules needed for ISO 42001. Solar delivers the inventory; the AIMS
registration is the IT Sec team's side.

| Field | Source | Leak risk | Retention |
|---|---|---|---|
| `api_key_id` | gateway registry (UUID FK) | Low (not a credential) | 90 days |
| `api_key_name` | gateway registry | Low (display name, not a credential) | 90 days |
| `client_ip` | request headers | Medium (PII) | 90 days |
| `start_timestamp` / `end_timestamp` | gateway | Low | 90 days |
| `error_message` | upstream error body / exception text | Medium (prompt fragments; capped at 200 chars — IT Sec #97) | 30 days |
| `model` / `resolved_model` | gateway routing | Low | 90 days |
| `duration_s` | gateway timing | Low | 90 days |
| `prompt_tokens` / `cached_tokens` / `completion_tokens` / `total_tokens` | gateway usage aggregation | Low | 90 days |
| `decode_tps` / `decode_ms_per_token` | gateway usage aggregation | Low | 90 days |

## Notes

- `error_message` is the only free-text field. Since #97 the terminal error
  paths cap echoed upstream bodies at 200 chars (matching the retryable
  path's pre-existing bound); prompt fragments can still surface within
  that cap — the retention period is the compensating control.
- The Redis endpoint-cache key name is hashed (HMAC-SHA256, IT Sec #97) and
  carries a 5-minute TTL — out of scope for the AIMS inventory (no
  persistent credential material, no request-log record).
- Retention periods are proposals pending IT Sec confirmation.
