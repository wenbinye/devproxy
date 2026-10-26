# Configuration (config.toml)

## Configuration

`~/.config/devproxy/config.toml` (**everything is optional**; omit a key and the default is
used; **the tool never generates a default config file**):

```toml
# Domain suffix. devproxy up codeup → https://codeup.test
# Only top-level domains reserved for local use by RFC 2606 / 6761 are allowed:
# test / example / localhost / invalid
domain_suffix = "test"

# DNS backend: auto | resolved | system
dns_backend = "auto"

# Caddy Admin API address
admin_addr = "127.0.0.1:2019"

# Caddy's HTTP / HTTPS ports
http_port = 80
https_port = 443

# Caddy storage (CA + certificates). Migration reuses the legacy environment's
# location so the CA stays continuous
storage = "~/.local/share/caddy"

# Ready wait timeout for up (seconds)
ready_timeout = 60

# caddy binary path (setup downloads it here)
caddy_bin = "~/.local/bin/caddy"

# Extra allowed domain suffixes (empty by default -- no public top-level domain
# is allowed). See "Only reserved top-level domains are allowed as the domain
# suffix" below.
# allowed_domain_suffixes = ["dev"]
```

Exact semantics of each key:

| Key | Default | Description |
|---|---|---|
| `domain_suffix` | `test` | A single DNS label (starts and ends with a lowercase letter/digit, may contain hyphens in the middle, contains no dots, ≤ 63 characters), **and must be a top-level domain reserved for local use** (`test` / `example` / `localhost` / `invalid`). An invalid value is rejected when the configuration is loaded |
| `allowed_domain_suffixes` | `[]` | Extra allowed domain suffixes (array of strings). By default **no public top-level domain is allowed**; listing one here means the user explicitly takes responsibility |
| `dns_backend` | `auto` | `auto` = use resolved if it is really running, otherwise use system. On Linux you can explicitly specify `resolved` / `system`; **macOS has no systemd-resolved, so explicitly writing `resolved` is rejected as a usage error (exit code 2)**, and `auto` on macOS silently uses per-domain resolver files |
| `admin_addr` | `127.0.0.1:2019` | `host:port` form. It is both Caddy's admin listen address and the target address for reload |
| `http_port` / `https_port` | `80` / `443` | Caddy's **global options**, written into the global block of the main Caddyfile; changing them really changes the listening ports |
| `storage` | `~/.local/share/caddy` | Caddy's storage directory; the CA and issued certificates live here. Changing it = Caddy regenerates the CA = all browser trust is invalidated |
| `ready_timeout` | `60` | Seconds `up` waits for the app to become ready (1..3600) |
| `caddy_bin` | `~/.local/bin/caddy` | Path to the caddy binary. setup downloads a pinned version here; changing it requires re-running setup so the service unit points at the new path |

**Precedence**: CLI arguments > `config.toml` > built-in defaults. Unknown keys raise an
error (they are never silently ignored).

Show the configuration currently in effect:

```bash
devproxy config
```

**Changing `domain_suffix` does not rewrite existing routes** -- the domain of a registered
route is fixed into `routes.json` at registration time; otherwise a running service would
suddenly lose its domain. Old routes need `devproxy down <name>` followed by a new `up`.
### Only reserved top-level domains are allowed as the domain suffix

`domain_suffix` can only be a **top-level domain reserved for local use by RFC 2606 /
RFC 6761**:

| Top-level domain | Source |
|---|---|
| `test` | RFC 6761 (**recommended**) |
| `example` | RFC 2606 |
| `localhost` | RFC 6761 |
| `invalid` | RFC 2606 |

`com` / `net` / `org` / `cn` / `dev` / `local` are **all rejected**, for three reasons:

- **Machine-wide resolution would be hijacked**: the suffix is written into **machine-wide**
  DNS routing
  (Linux: `Domains=~.<suffix>` in `/etc/systemd/resolved.conf.d/devproxy.conf`;
  macOS: `/etc/resolver/<suffix>`). With `Domains=~.com`,
  resolution of **every `.com` domain on this machine** would ask the local dnsmasq first --
  resolution of a real public top-level domain is hijacked;
- **Browser cookies would bleed across sites**: using a real domain for a local site
  carries the login state over to the real site;
- **HSTS would reject it outright**: `https://codeup.com` is forced to HTTPS by HSTS with
  no user exception, so local self-signed certificates always fail.

Using a **whitelist** rather than a blacklist is deliberate: public top-level domains added
in the future cannot slip through (a blacklist always would), and the four top-level
domains on the whitelist are exactly the ones meant for local development.

#### Really need another suffix: allow it explicitly

The tool allows it, but **no public top-level domain is allowed by default** -- allowing
one is a choice **you explicitly own**, written in `config.toml`:

```toml
domain_suffix = "dev"
allowed_domain_suffixes = ["dev"]
```

- `allowed_domain_suffixes` is an **exact list**, not a switch: allowing `dev` does not
  mean allowing `com`;
- Every entry must still be a single DNS label (starts and ends with a lowercase
  letter/digit, no dots);
- The list only affects `domain_suffix` -- the tool **does not** write it into any system
  configuration, it merely lets you use that suffix for local sites.

**Exit code difference** (same invalid value, different source):

| Written in | Exit code | Meaning |
|---|---|---|
| in `config.toml` | **1** | Configuration error |
| passed via `--domain-suffix` | **2** | Usage error |

#### Upgrade note: existing users may suddenly get an error

Before this guard existed, `~/.config/devproxy/config.toml` on this machine may already
contain a `domain_suffix` that is not on the whitelist (for example `dev`). After
upgrading, **every command reports an error** (configuration error, exit code 1). There are
two ways to handle it:

1. **Switch to a reserved top-level domain** (recommended): change `domain_suffix` to
   `test` (or `example` / `localhost` / `invalid`), then handle existing routes as
   described in the next section;
2. **Allow it explicitly**: add
   `allowed_domain_suffixes = ["<your original suffix>"]` to `config.toml`; the behavior is
   the same as before the upgrade.

```bash
devproxy down <name>    # the old route's domain was fixed at registration time -- changing the suffix requires re-registering
devproxy up <name> -- <original start command>
```

(Changing `domain_suffix` does **not** automatically rewrite existing routes; see "Exact
semantics of each key" above.)
### The storage archive: why changing `storage` in `config.toml` has no effect

When migrating a legacy environment, devproxy **archives the storage path declared in the
legacy Caddyfile** (the directory containing the CA) to
`~/.local/state/devproxy/caddy-storage.json`, and keeps using it on **every later command**
(the storage archive is read every time the runtime context is built -- `setup`, `up`, `ls`,
and `config` alike). If the storage location moves, Caddy regenerates the CA, and browser
trust plus the `tls internal` certificates already held by personal sites all become
invalid.

**The archive takes precedence over `config.toml`.** When the two disagree, devproxy says
so explicitly in the terminal (the effective path, the ignored configuration value, and the
archive file's location); it does not silently ignore the difference.

To go back to what is in `config.toml`: **delete
`~/.local/state/devproxy/caddy-storage.json`** (that file is the way out), then re-run
`devproxy setup` -- note that this makes Caddy regenerate the CA, and browsers need to
trust the new root certificate again.

---
