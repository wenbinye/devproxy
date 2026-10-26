# DNS and certificates

## DNS

| Platform | Mechanism | dnsmasq listens on |
|---|---|---|
| Linux (default `resolved`) | Writes `/etc/systemd/resolved.conf.d/devproxy.conf`, where `Domains=~.<suffix>` routes that suffix to dnsmasq | `127.0.0.1:5353` |
| Linux (`system`) | Writes `/etc/dnsmasq.d/devproxy.conf`, handled by the system dnsmasq | `127.0.0.1:5353` |
| macOS | Writes `/etc/resolver/<suffix>` (a per-domain resolver file); does not touch `/etc/resolv.conf` | `127.0.0.1:5300` |

The Linux resolved drop-in looks like:

```ini
[Resolve]
DNS=127.0.0.1:5353
FallbackDNS=1.1.1.1 8.8.8.8
Domains=~.test
```

**Note**: the `DNS=` in the drop-in points this machine's **global** DNS at the user-scope
dnsmasq -- a trade-off made to avoid occupying port 53. In practice this means:

- `DNS=127.0.0.1:5353` in resolved's **global section** decides that **every domain on
  this machine** asks the user-scope dnsmasq first, **not just `*.test`**;
- `FallbackDNS=1.1.1.1 8.8.8.8` in the same drop-in is the **machine-wide fallback**: if
  dnsmasq cannot start or cannot answer, all resolution degrades to these two public
  resolvers (rather than the whole machine going offline);
- `Domains=~.<suffix>` decides which suffix dnsmasq resolves to this machine internally.

For why the suffix in `Domains=~.<suffix>` **can only be a reserved top-level domain**,
see
[Only reserved top-level domains are allowed as the domain suffix](configuration.md#only-reserved-top-level-domains-are-allowed-as-the-domain-suffix) --
a real public top-level domain would hijack resolution of that suffix **across the whole
machine**.

As a companion guarantee, the tool ensures dnsmasq's configuration **always contains
`no-resolv`** (unless you wrote `resolv-file=` yourself): dnsmasq therefore **does not**
treat `/etc/resolv.conf` (on a resolved machine, the `127.0.0.53` stub) as an upstream,
otherwise resolved and dnsmasq would point at each other and ordinary domain resolution
would fail machine-wide.

dnsmasq's configuration is **merged incrementally**: domain entries, `filter-AAAA`, and the
like already in your `~/.config/dnsmasq/dnsmasq.conf` are preserved; the tool only ensures
that `port=`, `address=/.<suffix>/`, `listen-address=127.0.0.1`, `bind-interfaces`,
`no-resolv`, and the missing global upstreams exist. It **only listens on the loopback
address**, so it does not become an open recursive resolver on the LAN.
## What `doctor` and `setup` each check

- **`devproxy doctor` only does static checks**: whether the DNS route file exists,
  whether the service is running, whether the CA exists, and whether the unit points at
  this tool's Caddyfile and binary. **It does not do DNS resolution** -- it issues no
  `resolvectl` / `dig` query.
- **"Whether resolution really works" is `setup`'s self-check phase**: after applying
  changes, `setup` verifies by actually resolving `foo.<suffix>` once and confirming the
  result points at this machine. Note that this only happens when **setup actually changed
  something**; when `setup` takes the "all components ready" no-op branch, this self-check
  **does not** happen.

To confirm immediately whether resolution works, query it yourself:

```bash
resolvectl query foo.test          # Linux
dscacheutil -q host -a name foo.test   # macOS
```
## Certificates: exact site blocks, not wildcards

Each route renders as **one exact site block**:

```caddyfile
codeup.test {
	tls internal
	reverse_proxy 127.0.0.1:34567
}
```

**There is no `*.test` wildcard block and no on-demand TLS.** This yields a clear security
property:

> **An unregistered domain gets no certificate at all** -- the TLS handshake fails outright.

In other words, there is no "any process on this machine can trick the system into trusting
a certificate for any `.test` name" situation.

Trusting the CA requires installing it in different places:

| Platform | Installed into | Who reads it |
|---|---|---|
| Linux | System trust store (`/usr/local/share/ca-certificates/` + `update-ca-certificates`) | curl / Go / Java, etc. |
| Linux | NSS DB `~/.pki/nssdb` | **Chrome / Chromium only read the NSS DB** |
| macOS | System keychain `/Library/Keychains/System.keychain` | Safari / Chrome / the bundled `curl` all read it (verified on macOS 26, see E6 of the verification checklist) |
| Any | **Firefox reads none of the above** | Firefox only reads `cert9.db` in its own profile |

The two steps are independent: install only the system trust store and Chrome still reports
`net::ERR_CERT_AUTHORITY_INVALID`; install only the NSS DB and curl still does not accept
it. Installing the NSS DB on Linux requires `certutil`
(`sudo apt install libnss3-tools`).

**On Linux, after installing into the NSS DB you must fully quit and restart Chrome** --
closing the window is not enough; quit the process from the tray. Chrome only reads the NSS
DB at startup.

### Firefox needs separate trust (the tool does not install it for you)

**Firefox has its own certificate store** (`~/.mozilla/firefox/<profile>/cert9.db`) -- it
reads neither the NSS DB nor the system trust store. So after `devproxy trust` finishes,
**Chrome works but Firefox still reports a certificate error** -- this is not the tool
being broken; it is Firefox's design.

Two solutions (**both require fully quitting and restarting Firefox** to take effect):

1. **Import manually**: Firefox → Settings → Privacy & Security → Certificates → View
   Certificates → Import → choose `<storage>/pki/authorities/local/root.crt`
   (default `~/.local/share/caddy/pki/authorities/local/root.crt`);
2. **Make Firefox honor the system trust store too**: in `about:config`, enable
   `security.enterprise_roots.enabled` (set the boolean to `true`).

When `devproxy doctor` detects Firefox installed on this machine, it prints this hint;
after `devproxy trust` finishes it also reminds you. **This is a note, not a defect** --
Firefox not having the CA does not affect doctor's health-check verdict (the Chrome / curl
side may be perfectly fine).

The CA root certificate path is `<storage>/pki/authorities/local/root.crt`
(default `~/.local/share/caddy/pki/authorities/local/root.crt`).

---
