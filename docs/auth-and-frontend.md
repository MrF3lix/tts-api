# Authentication, frontend and sample storage

Plan for putting a login-protected web UI in front of the TTS API and keeping selected samples permanently.

- **This repo (`tts-ui`)**: gains an authentication layer, job ownership and an endpoint that saves a sample to PocketBase. Generation (gateway → Redis → workers) stays as it is.
- **New repo (frontend)**: a small Next.js app plus the PocketBase setup (schema, migrations, deployment).

The frontend and PocketBase need no GPU and may run on a different server than the DGX Spark. Everything below assumes they can be on separate machines, talking over HTTPS.

## 1. Architecture

```
            Server B (any small VM)                         Server A (DGX Spark)
 ┌───────────────────────────────────────┐        ┌──────────────────────────────────────┐
 │ Caddy (HTTPS)                         │        │ Caddy (HTTPS)                        │
 │   app.<domain> ──► Next.js            │  HTTPS │   api.<domain> ──► gateway (FastAPI) │
 │   pb.<domain>  ──► PocketBase         │◄──────►│                     │                │
 │                                       │        │                   Redis ◄──► workers │
 └───────────────────────────────────────┘        └──────────────────────────────────────┘
        ▲            ▲                                        ▲
        │ browser    │ browser (file URLs only)               │ scripts with API keys (optional)
```

| Component | Owns |
|---|---|
| **PocketBase** | Users and login, API keys, speaker references, kept samples (metadata + audio files), ratings |
| **Next.js** | UI; holds the user's PocketBase session in an httpOnly cookie; forwards API calls to the gateway on the user's behalf |
| **Gateway** | Checks every request's credentials against PocketBase, owns jobs, runs generation, writes kept samples to PocketBase |
| **Redis + workers** | Unchanged: queue, progress, temporary audio (24h) |

Traffic between machines:

| From → to | Why | Credential |
|---|---|---|
| Browser → Next.js | All UI traffic | PocketBase session cookie (httpOnly) |
| Browser → PocketBase | Only playing/downloading protected audio files (short-lived file token in the URL), and OAuth2 redirects if added later | File token |
| Next.js → PocketBase | Login, saved samples, ratings, speakers, API keys | User's PocketBase token |
| Next.js → gateway | Jobs, progress, sample audio, "keep" | User's PocketBase token in `Authorization` |
| Gateway → PocketBase | Check tokens, look up API keys, upload kept samples | Its own **service account** |
| Scripts → gateway | Programmatic use (optional) | Personal API key |

### Why the browser doesn't call the gateway directly

The browser calls Next.js route handlers, which forward to the gateway. Two browser limitations make this necessary:

- `EventSource` (the progress stream) and `<audio src>` can't send an `Authorization` header. Behind Next.js they're plain same-origin requests that carry the session cookie, and Next.js adds the header.
- The PocketBase token stays in an httpOnly cookie, so page scripts never see it, and the gateway needs no CORS setup for the UI.

The gateway still checks every request itself. It never trusts Next.js to have done that, so it can stay publicly reachable for scripts.

## 2. Request flows

**Login**: the user submits the login form to Next.js. Next.js calls PocketBase `authWithPassword`, stores the token in an httpOnly cookie, and from then on sends it with every gateway call.

**Generate**: `POST /api/jobs` (Next.js) forwards the multipart body as is to `POST /jobs` (gateway) with `Authorization: <pb token>`. The gateway checks the token, stores `owner=<user id>` on the job and returns `202`.

**Progress**: `GET /api/jobs/{id}/events` (Next.js) streams the gateway's SSE response through to the browser without buffering. The gateway returns `404` if the job belongs to someone else.

**Play a fresh sample**: `GET /api/jobs/{id}/samples/{i}` (Next.js) streams `GET /jobs/{id}/samples/{i}.wav` (gateway).

**Keep a sample**: `POST /api/jobs/{id}/samples/{i}/keep` (Next.js) → `POST /jobs/{id}/samples/{i}/keep` (gateway). The gateway:
1. Checks ownership, and that the sample is completed and its audio is still in Redis.
2. Finds or creates the `speakers` record for this reference (matched by owner + SHA-256 of the audio) and uploads the reference once.
3. Creates the `samples` record with the audio file and server-side metadata (model, checkpoint, seed, text, language, job id). Rating, tags and note from the request are included.
4. Returns the PocketBase record id. Keeping the same sample twice returns the existing record (unique on `job_id + sample_index`).

**Browse saved samples**: Next.js server components read `samples` and `speakers` from PocketBase with the user's token. Access rules limit results to the user's own records. Audio plays straight from PocketBase using a short-lived file token (`pb.files.getToken()`).

**Scripts**: `Authorization: Bearer tts_<key>` sent straight to the gateway. The gateway hashes the key and looks it up in `api_keys` using its service account.

## 3. Part 1 — this repo: authentication layer in the gateway

### 3.1 Configuration (`configs/api.yaml`)

```yaml
auth:
  enabled: true                       # false keeps today's open behaviour for local development
  pocketbase_url: ${oc.env:PB_URL,http://localhost:8090}
  users_collection: users
  service_collection: services
  service_email: ${oc.env:PB_SERVICE_EMAIL,null}
  service_password: ${oc.env:PB_SERVICE_PASSWORD,null}
  token_cache_seconds: 60             # how long a successful token check is reused
  api_key_prefix: tts_
  cors_origins: []                    # only needed if a browser ever calls the gateway directly

quotas:
  max_queued_samples_per_user: 24     # one user can't fill the whole queue
```

Secrets (`PB_SERVICE_PASSWORD`) come from the environment, i.e. a `.env` file next to `compose.yaml` that isn't committed. They never go in the YAML.

### 3.2 New module `src/tts_ui/auth.py`

- `User` (dataclass): `id`, `email`, `name`, `via` (`"token"` or `"api_key"`).
- `PocketBaseClient`: an async `httpx` client for `auth.pocketbase_url`.
  - **Service login**: `POST /api/collections/{service_collection}/auth-with-password` at startup. Store the token and log in again on a `401`.
  - **`verify_user_token(token)`**: `POST /api/collections/{users_collection}/auth-refresh` with `Authorization: <token>`. On `200`, the response body contains the user record. PocketBase tokens are signed with secrets only PocketBase knows, so the gateway can't check them on its own. Cache the result in memory, keyed by SHA-256 of the token, for `token_cache_seconds`.
  - **`verify_api_key(key)`**: SHA-256 the key and fetch `GET /api/collections/api_keys/records?filter=(key_hash='<hash>' && revoked=false)` with the service token. Check `expires_at`, update `last_used_at` at most once a minute, and cache like tokens.
  - **Uploads** for "keep": `POST /api/collections/speakers/records` and `POST /api/collections/samples/records` as multipart, with the service token.
- **FastAPI dependency `current_user`**: reads `Authorization`, accepting both `Bearer <x>` and a bare `<x>`. Values starting with `api_key_prefix` go to `verify_api_key`; everything else to `verify_user_token`.
  - Missing or invalid credentials: `401` with `WWW-Authenticate: Bearer`.
  - PocketBase unreachable: `503`, not `401`, so clients don't throw away a valid session. Still-cached entries keep working during the outage.
- If `auth.enabled=false`, `current_user` returns a fixed local user, so tests and local runs work without PocketBase.

Add `httpx` to the `api` extra.

### 3.3 Changes to `src/tts_ui/api.py`

- [ ] Create the `PocketBaseClient` in `lifespan` and log in the service account.
- [ ] Require `current_user` on every endpoint except a minimal `GET /health` (`{"status": "ok"}` only). Move worker counts to an authenticated `GET /status`.
- [ ] **Job ownership**
  - [ ] `create_job` stores `owner=<user.id>` in the job hash.
  - [ ] Add `load_owned_job(job_id, user)`: returns `404` when the job doesn't exist **or** belongs to someone else, so job ids can't be probed. Use it in `GET /jobs/{id}`, `/events`, `/samples/{i}.wav`, `DELETE`, and `/keep`.
- [ ] **Per-user job list**: on creation, `ZADD tts:user:{id}:jobs <created_at> <job_id>` with the same TTL as jobs. Add `GET /jobs?limit=50`, returning the user's jobs newest first and skipping expired ones. This feeds the UI's history.
- [ ] **Per-user quota**: a `tts:user:{id}:queued` counter, increased on create and decreased by the worker on pickup (the task carries the owner). Past the limit, return `429`. The worker needs a one-line change to decrease it.
- [ ] **`POST /jobs/{id}/samples/{i}/keep`**: form fields `rating`, `tags` (JSON list) and `note`, all optional.
  - [ ] Read the sample audio and the speaker reference from Redis. Return `409` if the sample isn't completed and `410` if the audio has expired.
  - [ ] Speaker dedup: look up `speakers` by `owner + sha256`; create it if missing.
  - [ ] Create the `samples` record and return `{"id", "speaker_id"}`. Make this idempotent through the unique `(job_id, sample_index)` index: on a duplicate, fetch and return the existing record.
  - [ ] Record `model` (model id) and `checkpoint` (the checkpoint identifier: the folder name of `model_path` for XTTS, e.g. `GPT_XTTS_v2.0_SwissGPC_epoch_6_subset_7`, or `pretrained` for Qwen3-TTS). Only the worker knows it, so the worker writes it into the job hash when it starts a sample (3.4).
- [ ] Show in the job view that a sample was kept: store `s{i}.kept_id` after a successful keep and include it per sample.
- [ ] Optional CORS middleware, enabled only when `auth.cors_origins` is non-empty.
- [ ] `POST /generate` requires `current_user` too.

### 3.4 Worker changes (`src/tts_ui/worker.py`)

- [ ] Decrease `tts:user:{owner}:queued` on pickup (the owner comes from the task fields).
- [ ] Write `s{i}.checkpoint` (see 3.3) when starting a sample.

### 3.5 Deployment changes (`compose.yaml`)

- [ ] Pass `PB_URL`, `PB_SERVICE_EMAIL` and `PB_SERVICE_PASSWORD` to the gateway from `.env`.
- [ ] Add Caddy for HTTPS on `api.<domain>`, published on 443/80 and proxying to the gateway. Turn off buffering for `/jobs/*/events` (`flush_interval -1`). The gateway's own port no longer needs to be published.
- [ ] Add `.env.example` documenting the variables.

### 3.6 Tests

- [ ] Unit tests for `current_user` with a mocked PocketBase: valid token, expired token, API key, revoked key, PocketBase down (expect `503`), and cache hits.
- [ ] Ownership: user B gets `404` for every endpoint of user A's job.
- [ ] Keep: success, idempotent repeat, expired audio (`410`), not finished (`409`), speaker dedup across two jobs with the same reference.
- [ ] Quota: the per-user `429` fires before the global one.

## 4. Part 2 — PocketBase (frontend repo)

Pin an exact PocketBase version (0.23 or newer; the rule syntax below uses 0.23+ names such as `@request.body` and `_superusers`). Define all collections in `pb_migrations/` (JS migrations) in git, not only through the dashboard. Upgrade deliberately: PocketBase is pre-1.0 and has changed incompatibly between releases before.

### 4.1 Collections

**`users`** (auth, built in)
- Extra fields: `name`, `role` (select: `user`, `admin`), `max_samples_per_job` (number, optional override).
- Sign-up disabled (`createRule = null`); an admin creates accounts in the dashboard or a script. Email/password auth on; OAuth2 (e.g. Microsoft for ZHAW accounts) can be added later.

**`services`** (auth)
- One record, used by the gateway. Least privilege: it's not a superuser.

**`speakers`**
| Field | Type | Notes |
|---|---|---|
| `owner` | relation → users | required |
| `name` | text | editable by the user |
| `audio` | file | **protected**, single, max ~15 MB, `audio/wav` |
| `sha256` | text | unique index together with `owner` |
| `created`, `updated` | autodate | |

**`samples`**
| Field | Type | Notes |
|---|---|---|
| `owner` | relation → users | required |
| `speaker` | relation → speakers | required, **cascade delete**: deleting a speaker deletes its samples |
| `audio` | file | **protected**, single, `audio/wav` |
| `text`, `ref_text`, `language`, `model`, `checkpoint` | text | set by the gateway |
| `seed` | number | set by the gateway |
| `duration_s` | number | |
| `job_id` | text | unique index together with `sample_index` |
| `sample_index` | number | |
| `rating` | number | 1–5, optional |
| `tags` | json | list of strings |
| `note` | text | |
| `created`, `updated` | autodate | |

**`api_keys`**
| Field | Type | Notes |
|---|---|---|
| `owner` | relation → users | required |
| `name` | text | e.g. "evaluation script" |
| `prefix` | text | first characters of the key, shown in the UI to tell keys apart |
| `key_hash` | text | SHA-256 of the full key; unique index. The key itself is never stored |
| `expires_at` | date | optional |
| `revoked` | bool | |
| `last_used_at` | date | written by the gateway |

### 4.2 Access rules

`svc` below stands for `@request.auth.collectionName = "services"`.

| Collection | list / view | create | update | delete |
|---|---|---|---|---|
| `speakers` | `owner = @request.auth.id \|\| svc` | `svc` | `owner = @request.auth.id && @request.body.audio:isset = false && @request.body.sha256:isset = false && @request.body.owner:isset = false` | `owner = @request.auth.id` |
| `samples` | `owner = @request.auth.id \|\| svc` | `svc` | owner, and only `rating`, `tags`, `note` may be set (deny `audio`, `seed`, `text`, `owner`, … with `:isset = false`) | `owner = @request.auth.id` |
| `api_keys` | `owner = @request.auth.id \|\| svc` | `owner = @request.auth.id && @request.body.owner = @request.auth.id` | `(owner = @request.auth.id && @request.body.key_hash:isset = false) \|\| svc` | `owner = @request.auth.id` |
| `users` | `id = @request.auth.id \|\| svc` | none (admins only) | `id = @request.auth.id` (deny `role` changes) | none |

Generated audio and its metadata can only be created by the gateway, so the stored seed, model and checkpoint are trustworthy for research use.

### 4.3 Running PocketBase

- A small Dockerfile that downloads the pinned release binary for the server's architecture, with `pb_data/` on a volume and `pb_migrations/` copied in.
- Behind Caddy at `pb.<domain>`. Block `/_/` (the admin dashboard) from the internet in Caddy, allowing only your network or VPN, and enable MFA for superusers.
- Backups: PocketBase's built-in scheduled backups (to S3 or a local folder), plus a copy of the volume off the server.
- SMTP settings if password reset by email is wanted.

## 5. Part 3 — Next.js app (frontend repo)

App Router, TypeScript, PocketBase JS SDK. Server-side only environment: `PB_URL` and `API_URL` (the gateway). Only `NEXT_PUBLIC_PB_URL` is exposed to the browser, for file URLs.

### 5.1 Auth

- A `lib/pocketbase.ts` helper that creates a PocketBase client per request, loads the auth state from the cookie (`pb.authStore.loadFromCookie`), and writes it back as an httpOnly, `Secure`, `SameSite=Lax` cookie (`exportToCookie`).
- `middleware.ts` redirects unauthenticated users to `/login`, except for `/login` itself.
- Login and logout as server actions. Refresh the token (`authRefresh`) when it's close to expiry.

### 5.2 Route handlers (forwarding to the gateway)

All of them read the token from the cookie, add `Authorization`, and pass the gateway's status code through. Mark them dynamic, on the Node.js runtime.

| Next.js route | Gateway | Notes |
|---|---|---|
| `POST /api/jobs` | `POST /jobs` | Forward the multipart body as a stream. Use a route handler, not a server action, because server actions limit request bodies to 1 MB by default |
| `GET /api/jobs` | `GET /jobs` | history |
| `GET /api/jobs/[id]` | `GET /jobs/{id}` | |
| `DELETE /api/jobs/[id]` | `DELETE /jobs/{id}` | |
| `GET /api/jobs/[id]/events` | `GET /jobs/{id}/events` | Return `new Response(upstream.body, …)` with `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. Cancel the upstream request when the client disconnects |
| `GET /api/jobs/[id]/samples/[i]` | `GET /jobs/{id}/samples/{i}.wav` | stream the audio |
| `POST /api/jobs/[id]/samples/[i]/keep` | `POST /jobs/{id}/samples/{i}/keep` | |
| `GET /api/status` | `GET /status` | workers / queue, for the status bar |

### 5.3 Pages

- **`/login`**
- **`/` (generate)**
  - Form: text with a character counter; language and model dropdowns (from the gateway); number of samples; seed under "advanced"; speaker reference by upload or microphone.
  - Microphone recording only works over HTTPS. Convert the recording to 16-bit WAV in the browser before uploading, because the gateway can't decode WebM/Opus.
  - The job list below the form, newest first, from `GET /api/jobs`. Each card subscribes to `/api/jobs/[id]/events` while running, and shows status, queue position, ETA and a player per finished sample. Per sample: keep (with optional rating/note) and download. Per job: cancel, and regenerate with the same settings.
- **`/saved`**: kept samples from PocketBase, filterable by language, speaker, tag and rating. Includes playback, inline editing of rating/tags/note, delete, and export (CSV of metadata + zip of audio, generated by a route handler).
- **`/speakers`**: references with their name and consent flag. Deleting one also deletes all its samples, after a confirmation that shows how many.
- **`/settings/api-keys`**: create a key (generated server-side as `tts_` + 32 random bytes in base64url; only the hash is stored; the full key is shown once), list keys, and revoke them.

## 6. Deployment across two servers

- **DNS**: `api.<domain>` → Spark, `app.<domain>` and `pb.<domain>` → server B. If both share one public IP, a single Caddy can route all three hostnames to the right machine.
- **Spark firewall / port forwarding**: only 443 (and 80 for certificate renewal) to Caddy. Redis and the gateway's port stay internal.
- **Gateway → PocketBase** goes over the public HTTPS URL, or a private network (VPN/Tailscale) if available, in which case `PB_URL` points at the private address.
- **Latency**: token checks are cached, and sample uploads are a few hundred KB, so running on separate servers adds no noticeable delay.
- **If PocketBase is down**, generation keeps working for sessions already in the cache. New logins and "keep" fail with `503`, and kept audio stays in Redis for 24h, so the user can retry.
- **If the Spark is down**, the UI and saved samples stay usable; generation shows an "API unavailable" message.

## 7. Security checklist

- [ ] HTTPS everywhere. Tokens and API keys never travel over plain HTTP.
- [ ] Tokens only in httpOnly cookies; never in `localStorage` or URLs. File tokens in audio URLs are the one exception: they're short-lived and scoped to files.
- [ ] API keys stored only as hashes and shown once at creation.
- [ ] PocketBase sign-up disabled; admin dashboard not reachable from the internet.
- [ ] The service account has only the rules above, not superuser rights.
- [ ] Gateway: `404` for other users' jobs, per-user quota, input limits as today.
- [ ] Speaker references treated as personal data: a consent field, deletion cascades to samples, and backups covered by the same retention rules.
- [ ] `.env` files with secrets are not committed.

## 8. Implementation order

1. **PocketBase**: migrations for all collections and rules, the service account, the Docker setup. Create a test user.
2. **Gateway auth**: `auth.py`, `current_user`, job ownership, `GET /jobs`, the per-user quota, and tests (with `auth.enabled=false` still working).
3. **Next.js login and forwarding**: auth cookie, middleware, the route handlers in section 5.2. Check generation and progress end-to-end.
4. **Generate page**: form, job list with live progress, playback.
5. **Keep**: gateway endpoint and `/saved`, then `/speakers`.
6. **API keys**: gateway check plus the settings page.
7. **Deployment**: Caddy on both servers, DNS, firewall, backups.

## 9. Open decisions

- **Script access**: if no scripts call the API, step 6 can wait and the gateway can be limited to accept requests only from server B.
