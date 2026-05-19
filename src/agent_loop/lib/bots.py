"""GitHub App bot identities — one bot per CLI (claude / codex / gemini).

Each persona's CLI maps to a GitHub App registered on the host:

    persona.cli  ──►  ~/.config/agent-loop/bots/<cli>.{pem,json}

so commits, PRs, reviews, and comments are attributed to short-lived bot
identities (`claude-bot[bot]`, `codex-bot[bot]`, `gemini-bot[bot]`) rather
than the human owner's PAT. Three apps total per host, regardless of how
many consumer projects the host runs.

Public surface:
  mint_installation_token(cli, repo) -> str       cached, ~1h TTL
  env_for(cli, repo) -> dict[str,str]             GH_TOKEN= for `gh` calls
  http_extraheader(cli, repo) -> str              for `git -c http.extraheader=...`
  bot_login(cli)  / bot_email(cli)                git commit author identity
  setup(cli, app_id, pem_text)                    idempotent registration
  list_configured() -> list[str]                  what's on this host

Layout on disk:
  ~/.config/agent-loop/bots/claude.pem      (0600 — private key)
  ~/.config/agent-loop/bots/claude.json     (0600 — {app_id, app_slug, bot_login, bot_user_id})
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


class BotError(RuntimeError):
    pass


def _bots_dir() -> Path:
    override = os.environ.get("AGENT_LOOP_BOTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".config" / "agent-loop" / "bots"


def _meta_path(cli: str) -> Path:
    return _bots_dir() / f"{cli}.json"


def _pem_path(cli: str) -> Path:
    return _bots_dir() / f"{cli}.pem"


def load_meta(cli: str) -> Optional[dict]:
    p = _meta_path(cli)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def is_configured(cli: str) -> bool:
    return _meta_path(cli).exists() and _pem_path(cli).exists()


def list_configured() -> list[str]:
    d = _bots_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json") if (d / f"{p.stem}.pem").exists())


# ---- JWT + token mint ------------------------------------------------------

# Cache installation tokens per (cli, repo) until close to expiry. GitHub
# installation tokens last ~1h; we refresh when <60s remain.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _make_jwt(cli: str) -> str:
    """RS256 JWT signed with the app's private key. Valid ~9 min."""
    try:
        import jwt as pyjwt
    except ImportError as e:
        raise BotError(
            "PyJWT not installed — run `uv add 'PyJWT[crypto]'` in triumvirate"
        ) from e

    meta = load_meta(cli)
    if not meta:
        raise BotError(
            f"no bot config for {cli!r}. Run `agent-loop bots add {cli}` first."
        )
    pem_path = _pem_path(cli)
    if not pem_path.exists():
        raise BotError(f"missing private key at {pem_path}")

    now = int(time.time())
    return pyjwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": str(meta["app_id"])},
        pem_path.read_text(),
        algorithm="RS256",
    )


def _gh_api(method: str, path: str, *, token: str, body: Optional[dict] = None) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        method=method,
        data=(json.dumps(body).encode() if body is not None else None),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-loop-bots/1",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise BotError(f"GitHub API {method} {path} → HTTP {e.code}: {detail.strip()[:300]}") from e


def mint_installation_token(cli: str, *, repo: str) -> str:
    """Return a short-lived installation token for `cli`'s bot on `repo`.

    `repo` is "owner/name" — must be installed for this app. Caches the token
    until close to expiry so back-to-back gh calls don't each hit the API.
    """
    cache_key = f"{cli}::{repo}"
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] - time.time() > 60:
        return cached[0]

    jwt_tok = _make_jwt(cli)
    owner, name = repo.split("/", 1)
    inst = _gh_api("GET", f"/repos/{owner}/{name}/installation", token=jwt_tok)
    iid = inst.get("id")
    if not iid:
        raise BotError(
            f"GitHub App for {cli!r} is not installed on {repo!r}. "
            f"Install it at github.com/settings/apps/<your-app-slug>/installations."
        )

    out = _gh_api("POST", f"/app/installations/{iid}/access_tokens", token=jwt_tok)
    token = out.get("token")
    if not token:
        raise BotError(f"installation token response missing 'token': {out}")

    expires_at = out.get("expires_at")
    if expires_at:
        ts = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
    else:
        ts = time.time() + 3000

    _TOKEN_CACHE[cache_key] = (token, ts)
    return token


# ---- identity ---------------------------------------------------------------


def bot_login(cli: str) -> str:
    """e.g. 'claude-bot[bot]'."""
    meta = load_meta(cli)
    if not meta or "bot_login" not in meta:
        raise BotError(f"bot login unknown for {cli!r}; rerun setup")
    return meta["bot_login"]


def bot_email(cli: str) -> str:
    """The noreply email GitHub attributes commits to."""
    meta = load_meta(cli)
    if not meta:
        raise BotError(f"no bot config for {cli!r}")
    uid = meta.get("bot_user_id")
    login = meta.get("bot_login")
    if not uid or not login:
        raise BotError(f"bot meta for {cli!r} is incomplete; rerun setup")
    return f"{uid}+{login}@users.noreply.github.com"


def configured_bot_logins() -> set[str]:
    """Logins of all bots configured on this host. Used to auto-trust them."""
    out: set[str] = set()
    for cli in list_configured():
        meta = load_meta(cli) or {}
        login = meta.get("bot_login")
        if login:
            out.add(login)
    return out


# ---- env helpers for child processes ----------------------------------------


def env_for(cli: str, *, repo: str) -> dict[str, str]:
    """Env vars that make a child `gh` call act as `cli`'s bot.

    Pass via subprocess.run(env=...). The token expires in ~1h — for
    long-running daemons each call mints/refreshes via the cache.
    """
    return {"GH_TOKEN": mint_installation_token(cli, repo=repo)}


def http_extraheader(cli: str, *, repo: str) -> str:
    """An `http.extraheader` value for `git -c http.extraheader=...`.

    GitHub git-over-https takes `Authorization: basic <base64('x-access-token:TOKEN')>`.
    """
    tok = mint_installation_token(cli, repo=repo)
    blob = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    return f"AUTHORIZATION: basic {blob}"


# ---- setup ------------------------------------------------------------------


def setup(cli: str, *, app_id: int, pem_text: str) -> dict:
    """Persist bot creds + resolve the bot's login & user id via the API.

    Idempotent: rerun to refresh the cached login/user-id (e.g. if the app
    was renamed). Returns the meta dict.
    """
    d = _bots_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass

    pem_p = _pem_path(cli)
    pem_p.write_text(pem_text.rstrip() + "\n")
    pem_p.chmod(0o600)

    # Write a stub so _make_jwt can read app_id.
    _meta_path(cli).write_text(json.dumps({"app_id": int(app_id)}))

    jwt_tok = _make_jwt(cli)
    app = _gh_api("GET", "/app", token=jwt_tok)
    slug = app.get("slug")
    if not slug:
        raise BotError(f"GET /app returned no slug for {cli!r}: {app}")

    # /users is a public endpoint; passing the App JWT here 401s. Hit it
    # unauthenticated.
    login = f"{slug}[bot]"
    req = urllib.request.Request(
        f"https://api.github.com/users/{urllib.parse.quote(login)}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-loop-bots/1",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        user = json.loads(resp.read() or b"{}")
    user_id = user.get("id")
    if not user_id:
        raise BotError(f"could not resolve bot user id for {login!r}")

    meta = {
        "app_id": int(app_id),
        "app_slug": slug,
        "bot_login": login,
        "bot_user_id": int(user_id),
    }
    _meta_path(cli).write_text(json.dumps(meta, indent=2) + "\n")
    _meta_path(cli).chmod(0o600)
    return meta


def installations_for(cli: str) -> list[dict]:
    """List repo installations for this app — useful for `bots test`."""
    jwt_tok = _make_jwt(cli)
    # /app/installations returns the orgs/users, then each has its own repos.
    insts = _gh_api("GET", "/app/installations", token=jwt_tok)
    if isinstance(insts, dict):
        # urllib gave us a single object — unexpected; treat as list
        insts = [insts]
    return insts or []


# ---- App Manifest provisioning ---------------------------------------------
#
# GitHub doesn't let you create an App purely via API — a human has to click
# "Create" once. The Manifest flow is the closest thing: we generate a manifest
# JSON, the user clicks one URL that auto-POSTs it to github.com, GitHub shows
# a pre-filled confirmation page, the user clicks Create, GitHub redirects
# with a one-time `code`, we exchange that code for the App's id + private key
# via an unauthenticated POST.
#
# We host a tiny local HTTP server on the host's Tailscale IP so the redirect
# lands back here and we can capture the code automatically. The user opens
# one URL from their workstation and clicks Create. That's it.


def _gh_user_login() -> str:
    """Read the host's current `gh` user login (used to make app names unique)."""
    import subprocess
    try:
        out = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or "user"
    except Exception:
        return "user"


def default_manifest(cli: str, *, app_name: str, redirect_url: str) -> dict:
    """The JSON manifest we POST to github.com/settings/apps/new.

    Permissions are the minimum the loop needs: write to contents/issues/PRs,
    read metadata/checks/workflows. Webhook disabled — the loop is pull-based.
    """
    return {
        "name": app_name,
        "url": "https://github.com/alexander-matthew/triumvirate",
        "description": (
            f"Bot identity for the `{cli}` CLI in the triumvirate agent loop. "
            f"Used so commits / PRs / reviews attribute to {cli}-bot instead "
            f"of the human owner."
        ),
        "hook_attributes": {
            "url": "https://example.com/unused",
            "active": False,
        },
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "metadata": "read",
            "checks": "read",
        },
        "default_events": [],
    }


def run_manifest_flow(
    cli: str, *,
    app_name: str,
    redirect_host: str,
    port: int = 8765,
    timeout_s: int = 300,
) -> dict:
    """Drive the App Manifest flow end-to-end.

    Steps:
      1. Start a tiny HTTP server bound to 0.0.0.0:`port`.
      2. GET / serves an HTML page that auto-POSTs the manifest to GitHub.
      3. GET /callback captures the `code` GitHub redirects with.
      4. Exchange the code for the App's id + pem via /app-manifests/.../conversions.
      5. Call setup() to persist + resolve bot login/user-id.

    `redirect_host` is what the user's browser will see — typically the
    tailnet MagicDNS name of this host (e.g. homelab.tail7815d8.ts.net) so
    GitHub's redirect lands back here.

    Returns the resolved meta dict, including `install_url` for the next step.
    """
    import http.server
    import secrets
    import socketserver
    import threading

    state = secrets.token_hex(16)
    redirect_url = f"http://{redirect_host}:{port}/callback"
    manifest = default_manifest(cli, app_name=app_name, redirect_url=redirect_url)
    manifest_json = json.dumps(manifest)

    captured: dict = {}
    done = threading.Event()

    auto_post_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Provision {app_name}</title></head>
<body style="font-family:sans-serif;max-width:560px;margin:3em auto;line-height:1.5">
  <h2>Provisioning <code>{app_name}</code></h2>
  <p>Redirecting to GitHub to confirm the new App…</p>
  <form id="f" action="https://github.com/settings/apps/new?state={state}" method="post">
    <input type="hidden" name="manifest" value='{manifest_json.replace("'", "&#39;")}'>
    <noscript><button type="submit">Continue to GitHub</button></noscript>
  </form>
  <script>document.getElementById("f").submit();</script>
</body></html>"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):  # silence access log
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/" or parsed.path == f"/start/{cli}":
                self._send(200, "text/html; charset=utf-8", auto_post_html.encode())
                return
            if parsed.path == "/callback":
                q = urllib.parse.parse_qs(parsed.query)
                code = (q.get("code") or [""])[0]
                got_state = (q.get("state") or [""])[0]
                if not code:
                    self._send(400, "text/plain", b"missing code")
                    return
                if got_state and got_state != state:
                    self._send(400, "text/plain", b"state mismatch")
                    return
                captured["code"] = code
                self._send(
                    200, "text/html; charset=utf-8",
                    (
                        f"<!doctype html><html><body style='font-family:sans-serif;"
                        f"max-width:560px;margin:3em auto;line-height:1.5'>"
                        f"<h2>Captured &mdash; you can close this tab.</h2>"
                        f"<p>Finishing setup for <code>{app_name}</code> on the host.</p>"
                        f"</body></html>"
                    ).encode(),
                )
                done.set()
                return
            self._send(404, "text/plain", b"not found")

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = _Server(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    start_url = f"http://{redirect_host}:{port}/"
    print(f"  → open this URL on your workstation: {start_url}")
    print(f"  → state={state}, waiting up to {timeout_s}s for the redirect…")

    try:
        if not done.wait(timeout=timeout_s):
            raise BotError(
                f"timed out waiting for GitHub redirect to {redirect_url}. "
                f"Did the browser actually reach the host? Check Tailscale + port {port}."
            )
        code = captured["code"]
        result = exchange_manifest_code(code)
    finally:
        httpd.shutdown()

    app_id = result.get("id")
    pem = result.get("pem")
    if not app_id or not pem:
        raise BotError(f"manifest conversion returned no id/pem: keys={list(result.keys())}")

    meta = setup(cli, app_id=int(app_id), pem_text=pem)
    meta["install_url"] = result.get("html_url", "") + "/installations/new"
    meta["app_html_url"] = result.get("html_url", "")
    return meta


def exchange_manifest_code(code: str) -> dict:
    """POST /app-manifests/{code}/conversions — exchanges the redirect code
    for the new App's id, slug, html_url, and pem.

    No auth required; the code is the auth.
    """
    req = urllib.request.Request(
        f"https://api.github.com/app-manifests/{urllib.parse.quote(code)}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-loop-bots/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise BotError(
            f"App manifest exchange failed: HTTP {e.code}: {detail.strip()[:300]}. "
            f"The code may have expired (1h TTL) or already been used."
        ) from e
