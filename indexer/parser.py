"""
ADF (Atlassian Document Format) to plain text extractor.

Recursively walks an ADF document tree and emits text. Node types handled:
  text        → emit .text value
  hardBreak   → emit newline
  mention     → emit attrs.text (e.g. @unknown)
  codeBlock   → emit child text with surrounding newlines
  heading     → emit child text + newline
  paragraph   → emit child text + newline
  listItem    → emit "- " + child text
  orderedList / bulletList → recurse into children
  mediaSingle / media / inlineCard → skip
  all other types → recurse into .content if present
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Do NOT embed invalid issues.
SKIP_RESOLUTIONS = {"Invalid"}


def extract_text(node: dict | list | None, *, _depth: int = 0) -> str:
    """
    Recursively extract plain text from an ADF node or list of nodes.
    Returns a single string; callers are responsible for joining paragraphs.
    """
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(extract_text(child, _depth=_depth) for child in node)

    node_type = node.get("type", "")
    content = node.get("content", [])
    attrs = node.get("attrs", {})

    if node_type == "text":
        return node.get("text", "")

    if node_type == "hardBreak":
        return "\n"

    if node_type == "mention":
        return attrs.get("text", "@unknown")

    if node_type == "emoji":
        return attrs.get("text", "")

    if node_type == "inlineCard":
        return ""

    if node_type in ("media", "mediaSingle", "mediaGroup"):
        return ""

    if node_type == "codeBlock":
        inner = extract_text(content, _depth=_depth + 1).strip()
        return f"\n{inner}\n"

    if node_type == "heading":
        inner = extract_text(content, _depth=_depth + 1).strip()
        return f"{inner}\n"

    if node_type == "paragraph":
        inner = extract_text(content, _depth=_depth + 1)
        return inner.rstrip(" \t") + "\n"

    if node_type in ("bulletList", "orderedList"):
        parts = []
        for i, item in enumerate(content, start=1):
            item_type = item.get("type", "")
            if item_type == "listItem":
                inner = extract_text(item.get("content", []), _depth=_depth + 1).strip()
                if node_type == "orderedList":
                    parts.append(f"{i}. {inner}")
                else:
                    parts.append(f"- {inner}")
            else:
                parts.append(extract_text(item, _depth=_depth + 1).strip())
        return "\n".join(parts) + "\n"

    if node_type == "listItem":
        return extract_text(content, _depth=_depth + 1)

    if node_type == "rule":
        return "\n"

    if node_type in ("table", "tableRow", "tableCell", "tableHeader"):
        return extract_text(content, _depth=_depth + 1)

    return extract_text(content, _depth=_depth + 1)


def parse_description(raw_description: str | None) -> str:
    """
    Parse a raw ADF JSON string (as returned by mojira.dev) into plain text.
    Returns "" if description is null, empty, or unparseable.
    """
    if not raw_description:
        return ""
    try:
        doc = json.loads(raw_description)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to JSON-decode description: %.80r", raw_description)
        return ""

    if not isinstance(doc, dict) or doc.get("type") != "doc":
        if isinstance(doc, str):
            return doc.strip()
        return ""

    raw = extract_text(doc)

    import re
    cleaned = re.sub(r"\n{3,}", "\n\n", raw).strip()
    return cleaned


def build_embed_text(issue: dict) -> str:
    """
    Build the embedding input for an issue dict.

    Format:
        KEY | summary
        label1 label2
        <description plain text>
    """
    key = issue.get("key", "")
    summary = issue.get("summary", "").strip()
    labels: list[str] = issue.get("labels") or []
    description_raw = issue.get("description")

    parts = [f"{key} | {summary}"]

    if labels:
        parts.append(" ".join(labels))

    description_text = parse_description(description_raw)
    if description_text:
        parts.append(description_text)

    return "\n".join(parts)


def should_embed(issue: dict) -> bool:
    resolution = issue.get("resolution") or "Unresolved"
    return resolution not in SKIP_RESOLUTIONS
