import argparse
import asyncio
import csv
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

from playwright.async_api import async_playwright, Page


BASE_URL = "https://www.pkulaw.com"


@dataclass
class Record:
    category: str  # "central" | "local"
    title: str
    url: str
    publish_date: str  # YYYY.MM.DD


PUBLISH_RE = re.compile(r"(\d{4}\.\d{2}\.\d{2})\s*公布")


async def goto_home(page: Page) -> None:
    await page.goto(BASE_URL + "/", wait_until="domcontentloaded")


async def search_by_title(page: Page, keyword: str) -> None:
    # 首页/结果页顶部都有同一个检索框
    box = page.locator("#txtSearch")
    await box.wait_for(state="visible", timeout=30000)
    await box.fill(keyword)

    # 点击“检索/新检索”按钮
    # 页面上通常是 a#btnSearch（文本可能为 检索/新检索）
    btn = page.locator("a#btnSearch")
    await btn.wait_for(state="visible", timeout=30000)
    await btn.click()

    # 等结果区域出现（至少出现一个 recordList）
    await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)


async def switch_category(page: Page, category: str) -> None:
    # category: "central" | "local"
    text = "中央法规" if category == "central" else "地方法规"

    # 顶部分类 tab（中央法规/地方法规）。
    # 页面里可能存在多个同文本链接，但“分类 tab”通常位于页面顶部且可见。
    tab = page.locator(f'a:has-text("{text}")').first
    await tab.wait_for(state="visible", timeout=30000)
    await tab.click()

    # 等待列表刷新
    await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)


async def apply_this_month_effective_filter(page: Page) -> None:
    # 左侧“相关提示”里点击 “本月生效 (xxx)”
    # 不同页面 id 会变化，但 title 文本稳定
    link = page.locator('a[title^="本月生效"]')
    await link.first.wait_for(state="visible", timeout=30000)
    await link.first.click()

    # 等待“检索条件”区域显示“本月生效”
    await page.locator("text=本月生效").first.wait_for(timeout=30000)
    await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)


async def extract_visible_records(page: Page, category: str) -> List[Record]:
    # 每条记录通常在 div.col 下，含 div.t(h4>a) + div.info(含日期)
    cols = page.locator("div.col")
    n = await cols.count()
    out: List[Record] = []

    for i in range(n):
        col = cols.nth(i)
        # 必须有 recordList checkbox 才算结果条目
        if await col.locator('input[name="recordList"]').count() == 0:
            continue

        a = col.locator(".t h4 a").first
        try:
            title = (await a.inner_text(timeout=10000)).strip()
        except Exception as e:
            print(f"元素未找到或超时: {e}")
            title = ""
        href = await a.get_attribute("href")
        if not href:
            continue
        url = href if href.startswith("http") else (BASE_URL + href)

        text = (await col.inner_text()).replace("\u00a0", " ")
        m = PUBLISH_RE.search(text)
        if not m:
            # 兜底：有些条目可能用“YYYY.MM.DD 公布”或别的格式
            # 这里尽量不硬失败
            publish_date = ""
        else:
            publish_date = m.group(1)

        out.append(Record(category=category, title=title, url=url, publish_date=publish_date))

    return out


async def click_load_more_until_done(
    page: Page,
    seen_keys: set,
    category: str,
    max_items: int,
) -> List[Record]:
    results: List[Record] = []

    async def collect_once() -> int:
        recs = await extract_visible_records(page, category)
        added = 0
        for r in recs:
            key = (r.url or "")
            if key and key not in seen_keys:
                seen_keys.add(key)
                results.append(r)
                added += 1
        return added

    await collect_once()

    while True:
        if max_items > 0 and len(results) >= max_items:
            break

        # 页面上有很多“更多”，我们只点列表区域里带 icon 的“更多”按钮
        more = page.locator('a:has(i.c-icon):has-text("更多")').last

        if await more.count() == 0:
            break

        try:
            await more.scroll_into_view_if_needed()
            await more.click(timeout=3000)
        except Exception:
            # 没有更多了 / 按钮不可点击
            break

        # 等待新内容加载：recordList 数量变化或稍等
        await page.wait_for_timeout(800)
        added = await collect_once()

        # 如果本轮没有新增，认为加载结束，避免死循环
        #（网站可能返回同一批内容）
        if added == 0:
            break

    # 若 max_items 截断
    if max_items > 0:
        results = results[:max_items]

    return results


def write_csv(path: Path, rows: Iterable[Record]) -> None:
    # 读取已有数据进行合并
    merged_map = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # 构造 Record 对象，处理可能缺失的字段
                    r = Record(
                        category=row.get("category", ""),
                        title=row.get("title", ""),
                        url=row.get("url", ""),
                        publish_date=row.get("publish_date", ""),
                    )
                    if r.url:
                        merged_map[r.url] = r
        except Exception as e:
            print(f"Warning: Failed to read existing CSV for merging: {e}")

    # 合并新查询到的数据（优先使用新数据）
    for r in rows:
        merged_map[r.url] = r

    # 按 publish_date 降序排序（由新到旧）
    sorted_records = sorted(
        merged_map.values(),
        key=lambda x: x.publish_date,
        reverse=True
    )

    # 写回文件
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["category", "title", "url", "publish_date"])
        w.writeheader()
        for r in sorted_records:
            w.writerow(asdict(r))


def write_json(path: Path, rows: Iterable[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


async def run(
    keyword: str,
    out_csv: Path,
    out_json: Optional[Path],
    headless: bool,
    slow_mo: int,
    max_items: int,
    user_data_dir: Optional[Path],
) -> List[Record]:
    async with async_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "slow_mo": slow_mo,
        }

        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                **launch_kwargs,
            )
            page = await context.new_page()
        else:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context()
            page = await context.new_page()

        try:
            await goto_home(page)
            await search_by_title(page, keyword)

            all_records: List[Record] = []

            # 中央法规
            await switch_category(page, "central")
            await apply_this_month_effective_filter(page)
            central = await click_load_more_until_done(page, set(), "central", max_items=max_items)
            all_records.extend(central)

            # 清除条件回到未筛选状态（避免“地方法规”tab消失）
            clear_btn = page.locator("a#btn-clear")
            if await clear_btn.count() > 0:
                await clear_btn.first.click()
                await page.wait_for_timeout(800)

            # 地方法规
            await switch_category(page, "local")
            await apply_this_month_effective_filter(page)
            local = await click_load_more_until_done(page, set(r.url for r in all_records), "local", max_items=max_items)
            all_records.extend(local)

            # 输出
            write_csv(out_csv, all_records)
            if out_json:
                write_json(out_json, all_records)

            return all_records
        finally:
            await context.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Query pkulaw.com search results with interactive filtering")
    ap.add_argument("--keyword", default="智能", help="检索词（默认：智能）")
    ap.add_argument("--out", default="results.csv", help="输出 CSV 路径")
    ap.add_argument("--out-json", default=None, help="输出 JSON 路径（可选）")

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--headless", action="store_true", help="无头模式（默认）")
    g.add_argument("--headed", action="store_true", help="有头模式")

    ap.add_argument("--slow-mo", type=int, default=0, help="操作放慢（毫秒），用于调试")
    ap.add_argument("--max-items", type=int, default=0, help="最多查询多少条（0=不限制）")
    ap.add_argument("--user-data-dir", default=None, help="持久化浏览器目录（用于复用登录态）")

    return ap.parse_args()


def main() -> None:
    args = parse_args()
    headless = True
    if args.headed:
        headless = False

    out_csv = Path(args.out)
    out_json = Path(args.out_json) if args.out_json else None
    user_data_dir = Path(args.user_data_dir) if args.user_data_dir else None

    records = asyncio.run(
        run(
            keyword=args.keyword,
            out_csv=out_csv,
            out_json=out_json,
            headless=headless,
            slow_mo=args.slow_mo,
            max_items=args.max_items,
            user_data_dir=user_data_dir,
        )
    )

    print(f"Done. Total records: {len(records)}")
    print(f"CSV: {out_csv.resolve()}")
    if out_json:
        print(f"JSON: {out_json.resolve()}")


if __name__ == "__main__":
    main()
