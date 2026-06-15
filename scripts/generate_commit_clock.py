#!/usr/bin/env python3
"""Generate a radial commit clock SVG from GitHub commit history.

Plots commits on a polar chart: hour of day on the angular axis (midnight
at top, noon at bottom), day of week on the radial axis (Monday inner,
Sunday outer). Dot size and brightness scale with commit count.
"""

import json
import math
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

USERNAME = "paulpessoa"
SVG_PATH = "assets/commit-clock.svg"

# Layout
WIDTH, HEIGHT = 500, 500
CX, CY = WIDTH // 2, HEIGHT // 2
R_INNER = 65
R_OUTER = 195
MAX_DOT_R = 8
HOURS = 24
DAYS = 7

DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Tokyo Night palette
BG = "#1a1b27"
GRID = "#292e42"
TEXT = "#565f89"
DOT_LO = (112, 165, 253)  # #70a5fd
DOT_HI = (125, 207, 255)  # #7dcfff


def hour_angle(h):
    """Convert hour (0-23) to radians with midnight at top."""
    return h * (2 * math.pi / HOURS) - math.pi / 2


def ring_radius(d):
    """Convert day index (0=Mon .. 6=Sun) to a radius."""
    return R_INNER + d * (R_OUTER - R_INNER) / (DAYS - 1)


def dot_color(intensity):
    """Interpolate between DOT_LO and DOT_HI based on intensity 0..1."""
    r = int(DOT_LO[0] + (DOT_HI[0] - DOT_LO[0]) * intensity)
    g = int(DOT_LO[1] + (DOT_HI[1] - DOT_LO[1]) * intensity)
    b = int(DOT_LO[2] + (DOT_HI[2] - DOT_LO[2]) * intensity)
    return f"#{r:02x}{g:02x}{b:02x}"


def fetch_commits():
    """Fetch commit timestamps from the GitHub search API (past year)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "commit-clock-generator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    counts = defaultdict(int)
    now = datetime.now(tz=None)

    # Split into 90-day windows so we can exceed the 1000-result search cap
    for i in range(4):
        end = now - timedelta(days=90 * i)
        start = now - timedelta(days=90 * (i + 1))
        q = f"author:{USERNAME} author-date:{start:%Y-%m-%d}..{end:%Y-%m-%d}"

        for page in range(1, 11):
            url = (
                f"https://api.github.com/search/commits"
                f"?q={urllib.parse.quote(q)}&per_page=100&page={page}"
                f"&sort=author-date"
            )
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
            except Exception as e:
                print(f"  Warning: {e}")
                break

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                date_str = item["commit"]["author"]["date"]
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                counts[(dt.weekday(), dt.hour)] += 1

            if len(items) < 100:
                break

    return counts


def generate_svg(counts):
    """Render the radial commit clock as an SVG string."""
    max_count = max(counts.values()) if counts else 1

    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}">'
    )
    svg.append(f'<rect width="{WIDTH}" height="{HEIGHT}" rx="12" fill="{BG}"/>')

    # Glow filter
    svg.append(
        '<defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="2" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
        '</filter></defs>'
    )

    # Concentric ring guides
    for d in range(DAYS):
        r = ring_radius(d)
        svg.append(
            f'<circle cx="{CX}" cy="{CY}" r="{r:.1f}" '
            f'fill="none" stroke="{GRID}" stroke-width="0.5"/>'
        )

    # Radial spokes every 3 hours
    for h in range(0, HOURS, 3):
        a = hour_angle(h)
        x1 = CX + (R_INNER - 6) * math.cos(a)
        y1 = CY + (R_INNER - 6) * math.sin(a)
        x2 = CX + (R_OUTER + 4) * math.cos(a)
        y2 = CY + (R_OUTER + 4) * math.sin(a)
        svg.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" '
            f'x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{GRID}" stroke-width="0.5"/>'
        )

    # Hour labels around the outside
    for h in range(0, HOURS, 3):
        a = hour_angle(h)
        x = CX + (R_OUTER + 18) * math.cos(a)
        y = CY + (R_OUTER + 18) * math.sin(a)
        svg.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" fill="{TEXT}" '
            f'font-family="monospace" font-size="10">{h:02d}</text>'
        )

    # Commit dots
    for d in range(DAYS):
        r = ring_radius(d)
        for h in range(HOURS):
            count = counts.get((d, h), 0)
            if count == 0:
                continue
            a = hour_angle(h)
            x = CX + r * math.cos(a)
            y = CY + r * math.sin(a)
            t = count / max_count
            dr = 2 + math.sqrt(t) * (MAX_DOT_R - 2)
            opacity = 0.3 + 0.7 * t
            color = dot_color(t)
            svg.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{dr:.1f}" '
                f'fill="{color}" opacity="{opacity:.2f}" filter="url(#glow)"/>'
            )

    # Day labels along the ~1:15 radial (upper right, between hour labels)
    label_a = 1.25 * (2 * math.pi / HOURS) - math.pi / 2
    for d in range(DAYS):
        r = ring_radius(d)
        x = CX + r * math.cos(label_a)
        y = CY + r * math.sin(label_a)
        svg.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" '
            f'dominant-baseline="central" fill="{TEXT}" '
            f'font-family="monospace" font-size="7" opacity="0.6">'
            f'{DAY_LABELS[d]}</text>'
        )

    svg.append("</svg>")
    return "\n".join(svg)


def main():
    print("Fetching commits...")
    counts = fetch_commits()
    total = sum(counts.values())
    print(f"Found {total} commits across {len(counts)} time slots.")

    svg = generate_svg(counts)

    os.makedirs(os.path.dirname(SVG_PATH), exist_ok=True)
    with open(SVG_PATH, "w") as f:
        f.write(svg)
    print(f"Generated {SVG_PATH}")


if __name__ == "__main__":
    main()
