#!/usr/bin/env python3
"""Render captured terminal sessions into a GIF.

Pipeline: captured ANSI text -> one SVG per frame -> PNG (rsvg-convert) -> GIF (ffmpeg).

Only the standard library is used: the SGR subset that Rich emits (reset, bold, dim,
underline, the 8/16 colours, 256-colour and truecolour) is parsed here.

Usage:
    python3 scripts/make-demo-gif.py <frames-dir> <output.gif> [duration-ms-per-frame]

Each frame is one file in <frames-dir>, named step-NN.txt, containing the raw
output of one command (ANSI escapes included). The frame order is the file name
order; the duration is per frame.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

# -- terminal look ---------------------------------------------------------

BG = "#0d1117"
FG = "#e6edf3"
CHROME = "#161b22"
TITLE_FG = "#8b949e"

FONT = "DejaVu Sans Mono"
FONT_SIZE = 17
LINE_HEIGHT = 23
PAD_X = 28
PAD_TOP = 52
PAD_BOTTOM = 22
WIDTH = 1280

# The 16 ANSI colours, tuned to a dark background.
PALETTE = {
    30: "#484f58", 31: "#ff7b72", 32: "#3fb950", 33: "#d29922",
    34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#b1bac4",
    90: "#6e7681", 91: "#ffa198", 92: "#56d364", 93: "#e3b341",
    94: "#79c0ff", 95: "#d2a8ff", 96: "#56d4dd", 97: "#f0f6fc",
}

# Glyphs the terminal font does not have would render as tofu boxes.
GLYPHS = {
    "\u2705": "\u2713",  # white heavy check mark
    "\u274c": "\u2717",  # cross mark
    "\u26d4": "\u2717",  # no entry
    "\u26a0\ufe0f": "!",  # warning sign
    "\u26a0": "!",
    "\U0001f512": "",      # lock
}

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def xterm_rgb(n: int) -> str:
    """The xterm-256 palette entry for index n."""
    if n < 16:
        base = [
            "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc",
            "#11a8cd", "#e5e5e5", "#666666", "#f14c4c", "#23d18b", "#f5f543",
            "#3b8eea", "#d670d6", "#29b8db", "#e5e5e5",
        ]
        return base[n]
    if n < 232:
        n -= 16
        levels = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (
            levels[n // 36],
            levels[(n // 6) % 6],
            levels[n % 6],
        )
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def parse_ansi(text: str) -> list[list[tuple[str, str, bool]]]:
    """Turn ANSI text into lines of (text, colour, bold) runs."""
    lines: list[list[tuple[str, str, bool]]] = [[]]
    colour = FG
    bold = False
    pos = 0

    def emit(chunk: str) -> None:
        chunk = OTHER_ESC_RE.sub("", chunk)
        for glyph, replacement in GLYPHS.items():
            chunk = chunk.replace(glyph, replacement)
        for index, part in enumerate(chunk.split("\n")):
            if index:
                lines.append([])
            if part:
                lines[-1].append((part, colour, bold))

    for m in ANSI_RE.finditer(text):
        emit(text[pos:m.start()])
        pos = m.end()
        codes = [int(c) for c in m.group(1).split(";") if c != ""] or [0]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                colour, bold = FG, False
            elif code == 1:
                bold = True
            elif code == 2:
                colour = "#8b949e"
            elif code == 22:
                bold = False
            elif code == 39:
                colour = FG
            elif code in PALETTE:
                colour = PALETTE[code]
            elif code == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
                colour = xterm_rgb(codes[i + 2])
                i += 2
            elif code == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
                colour = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                i += 4
            i += 1
    emit(text[pos:])
    return [line for line in lines if line]


def frame_svg(
    lines: list[list[tuple[str, str, bool]]], title: str, canvas_height: int | None = None
) -> str:
    """Render one frame as an SVG (terminal window with a title bar).

    Every frame uses the same canvas height: ffmpeg scales all inputs to one size,
    and different heights would be squashed.
    """
    height = canvas_height or (PAD_TOP + len(lines) * LINE_HEIGHT + PAD_BOTTOM)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}">',
        f'<rect width="{WIDTH}" height="{height}" fill="{BG}"/>',
        # title bar
        f'<rect width="{WIDTH}" height="38" fill="{CHROME}"/>',
        f'<circle cx="22" cy="19" r="6" fill="#ff5f57"/>',
        f'<circle cx="44" cy="19" r="6" fill="#febc2e"/>',
        f'<circle cx="66" cy="19" r="6" fill="#28c840"/>',
        f'<text x="{WIDTH/2}" y="24" fill="{TITLE_FG}" font-family="{FONT}" '
        f'font-size="14" text-anchor="middle">{html.escape(title)}</text>',
    ]
    y = PAD_TOP
    for line in lines:
        x = PAD_X
        for text, colour, bold in line:
            weight = ' font-weight="bold"' if bold else ""
            parts.append(
                f'<text x="{x}" y="{y}" fill="{colour}" font-family="{FONT}" '
                f'font-size="{FONT_SIZE}"{weight} xml:space="preserve">'
                f"{html.escape(text)}</text>"
            )
            # DejaVu Sans Mono advance width is 0.6023 em
            x += len(text) * FONT_SIZE * 0.6023
        y += LINE_HEIGHT
    parts.append("</svg>")
    return "".join(parts)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    frames_dir = Path(argv[1])
    output = Path(argv[2])
    duration_ms = int(argv[3]) if len(argv) > 3 else 1600

    steps = sorted(frames_dir.glob("step-*.txt"))
    if not steps:
        print(f"no frames in {frames_dir}")
        return 1

    tmp = frames_dir / "_render"
    tmp.mkdir(exist_ok=True)
    rendered = []
    for step in steps:
        text = step.read_text(encoding="utf-8", errors="replace")
        rendered.append(parse_ansi(text))
    canvas = max(
        PAD_TOP + len(lines) * LINE_HEIGHT + PAD_BOTTOM for lines in rendered
    )
    pngs = []
    for index, (step, lines) in enumerate(zip(steps, rendered, strict=True), start=1):
        svg = frame_svg(lines, title="devproxy", canvas_height=canvas)
        svg_path = tmp / f"frame-{index:02d}.svg"
        png_path = tmp / f"frame-{index:02d}.png"
        svg_path.write_text(svg, encoding="utf-8")
        subprocess.run(
            ["rsvg-convert", "-w", str(WIDTH), str(svg_path), "-o", str(png_path)],
            check=True,
            capture_output=True,
        )
        pngs.append(png_path)

    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for png in pngs:
        cmd += ["-loop", "1", "-t", str(duration_ms / 1000), "-i", str(png)]
    cmd += ["-filter_complex", f"fps={1000/duration_ms},split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"]
    cmd += [str(output)]
    subprocess.run(cmd, check=True)
    print(f"wrote {output} ({len(pngs)} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
