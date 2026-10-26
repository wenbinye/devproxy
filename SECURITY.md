# Security policy

中文版：[SECURITY.zh-CN.md](SECURITY.zh-CN.md)

## Supported versions

| Version | Status |
|---|---|
| 0.1.x | ✅ Supported |

## Reporting a vulnerability

**Do not open a public issue.** Use the private vulnerability reporting offered by the
hosting platform (GitHub: Security Advisories; Codeup: security advisories), or contact
the maintainer directly (see the repository home page).

Please include:

- affected version (`devproxy --version`) and operating system;
- reproduction steps and the **full command**;
- expected vs. actual behaviour;
- how it could be exploited (who can trigger it, what it requires).

We aim to respond within **7 days**, and to coordinate the fix and disclosure once the
report is confirmed.

## The tool's security boundaries (for users)

devproxy is designed around one premise: it only affects the **local development
environment on this machine**. It really does change the system, so those boundaries
must be spelled out.

### What it changes

- Installs a **self-signed CA root certificate** into the **system trust store** and the
  NSS DB / system keychain;
- Writes the DNS resolution path (a systemd-resolved drop-in on Linux, `/etc/resolver/`
  on macOS) — on the Linux `resolved` backend the drop-in lands in the **global**
  section, which affects the resolution order of **every domain on the machine**
  (domains not registered with this tool also ask the local dnsmasq first);
- Installs and enables **systemd user units / launchd jobs** (on macOS, Caddy is a root
  LaunchDaemon and binds 80/443);
- Downloads and runs a third-party binary (Caddy) — with a built-in sha256 check that
  aborts on mismatch.

### What it does not do

- It sends **nothing** to the internet: the route table, configuration, and logs stay on
  the machine;
- It collects no telemetry;
- It does not read or store your application code or secrets (it only captures the app's
  standard output into a log file).

### Things worth knowing

- The **CA private key** lives in `~/.local/share/caddy/pki/authorities/local/`. Anyone
  who can read that directory can issue certificates this machine trusts — its trust
  scope is equivalent to your user account.
- Once the CA is installed, **browsers and command-line tools on this machine trust
  certificates issued by the local Caddy**. `devproxy uninstall --remove-ca` removes it.
- `devproxy setup` changes the DNS resolution path **machine-wide** (see above). Be
  careful on shared machines.
- Route logs (`~/.local/state/devproxy/logs/routes/*.log`) may contain sensitive
  information your app prints. They live under your own home directory, but they are not
  a safe.
