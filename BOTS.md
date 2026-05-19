# GitHub App bot identities

By default, every action the loop takes on GitHub — opening a PR, posting a
review, adding a label, merging — happens under whatever `gh` auth the host
has on disk. That conflates "things the human did" with "things the agents
did" in the GitHub audit log, and it also means the engineer agent can't
formally review its own PR.

The fix: register **three GitHub Apps** on the host — one per CLI — and
have the loop mint short-lived installation tokens for them on demand.

| CLI    | Bot login         | Used by personas                              |
| ------ | ----------------- | --------------------------------------------- |
| claude | `claude-bot[bot]` | engineer, proposer, drift_watcher             |
| codex  | `codex-bot[bot]`  | reviewer-codex, arbiter-codex                 |
| gemini | `gemini-bot[bot]` | reviewer-gemini, arbiter-gemini, triage, security, librarian |

Three apps total per host, regardless of how many consumer projects the
host runs. Commits attribute to `<bot>[bot]`; reviews attribute to the
reviewer's bot (so the engineer's APPROVE-blocked self-review limitation
goes away).

## One-time setup — create the apps

Repeat this for each of `claude-bot`, `codex-bot`, `gemini-bot`.

1. Open <https://github.com/settings/apps/new> in a browser.
2. **GitHub App name**: `claude-bot` (must be globally unique — if taken,
   try `claude-bot-<your-handle>`).
3. **Homepage URL**: anything valid, e.g. your dotfiles repo URL.
4. **Webhook**: uncheck **Active**. We don't need callbacks.
5. **Permissions → Repository**:
   - Contents: **Read and write** (needed for `git push`)
   - Issues: **Read and write**
   - Pull requests: **Read and write**
   - Metadata: **Read-only** (always required)
   - Checks: **Read-only** (so the merge gate can see CI status)
6. **Where can this GitHub App be installed?** — choose **Only on this
   account**. The apps are owned by you and shouldn't be installable
   elsewhere.
7. Click **Create GitHub App**.
8. On the app's page:
   - Copy the **App ID** (top of the General tab).
   - Scroll to **Private keys** → **Generate a private key**. This
     downloads a `.pem` file. Move it somewhere safe; you'll point the
     loop at it in a moment, but the loop copies the contents into
     `~/.config/agent-loop/bots/` and you can then delete the original.
9. In the left nav, **Install App** → install on your user → choose
   **Only select repositories** and pick the repos the loop will run
   against (e.g. `agentdeck`, `personal-site`, …). You can add more
   repos later from the same install page.

Repeat for `codex-bot` and `gemini-bot`.

## Register the apps with the loop

From a directory with an `agent-loop` install on `PATH` (e.g. inside
`triumvirate/`):

```sh
agent-loop bots add claude --app-id 123456 --pem-file ~/Downloads/claude-bot.2026-05-18.private-key.pem
agent-loop bots add codex  --app-id 234567 --pem-file ~/Downloads/codex-bot.2026-05-18.private-key.pem
agent-loop bots add gemini --app-id 345678 --pem-file ~/Downloads/gemini-bot.2026-05-18.private-key.pem
```

Each call:
1. Copies the `.pem` to `~/.config/agent-loop/bots/<cli>.pem` (mode 0600).
2. Mints a JWT and calls `GET /app` to confirm the key pairs with the App ID.
3. Resolves the bot's login (`<slug>[bot]`) and user id, so future commits
   can attribute to `<bot>[bot]` via the noreply email.
4. Writes `~/.config/agent-loop/bots/<cli>.json` with the resolved meta.

After all three are added, delete the source `.pem` files from your
downloads folder.

```sh
agent-loop bots list
#  claude   claude-bot[bot]   app_id=123456  user_id=...
#  codex    codex-bot[bot]    app_id=234567  user_id=...
#  gemini   gemini-bot[bot]   app_id=345678  user_id=...
```

## Verify each bot can actually act on a repo

From a project root with `[project].repo = "<owner>/<name>"` in its
`config.toml`:

```sh
cd ~/code/agent-loop-configs/agentdeck
AGENT_LOOP_CONFIG_ROOT="$PWD" agent-loop bots test claude
AGENT_LOOP_CONFIG_ROOT="$PWD" agent-loop bots test codex
AGENT_LOOP_CONFIG_ROOT="$PWD" agent-loop bots test gemini
```

A successful test prints `token: minted (...XXX, ~1h TTL)` and
`api: OK — perms={...}`. If you see "GitHub App for X is not installed
on Y", revisit step 9 above and install the app on the target repo.

## What changes inside the loop

- Worktrees created by `git_worktree.create(as_cli="claude")` have local
  `user.name = claude-bot[bot]` / `user.email = <id>+claude-bot[bot]@...`
  set, so any commits the agent makes inside that worktree attribute
  correctly.
- `git_worktree.push(..., as_cli="claude")` pushes over HTTPS with the
  bot's installation token as `Authorization: basic <b64>` — bypassing
  whatever SSH/HTTPS auth `origin` is configured for.
- `gh.{comment,review,add_label,remove_label,create_pr,merge_pr}` accept
  `as_cli=…` and set `GH_TOKEN` for that subprocess.
- Read-only `gh` calls (`list_*`, `get_*`, `pr_diff`) still use the host's
  default `gh` login. They don't attribute anything, so there's no win
  from running them as a bot.
- `trust.author_trusted` automatically accepts any of the host's
  configured bot logins, so reviewer-bot's `##VERDICT: APPROVE` lands
  with full trust without re-listing it in every project's
  `trusted_authors`.

## Token cost & lifetime

- Installation tokens last ~1h. The loop caches them in-process per
  `(cli, repo)` and refreshes when <60s remain.
- A typical tick mints **at most 3 tokens** (one per CLI that does work).
- A daemon ticking every 60s for 8h mints ≤ 8 × 3 = 24 tokens per night.
  Well under any rate limit.

## Revoking / rotating a bot

To rotate a bot's private key:

1. On the app's page → **Private keys** → **Generate a private key**.
2. `agent-loop bots add <cli> --app-id <ID> --pem-file <new>.pem`
   (overwrites the on-disk pem; idempotent).
3. On the app's page → revoke any *old* private keys.

To revoke a bot entirely: uninstall the app from your account in
`Settings → Applications → Installed GitHub Apps`. The next `mint_*` call
will return "not installed" and the loop will surface that as an error.
