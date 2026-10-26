"""Caddyfile text processing: site block splitting, storage extraction, wildcard detection.

Pure text operations; touches neither the platform nor the filesystem (except
reading the input to legacy_storage_path).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path


def _strip_inline_comment(line: str) -> str:
    """Strip an inline comment, quote-aware (inside quotes, # is an ordinary character in a Caddyfile)."""
    quote: str | None = None
    escaped = False
    for i, ch in enumerate(line):
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _brace_delta(line: str) -> int:
    """Effect of one line on block depth (braces inside comments and quotes do not count)."""
    code = _strip_inline_comment(line)
    delta = 0
    quote: str | None = None
    escaped = False
    for ch in code:
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'`":
            quote = ch
        elif ch == "{":
            delta += 1
        elif ch == "}":
            delta -= 1
    return delta


def _block_head(block: str) -> str:
    """First meaningful line of a block (skipping comments and blank lines, stripping inline comments)."""
    for line in block.splitlines():
        s = _strip_inline_comment(line).strip()
        if not s:
            continue
        return s
    return ""


def _block_address(block: str) -> str:
    """Site address declared by the block; a global block (starting with "{") returns an empty string."""
    head = _block_head(block)
    if head.startswith("{"):
        return ""
    return head.split("{")[0].strip()


def _split_blocks(text: str) -> tuple[list[str], list[str]]:
    """Split top-level blocks by brace depth. Returns (global blocks, site blocks).

    Comments preceding a site block belong to that block -- each site in a user's
    Caddyfile usually carries an explanation, and splitting it off would discard
    the user's documentation. Comments (including inline comments) and braces
    inside quotes do not participate in depth counting; otherwise
    `example.test { # site } note` would be torn apart on the spot.
    """
    global_blocks: list[str] = []
    site_blocks: list[str] = []
    current: list[str] = []
    depth = 0
    for line in text.splitlines():
        current.append(line)
        depth += _brace_delta(line)
        if depth <= 0 and current:
            block = "\n".join(current).rstrip() + "\n"
            current = []
            depth = 0
            if _block_address(block) == "" and _block_head(block).startswith("{"):
                global_blocks.append(block)
            else:
                site_blocks.append(block)
    if current and any(line.strip() for line in current):
        # Trailing unclosed residual content: keep it as-is; do not swallow the user's config
        site_blocks.append("\n".join(current).rstrip() + "\n")
    return global_blocks, site_blocks


def _address_tokens(address_line: str) -> list[str]:
    """Split an address line like `foo.test, *.test` into individual address tokens."""
    return [t for t in re.split(r"[,\s]+", address_line.strip()) if t]


def _host_of(token: str) -> str:
    """Host name in an address token: strip scheme, path, and port; lowercase."""
    host = token.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.startswith("["):  # IPv6 literal
        end = host.find("]")
        if end != -1:
            return host[: end + 1].lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()


def _is_wildcard_host(host: str, suffix: str) -> bool:
    """Whether host is a wildcard site under this tool's suffix.

    Must do **exact suffix matching with a dot boundary**: `*.testing` does not
    belong to `test`, while `*.test` and `*.local.test` do. A whole-string
    startswith would falsely match the former and miss forms with a scheme such
    as `https://*.test`.
    """
    suffix = (suffix or "test").lower()
    if not host.startswith("*."):
        return False
    return host == f"*.{suffix}" or host.endswith(f".{suffix}")


def _block_is_wildcard(block: str, suffix: str) -> bool:
    address = _block_address(block)
    if not address:
        return False
    return any(_is_wildcard_host(_host_of(token), suffix) for token in _address_tokens(address))


def split_caddyfile_blocks(text: str, suffix: str = "test") -> tuple[list[str], list[str]]:
    """Extract personal site blocks. Returns (personal site blocks, discarded .test-related blocks).

    Only performs block-level splitting and does not parse Caddyfile syntax --
    site blocks are kept as-is.
    """
    _, sites = _split_blocks(text)
    personal: list[str] = []
    dropped: list[str] = []
    for block in sites:
        if _block_is_wildcard(block, suffix):
            dropped.append(block)
        else:
            personal.append(block)
    return personal, dropped


def split_caddyfile(text: str, suffix: str = "test") -> tuple[str, list[str]]:
    """Extract personal sites. Returns (personal site text, discarded .test-related blocks)."""
    personal, dropped = split_caddyfile_blocks(text, suffix)
    return "\n".join(personal), dropped


def block_addresses(blocks: list[str]) -> list[str]:
    """Addresses declared by a group of site blocks (global blocks and blocks whose address cannot be parsed are skipped)."""
    addresses = [_block_address(b) for b in blocks]
    return [a for a in addresses if a]


def wildcard_addresses(text: str, suffix: str = "test") -> list[str]:
    """Addresses of wildcard site blocks still present in the text.

    When a wildcard block (e.g. *.test) shares a suffix with the exact site
    blocks the tool generates, Caddy's TLS handshake fails entirely -- so doctor
    must be able to point out that "a wildcard block remains in the active
    config".
    """
    _, dropped = split_caddyfile_blocks(text, suffix)
    return block_addresses(dropped)


def legacy_storage_path(text: str) -> Path | None:
    """storage path in the global block of a legacy Caddyfile.

    Example: `storage file_system /home/u/.local/share/caddy`. Migration must
    reuse it -- if storage moves, Caddy regenerates the CA, and browser trust
    plus the existing sites' tls internal certificates are all invalidated.

    Inline comments (`storage file_system /data # old ca`) and quoted paths must
    be parsed lexically: the former used to be taken as part of the path, and
    the latter used to have its quotes stripped.
    """
    global_blocks, _ = _split_blocks(text)
    for block in global_blocks:
        for line in block.splitlines():
            s = _strip_inline_comment(line).strip()
            if not s:
                continue
            try:
                parts = shlex.split(s)
            except ValueError:
                continue
            if len(parts) >= 3 and parts[0] == "storage" and parts[1] == "file_system":
                if parts[2]:
                    return Path(parts[2]).expanduser()
    return None


def _has_content(text: str) -> bool:
    """Whether the text has substantive content (comments and blank lines do not count)."""
    return any(_strip_inline_comment(line).strip() for line in text.splitlines())
