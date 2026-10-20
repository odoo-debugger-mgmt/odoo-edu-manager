# Odoo Edu Manager on Windows

The app runs inside **WSL2** — a small Linux that Windows can run — because the
Odoo instances are Docker containers and the app drives Docker from Linux. You
do not need to know anything about WSL: **`install.cmd` sets all of it up**, and
afterwards you use *Odoo Edu Manager* like any other program from the Start menu.

## What you need

- Windows 10 version 2004 or newer, or Windows 11 — 64-bit Intel or AMD
- Virtualization switched on in the computer's firmware (BIOS/UEFI). Most
  computers have it on; if the installer later says it is off, see
  [Troubleshooting](#troubleshooting)
- About 10 GB of free disk space to start with, and a few more for each Odoo
  version you run (the Odoo images are 2–3 GB each)
- An internet connection for the install (about 1.2 GB in all: Ubuntu, Docker, the app,
  and the database and code-server images)
- A normal Windows account. Administrator rights are asked for **once**, only if
  WSL still has to be switched on

Docker Desktop is **not** needed, and it does not matter if you have it.

## Install

1. Download this repository as a ZIP — the green **Code** button, then
   **Download ZIP** — and **unzip** it. Do not run anything from inside the ZIP.
2. Open the unzipped folder, then the `windows` folder, and double-click
   **`install.cmd`**.
   - Windows may say *"Windows protected your PC"*. That is the normal warning for
     any program downloaded from the internet: choose **More info**, then
     **Run anyway**.
   - If Windows asks for permission to make changes, answer **Yes**. That is WSL
     being switched on.
3. **If it tells you to restart Windows, restart, then double-click
   `install.cmd` again.** Nothing is lost; the second run does the rest.

The long step takes a few minutes (about 5 on a fast connection, longer on a slow one,
because it downloads about 1 GB): it downloads Ubuntu (about 400 MB), checks it against
the checksum Ubuntu publishes, creates a Linux called `OdooEdu`, installs Docker in it
and installs the app, then does the app's first-run setup: it prepares the shared
database, fetches the list of Odoo versions and starts the code-server (together another
download of about 500 MB). You can leave it alone. When it says **All done**, you have
an *Odoo Edu Manager* entry in the Start menu and an icon on the desktop.

## Use

- Open **Odoo Edu Manager**. A window starts everything and your browser opens
  at `http://localhost:5001`.
- Keep that window open while you work. **Closing it stops the app, but your
  Odoo instances keep running.**
- **Stop Odoo Edu Manager** (also in the Start menu) stops the app *and* every
  instance and frees the memory. Your instances and their data are kept, but they
  stay stopped - and so they do after restarting Windows - until you open
  *Odoo Edu Manager* and press **Start** on the ones you need.

The code-server (VS Code in the browser) is already running at
`http://localhost:8443`; its password is on the app's **Settings** page.

Everything else is described on the app's own **Help** page.

### Folders from Windows

In *Addons folders* you can type or paste a Windows path such as
`C:\Users\you\exercises\my_module`; the app translates it (to
`/mnt/c/Users/you/exercises/my_module`). Two things to know:

- Files on `C:` are slower for Odoo to read than files inside Linux. For
  anything big, keep the folder inside Linux — it appears in File Explorer as
  `\\wsl$\OdooEdu\home\edu`.
- The **VS Code** button needs VS Code's **WSL** extension
  (`ms-vscode-remote.remote-wsl`). The **code-server** button needs nothing.

## Update

Download the new ZIP, unzip it, and run `windows\install.cmd` again. It updates
the app and keeps the Linux, your instances and your data. It does not restart the
Linux, so instances that are running keep running; if *Odoo Edu Manager* is open,
close its window and open it again afterwards to use the new version.

## Uninstall

Any one of these, then type `YES` when asked:

- **Start menu → Uninstall Odoo Edu Manager**
- **Settings → Apps → Installed apps → Odoo Edu Manager → Uninstall**
- **`windows\uninstall.cmd`** in the folder you unzipped

It removes the app, the `OdooEdu` Linux **and everything in it — every instance,
database and file** (there is no undo, so keep what you need first), the Start menu and
desktop entries, its entry in Settings → Apps, and its folder in `%LOCALAPPDATA%`
(including the downloaded Ubuntu image). Your other WSL distributions and your Windows
files are not touched.

What stays: **WSL itself and the Virtual Machine Platform**, if the installer had to
switch them on - other programs use them too. To turn them off: *Turn Windows features
on or off*, untick *Virtual Machine Platform* and *Windows Subsystem for Linux*, restart.
The ZIP and the unzipped folder you installed from are yours to delete.

If the Start menu entry and Settings → Apps are both gone (for example the install
folder was deleted by hand), use the unzipped `windows\uninstall.cmd`; if that is gone
too, run this in Command Prompt only when `%LOCALAPPDATA%\OdooEduManager` still exists:
`powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\OdooEduManager\uninstall.ps1"`.

## Troubleshooting

**"Virtualization is not enabled" / error `0x80370102`.** Windows or the BIOS has
virtualization off. Run `install.cmd` again after a restart (it switches on
Windows' *Virtual Machine Platform*); if the message stays, enable *Intel
Virtualization Technology* / *VT-x* or *AMD SVM* in the BIOS/UEFI setup.

**It says "a restart is still pending" (or "Windows has restarted twice and still...").**
Switching WSL on is a Windows update step that a restart completes. Usually one restart
does it. If Windows is *already* waiting for a restart from something else (an update), the
installer asks you to restart first and changes nothing. If Windows restarts twice and still
says it has not finished, Windows' own update machinery is stuck; another restart will not
help, and this is not something the installer can fix. In this order:

1. **Settings > Windows Update**: check for updates, install everything, restart (more than
   once if it asks).
2. Open **Command Prompt as administrator** and run `DISM /Online /Cleanup-Image /RestoreHealth`,
   then `sfc /scannow`, then restart.
3. Check the boot setting: `bcdedit /enum {current}`. If it shows `hypervisorlaunchtype Off`,
   run `bcdedit /set hypervisorlaunchtype auto` and restart.

Then double-click `install.cmd` again. `diagnose.cmd` shows what Windows says about all of this.

**A company-managed computer.** Group policies can block WSL or virtualization.
Ask IT to allow *Windows Subsystem for Linux* and *Virtual Machine Platform*.

**The page says "Docker isn't reachable".** Docker was still starting. Wait a
minute and press *Try again*. If it keeps saying so, run `diagnose.cmd` and send
the file it prints.

**Port 5001 is already used.** Another program has it. Close that program, or
ask for help — the port can be changed.

**Instance links (`http://name.localhost:8069`) do not open.** Current Chrome,
Edge and Firefox open them. If yours does not, give the instances a name that
resolves everywhere: open a terminal and run

```
wsl -d OdooEdu -u edu
echo 'export ODOO_EDU_MANAGER_BASE_DOMAIN=lvh.me' >> ~/.profile
exit
```

then stop and start Odoo Edu Manager. (`lvh.me` is a public domain that always
points to your own computer; instance names appear in DNS queries, so use it
only if you are comfortable with that.)

**It uses too much memory.** WSL may take half of the computer's memory. Create
`%UserProfile%\.wslconfig` containing

```
[wsl2]
memory=6GB
```

and run `wsl --shutdown`.

**Is everything working?** Double-click **`verify.cmd`**: it only reads, and prints a
PASS or FAIL line for each thing that has to work - the Linux, Docker, the containers,
the app and its pages, code-server and your instances. Open *Odoo Edu Manager* first, so
the web pages can be checked too.

**Something else.** Run **`diagnose.cmd`**: it only reads, changes nothing, and
writes a report (`%LOCALAPPDATA%\OdooEduManager\diagnose.txt`) with Windows, WSL
and Docker details. The installer's own log is `install.log` in the same folder.

## What it puts where

| What | Where |
|---|---|
| Scripts, settings, logs | `%LOCALAPPDATA%\OdooEduManager` |
| The Linux's disk (everything it stores, including instances) | `%LOCALAPPDATA%\OdooEduManager\distro\ext4.vhdx` |
| The downloaded Ubuntu image (safe to delete afterwards) | `%LOCALAPPDATA%\OdooEduManager\cache` |
| Start menu and desktop entries | your own Start menu and desktop |
| The entry in Settings → Apps | one key under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager` |
| The Linux itself | a WSL distribution named `OdooEdu` (see `wsl -l -v`) |

Nothing is written to `Program Files`, and nothing is installed for other users. That is
everything: no services, scheduled tasks, environment variables or firewall rules.

## For maintainers

- `install.cmd` → `install.ps1` → `lib.ps1` (`Invoke-OdooEduInstall`): check
  Windows, switch WSL on if needed (the only elevated step, `wsl --install
  --no-distribution`), import Ubuntu 24.04 from Ubuntu's own release server as
  `OdooEdu`, copy the app in, then run `provision.sh` inside it as root. That
  creates the `edu` user, merges `/etc/wsl.conf` and runs the repository's
  normal `install.sh` as that user. It is safe to run again.
- Windows PowerShell 5.1 is the target: the scripts are ASCII only and use no
  newer syntax. The tests check both.
- `tests/install/run-windows-scripts.sh` runs these scripts against a pretend
  Windows (fake `wsl.exe`, `curl.exe`, `robocopy.exe`, UAC and shortcuts) in a
  PowerShell container; `tests/install/run-install-sh.sh` runs `install.sh` in
  clean Ubuntu containers. Neither can show how a real Windows behaves — run
  `install.cmd` on one for that, and use `diagnose.cmd` if it misbehaves.
- `verify.cmd` / `verify.ps1` is the read-only health check: one PASS/FAIL/WARN line per
  check, exit code 0 or 1, saved to `verify.txt`. The installer copies it next to the other
  scripts. To test the installer on a real PC with an AI agent, point the agent at
  [AI-RUNBOOK.md](AI-RUNBOOK.md): the exact commands, what only a person can do, the
  expected results and what to report.
