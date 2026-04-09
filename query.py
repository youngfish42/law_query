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
# Pause (ms) between detail-page requests to avoid overloading the server
DETAIL_PAGE_DELAY_MS = 1000
# Maximum number of children an element may have and still be considered
# a "leaf-ish" label node during DOM traversal for metadata extraction.
# Elements with more children are likely containers, not individual labels.
_LABEL_MAX_CHILDREN = 3


@dataclass
class Record:
    category: str  # "中央法规" | "地方法规" | "立法资料" | "法规解读" | "法律动态"
    title: str
    url: str
    publish_date: str  # YYYY.MM.DD
    issuing_authority: str = ""  # 制定机关
    legal_hierarchy: str = ""   # 效力位阶


PUBLISH_RE = re.compile(r"(\d{4}\.\d{2}(?:\.\d{2})?)\s*公布")

CATEGORY_NAME_MAP = {
    "central": "中央法规",
    "local": "地方法规",
    "legislative_materials": "立法资料",
    "legislative_interpretations": "法规解读",
    "legal_updates": "法律动态",
    "中央法规": "中央法规",
    "地方法规": "地方法规",
    "立法资料": "立法资料",
    "法规解读": "法规解读",
    "法律动态": "法律动态",
}


def normalize_category(value: str) -> str:
    return CATEGORY_NAME_MAP.get((value or "").strip(), (value or "").strip())


def normalize_title(value: str) -> str:
    """标准化标题，用于按标题去重。"""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def merge_record_fields(base: Record, incoming: Record) -> Record:
    """合并同标题记录，优先保留较完整/较新的信息。"""
    if incoming.publish_date and incoming.publish_date > base.publish_date:
        base.publish_date = incoming.publish_date
        if incoming.url:
            base.url = incoming.url
        if incoming.category:
            base.category = normalize_category(incoming.category)

    if not base.url and incoming.url:
        base.url = incoming.url
    if not base.category and incoming.category:
        base.category = normalize_category(incoming.category)
    if not base.issuing_authority and incoming.issuing_authority:
        base.issuing_authority = incoming.issuing_authority
    if not base.legal_hierarchy and incoming.legal_hierarchy:
        base.legal_hierarchy = incoming.legal_hierarchy

    return base


def deduplicate_records_by_title(records: Iterable[Record]) -> List[Record]:
    """按标题去重；同标题时合并字段，避免信息丢失。"""
    merged: dict = {}
    for r in records:
        key = normalize_title(r.title)
        if not key:
            # 标题为空时回退到 URL，避免异常数据相互覆盖。
            key = f"__url__:{r.url}"
        if key in merged:
            merge_record_fields(merged[key], r)
        else:
            merged[key] = Record(
                category=normalize_category(r.category),
                title=normalize_title(r.title) or r.title,
                url=r.url,
                publish_date=r.publish_date,
                issuing_authority=r.issuing_authority,
                legal_hierarchy=r.legal_hierarchy,
            )
    return list(merged.values())


async def goto_home(page: Page) -> None:
    try:
        # 增加超时时间到 60 秒
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"Warning: 第一次尝试打开主页失败: {e}. 重试中...")
        await page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)


async def click_category_nav(page: Page, label: str) -> bool:
    """
    在首页点击分类导航按钮（如"中央法规"、"地方法规"）。
    由于页面加载慢，点击后等待较长时间。
    """
    print(f"正在切换到分类: {label}")

    try:
        # 尝试多种选择器策略从最具体到最通用
        strategies = [
            (f"a:text-is('{label}')", "精确文本匹配"),
            (f"a:has-text('{label}')", "包含文本匹配"),
            (f"a:has-text('{label.strip()}')", "去除空格后的文本匹配"),
        ]

        links = None
        strategy_used = ""

        for selector, desc in strategies:
            try:
                candidate = page.locator(selector)
                count = await candidate.count()
                print(f"DEBUG: 尝试 '{desc}' 选择器: {selector} -> 找到 {count} 个链接")

                if count > 0:
                    links = candidate
                    strategy_used = desc
                    print(f"DEBUG: 使用策略: {desc}")
                    break
            except Exception:
                continue

        if links is None:
            print(f"ERROR: 未找到文本为 '{label}' 的链接。")
            return False

        # 过滤可见的链接
        visible_links = []
        count = await links.count()
        for i in range(count):
            link = links.nth(i)
            try:
                is_visible = await link.is_visible()
                href = await link.get_attribute("href")
                text = await link.inner_text()
                print(f"DEBUG: 链接 {i}: 可见={is_visible}, 文本='{text}', href='{href}'")
                if is_visible:
                    visible_links.append(link)
            except Exception as e:
                print(f"DEBUG: 检查链接 {i} 时出错: {e}")

        if not visible_links:
            print(f"ERROR: 找到 {count} 个链接，但都不可见。")
            return False

        # 选择第一个可见链接
        target_link = visible_links[0]
        target_text = await target_link.inner_text()
        print(f"DEBUG: 选择目标链接: '{target_text}'")

        await target_link.click()
        print(f"已点击分类链接: {label}")

        # 按照用户要求，每步操作后停顿10秒以上
        print("点击分类后等待 12 秒...")
        await page.wait_for_timeout(12000)

        return True
    except Exception as e:
        print(f"ERROR: 切换到分类 '{label}' 时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


async def click_sub_tab(page: Page, label: str) -> bool:
    """
    在搜索结果页中点击子分类标签（如"立法资料"下的"法规解读"）。
    子分类标签文本包含数量后缀，如"法规解读（45）"，需要使用包含匹配。
    """
    print(f"正在切换到子分类: {label}")
    try:
        # 子分类标签是 <li><a href="javascript:void(0)">法规解读（N）</a></li>
        # 使用 has-text 匹配（因为文本包含数量后缀如"法规解读（45）"）
        # 排除搜索结果中标题链接（它们的 href 不是 javascript:void(0)）
        candidate = page.locator(
            f"li > a[href='javascript:void(0)']:has-text('{label}')"
        )
        count = await candidate.count()
        print(f"DEBUG: 子分类 '{label}' 找到 {count} 个候选链接")

        if count == 0:
            # 也尝试 javascript:void(0); 带分号的版本
            candidate = page.locator(
                f"li > a[href='javascript:void(0);']:has-text('{label}')"
            )
            count = await candidate.count()
            print(f"DEBUG: 子分类 '{label}' (带分号) 找到 {count} 个候选链接")

        if count == 0:
            print(f"WARNING: 未找到子分类 '{label}' 的标签。")
            return False

        # 选择第一个可见的
        for i in range(count):
            link = candidate.nth(i)
            if await link.is_visible():
                text = await link.inner_text()
                print(f"DEBUG: 点击子分类标签: '{text}'")
                await link.click()
                print(f"点击子分类后等待 12 秒...")
                await page.wait_for_timeout(12000)
                return True

        print(f"WARNING: 子分类 '{label}' 的标签均不可见。")
        return False
    except Exception as e:
        print(f"ERROR: 切换到子分类 '{label}' 时出错: {e}")
        import traceback
        traceback.print_exc()
        return False


async def search_by_title(page: Page, keyword: str) -> bool:
    print(f"正在检索: {keyword} 关键词相关法规")
    try:
        # 首页/结果页顶部都有同一个检索框
        box = page.locator("input#txtSearch")
        print("DEBUG: 等待搜索框出现...")
        await box.wait_for(state="visible", timeout=30000)
        await box.fill(keyword)
        print(f"DEBUG: 已输入关键词: {keyword}")

        # 输入后稍作停顿
        await page.wait_for_timeout(2000)

        # 点击"检索/新检索"按钮
        btn = page.locator("a#btnSearch")
        print("DEBUG: 等待搜索按钮出现...")
        await btn.wait_for(state="visible", timeout=30000)

        await btn.click()
        print("DEBUG: 已点击搜索按钮")

        # 强制等待，因为网页加载很慢
        print("等待 15 秒加载搜索结果...")
        await page.wait_for_timeout(15000)

        # 等结果区域出现
        print("DEBUG: 等待结果列表元素出现...")
        try:
            await page.locator('input[name="recordList"]').first.wait_for(timeout=30000)
            print("DEBUG: 结果列表元素已出现")
        except Exception:
            print("WARNING: 未检测到结果列表（超时）。")
            # 即使超时，也尝试继续
            record_count = await page.locator('input[name="recordList"]').count()
            print(f"DEBUG: 实际找到的 recordList 元素数: {record_count}")
            if record_count == 0:
                return False

        # 简单验证结果（兼容两种页面布局）
        # 中央法规/地方法规使用 .t h4 a；立法资料/法律动态使用 .list-title h4 a
        first_title_loc = page.locator(".t h4 a, .list-title h4 a").first
        try:
            count = await first_title_loc.count()
            print(f"DEBUG: 找到 {count} 个结果标题")
            if count > 0:
                 title = (await first_title_loc.inner_text(timeout=5000)).strip()
                 print(f"DEBUG: 第一条结果标题: '{title}'")
            return True
        except Exception as e:
            # 只要能看到 list 就认为成功，哪怕 title 读不到
            print(f"DEBUG: 读取标题时出错（但继续）: {e}")
            return True

    except Exception as e:
        print(f"ERROR: 搜索过程中出错: {e}")
        import traceback
        traceback.print_exc()
        return False

async def fetch_detail_info(page: Page, url: str) -> dict:
    """访问法规详情页，获取制定机关和效力位阶。"""
    result = {"issuing_authority": "", "legal_hierarchy": ""}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait until the expected metadata labels appear in the page body,
        # rather than sleeping for a fixed duration.
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body ? document.body.innerText : '';
                    return text.includes('制定机关') ||
                              text.includes('效力位阶');
                }""",
                timeout=5000,
            )
        except Exception:
            # If the expected labels do not appear in time, continue and let the
            # extraction logic attempt to parse whatever content is available.
            pass

        # Use JavaScript to walk the DOM and find label→value pairs.
        # pkulaw.com renders these fields in a table/dl where each label cell
        # is immediately followed (as next sibling or parent's next sibling) by
        # the value cell.
        detail = await page.evaluate(f"""() => {{
            const targets = {{
                '制定机关': 'issuing_authority',
                '效力位阶': 'legal_hierarchy'
            }};
            const result = {{
                issuing_authority: '',
                legal_hierarchy: ''
            }};

            function getText(el) {{
                return el ? el.textContent.trim() : '';
            }}

            const all = document.querySelectorAll('*');
            for (const el of all) {{
                // Only consider "leaf-ish" elements (≤ {_LABEL_MAX_CHILDREN} children) to avoid
                // matching large container elements that include the label text.
                if (el.children.length > {_LABEL_MAX_CHILDREN}) continue;

                const text = getText(el);
                for (const [label, key] of Object.entries(targets)) {{
                    if (result[key]) continue;

                    if (text === label || text === label + '：' || text === label + ':') {{
                        // Try next element sibling first
                        if (el.nextElementSibling) {{
                            const val = getText(el.nextElementSibling);
                            if (val) {{ result[key] = val; break; }}
                        }}
                        // Try parent element's next sibling
                        const parent = el.parentElement;
                        if (parent && parent.nextElementSibling) {{
                            const val = getText(parent.nextElementSibling);
                            if (val) {{ result[key] = val; break; }}
                        }}
                    }}
                }}
            }}
            return result;
        }}""")

        result.update({k: v for k, v in detail.items() if v})
    except Exception as e:
        print(f"获取详情页信息失败 ({url}): {e}")
    return result


def load_existing_records(path: Path) -> dict:
    """从 CSV 文件读取已有记录，返回 url -> Record 字典。"""
    existing: dict = {}
    if not path.exists():
        return existing
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = row.get("url", "")
                if url:
                    category = normalize_category(row.get("category", ""))
                    existing[url] = Record(
                        category=category,
                        title=row.get("title", ""),
                        url=url,
                        publish_date=row.get("publish_date", ""),
                        issuing_authority=row.get("issuing_authority", ""),
                        legal_hierarchy=row.get("legal_hierarchy", ""),
                    )
    except Exception as e:
        print(f"Warning: 读取现有CSV失败: {e}")
    return existing


async def enrich_records_with_details(
    page: Page,
    records: List[Record],
    existing: dict,
) -> None:
    """为每条记录抓取详情页信息（若已有则跳过）。"""
    for r in records:
        # Reuse any detail info that was already fetched in a previous run.
        # Only skip the fetch when all target detail fields are already populated.
        old = existing.get(r.url)
        if old:
            if old.issuing_authority:
                r.issuing_authority = old.issuing_authority
            if old.legal_hierarchy:
                r.legal_hierarchy = old.legal_hierarchy

        if r.issuing_authority and r.legal_hierarchy:
            print(f"复用已有详情: {r.title[:40]}")
            continue

        print(f"获取详情: {r.title[:40]}...")
        detail = await fetch_detail_info(page, r.url)
        r.issuing_authority = r.issuing_authority or detail.get("issuing_authority", "")
        r.legal_hierarchy = r.legal_hierarchy or detail.get("legal_hierarchy", "")
        # Brief pause to be polite to the server
        await page.wait_for_timeout(DETAIL_PAGE_DELAY_MS)


async def apply_this_month_effective_filter(page: Page) -> None:
    # 强制等待一下让页面稳定
    await page.wait_for_timeout(1000)

    # 左侧"相关提示"里点击 "本月生效"
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
    # pkulaw.com 使用两种不同的搜索结果布局：
    # - 中央法规/地方法规: div.col 容器，.t h4 a 标题，div.info 日期
    # - 立法资料/法律动态: div.block 容器，.list-title h4 a 标题，div.related-info 日期
    # 通过同时匹配两种选择器来兼容。

    # 选择所有包含 recordList checkbox 的结果容器
    containers = page.locator(
        "div.col:has(input[name='recordList']), "
        "div.block:has(input[name='recordList'])"
    )
    n = await containers.count()
    print(f"DEBUG: 在分类 '{category}' 中找到 {n} 个结果容器元素")
    out: List[Record] = []

    for i in range(n):
        container = containers.nth(i)

        # 标题链接：兼容两种布局
        a = container.locator(".t h4 a[href], .list-title h4 a[href]").first
        try:
            title = (await a.inner_text(timeout=5000)).strip()
            href = await a.get_attribute("href", timeout=5000)
        except Exception as e:
            print(f"DEBUG: 跳过无效记录 (缺少标题/链接): {e}")
            continue

        if not href:
            continue
        url = href if href.startswith("http") else (BASE_URL + href)

        # 获取整个容器的文本内容用于日期提取
        text = (await container.inner_text()).replace("\xa0", " ")
        # 也尝试从 .info 或 .related-info 获取额外文本
        for info_sel in [".info", ".related-info"]:
            if await container.locator(info_sel).count() > 0:
                info_text = await container.locator(info_sel).inner_text()
                text += " " + info_text

        # 解析日期
        publish_date = ""
        effective_date = ""

        # 匹配公布日期 "YYYY.MM.DD 公布"
        m_pub = PUBLISH_RE.search(text)
        if m_pub:
            publish_date = m_pub.group(1)

        # 匹配实施日期 "YYYY.MM.DD 实施" 或类似
        m_eff = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*(?:实施|生效|施行)", text)
        if m_eff:
            effective_date = m_eff.group(1)

        # 备选：如果没有标注日期，找任意日期（优先 YYYY.MM.DD，其次 YYYY.MM）
        if not publish_date and not effective_date:
             date_m = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
             if date_m:
                 publish_date = date_m.group(1)
             else:
                 # 法规解读等子分类可能只有 "YYYY.MM公布" 格式
                 date_m2 = re.search(r"(\d{4}\.\d{2})(?!\.\d)", text)
                 if date_m2:
                     publish_date = date_m2.group(1)

        # 当前月份前缀
        current_month = datetime.now().strftime("%Y.%m")

        # 我们只返回匹配"本月"的记录
        date_to_check = effective_date if effective_date else publish_date
        if not date_to_check.startswith(current_month):
            print(f"DEBUG: 跳过记录 '{title}' - 日期 {date_to_check} 不在 {current_month} 中")
            continue

        print(f"DEBUG: 添加记录到分类 '{category}': 标题='{title[:40]}', 日期={date_to_check}")
        out.append(Record(category=category, title=title, url=url, publish_date=date_to_check))


    print(f"DEBUG: 分类 '{category}' 共提取 {len(out)} 条本月记录")
    return out


async def click_load_more_until_done(
    page: Page,
    seen_title_keys: set,
    category: str,
    max_items: int,
) -> List[Record]:
    results: List[Record] = []

    async def collect_once() -> int:
        recs = await extract_visible_records(page, category)
        added = 0
        for r in recs:
            key = normalize_title(r.title)
            if key and key not in seen_title_keys:
                seen_title_keys.add(key)
                results.append(r)
                added += 1
        return added

    await collect_once()

    while True:
        if max_items > 0 and len(results) >= max_items:
            break

        # 页面上有很多"更多"，我们只点列表区域里带 icon 的"更多"按钮
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


def filter_records_by_keywords(records: List[Record], keywords: List[str]) -> List[Record]:
    """对记录列表按关键词清单进行二次过滤，仅保留标题中包含至少一个关键词的记录。"""
    normalized = [kw.strip().lower() for kw in keywords if kw.strip()]
    if not normalized:
        return records
    filtered = []
    for r in records:
        title_lower = r.title.lower()
        if any(kw in title_lower for kw in normalized):
            filtered.append(r)
        else:
            print(f"DEBUG: 关键词过滤 - 跳过记录 '{r.title[:50]}' (标题不含任何关键词)")
    return filtered


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
                        category=normalize_category(row.get("category", "")),
                        title=row.get("title", ""),
                        url=row.get("url", ""),
                        publish_date=row.get("publish_date", ""),
                        issuing_authority=row.get("issuing_authority", ""),
                        legal_hierarchy=row.get("legal_hierarchy", ""),
                    )
                    key = normalize_title(r.title)
                    if not key:
                        continue
                    if key in merged_map:
                        merge_record_fields(merged_map[key], r)
                    else:
                        merged_map[key] = r
        except Exception as e:
            print(f"Warning: 读取现有CSV合并失败: {e}")

    # 合并新查询到的数据（按标题去重，并保留/补全详情字段）
    for r in rows:
        key = normalize_title(r.title)
        if not key:
            continue
        if key in merged_map:
            merge_record_fields(merged_map[key], r)
        else:
            merged_map[key] = Record(
                category=normalize_category(r.category),
                title=normalize_title(r.title) or r.title,
                url=r.url,
                publish_date=r.publish_date,
                issuing_authority=r.issuing_authority,
                legal_hierarchy=r.legal_hierarchy,
            )

    # 按 publish_date 降序排序（由新到旧）
    sorted_records = sorted(
        merged_map.values(),
        key=lambda x: x.publish_date,
        reverse=True
    )

    # 写回文件
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["category", "title", "url", "publish_date",
                        "issuing_authority", "legal_hierarchy"],
        )
        w.writeheader()
        for r in sorted_records:
            w.writerow(asdict(r))


def write_json(path: Path, rows: Iterable[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


async def run_enrich_existing(
    out_csv: Path,
    headless: bool,
    slow_mo: int,
    user_data_dir: Optional[Path],
) -> List[Record]:
    """读取 CSV 中已存在但缺少制定机关/效力位阶的条目，访问其超链接补全信息。"""
    existing = load_existing_records(out_csv)
    if not existing:
        print("CSV 文件中没有找到任何记录。")
        return []

    to_enrich = [
        r for r in existing.values()
        if not (r.issuing_authority and r.legal_hierarchy)
    ]

    if not to_enrich:
        print("所有现有记录已包含完整的制定机关/效力位阶信息，无需补全。")
        return list(existing.values())

    print(f"共 {len(existing)} 条现有记录，其中 {len(to_enrich)} 条需要补全详情信息。")

    async with async_playwright() as p:
        launch_kwargs: dict = {
            "headless": headless,
            "slow_mo": slow_mo,
        }

        browser = None
        if user_data_dir:
            context = await p.chromium.launch_persistent_context(
                str(user_data_dir), **launch_kwargs
            )
            page = await context.new_page()
        else:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context()
            page = await context.new_page()

        try:
            for r in to_enrich:
                print(f"补全详情: {r.title[:50]}...")
                detail = await fetch_detail_info(page, r.url)
                r.issuing_authority = r.issuing_authority or detail.get("issuing_authority", "")
                r.legal_hierarchy = r.legal_hierarchy or detail.get("legal_hierarchy", "")
                await page.wait_for_timeout(DETAIL_PAGE_DELAY_MS)
        finally:
            await context.close()
            if browser:
                await browser.close()

    all_records = list(existing.values())
    write_csv(out_csv, all_records)
    print(f"已将补全后的 {len(all_records)} 条记录写回 {out_csv}")
    return all_records


async def run(
    keyword: str,
    out_csv: Path,
    out_json: Optional[Path],
    headless: bool,
    slow_mo: int,
    max_items: int,
    user_data_dir: Optional[Path],
    filter_keywords: Optional[List[str]] = None,
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

            # 加载已有数据，用于跳过已抓取详情的记录
            existing_data = load_existing_records(out_csv)

            # 使用当月作为Python端过滤
            current_month_prefix = datetime.now().strftime("%Y.%m")
            print(f"目标月份: {current_month_prefix}")

            # 定义分类及其标签以匹配标签页
            # nav_needed: 是否需要在首页点击分类标签（"中央法规"是默认分类，无需点击）
            # sub_tabs: 额外需要获取的子分类标签列表，格式为 (子分类key, 子分类标签文本)
            #   - "立法资料"默认显示"草案"子分类，额外获取"法规解读"
            categories = [
                ("central", "中央法规", False, []),
                ("local", "地方法规", True, []),
                ("legislative_materials", "立法资料", True,
                 [("legislative_interpretations", "法规解读")]),
                ("legal_updates", "法律动态", True, []),
            ]

            for cat_key, cat_label, nav_needed, sub_tabs in categories:
                print(f"正在处理分类: {cat_label} ({cat_key})")

                # 第一步: 进入首页
                await goto_home(page)
                # 首页加载完稍作等待
                await page.wait_for_timeout(5000)

                # 第二步: 在首页上点击分类标签
                # "中央法规"默认已选中，无需切换；其余分类需要点击对应标签。
                # 注意：必须在首页上点击分类标签（首页标签文本不含数字后缀），
                # 而非搜索结果页上的标签（标签文本含结果数量如"立法资料(171)"）。
                if nav_needed:
                    nav_ok = await click_category_nav(page, cat_label)
                    if not nav_ok:
                        print(f"跳过分类 '{cat_label}': 导航失败。")
                        continue
                else:
                    print(f"分类 '{cat_label}' 是默认分类。跳过导航。")

                # 第三步: 搜索关键词
                search_ok = await search_by_title(page, keyword)
                if not search_ok:
                    print(f"跳过分类 '{cat_label}': 搜索失败。")
                    continue

                # 第四步: 收集默认子分类的结果
                items_needed = max_items if max_items > 0 else 100
                all_seen_titles = set(normalize_title(r.title) for r in all_records if normalize_title(r.title))

                found_recs = await click_load_more_until_done(page, all_seen_titles, cat_label, max_items=items_needed)

                all_records.extend(found_recs)
                print(f"为 {cat_label} 找到 {len(found_recs)} 条记录")

                # 第五步: 处理额外的子分类标签（如"法规解读"）
                # 在同一个搜索结果页上切换子分类标签并收集结果
                for sub_key, sub_label in sub_tabs:
                    print(f"正在处理子分类: {sub_label} ({sub_key})")
                    sub_ok = await click_sub_tab(page, sub_label)
                    if not sub_ok:
                        print(f"跳过子分类 '{sub_label}': 切换失败。")
                        continue

                    all_seen_titles = set(normalize_title(r.title) for r in all_records if normalize_title(r.title))
                    sub_recs = await click_load_more_until_done(
                        page, all_seen_titles, sub_label, max_items=items_needed
                    )
                    all_records.extend(sub_recs)
                    print(f"为 {sub_label} 找到 {len(sub_recs)} 条记录")

            # 第五步: 访问每条记录的详情页，获取制定机关、效力位阶
            all_records = deduplicate_records_by_title(all_records)
            print(f"按标题去重后待补全详情记录数: {len(all_records)}")
            print(f"开始获取 {len(all_records)} 条记录的详情信息...")
            await enrich_records_with_details(page, all_records, existing_data)

            # 第六步: 按关键词清单对标题进行二次过滤
            effective_filter_keywords = filter_keywords if filter_keywords else [keyword]
            print(f"关键词二次过滤前: {len(all_records)} 条记录，过滤关键词: {effective_filter_keywords}")
            all_records = filter_records_by_keywords(all_records, effective_filter_keywords)
            all_records = deduplicate_records_by_title(all_records)
            print(f"关键词二次过滤后: {len(all_records)} 条记录")

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
    ap.add_argument(
        "--filter-keywords",
        default=None,
        help="标题二次过滤关键词，逗号分隔（默认使用 --keyword 的值）",
    )

    g = ap.add_mutually_exclusive_group()
    g.add_argument("--headless", action="store_true", help="无头模式（默认）")
    g.add_argument("--headed", action="store_true", help="有头模式")

    ap.add_argument("--slow-mo", type=int, default=0, help="操作放慢（毫秒），用于调试")
    ap.add_argument("--max-items", type=int, default=0, help="最多查询多少条（0=不限制）")
    ap.add_argument("--user-data-dir", default=None, help="持久化浏览器目录（用于复用登录态）")
    ap.add_argument(
        "--enrich-existing",
        action="store_true",
        help="仅对 CSV 中已存在但缺少制定机关/效力位阶的条目补全信息（不执行新搜索）",
    )

    return ap.parse_args()


def main() -> None:
    args = parse_args()
    headless = True
    if args.headed:
        headless = False

    out_csv = Path(args.out)
    out_json = Path(args.out_json) if args.out_json else None
    user_data_dir = Path(args.user_data_dir) if args.user_data_dir else None
    filter_keywords = (
        [kw.strip() for kw in args.filter_keywords.split(",") if kw.strip()]
        if args.filter_keywords
        else None
    )

    if args.enrich_existing:
        records = asyncio.run(
            run_enrich_existing(
                out_csv=out_csv,
                headless=headless,
                slow_mo=args.slow_mo,
                user_data_dir=user_data_dir,
            )
        )
        print(f"完成。总记录数: {len(records)}")
        print(f"CSV文件: {out_csv.resolve()}")
        return

    records = asyncio.run(
        run(
            keyword=args.keyword,
            out_csv=out_csv,
            out_json=out_json,
            headless=headless,
            slow_mo=args.slow_mo,
            max_items=args.max_items,
            user_data_dir=user_data_dir,
            filter_keywords=filter_keywords,
        )
    )

    print(f"完成。总记录数: {len(records)}")
    print(f"CSV文件: {out_csv.resolve()}")
    if out_json:
        print(f"JSON文件: {out_json.resolve()}")


if __name__ == "__main__":
    main()
