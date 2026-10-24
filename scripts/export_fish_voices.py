#!/usr/bin/env python3
"""Export the Fish Audio Chinese voice catalog to docs/voices.md + CSV.

Usage:
    FISH_API_KEY=... python scripts/export_fish_voices.py [--out-dir docs] [--pages 10]

The catalog endpoint only exposes a window of ~1000 public models, so raising
--pages does not always reveal more Chinese voices.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from datetime import date
from pathlib import Path

API = "https://api.fish.audio/model"


def fetch_pages(key: str, pages: int) -> list[dict]:
    items: list[dict] = []
    for page in range(1, pages + 1):
        url = f"{API}?page_size=100&page_number={page}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            payload = json.load(urllib.request.urlopen(request, timeout=40))
        except Exception as exc:  # noqa: BLE001 - report and stop paging
            print(f"page {page} failed: {exc}")
            break
        batch = payload.get("items") or []
        if not batch:
            break
        items.extend(batch)
    return items


def tags_of(item: dict) -> list[str]:
    raw = str(item.get("tags") or "")
    return [tag.strip(" '\"[]") for tag in raw.split(",") if tag.strip(" '\"[]")]


def gender_of(item: dict) -> str:
    tags = tags_of(item)
    for gender in ("female", "male", "neutral"):
        if gender in tags:
            return gender
    return "unknown"


def build_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        if "'zh'" not in str(item.get("languages") or ""):
            continue
        rows.append({
            "id": item["_id"],
            "title": item.get("title") or "",
            "gender": gender_of(item),
            "tags": " / ".join(tags_of(item)),
            "likes": item.get("like_count") or 0,
            "uses": item.get("task_count") or 0,
            "languages": str(item.get("languages") or "").strip("[]'\" "),
            "visibility": item.get("visibility") or "",
            "licensed": item.get("licensed"),
            "description": " ".join((item.get("description") or "").split()),
        })
    rows.sort(key=lambda row: (-row["likes"], row["title"]))
    return rows


def table(rows: list[dict]) -> str:
    lines = ["| 音色 | reference_id | 标签 | 热度 |", "|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| {row['title'].replace('|', '/')} | `{row['id']}` | "
            f"{row['tags'].replace('|', '/')} | {row['likes']:,} |"
        )
    return "\n".join(lines)


def write_outputs(rows: list[dict], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "fish-voices-zh.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id", "title", "gender", "tags", "likes", "uses",
                "languages", "visibility", "licensed", "description",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    female = [row for row in rows if row["gender"] == "female"]
    neutral = [row for row in rows if row["gender"] == "neutral"]
    other = [row for row in rows if row["gender"] == "unknown"]
    male = [row for row in rows if row["gender"] == "male"]
    markdown = f"""# Fish Audio 中文音色清单

> 生成日期：{date.today().isoformat()} · 数据来源：`GET {API}`
> 共 **{len(rows)}** 个中文（`zh`）音色。全部可用 `s2.1-pro-free` 免费合成，不按音色收费。
> 完整字段见 [`fish-voices-zh.csv`](fish-voices-zh.csv)。

## 女声（{len(female)}）

{table(female)}

## 中性 / 其他（{len(neutral) + len(other)}）

{table(neutral + other)}

## 男声（{len(male)}）

{table(male)}
"""
    md_path = out_dir / "voices.md"
    md_path.write_text(markdown, encoding="utf-8")
    return csv_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="export Fish Audio Chinese voices")
    parser.add_argument("--out-dir", default="docs")
    parser.add_argument("--pages", type=int, default=10)
    args = parser.parse_args()

    key = os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        print("FISH_API_KEY is not set")
        return 2
    rows = build_rows(fetch_pages(key, max(1, args.pages)))
    csv_path, md_path = write_outputs(rows, Path(args.out_dir))
    print(f"wrote {csv_path} ({len(rows)} rows) and {md_path}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
