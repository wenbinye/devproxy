"""Caddyfile 文本处理：站点块切分、storage 提取、通配块识别。

纯文本操作，不碰平台、不碰文件系统（除了读 legacy_storage_path 的入参）。
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

def _strip_inline_comment(line: str) -> str:
    """去掉引号感知的行内注释（Caddyfile 里引号内的 # 是普通字符）。"""
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
    """一行代码对块深度的影响（注释与引号里的花括号不算）。"""
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
    """块的首个有效行（跳过注释与空行，去掉行内注释）。"""
    for line in block.splitlines():
        s = _strip_inline_comment(line).strip()
        if not s:
            continue
        return s
    return ""

def _block_address(block: str) -> str:
    """块声明的站点地址；全局块（以 "{" 开头）返回空串。"""
    head = _block_head(block)
    if head.startswith("{"):
        return ""
    return head.split("{")[0].strip()

def _split_blocks(text: str) -> tuple[list[str], list[str]]:
    """按大括号深度切分顶层块。返回 (全局块列表, 站点块列表)。

    站点块前面的注释归入该块 —— 用户 Caddyfile 里每个站点都带一段说明，
    拆开丢掉等于丢掉用户的文档。注释（含行内注释）与引号里的花括号
    不参与深度计算，否则 `example.test { # 站点 } 说明` 会被当场切碎。
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
        # 结尾没有闭合的残留内容：原样保留，不吞掉用户的配置
        site_blocks.append("\n".join(current).rstrip() + "\n")
    return global_blocks, site_blocks

def _address_tokens(address_line: str) -> list[str]:
    """把 `foo.test, *.test` 这样的地址行拆成单个地址 token。"""
    return [t for t in re.split(r"[,\s]+", address_line.strip()) if t]

def _host_of(token: str) -> str:
    """地址 token 里的主机名：去 scheme、去路径、去端口、统一小写。"""
    host = token.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.startswith("["):  # IPv6 字面量
        end = host.find("]")
        if end != -1:
            return host[: end + 1].lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()

def _is_wildcard_host(host: str, suffix: str) -> bool:
    """host 是否为本工具后缀下的通配站点。

    必须做**带点边界的精确后缀匹配**：`*.testing` 不属于 `test`，
    `*.test` 与 `*.local.test` 才属于。整串 startswith 会把前者误杀，
    又会漏掉 `https://*.test` 这类带 scheme 的写法。
    """
    suffix = (suffix or "test").lower()
    if not host.startswith("*."):
        return False
    return host == f"*.{suffix}" or host.endswith(f".{suffix}")

def _block_is_wildcard(block: str, suffix: str) -> bool:
    address = _block_address(block)
    if not address:
        return False
    return any(
        _is_wildcard_host(_host_of(token), suffix)
        for token in _address_tokens(address)
    )

def split_caddyfile_blocks(
    text: str, suffix: str = "test"
) -> tuple[list[str], list[str]]:
    """拆出个人站点块。返回 (个人站点块列表, 被丢弃的 .test 相关块列表)。

    只做块级切分，不解析 Caddyfile 语法 —— 站点块原样保留。
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
    """拆出个人站点。返回 (个人站点文本, 被丢弃的 .test 相关块)。"""
    personal, dropped = split_caddyfile_blocks(text, suffix)
    return "\n".join(personal), dropped

def block_addresses(blocks: list[str]) -> list[str]:
    """一组站点块声明的地址（全局块与解析不出地址的块略过）。"""
    addresses = [_block_address(b) for b in blocks]
    return [a for a in addresses if a]

def wildcard_addresses(text: str, suffix: str = "test") -> list[str]:
    """文本里仍存在的通配站点块地址。

    通配块（如 *.test）与工具生成的精确站点块同后缀时，Caddy 的 TLS
    握手会整体失败 —— 所以 doctor 必须能指出"现役配置里还有通配块"。
    """
    _, dropped = split_caddyfile_blocks(text, suffix)
    return block_addresses(dropped)

def legacy_storage_path(text: str) -> Path | None:
    """遗留 Caddyfile 全局块里的 storage 路径。

    例：`storage file_system /home/u/.local/share/caddy`。
    迁移时必须沿用它 —— storage 换了位置，Caddy 会重新生成 CA，
    浏览器里的信任与已有站点的 tls internal 证书全部失效。

    行内注释（`storage file_system /data # old ca`）与引号路径都要按
    词法解析：前者曾被当成路径的一部分，后者曾被剥掉引号。
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
    """文本里有没有实质内容（注释与空行不算）。"""
    return any(
        _strip_inline_comment(line).strip() for line in text.splitlines()
    )
