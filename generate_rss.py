"""
Generate an RSS 2.0 feed (feed.xml) from 法规.csv and meta.json.
"""

import csv
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path

# Number of most-recent items to include in the feed
MAX_FEED_ITEMS = 100

CATEGORY_NAME_MAP = {
    "central": "中央法规",
    "local": "地方法规",
    "legislative_materials": "立法资料",
    "legislative_interpretations": "法规解读",
    "legal_updates": "法律动态",
}

SITE_URL = "https://youngfish42.github.io/law_query/"
FEED_PATH = Path("feed.xml")
CSV_PATH = Path("法规.csv")
META_PATH = Path("meta.json")


def parse_publish_date(date_str: str) -> datetime | None:
    """Try to parse a date string like '2025.03.15' or '2025.03' into a datetime."""
    if not date_str:
        return None
    for fmt in ("%Y.%m.%d", "%Y.%m"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            continue
    return None


def rfc2822(dt: datetime) -> str:
    """Convert a datetime to RFC 2822 format required by RSS."""
    return format_datetime(dt)


def load_records() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("title") and r.get("url")]
    # Sort by publish_date descending
    rows.sort(key=lambda r: r.get("publish_date", ""), reverse=True)
    return rows[:MAX_FEED_ITEMS]


def load_last_updated() -> datetime:
    tz_cst = timezone(timedelta(hours=8))
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
            dt = datetime.strptime(meta["updated_at"], "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=tz_cst)
        except Exception:
            pass
    return datetime.now(tz_cst)


def build_feed(records: list[dict], last_updated: datetime) -> ET.Element:
    rss = ET.Element("rss", version="2.0")
    # Add Atom namespace
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")

    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "AI法规库 - 最新法规"
    ET.SubElement(channel, "link").text = SITE_URL
    ET.SubElement(channel, "description").text = (
        "AI、数智化、大模型等相关法规的最新更新订阅"
    )
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = rfc2822(last_updated)
    ET.SubElement(channel, "ttl").text = "1440"  # refresh every 24 hours

    # Atom self-link (recommended)
    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", SITE_URL + "feed.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for row in records:
        item = ET.SubElement(channel, "item")

        category_key = (row.get("category") or "").lower()
        category_text = CATEGORY_NAME_MAP.get(category_key, row.get("category") or "")
        authority = row.get("issuing_authority") or ""
        hierarchy = row.get("legal_hierarchy") or ""
        publish_date_str = row.get("publish_date") or ""

        title = row["title"]
        url = row["url"]

        # Build a description with metadata
        desc_parts = []
        if category_text:
            desc_parts.append(f"分类：{category_text}")
        if publish_date_str:
            desc_parts.append(f"公布日期：{publish_date_str}")
        if authority:
            desc_parts.append(f"制定机关：{authority}")
        if hierarchy:
            desc_parts.append(f"效力位阶：{hierarchy}")
        description = "；".join(desc_parts) if desc_parts else title

        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "description").text = description
        ET.SubElement(item, "guid", isPermaLink="true").text = url

        if category_text:
            ET.SubElement(item, "category").text = category_text

        pub_dt = parse_publish_date(publish_date_str)
        if pub_dt:
            ET.SubElement(item, "pubDate").text = rfc2822(pub_dt)

    return rss


def indent_xml(elem: ET.Element, level: int = 0) -> None:
    """Add pretty-print indentation to an ElementTree in-place."""
    indent = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent
    if not level:
        elem.tail = "\n"


def main() -> None:
    records = load_records()
    last_updated = load_last_updated()

    rss = build_feed(records, last_updated)
    indent_xml(rss)

    tree = ET.ElementTree(rss)
    with FEED_PATH.open("w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)

    print(f"Generated {FEED_PATH} with {len(records)} items (last updated: {last_updated})")


if __name__ == "__main__":
    main()
