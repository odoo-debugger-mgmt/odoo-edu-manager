# Working in this repo

This is guidance for a Claude Code session working in `odoo-edu-manager`, on top
of what's already in [README.md](README.md) (requirements, installation, project
layout). It's mostly things that took real empirical digging to establish and
aren't obvious from reading the code cold — verify anything below against
current reality before relying on it if it looks stale.

This is a local tool for running and poking at Docker-based Odoo instances.
Recreating containers, dropping/restoring databases, editing system
parameters, etc. on those instances is routine and low-stakes — they are
disposable by design, not production. The manager app itself (this
codebase) is the thing to be careful editing.

**How "Connect as" logs in.** `connect_as()` in `docker_manager.py` mints an Odoo
session inside the instance's own container, and `CookieBootstrapMiddleware` in
`app.py` serves the one-shot `/_frame_bootstrap` hop that sets it as the
`session_id` cookie on the instance's own `<db>.localhost` host (or the
configured base domain) — and only there: cookies ignore the port, so one shared
`localhost` cookie jar would let one instance's session overwrite another's.

**Naming.** Everything that names *this app* is `odoo-edu-manager`: the
distribution, the `odoo_edu_manager` package, the `odoo-edu-manager` and
`odoo-edu-manager-mcp` commands, the `ODOO_EDU_MANAGER_*` environment variables
and the `~/.local/share/odoo-edu-manager` data directory. The Docker resources
(the shared Postgres container, the network, the code-server container and
volume, and the `app` label every instance carries) are named by constants in
`docker_manager.py` — `DB_CONTAINER_NAME`, `NETWORK_NAME`,
`CODE_SERVER_CONTAINER_NAME`, `CODE_SERVER_CONFIG_VOLUME`, `MANAGED_LABEL` and
`MANAGED_VALUE` — and are not derived from the app's name. Existing containers
are found by that label, so don't change them without a plan for ones that
already exist.

**Decisions to keep.** There is no "Set DB expiration → 2090" menu item (it
pushed an Enterprise database's `database.expiration_date` out); it was removed
deliberately, so don't add it back. "Include Odoo Enterprise" starts
**unticked** on a new instance, since Enterprise is a private repository cloned
over SSH that a team without that access cannot create an instance with. The
remote is `odoo-debugger-mgmt/odoo-edu-manager` on GitHub.

## Hard invariants

- **Flask + vanilla JS, no build step, no frontend framework.** Deliberate.
  Don't introduce React/webpack/a bundler/etc. — see the templates and
  `static/` for the existing style and match it.
- **No database for the manager app itself.** Every instance's configuration
  (version, addons paths, enterprise/design-themes/core revisions, debug
  settings, ...) is stored as Docker **labels** on its container — see
  `_run_container` in `docker_manager.py`. Add a new per-instance setting by
  adding a label there, and thread it through *all* of: `_run_container`,
  `create_instance`, `update_instance`, `_finalize_after_restore`, and
  `list_instances` — missing one means the value silently doesn't survive
  that particular path (recreate, restore-then-finalize, or just listing).
- **Data that isn't configuration goes in a file under `DATA_DIR`, keyed by
  database name — not in a label.** Labels are for an instance's settings;
  anything that describes what is *in* a database follows the database (kept
  if the instance is recreated, deleted by `_drop_database`) rather than the
  container. `activity-logs/` is the existing example of the shape.
- **One shared Postgres container** (`DB_CONTAINER_NAME` in `docker_manager.py`)
  holds every instance's database. Instances don't get their own Postgres.

## No hot reload — you must restart to see changes

The dev server does not pick up edits to `odoo_edu_manager/**` live. After
changing any `.py`, template, or static file, kill and relaunch the process
bound to port 5001 before testing in a browser — otherwise you're looking at
stale behavior and will draw wrong conclusions from it.

```bash
lsof -ti :5001 | xargs -r ps -o pid,lstart,cmd -p   # confirm which pid first
kill -TERM <the odoo-edu-manager pid, not anything else>
nohup venv/bin/odoo-edu-manager >| /tmp/odoo-edu-manager-5001.log 2>&1 &
disown
```

Two sharp edges here, both confirmed directly, more than once:

- `lsof -ti :5001` can return more than one pid on one line if some unrelated
  process (e.g. a Chrome subprocess) happens to have a matching entry.
  Piping that straight into `kill` fails ambiguously and leaves the *old*
  server running — which still answers HTTP 200, masking the failure. Always
  check `ps -o pid,cmd -p <pid>` first and kill the one actually running
  `.../venv/bin/odoo-edu-manager`.
- zsh on this box has `noclobber` set: plain `>` into a file that already
  exists fails with `file exists`. Use `>|` (or a fresh filename) when
  redirecting a relaunched server's output, or the relaunch silently doesn't
  happen while the rest of the command chain reports success.

After relaunching, verify the *new* code is actually live with a content
check (e.g. `curl ... | grep` for something you just added), not just an
HTTP 200 — a stale process can still be answering 200 if the kill above
didn't actually land.

## Testing

`pytest.ini` excludes `-m slow` by default, so the bare command is the fast
suite:

```bash
venv/bin/pytest -q        # ~80 tests, a couple of seconds
```

It needs a *reachable* Docker daemon even though it starts no containers — some
tests go through `docker_manager.client`, a lazy proxy that connects on first attribute
access (`monkeypatch.setattr(dm.client, …)` counts). The app itself starts without Docker.

Never run `pytest -m slow tests/test_instance_versions.py` casually — it
creates and boots a real container per Odoo version end to end, several
minutes each the first time. `tests/install/run.sh` is the other heavy one:
it installs the working tree into a bare Ubuntu container and smoke-tests
the result (see its README).

## Installers

- `install.sh` (repository root) does every manual step on Ubuntu, Debian and WSL: base
  packages, Docker Engine unless Docker already works, the `docker` group, `uv`, the app,
  and a start-up check. It installs the app from its own folder (a copy of it, so a
  read-only or `/mnt/c` folder is fine), never from GitHub — the repository is private, so
  people download the ZIP. Without systemd and without an init script (Ubuntu 24.04's
  `docker.io` has none) it runs `dockerd` itself; on WSL without systemd it switches
  systemd on in `/etc/wsl.conf`; WSL1 is refused.
- `windows/` is the Windows wrapper: `install.cmd` → `install.ps1` → `lib.ps1`
  (`Invoke-OdooEduInstall`). It enables WSL if needed (the only elevated step), imports a
  dedicated `OdooEdu` distribution from Ubuntu's own `.wsl` image (checked against the
  published SHA-256), and runs `provision.sh` inside it as root, which creates the `edu`
  user, merges `/etc/wsl.conf` and runs `install.sh` as that user. A hidden `sleep` keeps
  the WSL VM — and so the Odoo containers — alive after the launcher window closes. An
  update must not stop running instances, and restarting the distribution does:
  `Invoke-OdooEduInstall` therefore terminates it only when it is new or `/etc/wsl.conf`
  changed during provisioning (read before and after), and otherwise just checks Docker.
  People uninstall from the Start menu (*Uninstall Odoo Edu Manager*), from Settings > Apps
  (a per-user `HKCU\...\Uninstall\OdooEduManager` key, imported from a small `.reg` file because
  Windows PowerShell 5.1 mangles quotes in native arguments) or with `windows\uninstall.cmd`; all
  end in `uninstall.ps1`, which must stay in step with what `Invoke-OdooEduInstall` creates (the
  three shortcuts, the key) and starts its self-deleting helper outside the folder it deletes.
  Instance containers have no restart policy (only the code-server has one), so after
  Stop, a WSL restart or a Windows restart they stay stopped until Start is pressed on the
  dashboard — the user-facing texts say so; keep them that way.
  Windows PowerShell 5.1 is the target: ASCII only, no newer syntax.
- The installers also run the app's first-run setup, `odoo-edu-manager --setup`
  (`first_run_setup` in `app.py`): prepare the shared database (`ensure_shared_db`), fetch
  the list of Odoo versions and start the managed code-server (`ensure_code_server`, a
  ~350 MB image the first time). The database is in it because `main()` calls
  `ensure_shared_db()` *before* `app.run()`: on a machine that never pulled `postgres:17`
  the first start serves nothing while Docker downloads it. That is what made the
  installer's start-up check flaky (a fixed 30 s wait) before the setup did it; `check_app`
  now waits up to three minutes and gives up at once if the app has died. Each setup step
  reports a failure and the others still run; exit code 3 means something was skipped,
  which `install.sh` only warns about. `--skip-code-server` leaves the code-server out
  (`install.sh --no-code-server`); `install.sh --no-setup` skips the whole step. The
  container tests pass `--no-code-server` wherever it would start a container on a real
  Docker or add a pointless second pull.
- The app itself changes inside WSL: the VS Code button becomes the remote link
  (`vscode://vscode-remote/wsl+<distro>/…`, needs VS Code's WSL extension), Windows paths
  pasted into the addons/Enterprise folder fields are translated to `/mnt/c/…`, and a Docker
  that is not running shows an explanation page (the Docker client is built lazily, on first
  use, so the app starts anyway).
- Tests: `tests/install/run-install-sh.sh` and `tests/install/run-windows-scripts.sh` (see
  `tests/install/README.md`). What has run for real on Windows, on one Windows 11 Pro PC
  (PowerShell 5.1): the enable-WSL step with its restart message, then `install.cmd` as a
  first install (an earlier version) and as an in-place update that ran the first-run
  setup. `OdooEdu` came up under WSL2 with systemd and Docker, the Start menu and desktop
  entries appeared, and the app, an Odoo 20 instance (`localhost:8069` and
  `<name>.localhost:8069`), the code-server and `--help` all worked from Windows. Not run
  for real yet: the update that no longer restarts the Linux, `verify.ps1`, `uninstall.cmd`,
  `diagnose.cmd`, a fresh install of the newest version, a clean restart-then-resume,
  Windows 10, a Home edition, and the Uninstall shortcut and Settings > Apps entry. Say
  exactly which of these you have run when reporting.
- `windows/verify.ps1` (read-only, PASS/FAIL per check, exit code 0/1) is the health check;
  `windows/AI-RUNBOOK.md` is the step-by-step guide for an AI agent testing on a real PC.
  `verify.ps1` finds the containers by the names in `docker_manager.py`, copied as constants
  at its top; `tests/test_windows_names.py` fails if they drift. The harness holds it to a
  whitelist of read-only `wsl.exe` commands, so a change that makes it mutate something
  fails a test — keep it read-only.

## Docker/Odoo internals worth knowing

- **Odoo 20.0 hard-requires Postgres 16+, changed its own default HTTP
  bind address, and removed `odoo.http.root` — three independent breaking
  changes, each confirmed directly against the real `odoo:20.0` image, each
  broke a fresh instance in a different way.**
  - `MIN_PG_VERSION = 16` (`odoo/release.py`) isn't just a soft warning:
    `base`'s own install data triggers `_recompute_user_share()`
    (`res_users.py`), whose query uses `any_value()` — a real PG16
    aggregate — so install fails outright on anything older, not just logs
    a warning. The shared database container had been `postgres:13`
    since this app's own beginning; `DB_IMAGE` (`docker_manager.py`, next to
    `DB_USER`/`DB_PASSWORD`) is `postgres:17` now. Changing that constant
    only governs a *fresh* `ensure_shared_db()` call — an **existing**
    container needs a real migration, since Postgres data directories
    aren't binary-compatible across major versions (the image just refuses
    to start against an old one). Done once, by hand: `pg_dumpall` off the
    live `postgres:13` container (every instance stopped first, for a
    clean, connection-free snapshot), a fresh `postgres:17` container on a
    *new* volume, restore the dump into it, verified byte-for-byte against
    the original (table counts, key-table row counts, and specific row
    hashes/lengths for rows this session had directly edited, matched
    exactly across all 5 real databases) before cutting the real
    container name (`DB_CONTAINER_NAME`) over to it — the old `postgres:13` container
    kept, stopped and renamed, as a rollback path, not deleted.
    One real gotcha hit mid-migration: pointing a **fresh** `postgres:17`
    container (one bootstrapped with `POSTGRES_USER`/`POSTGRES_PASSWORD`
    env vars) at a volume that *already has data* makes the official image's
    entrypoint skip that whole bootstrap — the role's actual password is
    whatever the dump's own `CREATE ROLE` restored (or failed to, silently,
    if the role already existed from an earlier bootstrap on that same
    volume — exactly what happened here). `docker exec` + bare `psql -U
    odoo` never catches this: it connects over the local Unix socket, which
    the default `pg_hba.conf` trusts regardless of password, so it looks
    fine even when real TCP/password auth (what every Odoo container
    actually uses, over the Docker network) is broken. `ALTER ROLE odoo
    WITH PASSWORD ...` fixed it; the real test is a password-bearing
    connection string (`postgresql://user:pass@host/db`), not a bare
    `psql -U`.
  - `--http-interface`'s own default flipped from `0.0.0.0` (≤19.0) to
    `127.0.0.1` (20.0+) — confirmed in both images' `config.py`; 19.0 even
    carries its own pre-announcement warning for it. Binding to loopback
    *inside* the container makes it unreachable through the Docker port
    mapping every instance here depends on — Postgres was fine, the module
    registry loaded clean, and the instance was still a connection-reset
    from outside. `_run_container` now passes `--http-interface=0.0.0.0`
    explicitly — but only when `_supports_http_interface(version)` (next to
    `_core_package_path`, same "ask the image, don't hardcode a version
    cutoff" reasoning and caching) says the CLI accepts it at all: confirmed
    directly that `odoo:10.0` hard-errors on an option it doesn't recognize
    ("no such option"), so this can't be unconditional either. A version
    too old to support the flag keeps relying on whatever its own default
    is, exactly as before either of these existed.
  - `odoo.http.root` (the `Application` instance ≤19.0 code reaches
    `.session_store` through) doesn't exist in 20.0 at all — confirmed by
    reproducing the exact `ImportError: cannot import name 'root' from
    'odoo.http'` live, from `connect_as()`'s embedded odoo-shell script
    (`docker_manager.py`). The session store moved to its own module,
    `odoo.http.session`, and from an attribute to a `@functools.cache`
    zero-arg function: `odoo.http.session.session_store()` instead of
    `root.session_store`. `SessionStore.new()`/`.save(session)` and
    `Session`'s `.db`/`.uid`/`.login`/`.context`/`.sid` properties are
    otherwise unchanged between the two, and 19.0 has no forward-compatible
    spelling (`odoo.http.session` isn't even a module there) — so unlike
    the Postgres/HTTP-bind fixes, this one didn't get a host-side
    `_supports_x(version)` probe. The branch lives entirely inside the
    embedded script itself (`try: from odoo.http import root; store =
    root.session_store` / `except ImportError: from odoo.http.session import
    session_store; store = session_store()`), since the script already runs
    inside the target container via odoo-shell and a plain `ImportError` is
    all there is to branch on — no separate image probe needed the way a
    CLI-flag or container-startup decision would. `tests/test_unit.py::test_connect_as_mints_session_on_odoo_19_and_20`
    runs the real embedded script against fake `odoo.http` modules of each
    shape, and it was verified against real instances too: minted a sid,
    then called each instance's own `/web/session/get_session_info` with it
    and got back the right `uid`/`db`, not just "didn't crash".
- **A local custom-addons folder picked by branch (`ensure_addons_copy`) is a
  one-time, immutable `git clone` of that branch — not a live view of the
  working copy, and nothing ever refreshes it.** Confirmed directly: a
  migration script committed to the branch days after the instance was
  created was simply absent from the running container's copy of that addon,
  `git log` on the cached copy stopping several commits short of the branch
  tip. There is no refresh route/button; the fix is deleting that one cached
  directory (`data/addons-copies/<repo>-<hash>/<branch>`, deterministic from
  the source path + branch) so the next `ensure_addons_copy` call re-clones
  at the same path — recreating the instance alone does *not* help, since the
  cache check (`if os.path.isdir(dest/.git): return dest`) doesn't look at
  whether the source has moved on. Worth knowing before concluding a change
  "isn't running": check what the container's own addons path actually has
  on disk before suspecting the code.
- **The addons-folder Browse can also make a real `git worktree` instead of a
  copy (`ensure_addons_worktree`), which behaves very differently from the
  copy above.** A worktree is a live, independently checked-out branch of the
  repo you browsed (same object store and refs), mounted read-write like any
  other addons folder: edit it on the host, the running instance picks the
  change up, and you `git commit`/`git push` from its directory in a terminal
  — nothing git-related ever happens inside the container. It lives at
  `data/addons-worktrees/<repo>-<hash>/<checked-out branch>`. Things worth
  knowing: its `.git` is a *file* (a `gitdir:` pointer into the source repo's
  `.git/worktrees/`), so the cache-hit check is `os.path.exists`, not the
  copy's `isdir`. Git allows a branch to be checked out in only one place, so
  picking the branch the source repo is already on fails with an error naming
  where — and the real custom-addons checkouts here are each parked on one
  branch, so that is the common first try; the optional *new branch* field
  forks a branch off it instead, which never conflicts. A remote-tracking pick
  (`origin/foo`) gets a new tracking local branch; an existing local branch of
  that name is an error, never silently reused. There is deliberately no
  tmp-dir-then-move as the copy does: `git worktree add` records the
  destination's absolute path in the source repo, so a move would leave it
  pointing nowhere. No provenance label is kept either, since the user
  switches branches inside it freely. Nothing removes a worktree: deleting
  its directory by hand leaves a phantom entry in the source repo that this
  function's own conflict check then sees, so remove one with `git worktree
  remove <dir>` from the source repo; "Remove instance" never touches it, so
  uncommitted work survives. Moving or deleting the source repo breaks every
  worktree made from it. Only the addons-folders picker offers it (the
  Enterprise-folder one stays copy-only).
- **`--addons-path` on the odoo CLI is appended to the image's own baked-in
  addons_path, not prepended or substituted** — confirmed empirically
  (`odoo.addons.__path__` shows the image's own config-file entries first
  regardless of argument order). To actually replace what an existing path
  resolves to, bind-mount over that exact path instead of adding another
  `--addons-path` entry.
- **Docker Hub's official `odoo:<version>` images are not rebuilt on every
  upstream commit** — bundled addons content can lag a git branch by weeks.
  This is the whole reason `ensure_odoo_core_addons_source` /
  `CORE_ADDONS_REFRESH_VERIFIED_VERSIONS` exist.
- **`odoo/odoo`'s repo splits addons across two directories**: root `addons/`
  (the general pool) and framework-embedded `odoo/addons/` (contains `base`
  itself plus Odoo's own `test_*` modules) — merged into one only by Odoo's
  own packaging step. A raw git checkout needs that merge replicated by hand
  (copy, not symlink — only `addons/` gets bind-mounted into the container,
  so a symlink to the sibling directory would dangle).
- **`odoo/addons/__init__.py`'s presence is version-dependent**: 16.0's image
  has it (its `ir_cron.py` needs `odoo.addons.__path__` to be a plain list);
  19.0's image does *not* (its `module.py` needs `__path__` to stay a real
  `_NamespacePath`). Never assume either way — probe the real image for the
  version in question (see `_odoo_addons_init_py`).
- **The core-addons shadow-mount is deliberately scoped to
  `CORE_ADDONS_REFRESH_VERIFIED_VERSIONS`** (currently just `{"16.0"}`), and
  only swaps `addons/` — the rest of the Odoo framework still comes from the
  Docker image, unchanged, on every version including 16.0. Widening this set
  is not just a config change: testing it against 19.0 hit a real, deeper
  incompatibility (`odoo/orm/registry.py` doesn't even exist in 16.0's
  layout). Test each version individually before adding it.
- **`debug=1` only activates Odoo's developer mode from `location.search`
  at initial page load** — a hash-routed URL like `/web#model=...&debug=1`
  still gets you to the right record (Odoo's client-side router rewrites
  the hash to the modern `/odoo/<model>/<id>` path and cosmetically carries
  `debug=1` along into the visible URL), but developer mode itself never
  actually turns on, since that one-time bootstrap read already happened
  before the rewrite. Put `debug=1` in the query string ahead of the `#`,
  e.g. `/web?debug=1#model=...`, if you're building a link into a real
  Odoo session and need debug mode to actually take effect.

## Established code patterns

- **`odoo shell` calls print a `RESULT_JSON:<json>` marker line**; callers
  (`_odoo_shell_argv` + a python script piped via stdin) scan stdout for that
  prefix rather than parsing everything. Only reach for a full `odoo shell`
  round trip when you actually need the ORM (external-id lookups, model
  logic) — it takes seconds to boot the module graph. For a simple read or a
  plain key/value write, exec `psql` directly against the shared Postgres
  container instead (see `list_users`) — it's near-instant.
- **Raw SQL built by this codebase relies on validating inputs up front, not
  parameterized queries** — e.g. `_IDENT_RE` for anything used as a bare
  identifier, or computing a value server-side against a fixed shape/regex
  before it goes anywhere near an f-string `-c` query. Match that discipline
  for any new query rather than introducing a different escaping approach.
- **Connect-type links (Open, Connect as admin/as `<user>`) are `<a>` tags
  disabled via a CSS class** (`is-disabled`, `pointer-events: none`), not the
  `disabled` attribute, toggled by a poller in `base.html` hitting
  `/instances/<name>/ready` → `dm.instance_ready()`. Only give an element the
  `data-connect-instance` attribute if it's actually capable of becoming
  ready on its own (i.e. the instance is running) — otherwise the poller
  backs off but never truly stops, and the element spins forever. An
  instance that can't become ready without a state change (stopped, mid
  restore) should render already-disabled server-side instead.

## Working conventions

- More than one Claude Code session (and the user directly) can be working
  in this repo. Check `git log` / `git status` before assuming you have the
  full picture of what's changed.
- Before pushing, `git fetch && git rev-list --left-right --count
  origin/main...main` — don't assume local and `origin/main` are in sync.
