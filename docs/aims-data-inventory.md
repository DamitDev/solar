# AIMS adatleltár — Gateway kérésnaplók (solar-control)

Hiteles forrás: `apps/solar-control/app/database/logs.py` (`GatewayRequestLog`).
Terjedelem: IT Sec #97 — a megőrzött naplómezők és azok megtartási ideje,
az ISO 42001 által megkövetelt szabályok szerint. A Solar szolgáltatja a
leltárt; az AIMS regisztráció az IT Sec csapat oldala.

| Mező | Forrás | Szivárgási kockázat | Megőrzés |
|---|---|---|---|
| `api_key_id` | gateway regisztráció (UUID FK) | Alacsony (nem hitelesítő adat) | 90 nap |
| `api_key_name` | gateway regisztráció | Alacsony (megjelenített név, nem hitelesítő adat) | 90 nap |
| `client_ip` | kérés fejlécei | Közepes (PII) | 90 nap |
| `start_timestamp` / `end_timestamp` | gateway | Alacsony | 90 nap |
| `error_message` | upstream hiba törzse / kivétel szövege | Közepes (prompt töredékek; 200 karakternél csonkítva — IT Sec #97) | 30 nap |
| `model` / `resolved_model` | gateway routing | Alacsony | 90 nap |
| `duration_s` | gateway időmérés | Alacsony | 90 nap |
| `prompt_tokens` / `cached_tokens` / `completion_tokens` / `total_tokens` | gateway usage aggregáció | Alacsony | 90 nap |
| `decode_tps` / `decode_ms_per_token` | gateway usage aggregáció | Alacsony | 90 nap |

## Megjegyzések

- Az `error_message` az egyetlen szabad szöveges mező. A #97 óta a végleges
  hibaútvonalak a mentéskor és a kiküldéskor csonkítják a szöveget
  (200 karakter): a feldolgozott, kiküldött rekord mindig csonkolt, míg a
  hívó által kapott kivétel a teljes upstream hiba szövegét viszi.
- A Redis endpoint-cache kulcsnév hashelt (HMAC-SHA256, IT Sec #97) és
  5 perces TTL-t kap — az AIMS leltáron kívüli (tartós hitelesítő anyag
  nélkül, naplórekord nélkül).
- A megtartási idők az IT Sec megerősítését váró javaslatok.
