#!/usr/bin/env python3
"""Render zzuxzt's GitHub statistics using the original card design.

Fetches statistics directly from GitHub's API using GITHUB_TOKEN or GH_TOKEN.
Uses Python's standard library only. A failed fetch or invalid response leaves
the previous SVG and JSON untouched. Run with --snapshot response.json to
render a saved response without making a network request.
"""

import argparse
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

from fetch_github_stats import fetch_payload


ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://api.github.com/graphql"
USERNAME = "zzuxzt"
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

STAT_FIELDS = (
    "commitsToMyRepositories", "pullRequests", "commits",
    "commitsToAnotherRepositories", "pullRequestsToAnotherRepositories",
    "issues", "createdRepositories", "directStars", "indirectStars",
    "contributedTo", "contributedToOwnRepositories",
    "contributedToNotOwnerRepositories", "followers",
)


def require_number(value, label, integer=False):
    valid_type = type(value) is int if integer else type(value) in (int, float)
    if not valid_type or not math.isfinite(value) or value < 0:
        expected = "nonnegative integer" if integer else "finite nonnegative number"
        raise ValueError(f"{label} must be a {expected}")


def validate_payload(payload, levels):
    if not isinstance(payload, dict):
        raise ValueError("The statistics response must be a JSON object")
    stats = payload.get("userStats")
    if not isinstance(stats, dict) or stats.get("login") != USERNAME:
        raise ValueError(f"Expected userStats.login={USERNAME!r}")
    if not isinstance(stats.get("name"), (str, type(None))):
        raise ValueError("userStats.name must be a string or null")
    for field in STAT_FIELDS:
        require_number(stats.get(field), f"userStats.{field}", integer=True)
    last_fetch = stats.get("lastFetch")
    if not isinstance(last_fetch, str):
        raise ValueError("userStats.lastFetch must be an ISO 8601 timestamp")
    try:
        parsed_time = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid userStats.lastFetch timestamp") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise ValueError("userStats.lastFetch must include a timezone")
    require_number(payload.get("score"), "score")
    level = payload.get("level")
    if not isinstance(level, str) or level not in {item["rank"] for item in levels}:
        raise ValueError("Unknown or missing rank level")
    expected_level = max(
        (item for item in levels if payload["score"] >= item["points"]),
        key=lambda item: item["points"],
    )["rank"]
    if level != expected_level:
        raise ValueError("Rank level and score do not match the original rank configuration")
    weights = payload.get("rankPoints")
    if not isinstance(weights, dict):
        raise ValueError("Missing rankPoints object")
    for field in STAT_FIELDS:
        require_number(weights.get(field), f"rankPoints.{field}")
    if stats["contributedTo"] != (
        stats["contributedToOwnRepositories"]
        + stats["contributedToNotOwnerRepositories"]
    ):
        raise ValueError("Contributed repository counts are inconsistent")
    return stats


def progress_percent(score, level, levels):
    """Match UserStatsCard.CalculateProgressBar in the upstream project."""
    slices = sum(item["count_slice_progress_bar"] for item in levels)
    remaining = sum(
        item["count_slice_progress_bar"] and item["points"] > score
        for item in levels
    )
    following = sorted(
        (item for item in levels if item["points"] > score),
        key=lambda item: item["points"],
    )
    if not following:
        return 100.0
    current_points = next(item["points"] for item in levels if item["rank"] == level)
    fraction = (score - current_points) / (following[0]["points"] - current_points)
    return max(0.0, min(100.0, 100.0 / slices * (slices - remaining + fraction)))


def remove_keyframes(css):
    """Remove the upstream CSS animation blocks, respecting nested braces."""
    while True:
        match = re.search(r"@keyframes\s+[\w-]+\s*\{", css)
        if not match:
            return css
        depth, end = 1, match.end()
        while end < len(css) and depth:
            if css[end] == "{":
                depth += 1
            elif css[end] == "}":
                depth -= 1
            end += 1
        if depth:
            raise ValueError("Unclosed CSS keyframes block in the SVG template")
        css = css[:match.start()] + css[end:]


def render_svg(payload, levels):
    stats = validate_payload(payload, levels)
    width = 175 * progress_percent(payload["score"], payload["level"], levels) / 100
    values = {
        "Name": (stats.get("name") or stats["login"])[:25],
        "Stars": f'{stats["directStars"] + stats["indirectStars"]:,}',
        "Commits": f'{stats["commits"]:,}',
        "PRS": f'{stats["pullRequests"]:,}',
        "Issuers": f'{stats["issues"]:,}',
        "Contributions": f'{stats["contributedTo"]:,}',
        "StarsLabel": "Total Stars",
        "CommitsLabel": "Total Commits",
        "PullRequestLabel": "Total PRs",
        "IssuesLabel": "Total Issues",
        "ContributionsLabel": "Contributed to",
        "TitleColor": "faa627",
        "IconColor": "faa627",
        "TextColor": "ffffff",
        "BackgroundColor": "36393f",
        "BorderColor": "e4e2e2",
        "RingColor": "DD2727",
        "BorderRadius": "4",
        "ShowIcons": "block",
        "TextPosition": "25",
        "FontFamily": "Georgia, 'Times New Roman', serif",
        "FontCSS": "",
        "ProgressBarWidth": f"{width:.6f}".rstrip("0").rstrip("."),
    }
    template = (ROOT / "assets" / "octocat-card.svg").read_text(encoding="utf-8-sig")
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", html.escape(str(value), quote=True))
    if re.search(r"\{\{[^}]+\}\}", template):
        raise ValueError("Unresolved placeholder in the SVG template")
    svg = ET.fromstring(template)
    for element in svg.iter():
        if element.get("fill") == "rgba(0, 0, 0, 0)":
            element.set("fill", "none")
        if element.tag == f"{{{SVG_NS}}}svg" and element.get("viewBox") == "0 0 256 259.3":
            # Resolve the template's 0.99em x 1em icon viewport at the default
            # 16 px font size so standalone SVG renderers show the Octocat.
            element.set("width", "15.84")
            element.set("height", "16")
            element.attrib.pop("style", None)
        if element.tag == f"{{{SVG_NS}}}style":
            css = remove_keyframes(element.text or "")
            css = re.sub(r"\s*animation(?:-[a-z-]+)?\s*:[^;{}]+;", "", css)
            css = re.sub(r"\.stagger\s*\{[^}]*\}", ".stagger { opacity: 1; }", css)
            element.text = "\n".join(line.rstrip(" \t") for line in css.split("\n"))
        if "style" in element.attrib:
            declarations = [
                part.strip() for part in element.attrib["style"].split(";")
                if part.strip() and not part.strip().startswith("animation")
            ]
            if declarations:
                element.set("style", "; ".join(declarations))
            else:
                del element.attrib["style"]
        if element.tag == f"{{{SVG_NS}}}rect" and element.get("class") == "progressbar":
            element.set("width", values["ProgressBarWidth"])
        for child in list(element):
            if child.tag in {f"{{{SVG_NS}}}animate", f"{{{SVG_NS}}}animateTransform"}:
                element.remove(child)
    description = ET.Element(f"{{{SVG_NS}}}desc")
    description.text = (
        f'{stats.get("name") or stats["login"]}: '
        f'{stats["directStars"] + stats["indirectStars"]:,} total stars '
        f'({stats["directStars"]:,} direct + {stats["indirectStars"]:,} indirect). '
        f'Statistics data collected at {stats["lastFetch"]}.'
    )
    svg.insert(0, description)
    rendered = ET.tostring(svg, encoding="utf-8", xml_declaration=True)
    if b"fonts.googleapis.com" in rendered or b"<animate" in rendered:
        raise ValueError("SVG still contains a remote font or animation")
    return rendered + b"\n"


def save_outputs(output_dir, svg_bytes, json_bytes):
    """Stage both validated outputs before atomically replacing each file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for name, content in (("github-stats.svg", svg_bytes), ("github-stats.json", json_bytes)):
            with tempfile.NamedTemporaryFile(dir=output_dir, prefix=f".{name}.", delete=False) as temp:
                temp.write(content)
                temp.flush()
                os.fsync(temp.fileno())
                staged.append((Path(temp.name), output_dir / name))
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Saved statistics payload or generated metadata JSON; no network requests")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "profile")
    args = parser.parse_args()
    source_retrieved_at = None
    source_url = SOURCE_URL
    if args.snapshot:
        payload = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
        if isinstance(payload, dict) and "data" in payload and "source_url" in payload:
            source_url = payload["source_url"]
            if not isinstance(source_url, str) or not source_url:
                raise ValueError("Snapshot source_url must be a nonempty string")
            source_retrieved_at = payload.get("retrieved_at")
            payload = payload["data"]
        else:
            source_url = payload.get("source_url", "offline-snapshot") if isinstance(payload, dict) else "offline-snapshot"
    else:
        payload = fetch_payload(USERNAME)
        source_retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    levels = json.loads((ROOT / "assets" / "rank-config.json").read_text(encoding="utf-8"))["levels"]
    svg_bytes = render_svg(payload, levels)
    metadata = {
        "schema_version": 2,
        "source_url": source_url,
        "retrieval_mode": "offline-snapshot" if args.snapshot else "github-api",
        "retrieved_at": source_retrieved_at,
        "generated_at": generated_at,
        "lastFetch": payload["userStats"]["lastFetch"],
        "data": payload,
    }
    json_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    save_outputs(args.output_dir, svg_bytes, json_bytes)
    stats = payload["userStats"]
    print(
        f'Wrote {args.output_dir / "github-stats.svg"} and github-stats.json; '
        f'stars={stats["directStars"] + stats["indirectStars"]:,}, '
        f'data lastFetch={stats["lastFetch"]}'
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
        print(f"Card update failed; input/render errors do not overwrite existing files: {error}", file=sys.stderr)
        sys.exit(1)
