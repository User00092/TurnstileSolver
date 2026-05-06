# CaptchaSolver

A drop-in replacement for [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) built on [zendriver](https://github.com/stephanlensky/zendriver) (Chrome DevTools Protocol automation). Bypasses Cloudflare challenges and returns page HTML, cookies, and the raw Turnstile token.

---

## Features

- **FlareSolverr-compatible API** — same `POST /v1` interface, drop-in for existing integrations
- **Cloudflare bypass** — handles browser integrity checks and Turnstile widgets
- **Turnstile token extraction** — returns the raw `cf-turnstile-response` token in the response
- **Persistent sessions** — reuse browser instances across requests for speed
- **Concurrency control** — semaphore caps total browser instances (`MAX_BROWSERS`)
- **Docker-ready** — runs with Xvfb (virtual display) for reliable non-headless Chrome

---

## Quick Start

### Docker (recommended)

```bash
docker compose up -d
```

The service starts on port `8191`.

### Local (Windows/Mac/Linux)

**Requirements:** Python 3.12+, Chrome or Chromium installed

```bash
pip install -r requirements.txt
python main.py
```

---

## API Reference

All commands go through `POST /v1` with a JSON body containing a `cmd` field.

### `GET /`

Returns service status and the browser user agent.

```bash
curl http://localhost:8191/
```

```json
{"msg": "FlareSolverr is ready!", "version": "0.1.0", "userAgent": "Mozilla/5.0 ..."}
```

### `GET /health`

```bash
curl http://localhost:8191/health
```

```json
{"status": "ok"}
```

---

### `request.get`

Fetch a URL, bypass Cloudflare, return HTML + cookies + Turnstile token.

```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{
    "cmd": "request.get",
    "url": "https://example.com",
    "maxTimeout": 60000
  }'
```

**Response:**

```json
{
  "status": "ok",
  "startTimestamp": 1700000000000,
  "endTimestamp":   1700000010000,
  "version": "0.1.0",
  "solution": {
    "url": "https://example.com",
    "status": 200,
    "headers": {},
    "response": "<html>...</html>",
    "cookies": [
      {
        "name": "cf_clearance",
        "value": "abc123...",
        "domain": ".example.com",
        "path": "/",
        "expires": 1700086400,
        "httpOnly": true,
        "secure": true,
        "sameSite": "None"
      }
    ],
    "userAgent": "Mozilla/5.0 ...",
    "turnstile_token": "0.ABC123..."
  }
}
```

> `turnstile_token` is only present when the page uses a Cloudflare Turnstile widget.

---

### `request.post`

Submit a POST form, bypass Cloudflare, return the result.

```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{
    "cmd": "request.post",
    "url": "https://example.com/submit",
    "postData": "field1=value1&field2=value2",
    "maxTimeout": 60000
  }'
```

---

### Session management

Sessions keep a browser alive between requests, avoiding the startup cost.

**Create:**
```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.create"}'
```
```json
{"status": "ok", "session": "abc-123-def"}
```

**Use:**
```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd": "request.get", "url": "https://example.com", "session": "abc-123-def"}'
```

**List:**
```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.list"}'
```

**Destroy:**
```bash
curl -X POST http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd": "sessions.destroy", "session": "abc-123-def"}'
```

---

## Request Parameters

| Field | Type | Default | Description |
|---|---|---|---|
| `cmd` | string | required | Command to run |
| `url` | string | required (for requests) | Target URL |
| `maxTimeout` | int | `60000` | Timeout in milliseconds |
| `session` | string | — | Session ID to reuse |
| `session_ttl_minutes` | int | — | Auto-expire session after N minutes of inactivity |
| `postData` | string | — | URL-encoded POST body (`request.post` only) |
| `cookies` | array | — | Cookies to inject before navigation |
| `proxy` | object | — | `{"url": "http://host:port", "username": "u", "password": "p"}` |
| `returnOnlyCookies` | bool | `false` | Skip HTML in response |
| `returnScreenshot` | bool | `false` | Include base64 PNG screenshot instead of HTML |
| `disableMedia` | bool | `false` | Block images, video, fonts (faster loads) |

---

## Configuration

Set via environment variables or a `.env` file in the project root.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8191` | Listen port |
| `LOG_LEVEL` | `info` | Logging level (`debug`, `info`, `warning`, `error`) |
| `MAX_BROWSERS` | `10` | Max concurrent browser instances |
| `MAX_TIMEOUT` | `60000` | Hard cap on `maxTimeout` (ms) |
| `HEADLESS` | `false` | Run Chrome headless (not recommended — CF detects it) |
| `BROWSER_EXECUTABLE_PATH` | auto-detect | Path to Chrome/Chromium binary |

**Example `.env`:**
```
PORT=8191
MAX_BROWSERS=5
LOG_LEVEL=debug
BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
```

---

## Docker Configuration

`docker-compose.yml` sets sensible defaults:

```yaml
shm_size: "512mb"      # Chrome needs shared memory
tmpfs:
  - /tmp:exec,mode=1777  # Chrome extracts helpers to /tmp — needs exec
deploy:
  resources:
    limits:
      memory: 2g
```

To override settings without editing the file:

```bash
docker compose run -e MAX_BROWSERS=3 -e LOG_LEVEL=debug turnstile-solver
```

---

## How It Works

1. Request arrives at `POST /v1`
2. A Chrome browser is launched (or a persistent session is reused)
3. The target URL is navigated to in a new tab
4. If a Cloudflare challenge is detected (title heuristic + DOM inspection), `verify_cf()` clicks the Turnstile checkbox
5. Once the challenge passes, the Turnstile token is read from `input[name="cf-turnstile-response"]` via CDP DOM node — this uses the HTML attribute directly, which CF sets via `setAttribute`, making it readable even after React re-renders clear the JS property
6. Cookies, HTML, and the token are returned in the response
7. The tab is closed; ephemeral browsers are stopped and the semaphore slot is released

**Why non-headless + Xvfb?** Cloudflare detects `--headless=new` and cycles the challenge without completing it. Chrome running against a virtual framebuffer (Xvfb) is indistinguishable from a real display session.

---

## Project Structure

```
CaptchaSolver/
├── app/
│   ├── api.py        # FastAPI routes and lifespan hooks
│   ├── config.py     # Environment variable settings
│   ├── models.py     # Pydantic request/response models
│   ├── sessions.py   # Persistent browser session manager
│   └── solver.py     # Cloudflare challenge solving logic
├── main.py           # Uvicorn entrypoint (workers=1 required)
├── entrypoint.sh     # Docker: starts Xvfb then the app
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Troubleshooting

**Challenge never solves / loops forever**
- Make sure `HEADLESS=false` (default). CF detects headless Chrome reliably.
- In Docker, Xvfb is started automatically by `entrypoint.sh`.

**`turnstile_token` missing from response**
- Only present when the page has a Turnstile widget. Not all CF-protected pages use one.
- Increase `maxTimeout` — the token is read right after `verify_cf` completes.

**Browser crashes / OOM in Docker**
- Increase `shm_size` to `1gb` in `docker-compose.yml`.
- Reduce `MAX_BROWSERS` (each Chrome instance uses ~300–500 MB).

**Port already in use**
- Change the host port mapping: `"8192:8191"` in `docker-compose.yml`.
