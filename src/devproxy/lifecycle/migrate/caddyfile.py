"""Caddyfile 文本处理：站点块切分、storage 提取、通配块识别。

纯文本操作，不碰平台、不碰文件系统（除了读 legacy_storage_path 的入参）。
"""
from __future__ import annotations

from pathlib import Path

TEST_BLOCK_MARKERS = ("*.test", "http://*.test", "http://*.local.test")

def _block_head(block: str) -> str:
    """块的首个有效行（跳过注释与空行）。"""
    for line in block.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
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
    拆开丢掉等于丢掉用户的文档。注释里的花括号不参与深度计算。
    """
    global_blocks: list[str] = []
    site_blocks: list[str] = []
    current: list[str] = []
    depth = 0
    for line in text.splitlines():
        current.append(line)
        stripped = line.strip()
        if not stripped.startswith("#"):
            depth += line.count("{") - line.count("}")
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

def split_caddyfile_blocks(
    text: str, suffix: str = "test"
) -> tuple[list[str], list[str]]:
    """拆出个人站点块。返回 (个人站点块列表, 被丢弃的 .test 相关块列表)。

    只做块级切分，不解析 Caddyfile 语法 —— 站点块原样保留。
    """
    _, sites = _split_blocks(text)
    markers = TEST_BLOCK_MARKERS
    if suffix and suffix != "test":
        markers = (
            f"*.{suffix}",
            f"http://*.{suffix}",
            f"http://*.local.{suffix}",
        )
    personal: list[str] = []
    dropped: list[str] = []
    for block in sites:
        address = _block_address(block)
        if address and any(address.startswith(m) for m in markers):
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
    """
    global_blocks, _ = _split_blocks(text)
    for block in global_blocks:
        for line in block.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or not s.startswith("storage"):
                continue
            parts = s.split(None, 2)
            if len(parts) >= 3 and parts[1] == "file_system":
                return Path(parts[2].strip().strip('"')).expanduser()
    return None

def _has_content(text: str) -> bool:
    """文本里有没有实质内容（注释与空行不算）。"""
    return any(
        line.strip() and not line.strip().startswith("#") for line in text.splitlines()
    )
