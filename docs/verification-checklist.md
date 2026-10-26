# devproxy real-machine verification checklist

Behavior that the automated tests (1084 cases) do not cover and that **must be run once on
a real machine**. Tick each item before a release; for anything you cannot tick, write
"not verified + reason".

**Item counts**

| Group | Items | Source |
|---|---|---|
| A. macOS behavior | 22 | Review gaps from the migration and macOS phase (Task 16 §7 items 1-17 + second-round items 18-22) |
| B. Remaining gaps from the Task 16 review | 5 | Same as above, ①-⑤ of "Task 16: minor (deferred)" |
| C. Residual risk in migration detection | 1 | `migrate.detect_legacy()` "cannot read the legacy unit ⇒ treat as no legacy environment" |
| **Subtotal (A+B+C)** | **28** | |
| D. Linux real-machine checks | 8 | Linux-side behavior the unit tests do not cover (not counted in the 28) |
| F. Real-machine checks for new features | 12 | Added from 2026-10-23: `uninstall` / `up` (formerly sync) / `run` / domain-suffix guard / child-process timeout / `logs --edit` (F10 was verified on 2026-10-25 in an ubuntu:24.04 container) |

Conventions:

- Each item has a **Check** and an **Expected** part: Check is a command you can type
  as-is; Expected is a decidable conclusion.
- Items already run on a real machine with a conclusion have their conclusions in groups
  E / F; groups A / B / C / D are the **pending** list.
- Run every command on a **real machine**; do not use the fake platform.
- Confirm before running steps that involve sudo.
- Group A (macOS) needs a **real Mac**; this project's development machine is Linux, so
  most A items have **not been verified on a real machine** to date.
- Since 2026-10-26 the **unit suite runs in CI on macOS as well** (Linux + macOS x
  Python 3.11/3.12/3.13). That covers the *code* on macOS -- it is not a substitute
  for the items below: the suite runs against a fake platform and a fake home
  directory, so real launchd, a real keychain, a real `/etc/resolver` and a real
  `/Library/LaunchDaemons` are still untested. See the note at the top of group A.

**Revision history**

| Date | What changed |
|---|---|
| 2026-10-24 | Added group F (real-machine checks for new features); marked C1 as fixed at the code level; updated the test count; removed dangling references to a gitignored directory |
| 2026-10-25 | **F10 complete**: the full chain ran in an ubuntu:24.04 container (no systemd) — install → init → graceful failure of setup → run supervision → sync → trust → `curl https://demo.test` 200 (without -k) → down |
| 2026-10-26 | The unit suite entered CI on **macOS** and immediately turned up three real platform gaps, all fixed: `app_process_alive` read `/proc` (no such thing on macOS — every live app looked dead, so sync tore down and rebuilt running apps), the process-group cleanup was skipped entirely on macOS (no `WNOWAIT`, so the leader was reaped before the group was signaled and grandchildren survived), and two tests raced the machine's load. The suite is now green on both platforms; the items in group A are still pending -- CI runs against fakes, not a real Mac. |

---

## A. macOS behavior (22 items; most still unverified, conclusions in group E)

> The behavior in this group is implemented and unit-tested in the code (using fake
> subprocesses to verify "what was called and with which arguments"), but **feedback from
> the real system** can only be obtained on a Mac. Run each item and record the result.
>
> **Since 2026-10-26 the unit suite also runs in CI on macOS** (it is green there). That
> is not a substitute for the items below: the suite drives a fake platform, a fake home
> directory and fake processes, so the real launchd / keychain / `/etc/resolver` /
> `/Library/LaunchDaemons` behaviour is still unverified. What CI *did* find on macOS were
> three code-level gaps (process liveness probing, process-group cleanup, and two
> timing-sensitive tests) -- all fixed; the items below are about the system, not the code.

### A1. launchd plist key semantics and effect

- Check: after `devproxy setup`, inspect
  `~/Library/LaunchAgents/com.devproxy.dnsmasq.plist` and
  `/Library/LaunchDaemons/com.devproxy.caddy.plist`, and confirm all eight keys `Label` /
  `ProgramArguments` / `EnvironmentVariables` / `RunAtLoad` / `KeepAlive` /
  `StandardOutPath` / `StandardErrorPath` / `Description` are present.
- Expected: launchd accepts these keys as-is; in the output of
  `launchctl print gui/$(id -u)/com.devproxy.dnsmasq`, `state = running`, and the
  `RunAtLoad` + `KeepAlive` combination does not cause the process to restart repeatedly.

### A2. Actual output and exit codes of `launchctl print`

- Check: run `launchctl print system/com.devproxy.caddy` and
  `launchctl print gui/$(id -u)/com.devproxy.dnsmasq` separately; record exit codes and
  output.
- Expected: both targets print job information; **whether a regular user needs root to
  read the system domain must be settled here** — if root is required,
  `service_active("caddy")` will always be False and `devproxy doctor` will falsely report
  "caddy service not running", so a read-only probe must replace it.

### A3. Real behavior of `launchctl bootstrap` / `bootout`

- Check: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.devproxy.dnsmasq.plist`;
  bootstrap a second time; run `launchctl bootout` on the already-loaded plist; try once
  with a deliberately wrong domain.
- Expected: repeated bootstrap fails and **does not break the running job**; after
  bootout the job stops; a wrong domain returns a non-zero exit code with a readable
  error, and devproxy does not treat it as success.

### A4. Whether `launchctl kickstart -k` really restarts

- Check: `launchctl kickstart -k gui/$(id -u)/com.devproxy.dnsmasq`, and use `ps` to see
  whether the pid changes; then `kill -9` the process and observe whether `KeepAlive`
  starts it again automatically.
- Expected: the pid changes after kickstart; after the process is killed it is started
  again automatically (KeepAlive works).

### A5. Format of `/etc/resolver/<suffix>` and the `port` directive

- Check: `cat /etc/resolver/test` should be `nameserver 127.0.0.1` + `port 5300`; run
  `dscacheutil -q host -a name foo.test` to see whether it resolves to 127.0.0.1.
- Expected: mDNSResponder accepts the file; **the `port 5300` line is honored** (behavior
  may differ across macOS versions — this is the most suspect item in this group); the
  resolution result is 127.0.0.1.

### A6. Real effect of `security add-trusted-cert -d -r trustRoot`

- Check: after `devproxy trust`, verify with `security verify-cert -c <ca> -p ssl`;
  access `https://<name>.test` in Safari and Chrome separately.
- Expected: the command needs no extra interactive authorization (it relies only on the
  sudo cache); both Safari and Chrome trust the CA; Chrome takes effect without a restart
  (macOS uses the keychain, unlike NSS which requires a process restart).

### A7. Exit codes and output format of `security verify-cert`

- Check: `security verify-cert -c <ca> -p ssl; echo $?`, then swap in an untrusted
  certificate.
- Expected: exit code 0 when trusted, non-zero when untrusted — `ca_trusted()` relies on
  this exit code for its verdict.

### A8. Whether root's LaunchDaemon can bind 80/443

- Check: `sudo lsof -nP -iTCP:80 -sTCP:LISTEN` and the same for 443; access
  `https://<name>.test`.
- Expected: caddy (running as root) successfully listens on 80/443, with no interception
  under SIP / launchd policy; port 80 automatically 308-redirects to 443.

### A9. Output format of `dscacheutil -q host -a name` and the dig fallback

- Check: run `dscacheutil -q host -a name foo.test` and inspect the output lines; then
  temporarily make dnsmasq not respond, and observe whether it falls back to
  `dig +short`.
- Expected: the output contains `ip_address:` or `ipv6_address:` lines, and the tool takes
  the first A/AAAA answer; the fallback path obtains the same answer.

### A10. Behavior of macOS's bundled curl

- Check: `curl -fsS --max-time 3 https://<name>.test`; then run the same command against a
  nonexistent domain.
- Expected: succeeds for a registered domain; fails for an **unregistered** domain (TLS
  handshake failure) — exactly the security property that "an unregistered domain gets no
  certificate" also holds on macOS.

### A11. Homebrew's dnsmasq install location

- Check: `ls -l /opt/homebrew/sbin/dnsmasq /usr/local/sbin/dnsmasq /usr/sbin/dnsmasq`;
  `brew --prefix`.
- Expected: `/opt/homebrew/sbin/dnsmasq` on Apple Silicon, `/usr/local/sbin/dnsmasq` on
  Intel; the path chosen by the tool matches the actual install location (the unit
  contains absolute paths; launchd does not honor PATH).

### A12. BSD versions of `install -d` / `cp`

- Check: `sudo install -d /etc/resolver` and `sudo cp <src> /etc/resolver/test` (on a
  clean machine).
- Expected: the directory is created and the file is overwritten successfully, with no
  errors caused by BSD/GNU behavior differences.

### A13. Streaming follow of `tail -f` and Ctrl-C

- Check: `devproxy logs dnsmasq`; observe whether output streams continuously and whether
  Ctrl-C exits.
- Expected: live follow without buffering the whole output; Ctrl-C exits immediately and
  prints no traceback.

### A14. Whether `sudo -v` pre-authentication is enough

- Check: run one `devproxy setup` (with multiple elevation actions), and count how many
  times it asks for a password.
- Expected: **only one prompt for the whole run** (the elevated session is cached and
  reused).

### A15. plist ownership and permissions

- Check: `ls -l /Library/LaunchDaemons/com.devproxy.caddy.plist`.
- Expected: mode 0644, owner `root:wheel` (`chown` failure is non-blocking, but a
  `chmod 644` failure must be reported as an error).

### A16. Elevation session cache covers the whole run

- Check: same as A14, but immediately after `setup` run `devproxy trust --yes` and observe
  whether it asks for a password again.
- Expected: authenticate only once within the same run.

### A17. `doctor` wording and exit code on macOS

- Check: `devproxy doctor`; deliberately stop dnsmasq and run it again.
- Expected: exit code 3 when unhealthy; the hint mentions `launchctl`, not `systemctl`.

### A18. Whether devproxy itself can read the plist it installed

- Check: `cat /Library/LaunchDaemons/com.devproxy.caddy.plist` (**regular user**, without
  sudo).
- Expected: readable (0644); `_unit_current()`'s elevation fallback obtains the content on
  a real machine, so doctor does not permanently report red because "the unit cannot be
  read".

### A19. Who creates the system-scope log directory, and whether it exists before first start

- Check: `ls -ld ~/.local/state/devproxy/logs`; on a fresh user, run setup once and confirm
  the directory exists **before** the first launchd spawn.
- Expected: the directory is created **as the user identity** (owned by the user), and
  root-run caddy can write into it; logs are not lost even when the home directory is on a
  network volume or under SIP restrictions.

### A20. `launchctl print` success ≠ the job is running

- Check: `launchctl print system/com.devproxy.caddy` and inspect the exact `state` line
  (`state = running`? how is it indented? are there multiple state lines?); then kill the
  caddy process and see what `state` becomes.
- Expected: when loaded but exited, `state` is **not** `running`, and the tool treats it
  as "not running"; when no `state` line is visible, treat it as "not running" (fail
  closed), so doctor does not falsely report green.

### A21. Root-owned files left in the user's home by root-run caddy

- Check: `ls -l ~/.local/share/caddy/pki/authorities/local/root.crt`,
  `ls -l ~/.local/state/devproxy/logs/`; try reading and deleting these files as a
  regular user.
- Expected: the files are readable (the browser can read the CA, `tail` can read the
  logs); the user **can delete** them (root-owned files in a user's home are usually
  deletable because of the parent directory permissions, but confirm).

### A22. Homebrew's own dnsmasq config is bypassed

- Check: confirm `~/.config/dnsmasq/dnsmasq.conf` is written by this tool;
  `brew services list` to see whether Homebrew's dnsmasq is running and grabbing port
  5300.
- Expected: only this tool's dnsmasq is running (the unit uses
  `--conf-dir=~/.config/dnsmasq`, so Homebrew's default config does not take effect);
  `/etc/resolver/test` really makes the system resolver split traffic by domain.

---

## B. Remaining gaps from the Task 16 review (5 items)

> Source: ①-⑤ of "Task 16: minor (deferred, handed to the final review for triage)" in
> `progress.md`. ①-④ are side effects of code shared by Linux/macOS and need their
> direction confirmed on a real machine; ⑤ is a gap in the checklist itself.

### B1. Impact on Linux of tightening the `_unit_current()` criterion

- Check: on Linux, manually change `ExecStart=` in
  `~/.config/systemd/user/caddy.service` (add a leading argument / change the argument
  order), then run `devproxy doctor` and `devproxy setup`.
- Expected: judged "unit outdated" (the safe direction — the unit is reinstalled), doctor
  reports one more red, and setup does one more reinstall. Acceptable, but confirm that
  setup does not ask for a password every time.

### B2. doctor's elevation fallback pops a password prompt

- Check: change `~/.config/systemd/user/caddy.service` to `chmod 000` (or root-owned
  0600), then run `devproxy doctor`.
- Expected: doctor **may pop a sudo password prompt** (the cost of reading the unit with
  elevation) — confirm this behavior is acceptable; if not, change it to a non-interactive
  read such as `sudo -n`.

### B3. `unit_exec_argv` does not understand systemd's quoting syntax

- Check: write `ExecStart=` with a quoted path
  (`ExecStart="/path with space/caddy" run --config …`), and run `devproxy doctor`.
- Expected: judged "unit outdated" → setup reinstalls the unit (safe direction; it will
  not run the wrong config).

### B4. Direction problem in `migrate.detect_legacy()`

- Check: see group C (this item and C are two manifestations of the same risk; C is the
  executable version).

### B5. Whether the checklist covers items 18-22

- Check: compare group A of this document item by item against the "list of unverified
  behavior (22 items)" in `task-16-report.md`.
- Expected: the 22 entries correspond one-to-one with no omissions (A18-A22 are precisely
  the five items the review specifically asked to add).

---

## C. Residual risk in migration detection (1 item)

### C1. `migrate.detect_legacy()`: cannot read the legacy unit ⇒ treat as "no legacy environment"

> **Fixed at the code level (2026-10-23, Clean Code review P1 batch)**.
> `detect_legacy()` now **no longer** returns `None` (= "no legacy environment") when the
> legacy unit cannot be read; instead it returns a `LegacyState` carrying an `error`; when
> `migrate()` sees `state.error` it **raises `MigrationError` and aborts the migration**
> (`no files changed`), and the error message includes the unit path and the
> explanation "to avoid overwriting a hand-maintained unit without a backup, migration
> was aborted".
>
> The same batch also closed two sibling branches: `--config` **cannot be parsed** out of
> `ExecStart=`, and the unit uses **`EnvironmentFile=`** (variables cannot be carried
> safely into the new unit) — both refuse migration explicitly.
>
> The item below **still needs to be run on a real machine**: the fix itself is designed as
> "cannot read = abort", but we must confirm that a real machine really takes the "abort"
> path and does not slide back into "silent takeover".

- Background: when `detect_legacy()` **cannot read** the legacy `caddy.service` (a regular
  user has no permission, sudo is unavailable), it returned `None`, meaning "no legacy
  environment". In an extreme case, migration would **take over the user's hand-maintained
  Caddyfile and unit environment variables** (for example `MINIKUBE_IP`) without the user
  knowing at all. An elevation fallback has been added to the code, but the "even
  elevation cannot read it" path still exists.
- Check:
  1. Create a legacy environment: `~/.config/systemd/user/caddy.service` points at another
     Caddyfile, and `ExecStart` carries `Environment=MINIKUBE_IP=1.2.3.4`;
  2. Make the unit unreadable to a regular user (`chmod 000`).
     **Note: root can bypass permission bits** — as long as `sudo` is available, the
     elevated read still succeeds, so this "even elevation cannot read it" branch is
     **practically unreachable on Linux with working sudo**; to really reach it, you must
     create an environment where "elevation is unavailable" (no usable sudo credentials /
     sudo denied). Otherwise this item can only verify the "elevation fallback works" half
     (a regular user cannot read it → elevation reads it → the legacy environment is still
     recognized);
  3. Run `devproxy setup` and see whether it reports "Found a legacy caddy service (not
     managed by this tool)".
- Expected: **either** it correctly detects the legacy environment (printing "Found a
  legacy caddy service (not managed by this tool)") and gives the backup and rollback
  hints, **or** it explicitly reports "cannot read legacy caddy unit <path>; its config and
  origin cannot be confirmed … migration was aborted" and **aborts the migration** rather
  than taking over silently. If silent takeover occurs (the unit is rewritten without any
  notice), mark this item as failed and report it.

  > One more easily triggered check: remove `--config` from `ExecStart=` (or use a strange
  > form other than `--config=<path>`); `devproxy setup` should **refuse migration
  > explicitly** and explain why, rather than treating the machine as clean and taking
  > over.

---

## D. Linux real-machine checks (8 items, not counted in the 28)

> These are Linux-side behaviors that the unit tests (all hermetic, with the subprocess
> layer fully stubbed) **cannot cover**.

### D1. Full `devproxy setup` flow under a clean user

- Check: run `devproxy setup` as a new user (or with `HOME` pointing at a temporary
  directory), then run `devproxy doctor`.
- Expected: setup runs through end to end, and all four doctor components are ready; the
  number of sudo prompts matches the list of "steps that need sudo".

### D2. Idempotency of `setup`

- Check: immediately run `devproxy setup` again.
- Expected: it prints "All components are ready; nothing to change.", **does not ask for
  a password again**, and does not restart systemd-resolved; when the CA has not been
  generated yet, it additionally shows "no CA root certificate yet (Caddy will generate it
  when the first route is registered)".

### D3. `caddy validate` against a config containing real site blocks

- Check: write the user's own site blocks into `~/.config/devproxy/sites.caddy`
  (including `tls internal`, `reverse_proxy`, `{$MINIKUBE_IP:…}`, and the like), then run
  `~/.local/bin/caddy validate --config ~/.config/devproxy/Caddyfile`.
- Expected: validation passes; the migration flow runs the same validation (before
  swapping the unit).

### D4. `dig` / `resolvectl` against the real resolver

- Check: `dig @127.0.0.1 -p 5353 foo.test +short` and `resolvectl query foo.test`; then
  `resolvectl status` to inspect the `~.test` route.
- Expected: both resolve `foo.test` to `127.0.0.1` (or `::1`);
  `/etc/systemd/resolved.conf.d/devproxy.conf` takes effect and `Domains=~.test` is
  registered.

### D5. `curl` against a real `https://<name>.test`

- Check: first run `devproxy up demo -- sh -c 'python3 -m http.server $PORT'`, then
  `curl -v https://demo.test`; then `curl -v https://not-registered.test`.
  (`python3 -m http.server` **does not read `$PORT`** — it only accepts a port argument on
  the command line, so without `sh -c` it always binds to 8000, and `up` waits out the full
  ready timeout and cannot register any route.)
- Expected: the registered domain completes the handshake and its certificate is issued by
  devproxy's CA; the **unregistered domain fails the handshake** (gets no certificate),
  which is exactly the security property.

### D6. NSS DB and Chrome trust

- Check: `certutil -L -d sql:~/.pki/nssdb` to see `devproxy Local CA` and the trust flags
  `C,,`; then access `https://<name>.test` in Chrome.
- Expected: the CA is in the NSS DB; Chrome shows no warning **after fully quitting and
  restarting** (with only the system trust store installed, Chrome necessarily reports
  `net::ERR_CERT_AUTHORITY_INVALID`).

### D7. `loginctl enable-linger`

- Check: `loginctl show-user $USER -p Linger --value` should be `yes`;
  `systemctl --user status caddy` should also be active in a **logged-out** session.
- Expected: linger is enabled and caddy autostarts on boot (runs without login).

### D8. Backup / rollback round trip (real machine)

- Check: on a machine with a legacy environment (a hand-written `caddy.service` +
  `Caddyfile`), run `devproxy setup`, confirm that
  `~/.local/state/devproxy/backups/<UTC timestamp>/` contains `manifest.json`,
  `MANIFEST.txt`, and a `.bak` for each file; then run
  `devproxy setup --rollback ~/.local/state/devproxy/backups/<UTC timestamp>`.
- Expected: after rollback, the legacy `caddy.service` and old `Caddyfile` are restored
  **byte for byte** (compare `sha256sum` against the hashes in the manifest), files that
  did not exist originally are deleted, and the caddy service is restarted.

---

## E. macOS real-machine test results (2026-10-23)

Machine: `my-mac` (WenbindeMBP, macOS 26.1 / 25B78, Intel x86_64).
Install method: `python3 -m venv` + `pip install -e` (that machine has no uv); everything
landed under `~/devproxy-test/`.

### E1. Verified

| Item | Result |
|---|---|
| Full test suite | **662 passed** (the case count at the time; by 2026-10-24 it had reached **997 passed**, covering both Linux and macOS) |
| `devproxy config` | ✅ paths and config rendering normal |
| `devproxy doctor` | ✅ table rendering and exit-code semantics normal |
| `devproxy selftest` | ✅ **11/11 all passed** — real Caddy 2.11.7 on macOS: isolated instance starts, trust store installation is disabled (corroborated by caddy logs), register→reload→hit upstream, unregistered domain gets no certificate, http→https redirect, unregister→no route remains |
| Caddy download (mac/amd64) | ✅ platform key `('mac','amd64')`, 53 MB, **sha256 verified** |
| brew's dnsmasq location | ✅ found `/usr/local/sbin/dnsmasq` (→ Cellar 2.91) |
| Elevation failure path | ✅ in password mode with no TTY it raises `PermissionError`; the message contains both `sudo -v` and `sudo -n true` output |
| Non-interactive + privileged actions | ✅ exit code **4**, and **no changes are made** |
| Elevation checklist accuracy | ✅ after the fix it correctly lists 2 items (caddy LaunchDaemon + `/etc/resolver`) |

### E2. Three defects found and fixed on macOS this round

1. **The test helper `_process_running` always returns False on macOS**
   (`tests/test_runner.py`) — `FileNotFoundError` is an `OSError` subclass, swallowed
   first by a separate `except`, so the `os.kill` fallback is never reached. The
   consequence is not just 2 tests turning red:
   `wait_until(lambda: not _process_running(...))` holds immediately and **becomes an
   always-true empty assertion**, so a cleanup-logic regression would also go unnoticed.
   Fix: on macOS use `ps -o stat=` (a zombie process still makes `kill(pid,0)` return
   success).

2. **caddy's install-service action was not marked `privileged` on macOS**
   (`components/caddy.py`) — on macOS caddy is a root daemon under
   `/Library/LaunchDaemons`, while on Linux it is a user unit. The missing flag caused the
   sudo list to under-report one item (the real machine reported 1 item while the actual
   number was 2), and when only this step remained it would **bypass the non-interactive
   guard** and pop a password dialog midway. A mirrored pair of assertions was added (mac
   must be True / linux must be False).

3. **`DnsComponent.check()` only looked at file existence, inconsistent with `plan()`'s
   content criterion** (`components/dns.py`) — on that machine `/etc/resolver/test` was
   left by **another tool in 2022** with `nameserver 127.0.0.1` and **no `port 5300`**, so
   queries were sent to `:53` (mDNSResponder) instead of the tool's dnsmasq. `doctor` still
   reported `dns ✔ ready`. Changed to content-first, falling back to the platform
   existence criterion only when the content cannot be read. This is the same class as
   the previously recorded Linux-side V2: "only recognizes existence / only recognizes its
   own artifacts".

### E2b. Fourth defect: macOS refuses to change system trust settings in a non-graphical session

Real-machine result of running `devproxy trust` on macOS 26 + SSH:

```
devproxy: 1 step(s) failed: add the CA root certificate to the system keychain (…)
  (failed to install the CA into the system keychain: SecTrustSettingsSetTrustSettings:
    The authorization was denied since no user interaction was possible.)
Exit code: 1
```

**Troubleshooting conclusion (verified item by item)**:

- The certificate **did** enter the System keychain (it can be found by fingerprint in
  `/Library/Keychains/System.keychain`) — only the "set trust settings" step failed.
- `security verify-cert -c <ca> -p ssl` → `CSSMERR_TP_NOT_TRUSTED`, consistent with the
  tool's verdict (the tool's `ca_trusted` uses exactly this).
- Root cause: `SecTrustSettingsSetTrustSettings` needs **an authorization dialog in the
  graphical interface**; an SSH session has no graphics context, so the system refuses
  outright (`errAuthorizationInteractionNotAllowed`). **sudo itself succeeded** — this is
  not insufficient permission.
- The `launchctl asuser <uid>` workaround also fails over pure SSH: the inner `sudo` still
  needs a TTY.

**Fix** (`4d7fc8a`): recognize this system text, throw `PermissionError` instead (the CLI
maps it to exit code 4), and print "Run this in a Terminal window on that Mac: devproxy
trust" on its own line (the whole paragraph is wrapped by Rich, and the command must stay
complete and copyable).

**Conclusion**: this step **can only be completed in the Mac's graphical interface**; it is
a system limitation, not a tool defect. All the tool can do is explain the failure clearly
and give the right exit code.

### E3. Still unverified (requires root; must be run interactively by the machine's owner)

All of the following happen during the elevation phase of `devproxy setup` and cannot be
completed over non-interactive SSH (sudo on that machine needs a password):

- A1/A2/A3/A4: landing of the LaunchDaemon plists and the real behavior of `launchctl`
- A5: the actual write of `/etc/resolver/<suffix>` and whether the `port` directive is
  accepted by mDNSResponder
- A6/A7: the CA entering the System keychain and the real results of
  `security verify-cert`
- A8: whether root's LaunchDaemon can bind 80/443 — **port :80 on that machine is occupied
  by nginx**, so nginx must be stopped first or `http_port` changed
- A9/A10/A13: real behavior of `dscacheutil` / curl / `tail -f`
- A15/A16/A18/A21: plist permissions, the elevation session cache, whether devproxy can
  read back the plist it installed, root-owned files

### E4. Progress after 2026-10-24

- All unit tests are still **green on both ends** (Linux 997 passed / macOS 997 passed).
- During this period the three commands `uninstall`, `sync`, `run` and the domain-suffix
  allowlist guard were added — their real-machine checks are in **group F**.
- That Mac still has not had the `devproxy` itself installed (only the test scaffolding),
  and `devproxy trust` has **not** been run in the graphical interface — all items in
  groups A / E that need graphical authorization remain unverified.

### E5. Second real-machine round (2026-10-26)

Trigger: the unit suite entered CI on macOS and turned up three platform gaps (all fixed
— see `docs/milestones/0.1/TODO.md` #5). With the CI green, group A was run against the
machine again.

Setup: `my-mac` (WenbindeMBP, macOS 26.1 / 25B78, Intel x86_64). `devproxy` was
re-installed from the **public GitHub repo** (it had been installed from the internal
Codeup mirror on 2026-10-23, so it was still the pre-translation build); `uv` comes from
**mise** (`mise activate zsh` in `~/.zshrc` — a non-interactive SSH session does not read
it, so commands run as `zsh -lic "…"`). Elevation was driven through `sudo -S` with the
password piped from a local `.env.local` (never in argv, never echoed).

| Item | Result |
|---|---|
| A1 plist keys | ✅ all eight present: Description / EnvironmentVariables / KeepAlive / Label / ProgramArguments / RunAtLoad / StandardErrorPath / StandardOutPath |
| A3 `launchctl bootstrap` / `bootout` | ✅ repeated bootstrap fails with exit **5** ("Bootstrap failed: 5: Input/output error") and leaves the running job alone; `bootout` stops it; bootstrapping again brings it back (`state = running`); a wrong domain fails with exit **112** ("Could not find domain for user gui: 999999") |
| A4 `launchctl kickstart -k` | ✅ really restarts: pid 29145 → 84675, `state = running` throughout |
| A12 BSD `install -d` / `cp` | ✅ `install -d /etc/resolver` is idempotent (exit 0), `cp` of a probe file works (exit 0) and reads back, `rm -f` removes it (exit 0). The live `/etc/resolver/test` was **not** touched — the directory afterwards holds only the files it had before |
| A2 + A20 `launchctl print` | ✅ exit 0, top-level `state = running`, pid, stdout/stderr paths (there is also an inner `state = active` — the "running" line is the one the code keys on) |
| A5 `/etc/resolver/test` | ✅ `nameserver 127.0.0.1` + **`port 5300` — the `port` directive IS honoured by mDNSResponder** (the biggest open question of that group) |
| A7 `security verify-cert` (untrusted cert) | ✅ same `CSSMERR_TP_NOT_TRUSTED`, exit 1 |
| A8 root binds 80/443 | ✅ caddy (root, pid 29141) LISTENs on **both** 80 and 443. Note: a plain user's `lsof` cannot see root processes — this check needs sudo |
| A9 `dscacheutil -q host` | ✅ output carries `name/ipv6_address` and `name/ip_address` blocks, as the code expects |
| A10 bundled curl | ❌ **does not trust the keychain**: `curl https://demo.test` → exit 60 "unable to get local issuer certificate". An unregistered domain correctly gets no certificate (TLS alert) ✓. To make curl trust it, the CA has to go into `/etc/ssl/cert.pem` (the keychain only covers Safari/Chrome) |
| A11 brew's dnsmasq | ✅ `/usr/local/sbin/dnsmasq` → Cellar 2.91 (the Intel-Mac location) |
| A13 `devproxy logs <service>` | ✅ still following after 3 s — it never ends on its own. Ctrl-C needs a real terminal, so that half stays unverified here |
| A14 + A16 sudo timestamp cache | ✅ **hits within one session** (second `sudo -n` succeeds); ✗ **does not hit across sessions** — the cache is per-tty, so every new SSH session asks again |
| A15 plist permissions | ✅ `-rw-r--r-- root wheel` |
| A17 `devproxy doctor` | ✅ English wording, exit code **3** while the CA is untrusted, hint `devproxy trust` |
| A18 plain user reads the plist | ✅ readable |
| A19 log directory | ✅ the directory itself is **user-owned** (the tool creates it); caddy (root) only writes files into it; `caddy.log` is root-owned but 0644 |
| A21 root files in the home dir | ✅ `root.crt` is **user-owned**; `caddy.log` is root-owned but readable — the user can delete both |
| A22 dnsmasq config | ✅ written by this tool (`port=5300`, `address=/.test/…`); Homebrew's own config is not involved |
| A6 keychain trust | ❌ **still impossible without a graphical session**: `security add-trusted-cert -d -r trustRoot` fails with `SecTrustSettingsSetTrustSettings: The authorization was denied since no user interaction was possible` **even with sudo**. The certificate does land in the keychain, but the **trust flag** does not — so `verify-cert` still reports NOT_TRUSTED and `doctor` still reports the cert missing. Same conclusion as E2b, now pinned to the trust-settings step |

**All 22 items of group A now have a conclusion** (A6 is a "cannot be done
non-interactively", which is itself the conclusion). The three that touch a running
job or write under `/etc/resolver` were run with explicit authorization; the machine
was left exactly as it was found (the dnsmasq job is running again, the probe file is
gone).

## F. Real-machine checks for new features (12 items)

Commands and guards added after 2026-10-23. Unit tests cover the logic branches, but the
**side effects on a real machine** (really deleting files, really starting processes,
really changing the trust store) can only be verified here.

### F1. Zero side effects from `uninstall --dry-run`

- Check: run `devproxy uninstall --dry-run`, and compare before and after: caddy/dnsmasq
  service status, DNS route files under `/etc`, the CA in the system trust store, and the
  existence and mtime of each file under `~/.local/state/devproxy/`.
- Expected: **completely unchanged**. The output should list every step it would take and
  clearly mark "the following will be kept".

### F2. Actual uninstall and kept list of `uninstall`

- Check: on a **test machine**, run `devproxy uninstall --yes`, then verify item by item.
- Expected: caddy/dnsmasq services are stopped and their units removed; the DNS route
  file is deleted; the CA is removed from the system trust store and the NSS DB;
  `sites.caddy`, `config.toml`, `~/.local/share/caddy`, `backups/`, and linger **are all
  still there**; the end prints "what was kept".

### F3. The CA is removed by **fingerprint**, not by nickname

- Check: first install a CA with the **same nickname but different content** into the NSS
  DB, then run `uninstall --remove-ca`.
- Expected: only the tool's certificate is deleted; the other one with the same nickname
  **must still be there** (the nickname is not the identity; the bytes are).

### F4. Extra deletions from `uninstall --purge`

- Check: run `devproxy uninstall --yes --purge`.
- Expected: in addition to the items in F2, `~/.local/share/caddy`, `backups/`, and
  `config.caddy_bin_path` are also deleted; **the migration backups disappear with them**
  — the order must be "roll back first, then uninstall" or "uninstall without purge".

### F5. `uninstall` idempotency

- Check: run `devproxy uninstall --yes` twice in a row.
- Expected: the second run prints "Nothing to uninstall." and **exits 0**.

### F6. `up` end to end: declare → start the service → domain reachable

- Check: put a `devproxy.toml` (`[[routes]]` + `command`) at the project root, run
  `devproxy up`, then `curl -k https://<name>.test/`.
- Expected: the process starts, the route is registered, and **the domain really works**;
  app logs are in `~/.local/state/devproxy/logs/routes/<name>.log`.

### F7. `up`'s conflict report **does not kill processes**

- Check: run `devproxy up` once, change `command` in `devproxy.toml`, and run it again.
- Expected: it reports a conflict (current state vs desired state + the fix command), the
  **old process is still there** (visible in `ps`), and the exit code is 1 (config drift
  must be visible to CI).

### F8. Boundaries of `up --prune`

- Check: manually create a route with `devproxy up manual-route`; then delete one entry
  from `devproxy.toml` and run `devproxy up --prune --yes`.
- Expected: only the entry **declared by this file** is deleted; the manually created one
  **must still be there**.

### F9. `run` foreground supervision and graceful Ctrl-C exit

- Check: stop the live caddy service, run `devproxy run`, open another terminal and
  `curl -k https://<name>.test/`, then press Ctrl-C in the `run` terminal.
- Expected: the domain is reachable during `run`; after Ctrl-C, Caddy **exits gracefully**
  (the log contains `shutdown complete`), devproxy exits 0, and **no processes remain**
  (`pgrep -f caddy` is empty). After the service is stopped the routes are **no longer
  served** (the route table is a file, but nobody loads it).

### F10. `run` works in an environment **without systemd** (container / WSL) — ✔ verified (2026-10-25)

**Reproducible**: `just verify-clean` (that is, `scripts/verify-clean-machine.sh`, which
runs 19 checks in one go and keeps the container for troubleshooting on failure).

**Environment**: Docker container `ubuntu:24.04`, non-root user `dev` + passwordless sudo,
**no systemd** (`/run/systemd/system` does not exist, `systemctl` does not exist).
Installed with `pip install` from clean source produced by `git archive HEAD`; the only
runtime dependencies are typer/rich.

| # | Verification point | Result |
|---|---|---|
| 1 | Install (`pip install <source dir>`) | ✔ the only dependency on a clean machine is python3-venv |
| 2 | `config` / `ls` / `init` | ✔ all normal; an empty `ls` table gracefully shows `(no routes)` |
| 3 | `sudo devproxy setup` | ✔ **exit 3**: "run devproxy as a regular user: privileged steps are performed by the tool via sudo internally; running as root installs services into root's home directory" |
| 4 | `setup --yes` (bare machine, missing libcap2-bin / dnsmasq / systemctl) | ✔ plan table renders correctly; every failure is **graceful and actionable** ("setcap failed (caddy cannot bind 80/443): sudo: setcap: command not found; install libcap2-bin first (sudo apt install libcap2-bin)"), exit code 1, **no traceback** |
| 5 | `setup --yes` after installing the packages | ✔ caddy download + **sha256 verification + setcap**, main Caddyfile, `/etc/dnsmasq.d/devproxy.conf`, and the merged dnsmasq config all land; only the systemd steps fail (the container boundary — exactly what `run` fills in) |
| 6 | Manual dnsmasq (port 53) + `/etc/resolv.conf` pointing at it | ✔ `demo.test → 127.0.0.1` |
| 7 | `devproxy run` foreground supervision of Caddy | ✔ starts, binds 80/443 (setcap effective), Admin API responds |
| 8 | `devproxy up` | ✔ starts the app + registers the route + **generates the CA** + proactively hints to run `devproxy trust` |
| 9 | `devproxy trust --yes` | ✔ system trust store installed; the NSS step **fails gracefully** (no certutil, hint `certutil is missing (Chrome trust requires the NSS DB): sudo apt install libnss3-tools`) |
| 10 | `curl https://demo.test` | ✔ **HTTP 200, no `-k`** — DNS goes through the tool's dnsmasq, and the certificate is signed by the local CA |
| 11 | `devproxy down demo` | ✔ after unregistering, the handshake fails (the route really is gone) |

**Real findings during the process (the 2026-10-25 reproduction script added two more, four
in total)**:

- **Caddy installs trust itself too**: after generating the root CA at startup, caddy
  **borrows sudo** to install it into the system trust store (works because the `dev` user
  has passwordless sudo). By the time `devproxy trust` runs, the system step sees "already
  trusted" and skips it — so **do not** use "whether `devproxy-local.crt` exists" to decide
  whether trust is in place; use a property assertion such as
  `openssl verify -CApath /etc/ssl/certs <ca>`.
- **443 is not fully rebound when `up` returns** (the command was called `sync` back then): measured, connecting to 443 immediately
  after up returns is refused; it works about 2 seconds later (the listener rebind during
  reload is an asynchronous tail). The script tolerates it with retries; on the product
  side this is a known race — users of `up && curl` may hit it, and the fix requires
  making mutate wait for the port to be truly ready after reload; recorded, not changed for
  now.

- The verifier hit the "`$PORT` not wrapped in `sh -c`" trap too — the error is clear
  (python's `invalid int value: '$PORT'`), and the fix matches the documentation. This is
  exactly what the header comment generated by `init` warns about.
- `trust --yes` exits 1 overall when **only the NSS step fails**: the system trust store
  (what curl/browsers need) was actually installed. For a server without Chrome this is
  noise, but it counts as the "report honestly if any step fails" design — not changed for
  now, left as known behavior.

- Expected: `run` can start and supervise routes; that is precisely why it exists.

### F11. Domain-suffix allowlist and child-process timeout guard

- Check: ① write `domain_suffix = "com"` in `config.toml` → run any command;
  ② change it to `domain_suffix = "dev"` + `allowed_domain_suffixes = ["dev"]` → run
  again.
- Expected: ① exit code **1** (config error) and an explanation about "machine-wide
  resolution would be hijacked / cookie bleed / HSTS"; ② normal.

> **Child-process timeout** (the product of the 2026-10-23 real-machine incident):
> previously `certutil -N --empty-password` looped forever on the password prompt, writing
> **200GB×2** into the redirect file in two hours and filling `/home` to 100%. The fix is
> to add a timeout to ordinary subprocess calls (`_run` in `platform/linux.py`; timeout
> returns 124). **Already measured on this machine** (that incident itself), so there is no
> need to verify it again; it is recorded here so it is not treated as "never happened".

### F12. `logs --edit` opens logs in an editor

- Check: ① `EDITOR=vim devproxy logs <route name> --edit` (or `-e`), change something, save
  and exit; ② `devproxy logs caddy --edit`; ③
  `env -u EDITOR -u VISUAL devproxy logs <route name> --edit`; ④ `devproxy logs --edit`
  (without a name); ⑤ `devproxy logs --list --edit`.
- Expected: ① the editor **takes over the terminal** and opens that route's log file
  (`~/.local/state/devproxy/logs/routes/<name>.log`), and the file content really changes
  after saving; ② service logs **error with exit 2** and hint to use follow mode instead
  (journalctl logs are not files); ③ with no `EDITOR` set, it looks for `vim` / `vi` /
  `nano` in order and opens the first one on PATH; only when none is on PATH does it exit
  2 and hint `export EDITOR=vim`; ④ exits 2 and explains that a name is needed; ⑤ exits 2
  (`--list` and `--edit` are mutually exclusive).

---

## Appendix: automated coverage outside this checklist

The following are already covered by automated tests and **do not need** to be verified
again on a real machine:

- Route table CRUD, deterministic rendering, rejection of invalid domains / upstreams →
  `tests/test_render.py`, `tests/test_names.py`
- Transactionality of route writes (lock → render → validate → write to disk → reload,
  rollback on failure) → `tests/test_store.py`
- Port allocation, child-process lifecycle, process-group cleanup, ready timeout →
  `tests/test_runner.py`
- Migration's Caddyfile splitting, storage parsing, backup manifests, and rollback →
  `tests/test_migrate.py`, `tests/test_backup.py`
- check/plan/apply/verify of the four components and the macOS branches →
  `tests/test_component_*.py`, `tests/test_platform_macos.py`
- Every CLI subcommand and its exit codes → `tests/test_cli.py`
- End-to-end self-check → `tests/test_selftest.py`, `tests/test_integration.py`,
  `devproxy selftest`
- Domain-suffix allowlist and the opt-in mechanism → `tests/test_config.py`
- Certificate trust probing and the Firefox note → `tests/test_component_certs.py`,
  `tests/test_doctor.py`
- Uninstall planning, kept list, idempotency, CA removal by fingerprint →
  `tests/test_uninstall.py`
- Project-level config parsing, discovery, conflict decisions, prune boundaries →
  `tests/test_project.py`, `tests/test_sync.py`
- Signal handling and preconditions of foreground supervision → `tests/test_run.py`
- Platform layer (systemd / launchd / fake platform) and child-process timeouts →
  `tests/test_platform_linux.py`, `tests/test_platform_macos.py`
