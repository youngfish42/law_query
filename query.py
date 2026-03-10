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
    sub_category: str # Specific category from sidebar, e.g. "法律", "行政法规"
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


async def click_category_nav(page: Page, label: str) -> None:
    """
    在首页点击分类导航按钮（如“中央法规”、“地方法规”）。
    由于页面加载慢，点击后等待较长时间。
    """
    print(f"Navigating to category: {label}")
    
    # 尝试找到准确文本的链接
    # 首页通常有明显的 "中央法规" 链接
    # 使用 a:text-is 或者 a:has-text，优先精确匹配
    link = page.locator(f"a:has-text('{label}')").first
    
    # 为了防止点到不相关的链接，稍微过滤一下可见性
    if await link.count() == 0:
        print(f"Error: Link with text '{label}' not found.")
        return

    await link.wait_for(state="visible", timeout=30000)
    await link.click()
    
    # 按照用户要求，每步操作后停顿10秒以上
    print("Waiting 12s after clicking category...")
    await page.wait_for_timeout(12000)
    
    # 注意：点击"中央法规"后，可能会跳转到 /chl 页面。
    # 此时页面元素可能完全刷新，需要重新定位后续的搜索框。
    # search_by_title 函数里会重新 locate 搜索框，所以这里不需要额外操作。


async def search_by_title(page: Page, keyword: str) -> None:
    print(f"Searching for: {keyword}")
    # 首页/结果页顶部都有同一个检索框
    box = page.locator("input#txtSearch")
    await box.wait_for(state="visible", timeout=30000)
    await box.fill(keyword)
    
    # 输入后稍作停顿
    await page.wait_for_timeout(2000)

    # 点击“检索/新检索”按钮
    btn = page.locator("a#btnSearch")
    await btn.wait_for(state="visible", timeout=30000)
    
    await btn.click()
    
    # 强制等待，因为网页加载很慢
    print("Waiting 15s for search results to load...")
    await page.wait_for_timeout(15000)

    # 等结果区域出现
    try:
        await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
    except:
        print("Warning: Result list not detected (timeout).")

    # 简单验证结果
    first_title_loc = page.locator(".t h4 a").first
    try:
        if await first_title_loc.count() > 0:
             title = (await first_title_loc.inner_text(timeout=5000)).strip()
             print(f"DEBUG: First result title: '{title}'")
    except:
        pass



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


async def extract_visible_records(page: Page, category: str, sub_category: str) -> List[Record]:
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

        out.append(Record(category=category, sub_category=sub_category, title=title, url=url, publish_date=date_to_check))


    return out


async def click_load_more_until_done(
    page: Page,
    seen_keys: set,
    category: str,
    sub_category: str,
    max_items: int,
) -> List[Record]:
    results: List[Record] = []

    async def collect_once() -> int:
        recs = await extract_visible_records(page, category, sub_category)
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
        # Wait for "More" to be strictly visible or enabled
        if await more.count() == 0 or not await more.is_visible():
            break

        print("Clicking 'More'...")
        try:
            await more.scroll_into_view_if_needed()
            await more.click(timeout=5000)
            # User wants 10s pause?
            await page.wait_for_timeout(10000)
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


async def iterate_sub_categories(
    page: Page,
    main_category: str,
    max_items: int,
) -> List[Record]:
    """
    Look for effectiveness level / category facets on the sidebar, iterate through them,
    and rescue records.
    """
    found_records: List[Record] = []
    
    # 策略：
    # 1. 识别左侧所有可能的一级分类 (Class Name + Count)
    # 2. 依次点击 -> 抓取 -> 回到上一状态（或者继续点下一个）
    
    # 获取所有符合 "Text(Number)" 格式的链接
    # 注意：可能会抓到太多无关的。我们需要聚焦。
    # 通常分类在特定的 block 里，比如 "效力级别" (Central) 或 "文件类型" (Local)
    # 这里我们尝试通过文本定位标题，然后找该标题下的列表。
    # 由于页面结构未知，我们尝试普遍撒网但根据关键词过滤。
    
    # 等待链接加载
    try:
        await page.wait_for_selector("a", state="attached", timeout=10000)
    except:
        pass
        
    links = page.locator("a")
    count = await links.count()
    
    targets = [] # List of (name, count) tuples
    check_pattern = re.compile(r"^(.+?)[（\(](\d+)[）\)]$")
    
    print(" scanning sidebar for sub-categories...")
    
    # 收集阶段
    for i in range(count):
        lk = links.nth(i)
        if not await lk.is_visible():
            continue
            
        txt = (await lk.inner_text()).strip()
        m = check_pattern.match(txt)
        if m:
            name = m.group(1).strip()
            # 过滤逻辑
            if re.match(r"^\d{4}$", name): continue # 年份
            if "更多" in name or "全部" in name: continue
            if "收起" in name: continue
            if len(name) > 20: continue # 名字太长可能不是分类
            
            # 排除本身是 "中央法规" "地方法规" 的大类导航
            if name in ["中央法规", "地方法规", "法律法规", "司法解释"]:
                 # 这些可能是顶级类，点击会重置或者进入子页，我们保留，
                 # 但是如果在左侧树状结构里，它们通常是父节点。
                 # 如果我们已经在 "Central" 页面，也许只需要点 "法律", "行政法规"...
                 pass
            
            # 保存名字，后续点击时重新定位
            if name not in [t[0] for t in targets]:
                targets.append((name, int(m.group(2))))

    print(f"Potential sub-categories found: {targets}")
    
    if not targets:
        print("No sub-categories found. Scraping current list.")
        return await click_load_more_until_done(
            page, set(), main_category, "", max_items
        )

    # 遍历阶段
    all_collected_urls = set()
    
    for cat_name, cat_count in targets:
        print(f"\n>>> Processing Sub-Category: {cat_name} (Count: {cat_count})")
        
        # 重新寻找并点击链接
        clicked = False
        page_links = page.locator("a")
        cnt = await page_links.count()
        
        for i in range(cnt):
            lk = page_links.nth(i)
            if await lk.is_visible():
                txt = (await lk.inner_text()).strip()
                # 必须完全匹配之前发现的名字部分 (忽略数字变化，或者不忽略)
                # 最好使用 startswith
                if txt.startswith(cat_name) and "(" in txt:
                    try:
                        await lk.scroll_into_view_if_needed()
                        await lk.click()
                        clicked = True
                        break
                    except Exception as e:
                        print(f"Failed to click {cat_name}: {e}")
                        
        if not clicked:
            print(f"Skipping {cat_name}: Link not found/clickable.")
            continue
            
        print("Waiting 10s for category load...")
        await page.wait_for_timeout(10000)
        
        # 抓取当前分类下的所有每页
        # 这里即使 URL 重复我们也抓取，因为要打上不同的 sub_category 标签
        # (虽然通常一个法规只属于一个效力级别)
        # 如果需要去重，可以在 write_csv时不覆盖 sub_category?
        # 不，用户要的是 dimension。
        
        # 为了避免无限加载，我们只抓取当前分类的，不需要去重全局
        cat_seen = set()
        
        recs = await click_load_more_until_done(
            page, cat_seen, main_category, cat_name, max_items
        )
        
        found_records.extend(recs)
        
        # 完成一个分类后，页面状态变了（被筛选了）。
        # 点击下一个同级分类通常会取消当前、选中下一个。
        # 如果点击失败，可能需要取消当前？
        # 在 pkulaw 左侧，通常都是链接切换。
        # 继续循环即可。如果是层级结构，可能需要 Back?
        # 假设左侧栏始终可见。
        
    return found_records


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
                        sub_category=row.get("sub_category", ""),
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
        w = csv.DictWriter(f, fieldnames=["category", "sub_category", "title", "url", "publish_date"])
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
                
                # Step 1: Go to Home
                await goto_home(page)
                # 首页加载完稍作等待
                await page.wait_for_timeout(5000)
                
                # Step 2: Click Category Tab *BEFORE* Searching
                # As per user request: click "中央法规" or "地方法规" first
                await click_category_nav(page, cat_label)

                # Step 3: Search text
                await search_by_title(page, keyword)
                
                # Step 4: Iterate categories form sidebar and collect
                # This replaces the simple 'click_load_more_until_done'
                
                # Notice: search_by_title already waits for the result list.
                # Now we look for sidebar categories and iterate them.
                
                items_needed = max_items if max_items > 0 else 100
                all_seen_urls = set(r.url for r in all_records)
                
                # 使用改进后的 iterate_sub_categories
                found_recs = await iterate_sub_categories(page, cat_key, items_needed)
                
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
