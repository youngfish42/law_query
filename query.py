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
    try:
        # Increase timeout to 60s
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"Warning: First attempt to open homepage failed: {e}. Retrying...")
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)


async def search_by_title(page: Page, keyword: str) -> None:
    # 首页/结果页顶部都有同一个检索框
    box = page.locator("#txtSearch")
    await box.wait_for(state="visible", timeout=30000)
    await box.fill(keyword)

    # 点击“检索/新检索”按钮
    # 页面上通常是 a#btnSearch（文本可能为 检索/新检索）
    btn = page.locator("a#btnSearch")
    await btn.wait_for(state="visible", timeout=30000)
    
    # 获取操作前列表的第一条，用于检测列表刷新
    old_item = page.locator('input[name="recordList"]').first
    has_old = False
    if await old_item.count() > 0:
        has_old = True
        # 尝试等待该特定元素消失（detached）
        # 但因为这里是 Locator，我们需要它的唯一性，或者我们在点击后等待它消失
        # 但如果新列表第一条和旧列表长得一样（HTML结构），Playwright Locator 可能会立即匹配到新元素
        # 所以最好是简单等待一下网络空闲，或者特定的加载遮罩消失
        pass

    await btn.click()
    
    # 强制等待，因为 URL 不变且 DOM 变化可能需要时间 (AJAX)
    # 没有可靠的特定元素只能等待
    await page.wait_for_timeout(10000)

    # 等结果区域出现
    await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)

    # 简单验证结果
    first_title_loc = page.locator(".t h4 a").first
    try:
        if await first_title_loc.count() > 0:
             title = (await first_title_loc.inner_text(timeout=5000)).strip()
             print(f"DEBUG: First result title: '{title}', Keyword: '{keyword}'")
    except:
        pass


async def switch_category(page: Page, category: str) -> None:
    # category: "central" | "local"
    text = "中央法规" if category == "central" else "地方法规"

    # Iterate through links to find the one that preserves the search session (contains "Keywords" or similar)
    # The global nav link usually points to /chl or /lar without query params.
    # The search tab usually points to /s?Keywords=...
    links = page.locator(f'a:has-text("{text}")')
    count = await links.count()
    
    target_link = None
    for i in range(count):
        link = links.nth(i)
        if not await link.is_visible():
            continue
            
        href = await link.get_attribute("href")
        if href and ("Keywords" in href or "search" in href.lower() or "javascript" in href.lower()):
            # This looks like the correct tab
            target_link = link
            break
    
    clicked = False
    if target_link:
        await target_link.click()
        clicked = True
    else:
        # Fallback: Check if the first/second link is safe. 
        # Often the first is global nav (unsafe), second is tab (safe).
        print(f"Warning: Specific search tab for {text} not found by heuristic. Checking candidates...")
        for i in range(count):
             lk = links.nth(i)
             if not await lk.is_visible(): continue
             href = await lk.get_attribute("href") or ""
             # If href is exactly the base category URL, it's a reset. Skip it for search results page
             # BUT here we are likely on Home Page, so clicking it is fine/desired.
             # e.g. /chl/ or /lar/ or http://.../chl/
             # if href.rstrip('/').endswith(("/chl", "/lar")):
             #    print(f"Skipping link {href} as it looks like a global navigation reset.")
             #    continue
             
             # If it's not a reset, it might be the tab.
             print(f"Clicking fallback link: {href}")
             await lk.click()
             clicked = True
             break
    
    if not clicked:
        print(f"Error: Could not find link for category '{text}'. Aborting switch.")
        return

    # 等待列表刷新
    try:
        await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
    except Exception as e:
        print(f"Warning: filter list not found after switching category. This might be OK if there are no records. Error: {e}")


async def apply_this_month_effective_filter(page: Page) -> None:
    # Look for "This Month Effective" in the filter section specifically.
    # Avoid global "New Laws" or sidebar promos that reset search.
    # Search filters usually have "Keywords" in the href or appear in a specific facet list.
    
    link_locator = page.locator('a[title^="本月生效"]')
    count = await link_locator.count()
    
    target_link = None
    for i in range(count):
        link = link_locator.nth(i)
        if not await link.is_visible():
            continue
            
        href = await link.get_attribute("href")
        # Same heuristic: must look like a filter for the current search
        # Usually contains "Keywords" or is a javascript postback for the filter.
        # Links to purely /chl/ or /lar/xxxx without params are suspicious.
        if href and ("Keywords" in href or "search/result" in href or "javascript" in href.lower()):
            target_link = link
            break
            
async def apply_this_month_effective_filter(page: Page) -> None:
    # 强制等待一下让页面稳定
    await page.wait_for_timeout(1000)

    # 左侧“相关提示”里点击 “本月生效”
    # 如果找不到可能是因为没有本月生效的法规，或者 UI 变了
    # 我们直接找可见的文本链接
    links = page.locator('a:has-text("本月生效")')
    count = await links.count()
    clicked = False
    
    for i in range(count):
        lk = links.nth(i)
        if await lk.is_visible():
            # 简单假设可见的那个就是我们要点的（通常是左侧栏那个）
            try:
                await lk.click(timeout=5000)
                clicked = True
                break
            except Exception as e:
                print(f"Failed to click visible filter link: {e}")

    if clicked:
        # 点击后等待刷新
        await page.wait_for_timeout(2000)
        await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
    else:
        print("Warning: 'This Month Effective' filter link not found or not clickable.")


async def extract_visible_records(page: Page, category: str) -> List[Record]:
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
            title = (await a.inner_text(timeout=5000)).strip()
            # If the user keyword is provided, assume it's mandatory for validity check
            # unless the search failed, but if search succeeded, the keyword *should* be in title most times.
            # But sometimes it's in content only. However, if the result is completely off-topic (like "2026 Bond"), the search definitely failed.
            # But we can't be too strict here. "智能" might be in content.
            href = await a.get_attribute("href", timeout=5000)
        except Exception as e:
            print(f"Skipping invalid record (title/href missing): {e}")
            continue

        if not href:
            continue
        url = href if href.startswith("http") else (BASE_URL + href)

        text = (await col.inner_text()).replace("\u00a0", " ")
        if await col.locator(".info").count() > 0:
            info_text = await col.locator(".info").inner_text()
            text += " " + info_text
            
        # Parse Dates
        publish_date = ""
        effective_date = ""
        
        # Regex for Publish Date "YYYY.MM.DD 公布"
        m_pub = PUBLISH_RE.search(text)
        if m_pub:
            publish_date = m_pub.group(1)
            
        # Regex for Effective Date "YYYY.MM.DD 实施" or similar
        # If not labeled, we might guess if there's another date.
        # But let's look for "实施" or "生效"
        m_eff = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*(?:实施|生效|施行)", text)
        if m_eff:
            effective_date = m_eff.group(1)
        
        # Fallback: if no labeled date, just find any date?
        if not publish_date and not effective_date:
             date_m = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
             if date_m:
                 publish_date = date_m.group(1) # Assume first date found is publish date
        
        # If user wants "Effective This Month", we check against effective_date if found, else publish_date.
        # Current month prefix
        current_month = datetime.now().strftime("%Y.%m")
        
        # Strict Filtering Logic:
        # If effective_date is available, check it.
        # If not, check publish_date (often same month).
        # We only return records that match "This Month"
        
        date_to_check = effective_date if effective_date else publish_date
        if not date_to_check.startswith(current_month):
            # Record is not from this month. Skip it.
            # print(f"Skipping record {title} - Date {date_to_check} not in {current_month}")
            continue

        out.append(Record(category=category, title=title, url=url, publish_date=date_to_check))


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
        await page.wait_for_timeout(2000) # Increased to 2s to allow AJAX load
        added = await collect_once()
        print(f"Loaded more: +{added} records")

        # 如果本轮没有新增，认为加载结束，避免死循环
        #（网站可能返回同一批内容）
        if added == 0:
            # Maybe retry once?
            await page.wait_for_timeout(2000)
            added = await collect_once()
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
            all_records: List[Record] = []
            
            # Use current month for python-side filtering
            current_month_prefix = datetime.now().strftime("%Y.%m")
            print(f"Target Month: {current_month_prefix}")

            # Define the categories and their labels to match tabs
            categories = [("central", "中央法规"), ("local", "地方法规")]
            
            for cat_key, cat_label in categories:
                print(f"Processing Category: {cat_label} ({cat_key})")
                
                # Step 1: Go to Home (Reset state)
                await goto_home(page)
                
                # Step 2: Click Category Tab *BEFORE* Searching
                # This ensures we are in the correct 'Library' scope if the tabs work that way.
                await switch_category(page, cat_key)
                
                # Step 3: Search text
                # Note: If 'Full Library Search' is checked by default, we might need to rely on 
                # result filtering or hope the tab set the context.
                await search_by_title(page, keyword)
                
                # Step 4: Collect results
                # We skip 'apply_this_month_effective_filter' because it's unreliable/resets search.
                # If the user script MUST filter by 'Effective Date', we should scrape that date.
                # However, scraping 'Effective Date' requires reading more detail from result list.
                # Standard result item text often spans 'Publish Date' and 'Effective Date'.
                
                # We'll fetch a bit more items to increase chance of finding items
                items_needed = max_items if max_items > 0 else 100 
                
                # Filter seen_keys global check?
                # records from central vs local might overlap if search is global?
                # If search is global, we get duplicates. 
                # If tabs worked, we get distinct sets.
                # Use a set to dedup.
                all_seen_urls = set(r.url for r in all_records)
                
                found_recs = await click_load_more_until_done(page, all_seen_urls, cat_key, max_items=items_needed)
                
                # If we found NOTHING with keyword, maybe verify?
                # But let's assume search returned OK.
                
                all_records.extend(found_recs)
                print(f"Found {len(found_recs)} records for {cat_label}")
                
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
