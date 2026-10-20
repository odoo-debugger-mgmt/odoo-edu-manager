# Install smoke-test

Proves the documented install works on a machine that has never seen this
project: a bare `ubuntu:24.04` with only python3, git and the Docker CLI,
which installs the working tree with `uv tool install` and then checks the
things that break on someone else's machine rather than the developer's.

```bash
./tests/install/run.sh            # default base domain (localhost)
./tests/install/run.sh lvh.me     # the base domain macOS/Windows need
```

It builds an image and mounts the host Docker socket, so it isn't part of
the `pytest` run. Checks: console scripts on `PATH`, templates and
`static/` actually shipped inside the wheel, `DATA_DIR` landing in the XDG
location rather than a stale checkout path, five HTTP routes, the
per-instance cookie hop under the configured base domain, and the MCP
server completing a real stdio handshake.

## The installers

The repository has two installers, and each has its own test.

```bash
./tests/install/run-install-sh.sh [fresh|existing|wsl|all]
./tests/install/run-windows-scripts.sh
```

**`install.sh`** (Ubuntu, Debian, WSL) is run in clean Ubuntu 24.04 containers, as an
ordinary user with sudo - what a fresh WSL distribution is:

- `fresh` - no Docker anywhere. It must install Docker Engine, give the user access,
  install the app and start it, do the first-run setup for real (the shared database is
  running, the list of Odoo versions is fetched, and a real code-server container is
  pulled, started and answers on 8443),
  and a second run must change nothing. A nested Docker really runs containers here, so
  this needs `--privileged`.
- `existing` - a working Docker is already reachable (the host's, through its socket:
  what Docker Desktop's WSL integration looks like from inside). It must leave that
  Docker alone and still install the app.
- `wsl` - pretends to be an older WSL distribution with no systemd and a `wsl.conf` of
  its own: systemd must be switched on without losing those settings, Docker must still
  run in this session, and a WSL1 distribution must be refused. It installs with
  `--no-setup`, so the app's very first start has to download the database image while
  `install.sh`'s start-up check waits for it - the case that check once gave up on too soon.

**`windows/*.ps1`** are run by `windows-scripts.ps1` in Microsoft's PowerShell container
against a pretend Windows: `wsl.exe`, `curl.exe`, `robocopy.exe`, the administrator prompt
and the Start menu shortcut object are fakes that record what they were asked and answer
from a script (one of them is the real `wsl --status` text of a laptop where WSL cannot
start yet). Everything above that boundary - `install.ps1`, `lib.ps1`, `start.ps1`, ... -
is the real code, so typos, wrong arguments, bad ordering and mishandled failures show up.

Things learned the hard way, worth keeping in mind when changing them:

- Ubuntu 24.04's `docker.io` ships a systemd unit and **no init script**, so
  `service docker start` does nothing without systemd; `install.sh` starts `dockerd`
  itself there.
- In PowerShell `$script:` means the *calling* script's scope, so fakes that share state
  with the harness use `$global:`; and `$calls` is the same variable as `$script:Calls`.
- Windows PowerShell 5.1 reads a script without a byte-order mark as the system code page,
  so the scripts are ASCII only; the harness checks that, and that nothing newer than 5.1
  syntax is used. PSScriptAnalyzer (pin 1.22.0 on the 7.4.2 container) reports only the
  expected style notes: `Write-Host` in an interactive installer, a few plural nouns and
  `ShouldProcess` on helper functions - no errors.
- The Windows-scripts suite is meant to run on a real Windows PowerShell 5.1 as well, and it
  does (every check passes on a Windows 11 laptop) - which is how two of its own mistakes were found:
  it must not override `%SystemRoot%` (the crypto provider behind `Get-FileHash` finds its
  DLL through it, so hashing then fails), and on 5.1 a script-scope fake does not shadow a
  command that is a *function* in a module (`Get-FileHash` is one there). So it fills in
  Windows variables only where they are missing, always redirects `%LOCALAPPDATA%` (and
  deletes only inside its own temp folder), defines every fake as `global:`, and refuses to
  start unless each pretend command is really the one that would run - so a test run can
  never raise a real administrator prompt or touch real shortcuts.
- This shell's `grep` is a function that skips files containing NUL characters, which the
  harness log has on purpose: use `/usr/bin/grep -a` on it.

## What it covers, and what it can't

**Linux** — directly, this is the platform.

**WSL2** — well. WSL2 is a normal Linux userland on a real kernel; a
container is the same userland on the same kernel. Everything this test
checks behaves identically there. Two things remain Windows-side and
untestable here: Docker Desktop's WSL2 socket integration, and the Windows
browser resolving the per-instance hostnames (`*.localhost` does not resolve on
Windows, which is why `ODOO_EDU_MANAGER_BASE_DOMAIN` exists — hence the
`lvh.me` run above).

**macOS** — not at all, and no container can. Containers share the host
kernel, and macOS needs Darwin; images that run macOS under QEMU exist but
need KVM and sit outside Apple's licence terms. What actually differs on
macOS is not the OS but the *daemon*: Docker Desktop runs it in a VM, so
`--network host` is the VM's network and `localhost` inside a container is
not the Mac. Those branches — `host_networking_supported`,
`_code_server_network_kwargs`, `debug_attach_host` — are pinned in
`tests/test_unit.py` against a faked `client.info()`, which is the honest
substitute: it tests the decision, not the platform.

So a green run here plus a green `pytest` means the install path and the
Desktop branches are both covered. It does not mean the app has been run
on a Mac. That still needs a Mac.

**The Windows installer** is the part nothing here can prove: the fakes show that the
scripts do the right things in the right order and fail sensibly (also under a real Windows
PowerShell 5.1), not that a real Windows answers the way the fakes do (what `wsl --import` prints, whether the Ubuntu `.wsl` image
imports as documented, how `localhost` is forwarded). That needs `windows\install.cmd` run
on a real PC, with `windows\diagnose.cmd` when it misbehaves. Real runs have been made
(on Windows 11 Pro; CLAUDE.md lists what they covered). It worked end to
end, and the only thing the fakes had got wrong was one they could not know: `wsl.exe`
exits with 255 on a healthy WSL that has no distribution yet, which the tests now cover.
