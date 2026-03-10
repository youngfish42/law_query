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
        # 增加超时时间到 60 秒
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"Warning: 第一次尝试打开主页失败: {e}. 重试中...")
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)


async def click_category_nav(page: Page, label: str) -> bool:
    """
    在首页点击分类导航按钮（如“中央法规”、“地方法规”）。
    由于页面加载慢，点击后等待较长时间。
    """
    print(f"正在切换到分类: {label}")
    
    try:
        # 尝试找到准确文本的链接
        # 首页通常有明显的 "中央法规" 链接
        # 使用 a:text-is 或者 a:has-text，优先精确匹配
        link = page.locator(f"a:has-text('{label}')").first
        
        # 为了防止点到不相关的链接，稍微过滤一下可见性
        if await link.count() == 0:
            print(f"Error: 未找到文本为 '{label}' 的链接。")
            return False

        await link.wait_for(state="visible", timeout=30000)
        await link.click()
        
        # 按照用户要求，每步操作后停顿10秒以上
        print("点击分类后等待 12 秒...")
        await page.wait_for_timeout(12000)

        # 验证是否真的切换了? 
        # 简单起见，只要点击成功且没有报错，就认为成功。
        return True
    except Exception as e:
        print(f"切换到分类 '{label}' 时出错: {e}")
        return False


async def search_by_title(page: Page, keyword: str) -> bool:
    print(f"正在检索: {keyword} 关键词相关法规")
    try:
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
        print("等待 15 秒加载搜索结果...")
        await page.wait_for_timeout(15000)

        # 等结果区域出现
        try:
            await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
        except Exception:
            print("Warning: 未检测到结果列表（超时）。")
            return False

        # 简单验证结果
        first_title_loc = page.locator(".t h4 a").first
        try:
            if await first_title_loc.count() > 0:
                 title = (await first_title_loc.inner_text(timeout=5000)).strip()
                 print(f"DEBUG: 第一条结果标题: '{title}'")
            return True
        except Exception:
            # 只要能看到 list 就认为成功，哪怕 title 读不到
            return True
            
    except Exception as e:
        print(f"搜索过程中出错: {e}")
        return False



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
                print(f"点击可见的过滤链接失败: {e}")

    if clicked:
        # 点击后等待刷新
        await page.wait_for_timeout(2000)
        await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
    else:
        print("Warning: 未找到或无法点击 '本月生效' 过滤链接。")


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
            # 如果提供了用户关键字，通常假设它是校验有效性的必要条件
            # 除非搜索失败，但如果搜索成功，关键字应该在标题中。
            # 但有时它只在内容中。然而，如果结果完全不相关（如“2026 债券”），则搜索肯定失败了。
            # 但这里不能太严格。“智能”可能在正文中。
            href = await a.get_attribute("href", timeout=5000)
        except Exception as e:
            print(f"跳过无效记录 (缺少标题/链接): {e}")
            continue

        if not href:
            continue
        url = href if href.startswith("http") else (BASE_URL + href)

        text = (await col.inner_text()).replace("\u00a0", " ")
        if await col.locator(".info").count() > 0:
            info_text = await col.locator(".info").inner_text()
            text += " " + info_text
            
        # 解析日期
        publish_date = ""
        effective_date = ""
        
        # 匹配公布日期 "YYYY.MM.DD 公布"
        m_pub = PUBLISH_RE.search(text)
        if m_pub:
            publish_date = m_pub.group(1)
            
        # 匹配实施日期 "YYYY.MM.DD 实施" 或类似
        # 如果没有标签，我们可能猜测是否有另一个日期。
        # 但我们找 "实施" 或 "生效"
        m_eff = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*(?:实施|生效|施行)", text)
        if m_eff:
            effective_date = m_eff.group(1)
        
        # 备选：如果没有标注日期，随便找个日期？
        if not publish_date and not effective_date:
             date_m = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
             if date_m:
                 publish_date = date_m.group(1) # 假设找到的第一个日期是公布日期
        
        # 如果用户想要“本月生效”，我们检查 effective_date（如果找到），否则检查 publish_date。
        # 当前月份前缀
        current_month = datetime.now().strftime("%Y.%m")
        
        # 严格过滤逻辑：
        # 如果有生效日期，检查它。
        # 如果没有，检查公布日期（通常是同一个月）。
        # 我们只返回匹配“本月”的记录
        
        date_to_check = effective_date if effective_date else publish_date
        if not date_to_check.startswith(current_month):
            # 记录不是本月的。跳过。
            print(f"跳过记录 {title} - 日期 {date_to_check} 不在 {current_month} 中")
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
        await page.wait_for_timeout(20000) # 增加到20秒以便AJAX加载
        added = await collect_once()
        print(f"更多加载: +{added} 条记录")

        # 如果本轮没有新增，认为加载结束，避免死循环
        #（网站可能返回同一批内容）
        if added == 0:
            # 也许重试一次？
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
            print(f"Warning: 读取现有CSV合并失败: {e}")

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
            
            # 使用当月作为Python端过滤
            current_month_prefix = datetime.now().strftime("%Y.%m")
            print(f"目标月份: {current_month_prefix}")

            # 定义分类及其标签以匹配标签页
            categories = [("central", "中央法规"), ("local", "地方法规")]
            
            for cat_key, cat_label in categories:
                print(f"正在处理分类: {cat_label} ({cat_key})")
                
                # 第一步: 进入首页
                await goto_home(page)
                # 首页加载完稍作等待
                await page.wait_for_timeout(5000)
                
                # 第二步: 搜索前点击分类标签
                # "中央法规"无需点击，首页默认即是；"地方法规"需要点击切换
                if cat_key == "local":
                    nav_ok = await click_category_nav(page, cat_label)
                    if not nav_ok:
                        print(f"跳过分类 '{cat_label}': 导航失败。")
                        continue
                else:
                    print(f"分类 '{cat_label}' 是默认分类。跳过导航。")

                # 第三步: 搜索文本
                # 仅当导航成功后
                search_ok = await search_by_title(page, keyword)
                if not search_ok:
                    print(f"跳过分类 '{cat_label}': 搜索失败。")
                    continue
                
                # 第四步: 收集结果
                # 我们跳过 'apply_this_month_effective_filter' 以为它不可靠/重置搜索。
                # 如果用户脚本必须按 '生效日期' 过滤，我们应该抓取该日期。
                # 然而，抓取 '生效日期' 需要从结果列表中读取更多细节。
                # 标准结果项文本通常跨越 '公布日期' 和 '生效日期'。
                
                # 我们多抓取一些项目以增加找到匹配项的几率
                items_needed = max_items if max_items > 0 else 100 
                
                # 过滤全局已见的键?
                # records from central vs local might overlap if search is global?
                # 如果搜索是全局的，我们会得到重复项。
                # 如果标签有效，我们将得到不同的集合。
                # 使用集合去重。
                all_seen_urls = set(r.url for r in all_records)
                
                found_recs = await click_load_more_until_done(page, all_seen_urls, cat_key, max_items=items_needed)
                
                # 如果关键字没有找到任何内容，也许验证一下？
                # 但让我们假设搜索返回正常。
                
                all_records.extend(found_recs)
                print(f"为 {cat_label} 找到 {len(found_recs)} 条记录")
                
            # 输出
            write_csv(out_csv, all_records)
            if out_json:
                write_json(out_json, all_records)

            return all_records
        finally:
            await context.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="查询法规信息并带交互式过滤")
    ap.add_argument("--keyword", default="智能", help="检索词（默认：智能）")
    ap.add_argument("--out", default="法规.csv", help="输出 CSV 路径")
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

    print(f"完成。总记录数: {len(records)}")
    print(f"CSV文件: {out_csv.resolve()}")
    if out_json:
        print(f"JSON文件: {out_json.resolve()}")


if __name__ == "__main__":
    main()
