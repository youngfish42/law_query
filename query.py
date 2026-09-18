import argparse
import asyncio
import calendar
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, List, Optional

try:
    from playwright.async_api import async_playwright, Page
except ImportError:
    # MCP 模式（--source mcp）仅用标准库，允许在未安装 playwright 的环境中运行
    async_playwright = None
    Page = Any


CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    """返回北京时间（Asia/Shanghai）的当前时间，避免 CI 使用 UTC 时错判‘本月’。"""
    return datetime.now(CN_TZ)


BASE_URL = "https://www.pkulaw.com"
# Pause (ms) between detail-page requests to avoid overloading the server
DETAIL_PAGE_DELAY_MS = 1000

# 北大法宝 MCP 服务（streamable HTTP，JSON-RPC）。授权码从环境变量读取，不落盘。
MCP_LAW_ENDPOINT = "https://apim-gateway.pkulaw.com/mcp-law"
MCP_TOKEN_ENV = "PKULAW_MCP_TOKEN"
MCP_REQUEST_TIMEOUT_S = 60
# get_law_list 的时效性过滤：排除废止/失效/已被修改的条目
MCP_TIMELINESS = ["现行有效", "尚未施行"]
# 相邻两次 MCP 调用的间隔（秒），保持礼貌请求节奏
MCP_CALL_DELAY_S = 0.3
# 每个检索字段（标题/全文）在自适应日期窗拆分下的最大调用次数，防止异常失控
MCP_MAX_WINDOW_CALLS = 30
# get_law_list 每次调用的大致积分消耗（用于回填模式的预算换算）
MCP_POINTS_PER_CALL = 25
# 回填进度状态文件（随仓库提交，记录已完成的 月份|关键词 组合）
MCP_BACKFILL_STATE_PATH = Path("mcp_backfill_state.json")
# MCP 原始数据全字段落盘文件（JSONL，每行一个 get_law_list 原始 item + __meta）
MCP_JSONL_DEFAULT_PATH = Path("法规_mcp.jsonl")
# 旧版 MCP 结果文件（一次性迁移到 JSONL 后不再写入）
MCP_LEGACY_CSV_PATH = Path("法规_mcp.csv")
# MCP 派生记录与浏览器抓取结果融合后的统一 CSV
MERGED_CSV_PATH = Path("法规.csv")

# 反 WAF 人机验证（“访问安全验证”拦截页）所需的浏览器参数。
# 站点对 headless Chromium 指纹（navigator.webdriver 等）返回 567 拦截页，
# 此处统一覆盖为普通桌面 Chrome 的特征。不含第三方依赖。
_STEALTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
"""
_STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Maximum number of children an element may have and still be considered
# a "leaf-ish" label node during DOM traversal for metadata extraction.
# Elements with more children are likely containers, not individual labels.
_LABEL_MAX_CHILDREN = 3


@dataclass
class Record:
    category: str  # "中央法规" | "地方法规" | "立法资料" | "法规解读" | "法律动态"
    title: str
    url: str
    publish_date: str  # YYYY.MM.DD，来源于详情/列表的“公布”日期
    issuing_authority: str = ""  # 制定机关
    legal_hierarchy: str = ""   # 效力位阶
    effective_date: str = ""    # YYYY.MM.DD，来源于列表的“施行/实施/生效”日期
    source: str = "browser"     # 数据来源：browser | mcp
    timeliness: str = ""        # 时效性（MCP TimelinessDic，；连接）
    document_no: str = ""       # 发文字号（MCP DocumentNO）
    subject_tags: str = ""      # 主题分类（MCP Category 数组，；连接）


PUBLISH_RE = re.compile(r"(\d{4}\.\d{2}(?:\.\d{2})?)\s*公布")

# 兼容性日期抓取：站点当前统一使用 YYYY.MM.DD/YYYY.MM，这里额外识别少见的
# YYYY-MM-DD / YYYY/MM/DD 字面，返回时统一为点号格式。仅在主正则未命中时兜底。
_ALT_DATE_LABELED_RE = re.compile(
    r"(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?\s*(公布|发布|印发|施行|实施|生效)"
)


def normalize_date_token(year: str, month: str, day: str = "") -> str:
    """把 (YYYY, M, D) 拼成规范化的 'YYYY.MM.DD' 或 'YYYY.MM'。
    月/日不足两位时前补 0；若 day 为空则只返回 'YYYY.MM'。"""
    y = year.strip()
    m = month.strip().zfill(2)
    if day and day.strip():
        return f"{y}.{m}.{day.strip().zfill(2)}"
    return f"{y}.{m}"

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

URL_PATH_TO_CATEGORY = {
    "chl": "中央法规",
    "lar": "地方法规",
    "news": "法律动态",
    "protocol": "立法资料",
    "lawexplanation": "法规解读",
}


def normalize_category(value: str) -> str:
    return CATEGORY_NAME_MAP.get((value or "").strip(), (value or "").strip())


def normalize_title(value: str) -> str:
    """标准化标题（用于展示）：压缩多余空白为单空格。"""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def title_dedup_key(value: str) -> str:
    """生成去重键：仅做保守归一，用于识别‘明显是同一条但排版微异’的标题。

    保守策略（不合并任何语义上不同的条目）：
    1. NFKC 归一：把全角字母/数字/半角括号统一为半角（例如"（"→"("、"Ａ"→"A"）；
    2. 去除所有空白字符（含全角空格 U+3000）。
    不做书名号/中文括号 → 半角的替换，不删除任何标点，避免把"《A》"与"「A」"
    这类可能真的不同的题名合并。
    """
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value.replace("\u3000", ""))
    return re.sub(r"\s+", "", normalized)


def url_path_key(url: str) -> str:
    """提取 URL 的 host+path（忽略 query/fragment），作为同一篇法规的稳定标识。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if not p.netloc or not p.path:
            return ""
        return f"{p.netloc}{p.path}".lower().rstrip("/")
    except Exception:
        return ""


def enforce_category_by_url(category: str, url: str) -> str:
    """URL 第一段（chl/lar/news/protocol/lawexplanation）是北大法宝的确定性分类，
    若与传入 category 矛盾，以 URL 为准并打印警告。"""
    if not url:
        return normalize_category(category)
    try:
        from urllib.parse import urlparse
        seg = urlparse(url).path.strip("/").split("/", 1)[0].lower()
    except Exception:
        return normalize_category(category)
    expected = URL_PATH_TO_CATEGORY.get(seg)
    current = normalize_category(category)
    if expected and current and expected != current:
        print(f"Warning: 分类与 URL 段不一致，已校正 '{current}' -> '{expected}' ({url})")
        return expected
    return expected or current


def merge_record_fields(base: Record, incoming: Record) -> Record:
    """合并同记录字段，优先保留较完整/较新的信息。"""
    if incoming.publish_date and incoming.publish_date > base.publish_date:
        base.publish_date = incoming.publish_date
        if incoming.url:
            base.url = incoming.url
        if incoming.category:
            base.category = enforce_category_by_url(incoming.category, incoming.url or base.url)

    if not base.url and incoming.url:
        base.url = incoming.url
    if not base.category and incoming.category:
        base.category = enforce_category_by_url(incoming.category, incoming.url or base.url)
    if not base.issuing_authority and incoming.issuing_authority:
        base.issuing_authority = incoming.issuing_authority
    elif incoming.source == "mcp" and len(incoming.issuing_authority) > len(base.issuing_authority):
        # MCP 现在保留全量制定机关，更完整的值覆盖旧的截断值
        base.issuing_authority = incoming.issuing_authority
    if not base.legal_hierarchy and incoming.legal_hierarchy:
        base.legal_hierarchy = incoming.legal_hierarchy
    elif incoming.source == "mcp" and len(incoming.legal_hierarchy) > len(base.legal_hierarchy):
        base.legal_hierarchy = incoming.legal_hierarchy
    if not base.effective_date and incoming.effective_date:
        base.effective_date = incoming.effective_date
    # 融合语义：浏览器记录被 MCP 数据命中后升级为 mcp，其余情况保留 base.source
    if base.source == "browser" and incoming.source == "mcp":
        base.source = "mcp"
    # 富字段：base 空则补；MCP 来源且更长则覆盖（重扫富化旧数据）。
    # timeliness 值域小、状态翻转（尚未施行→现行有效）语义重要，等长不同值也要更新。
    for field in ("timeliness", "document_no", "subject_tags"):
        new_val = getattr(incoming, field)
        old_val = getattr(base, field)
        if not old_val and new_val:
            setattr(base, field, new_val)
        elif incoming.source == "mcp" and new_val and (
            len(new_val) > len(old_val)
            or (field == "timeliness" and new_val != old_val)
        ):
            setattr(base, field, new_val)

    # 同步根据当前 URL 复核 base.category，纠正历史脏数据。
    base.category = enforce_category_by_url(base.category, base.url)
    return base


def _merge_into_maps(record: Record, by_title: dict, by_url: dict) -> None:
    """双 key 合并辅助：先按 URL path 合，再按标题（去全部空白）合，最后回填两个索引。"""
    record.category = enforce_category_by_url(record.category, record.url)
    record.issuing_authority = infer_authority_for_news(record)

    tkey = title_dedup_key(record.title)
    ukey = url_path_key(record.url)

    target: Optional[Record] = None
    if ukey and ukey in by_url:
        target = by_url[ukey]
    elif tkey and tkey in by_title:
        target = by_title[tkey]

    if target is None:
        # 新条目：标题归一为展示用单空格写法；类别按 URL 复核。
        new_rec = Record(
            category=enforce_category_by_url(record.category, record.url),
            title=normalize_title(record.title) or record.title,
            url=record.url,
            publish_date=record.publish_date,
            issuing_authority=record.issuing_authority,
            legal_hierarchy=record.legal_hierarchy,
            effective_date=record.effective_date,
            source=record.source,
            timeliness=record.timeliness,
            document_no=record.document_no,
            subject_tags=record.subject_tags,
        )
        if tkey:
            by_title[tkey] = new_rec
        if ukey:
            by_url[ukey] = new_rec
        # 同时把无 key 的条目也放进 by_title 兜底，避免相互覆盖。
        if not tkey and not ukey:
            by_title[f"__noid__:{id(new_rec)}"] = new_rec
        return

    merge_record_fields(target, record)
    # 合并后双向回填，避免后续命中另一 key 时再次创建。
    if tkey and tkey not in by_title:
        by_title[tkey] = target
    if ukey and ukey not in by_url:
        by_url[ukey] = target


def deduplicate_records_by_title(records: Iterable[Record]) -> List[Record]:
    """按 (URL path, 标题去全部空白) 双 key 去重，同记录时合并字段、不丢信息。"""
    by_title: dict = {}
    by_url: dict = {}
    for r in records:
        _merge_into_maps(r, by_title, by_url)
    # 用 id 去重得到唯一 Record 列表（多个 key 可能指向同一对象）。
    seen = set()
    out: List[Record] = []
    for rec in list(by_title.values()) + list(by_url.values()):
        if id(rec) in seen:
            continue
        seen.add(id(rec))
        out.append(rec)
    return out


# === 法律动态（/news/）等条目的轻量字段推断 ===
# 优先尝试“标题前缀”匹配（准确度最高），若失败再退而求其次在标题任意位置寻找机关名。
_NEWS_AUTHORITY_PREFIX_PATTERNS = [
    re.compile(r"^(国家[\u4e00-\u9fa5]{2,12}?(?:总局|局|委员会|办公室|部|署|院))"),
    re.compile(r"^(最高人民(?:法院|检察院))"),
    re.compile(r"^([\u4e00-\u9fa5]{2,4}省[\u4e00-\u9fa5]{2,15}?(?:厅|局|委员会|办公室|人民政府|政府|法院|检察院))"),
    re.compile(r"^([\u4e00-\u9fa5]{2,4}市[\u4e00-\u9fa5]{2,15}?(?:厅|局|委员会|办公室|人民政府|政府|法院|检察院|互联网法院))"),
    re.compile(r"^([\u4e00-\u9fa5]{2,10}自治区[\u4e00-\u9fa5]{0,15}?(?:厅|局|委员会|办公室|人民政府|政府|法院|检察院)?)"),
    re.compile(r"^([\u4e00-\u9fa5]{2,15}?(?:部|委员会|办公室|总局|总署))"),
]
# 省/直辖市/自治区简称，用于识别“辽宁出台…”“云南推广…”这类以地区简称开头、
# 紧跟动作词的地方新闻（不含“省/市/自治区”后缀）。
_PROVINCE_ALIASES = (
    "北京|上海|天津|重庆|"
    "河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|"
    "广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|"
    "内蒙古|广西|西藏|宁夏|新疆|香港|澳门"
)
# 主谓句式：机关名后紧跟“发布/印发/公布/出台/召开/组织/联合/答记者问”等动作词。
_NEWS_AUTHORITY_INLINE_PATTERNS = [
    re.compile(r"([\u4e00-\u9fa5]{2,8}(?:互联网法院|人民法院|人民检察院|法院|检察院))"),
    re.compile(r"([\u4e00-\u9fa5]{2,6}(?:市政府|省政府|人民政府))"),
    re.compile(r"([\u4e00-\u9fa5]{2,4}(?:省|市|自治区|特别行政区)(?:人民政府|政府|司法厅|司法局|教育厅|教育局|工业和信息化厅|工业和信息化局|大数据管理局|通信管理局)?)(?=\S*?(?:发布|印发|公布|出台|召开|通过))"),
    re.compile(r"(国务院[\u4e00-\u9fa5]{0,10}(?:办公厅|办公室)?)(?=\S*?(?:发布|印发|公布|出台|决定))"),
    re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:总局|总署|部|委员会|办公厅|办公室))(?=[、，,]?\s*[\u4e00-\u9fa5]*(?:发布|印发|公布|出台|联合))"),
    re.compile(
        r"^(" + _PROVINCE_ALIASES + r")"
        r"(?=[\u4e00-\u9fa5]*?(?:出台|印发|发布|公布|推广|推进|启动|部署|开展|上线|通过|六大|十大|三大|五大))"
    ),
]


def infer_authority_for_news(record: Record) -> str:
    """仅当 category 为「法律动态」且 issuing_authority 为空时，
    从标题正则推断机关；推断不到则保持空，不写入猜测数据。"""
    if record.issuing_authority:
        return record.issuing_authority
    if normalize_category(record.category) != "法律动态":
        return record.issuing_authority
    title = (record.title or "").strip()
    if not title:
        return ""
    for pat in _NEWS_AUTHORITY_PREFIX_PATTERNS:
        m = pat.match(title)
        if m:
            return m.group(1)
    for pat in _NEWS_AUTHORITY_INLINE_PATTERNS:
        m = pat.search(title)
        if m:
            return m.group(1)
    return ""


def _require_playwright() -> None:
    if async_playwright is None:
        raise SystemExit(
            "浏览器模式需要安装 playwright："
            "pip install -r requirements.txt && playwright install chromium"
        )


async def new_stealth_context(
    p,
    headless: bool,
    slow_mo: int,
    user_data_dir: Optional[Path] = None,
):
    """创建带反 WAF 特征的浏览器 context（站点会拦截裸 headless Chromium）。

    返回 (context, browser)；browser 可能为 None（持久化 context 模式），
    关闭时统一调用 close_browser_context(context, browser)。"""
    launch_kwargs = {
        "headless": headless,
        "slow_mo": slow_mo,
        "args": _STEALTH_LAUNCH_ARGS,
    }
    if user_data_dir:
        context = await p.chromium.launch_persistent_context(
            str(user_data_dir), **launch_kwargs
        )
        browser = None
    else:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=_STEALTH_USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
    await context.add_init_script(_STEALTH_INIT_SCRIPT)
    return context, browser


async def close_browser_context(context, browser) -> None:
    if context:
        await context.close()
    if browser:
        await browser.close()


async def _is_waf_blocked(page: Page) -> bool:
    """检测是否命中 WAF 人机验证页（HTTP 567“访问安全验证”拦截）。"""
    try:
        if await page.locator("input#txtSearch").count() > 0:
            return False
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        return "安全验证" in text or "访问频率" in text
    except Exception:
        return False


class WafBlockedError(RuntimeError):
    """主页被 WAF 人机验证拦截（HTTP 567“访问安全验证”页）。"""


async def _goto_home_once(page: Page):
    try:
        # 增加超时时间到 60 秒
        return await page.goto(
            BASE_URL + "/", wait_until="domcontentloaded", timeout=60000
        )
    except Exception as e:
        print(f"Warning: 第一次尝试打开主页失败: {e}. 重试中...")
        return await page.goto(
            BASE_URL + "/", wait_until="domcontentloaded", timeout=60000
        )


async def goto_home(page: Page) -> None:
    resp = await _goto_home_once(page)
    status = resp.status if resp else None
    blocked = (status and not (200 <= status < 300)) or await _is_waf_blocked(page)
    if not blocked:
        return
    reason = (
        f"主页返回异常状态码 {status}（疑似 WAF 拦截）"
        if status and not (200 <= status < 300)
        else "主页被 WAF 人机验证拦截（“访问安全验证”页）"
    )
    print(f"WARNING: {reason}，等待 {_WAF_RETRY_WAIT_MS / 1000:.0f}s 后重试...")
    await page.wait_for_timeout(_WAF_RETRY_WAIT_MS)
    resp = await _goto_home_once(page)
    status = resp.status if resp else None
    blocked = (status and not (200 <= status < 300)) or await _is_waf_blocked(page)
    if blocked:
        raise WafBlockedError(f"{reason}，重试后仍被拦截。")


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

async def _page_hit_waf(page: Page) -> bool:
    """页面正文是否为 WAF「访问安全验证」拦截页。"""
    try:
        text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        return "安全验证" in text or "访问频率" in text
    except Exception:
        return False


# 详情页命中 WAF 拦截后的重试：等待并刷新一次，仍被拦则放弃（留待下次运行补全）。
_WAF_RETRY_WAIT_MS = 15000

async def fetch_detail_info(page: Page, url: str) -> dict:
    """访问法规详情页，获取制定机关和效力位阶。

    对「法律动态」详情页（URL 含 /news/）而言，页面没有“制定机关/效力位阶”字段，
    但通常带有“新闻来源：XXX”这一稳定信息，用作 issuing_authority；
    legal_hierarchy 对法律动态而言无意义，返回空即可。"""
    result = {"issuing_authority": "", "legal_hierarchy": ""}
    is_news = "/news/" in url
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 详情页在高频访问时也会单独被 WAF 挑战（HTTP 200 + 验证页正文），
        # 识别后等待重试一次，避免把空字段误当作“该页面无此信息”。
        if await _page_hit_waf(page):
            print(f"WARNING: 详情页命中 WAF 拦截，等待重试: {url}")
            await page.wait_for_timeout(_WAF_RETRY_WAIT_MS)
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2000)
            if await _page_hit_waf(page):
                print(f"WARNING: 详情页重试后仍被 WAF 拦截，跳过: {url}")
                return result

        # Wait until the expected metadata labels appear in the page body,
        # rather than sleeping for a fixed duration.
        wait_expr = (
            "() => { const text = document.body ? document.body.innerText : '';"
            " return text.includes('新闻来源') || text.includes('新闻分类'); }"
            if is_news
            else "() => { const text = document.body ? document.body.innerText : '';"
            " return text.includes('制定机关') || text.includes('效力位阶'); }"
        )
        try:
            await page.wait_for_function(wait_expr, timeout=5000)
        except Exception:
            # If the expected labels do not appear in time, continue and let the
            # extraction logic attempt to parse whatever content is available.
            pass

        if is_news:
            source = await page.evaluate(
                """() => {
                    const items = document.querySelectorAll('li,dt,dd,span,p,div');
                    for (const el of items) {
                        if (el.children.length > 3) continue;
                        const t = el.innerText ? el.innerText.trim() : '';
                        if (!t) continue;
                        const m = t.match(/^新闻来源[：:]\\s*(.+)$/);
                        if (m) return m[1].trim();
                    }
                    return '';
                }"""
            )
            if source:
                result["issuing_authority"] = source
            return result

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
                        effective_date=row.get("effective_date", ""),
                        source=row.get("source", "") or "browser",
                        timeliness=row.get("timeliness", ""),
                        document_no=_clean_document_no(row.get("document_no", "")),
                        subject_tags=row.get("subject_tags", ""),
                    )
    except Exception as e:
        print(f"Warning: 读取现有CSV失败: {e}")
    return existing


async def enrich_records_with_details(
    page: Page,
    records: List[Record],
    existing: dict,
) -> None:
    """为每条记录抓取详情页信息（若已有则跳过）。

    「法律动态」类目仅关心 issuing_authority；legal_hierarchy 在该类目页面不存在，
    不作为“未完成”的判据，以免每次都重复访问 news 详情页。"""
    for r in records:
        # Reuse any detail info that was already fetched in a previous run.
        # Only skip the fetch when all target detail fields are already populated.
        old = existing.get(r.url)
        if old:
            if old.issuing_authority:
                r.issuing_authority = old.issuing_authority
            if old.legal_hierarchy:
                r.legal_hierarchy = old.legal_hierarchy

        is_news = normalize_category(r.category) == "法律动态"
        already_done = bool(r.issuing_authority) if is_news else bool(
            r.issuing_authority and r.legal_hierarchy
        )
        if already_done:
            print(f"复用已有详情: {r.title[:40]}")
            continue

        print(f"获取详情: {r.title[:40]}...")
        detail = await fetch_detail_info(page, r.url)
        r.issuing_authority = r.issuing_authority or detail.get("issuing_authority", "")
        r.legal_hierarchy = r.legal_hierarchy or detail.get("legal_hierarchy", "")
        # Brief pause to be polite to the server
        await page.wait_for_timeout(DETAIL_PAGE_DELAY_MS)


async def extract_visible_records(page: Page, category: str, month_prefix: str) -> List[Record]:
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

        # 匹配公布日期 "YYYY.MM.DD 公布" 或 "YYYY.MM 公布"
        m_pub = PUBLISH_RE.search(text)
        if m_pub:
            publish_date = m_pub.group(1)

        # 匹配实施日期 "YYYY.MM.DD 实施/生效/施行"，兼容 "YYYY.MM 施行"
        m_eff = re.search(r"(\d{4}\.\d{2}(?:\.\d{2})?)\s*(?:实施|生效|施行)", text)
        if m_eff:
            effective_date = m_eff.group(1)

        # 兼容兜底：若主正则未命中（例如站点偶发使用 YYYY-MM-DD/YYYY/MM/DD），
        # 从带标签的日期文本中提取，并统一转换为 YYYY.MM.DD 点号格式。
        if not publish_date or not effective_date:
            for y, mo, d, label in _ALT_DATE_LABELED_RE.findall(text):
                token = normalize_date_token(y, mo, d)
                if label in ("公布", "发布", "印发") and not publish_date:
                    publish_date = token
                elif label in ("施行", "实施", "生效") and not effective_date:
                    effective_date = token

        # 备选：如果没有标注日期，找任意日期（优先 YYYY.MM.DD，其次 YYYY.MM）
        # 注意：这里只回填 publish_date，绝不把无标签日期塞给 effective_date，
        # 避免"实施日期"污染"公布日期"。
        if not publish_date and not effective_date:
             date_m = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
             if date_m:
                 publish_date = date_m.group(1)
             else:
                 # 法规解读等子分类可能只有 "YYYY.MM公布" 格式
                 date_m2 = re.search(r"(\d{4}\.\d{2})(?!\.\d)", text)
                 if date_m2:
                     publish_date = date_m2.group(1)

        # 目标月份前缀（默认当月，可用 --month 指定历史月份做回填）
        current_month = month_prefix

        # “本月”判定：优先看施行日期（若已到 CI 当月生效），否则看公布日期。
        # 但写入 Record 的 publish_date 严格来自“公布”匹配，effective_date 独立保留，
        # 避免下游把施行日期当成公布日期。
        date_to_check = effective_date if effective_date else publish_date
        if not date_to_check.startswith(current_month):
            print(f"DEBUG: 跳过记录 '{title}' - 日期 {date_to_check} 不在 {current_month} 中")
            continue

        print(
            f"DEBUG: 添加记录到分类 '{category}': 标题='{title[:40]}', "
            f"公布={publish_date or '-'}, 施行={effective_date or '-'}"
        )
        out.append(Record(
            category=category,
            title=title,
            url=url,
            publish_date=publish_date,
            effective_date=effective_date,
        ))


    print(f"DEBUG: 分类 '{category}' 共提取 {len(out)} 条本月记录")
    return out


async def click_load_more_until_done(
    page: Page,
    seen_title_keys: set,
    category: str,
    max_items: int,
    month_prefix: str,
    max_click_rounds: int = 20,
) -> List[Record]:
    """连续点击列表页的“更多”按钮，直到没有新增或触及安全上限。

    max_click_rounds 是保护性上限，避免网站在某些异常状况下返回同一批数据
    却仍旧显示“更多”按钮，导致本函数无限循环。默认 20 轮足以覆盖数百条记录。
    """
    results: List[Record] = []

    async def collect_once() -> int:
        recs = await extract_visible_records(page, category, month_prefix)
        added = 0
        for r in recs:
            key = title_dedup_key(r.title)
            if key and key not in seen_title_keys:
                seen_title_keys.add(key)
                results.append(r)
                added += 1
        return added

    await collect_once()

    rounds = 0
    while True:
        if max_items > 0 and len(results) >= max_items:
            break
        if rounds >= max_click_rounds:
            print(
                f"Warning: 已达‘更多’按钮最大点击轮次 {max_click_rounds}，停止翻页以避免死循环。"
            )
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

        rounds += 1
        # 等待新内容加载：recordList 数量变化或稍等
        await page.wait_for_timeout(20000) # 增加到20秒以便AJAX加载
        added = await collect_once()
        print(f"更多加载: +{added} 条记录 (第 {rounds}/{max_click_rounds} 轮)")

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


# === 北大法宝 MCP 服务（JSON-RPC over streamable HTTP，仅 stdlib） ===

class McpUnavailableError(RuntimeError):
    """MCP 服务当日不可用（积分不足 / Token 无效 / 服务未开通），重试无意义。"""


# 官方《错误处理指南》：401=Token 无效或过期；403=服务未开通/已过期；
# 429=频率超限或配额（积分）已用完；402 兜底。响应体含积分类关键字同样视为不可用。
_MCP_UNAVAILABLE_STATUS = {401, 402, 403, 429}
_MCP_UNAVAILABLE_KEYWORDS = ("积分", "quota", "余额")

# 退出码约定：0=成功；2=MCP 不可用（确定性错误，当日重试无意义）；1=其他（瞬时）错误
EXIT_MCP_UNAVAILABLE = 2


def _mcp_rpc(token: str, method: str, params: Optional[dict], req_id: Optional[int]) -> dict:
    """向 MCP 端点发送一条 JSON-RPC 消息并返回响应（响应可能是 JSON 或 SSE）。"""
    message: dict = {"jsonrpc": "2.0", "method": method}
    if req_id is not None:
        message["id"] = req_id
    if params is not None:
        message["params"] = params
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MCP_LAW_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=MCP_REQUEST_TIMEOUT_S) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        if e.code in _MCP_UNAVAILABLE_STATUS or any(
            k in detail for k in _MCP_UNAVAILABLE_KEYWORDS
        ):
            raise McpUnavailableError(f"HTTP {e.code}: {detail}") from e
        if e.code == 400:
            raise RuntimeError(f"MCP 请求参数错误（HTTP 400）: {detail}") from e
        raise RuntimeError(f"MCP 请求失败（HTTP {e.code}）: {detail}") from e
    if req_id is None:
        return {}
    if "text/event-stream" in content_type:
        # SSE：逐行扫描 data: 负载，取与本请求 id 匹配的那条响应。
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                return msg
        raise RuntimeError(f"MCP 响应中未找到 id={req_id} 的结果: {raw[:200]}")
    return json.loads(raw)


def _month_implement_range(month_prefix: str) -> tuple:
    """由 'YYYY.MM' 生成当月施行日期范围（'YYYY.M.D' 非补零，与接口文档示例一致）。"""
    year, month = (int(part) for part in month_prefix.split("."))
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}.{month}.1", f"{year}.{month}.{last_day}"


def _recent_windows(today: datetime, days: int) -> List[tuple]:
    """把“最近 N 天 ~ 当月月末”的检索范围拆为按月分段 (month_prefix, day_start, day_end)。

    向后延伸至当月月末可提前捕获已公布但尚未施行的条目；
    月初向前跨入上月时产生两段，保证上月末新发布的条目不被漏掉。"""
    start = today - timedelta(days=days - 1)
    same_month = (start.year, start.month) == (today.year, today.month)
    segments: List[tuple] = []
    if not same_month:
        prev_last_day = calendar.monthrange(start.year, start.month)[1]
        segments.append((f"{start.year}.{start.month:02d}", start.day, prev_last_day))
    cur_last_day = calendar.monthrange(today.year, today.month)[1]
    cur_start = start.day if same_month else 1
    segments.append((f"{today.year}.{today.month:02d}", cur_start, cur_last_day))
    return segments


def _mcp_handshake(token: str) -> None:
    """完成 MCP 会话初始化（该服务端无会话状态，每次进程初始化一次即可）。"""
    _mcp_rpc(token, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "law_scraper", "version": "1.0"},
    }, req_id=1)
    _mcp_rpc(token, "notifications/initialized", None, req_id=None)


def _mcp_call_get_law_list(
    token: str,
    keyword: str,
    start_date: str,
    end_date: str,
    field: str = "title",
) -> List[dict]:
    """调用 get_law_list 工具检索法规列表（服务端单次上限 20 条、按相关度排序）。

    field 为 'title'（标题关键词）或 'fulltext'（正文关键词）；
    必须传入施行日期范围，否则当月新法规会被历史高相关度条目挤出前 20 条（实测 0/20）。"""
    arguments = {
        field: keyword,
        "timeliness": MCP_TIMELINESS,
        "startImplementDate": start_date,
        "endImplementDate": end_date,
    }
    resp = _mcp_rpc(token, "tools/call", {
        "name": "get_law_list",
        "arguments": arguments,
    }, req_id=2)
    if "error" in resp:
        msg = str(resp["error"])[:200]
        if any(k in msg for k in _MCP_UNAVAILABLE_KEYWORDS):
            raise McpUnavailableError(msg)
        raise RuntimeError(f"MCP 调用失败: {msg}")
    result = resp.get("result", {})
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", [])]
        raise RuntimeError(f"MCP 工具返回错误: {' '.join(texts)[:200]}")
    payload = result.get("structuredContent")
    if payload is None:
        # 兼容无 structuredContent 的服务端：content[0].text 为 JSON 文本
        for c in result.get("content", []):
            if c.get("type") == "text":
                payload = json.loads(c.get("text") or "{}")
                break
    if not isinstance(payload, dict):
        return []
    data = payload.get("Data")
    if not isinstance(data, list):
        print(f"WARNING: MCP 返回无数据（Message={payload.get('Message', '?')}）")
        return []
    return data


def _mcp_search_month(
    token: str,
    keyword: str,
    month_prefix: str,
    field: str,
    max_calls: int = MCP_MAX_WINDOW_CALLS,
    day_start: int = 1,
    day_end: Optional[int] = None,
) -> tuple:
    """对目标月份（可限定日区间）做自适应日期窗拆分检索，规避服务端 20 条上限。

    从整月窗口开始，凡命中 20 条上限（结果可能被截断）的窗口二分拆分后重查，
    最小粒度为天（单日超过 20 条时接受截断）。调用间隔 MCP_CALL_DELAY_S。
    max_calls 限制本月的最大调用次数（回填模式按剩余积分预算收紧）。
    返回 (原始条目列表, 实际调用次数, 是否因 max_calls 截断而未查完, 已完整解析的日区间列表)。
    已解析区间互不相交，且与剩余未查窗口的并集等于请求范围，可直接入覆盖账本。"""
    year, month = (int(part) for part in month_prefix.split("."))
    last_day = calendar.monthrange(year, month)[1]
    if day_end is None or day_end > last_day:
        day_end = last_day

    items: List[dict] = []
    calls = 0
    windows = [(day_start, day_end)]
    covered: List[list] = []
    truncated = False
    while windows:
        if calls >= max_calls:
            truncated = True
            print(
                f"WARNING: {field} 检索已达最大调用次数 {max_calls}，"
                f"剩余 {len(windows)} 个日期窗未查，结果可能不全。"
            )
            break
        day_start, day_end = windows.pop()
        start_date = f"{year}.{month}.{day_start}"
        end_date = f"{year}.{month}.{day_end}"
        data = _mcp_call_get_law_list(token, keyword, start_date, end_date, field)
        calls += 1
        time.sleep(MCP_CALL_DELAY_S)
        if len(data) >= 20 and day_end > day_start:
            mid = (day_start + day_end) // 2
            windows.append((day_start, mid))
            windows.append((mid + 1, day_end))
            print(f"DEBUG: {field} 窗口 {start_date}~{end_date} 命中上限 20 条，拆分后继续")
            continue
        if len(data) >= 20:
            # 单日仍命中上限：无法再拆，属服务端 20 条硬上限的固有损耗，接受截断
            print(f"WARNING: {field} 单日 {start_date} 结果超过 20 条上限，已接受截断。")
        items.extend(data)
        covered.append([day_start, day_end])

    print(f"MCP {field} 检索: 关键词 '{keyword}' 共 {calls} 次调用，返回 {len(items)} 条原始结果")
    return items, calls, truncated, covered


_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_MCP_DATE_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?")


def _normalize_mcp_date(value: str) -> str:
    """MCP 返回的日期可能是 '2026.9.7' 这类非补零格式，统一规整为 YYYY.MM.DD。"""
    m = _MCP_DATE_RE.search(value or "")
    if not m:
        return ""
    return normalize_date_token(m.group(1), m.group(2), m.group(3) or "")


def _extract_mcp_url(item: dict) -> str:
    """提取 MCP item 的 Url 字段中的真实链接（可能带 markdown 链接语法）。"""
    raw_url = (item.get("Url") or "").strip()
    m = _MD_LINK_RE.search(raw_url)
    url = m.group(1) if m else raw_url
    # 去掉 MCP 追踪参数，使 URL 与浏览器抓取的规范形式一致
    return re.sub(r"[?&]way=mcp", "", url)


def _clean_document_no(value) -> str:
    """发文字号清洗：去全部空白（MCP 数据有内嵌换行/制表符）；纯符号占位符（如 ---）视为无文号。"""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"\s+", "", value)
    if cleaned and not re.search(r"[一-鿿0-9A-Za-z]", cleaned):
        return ""
    return cleaned


def _clean_str_list(value) -> List[str]:
    """把 MCP 返回的数组字段清洗为字符串列表（防御非 list/非 str 元素）。"""
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _record_from_mcp_item(item: dict) -> Optional[Record]:
    """把 get_law_list 的单条结果映射为 Record。分类优先取 item 的 Category
    （旧 CSV 迁移行携带），最终由 URL 路径段（chl/lar）复核。"""
    title = (item.get("Title") or "").strip()
    url = _extract_mcp_url(item)
    if not title or not url:
        return None

    departments = _clean_str_list(item.get("IssueDepartment"))
    hierarchies = _clean_str_list(item.get("EffectivenessDic"))
    timeliness = "；".join(_clean_str_list(item.get("TimelinessDic")))
    # 发文字号可能带内嵌换行/制表符等脏空白；纯符号占位符（如 ---）视为无文号
    document_no = _clean_document_no(item.get("DocumentNO"))
    # Category 是数组时表示主题分类（进 subject_tags）；迁移行是"中央/地方法规"
    # 类别字符串，仅作 category 依据，不进 subject_tags
    raw_category = item.get("Category") or ""
    subject_tags = ""
    if isinstance(raw_category, list):
        subject_tags = "；".join(_clean_str_list(raw_category))
        raw_category = ""
    elif not isinstance(raw_category, str):
        raw_category = ""

    return Record(
        category=enforce_category_by_url(raw_category, url),
        title=title,
        url=url,
        publish_date=_normalize_mcp_date(item.get("IssueDate") or ""),
        issuing_authority="；".join(departments),
        legal_hierarchy="；".join(hierarchies),
        effective_date=_normalize_mcp_date(item.get("ImplementDate") or ""),
        source="mcp",
        timeliness=timeliness,
        document_no=document_no,
        subject_tags=subject_tags,
    )


def _records_from_mcp_items(items: List[dict], month_prefixes) -> List[Record]:
    """把 MCP 原始条目映射为 Record 并按目标月份过滤（优先施行日期，否则公布日期）。

    month_prefixes 为可接受的 'YYYY.MM' 集合（增量扫描跨月时含上月与当月）。"""
    if isinstance(month_prefixes, str):
        month_prefixes = (month_prefixes,)
    records: List[Record] = []
    for item in items:
        rec = _record_from_mcp_item(item)
        if rec is None:
            continue
        # 与浏览器路径一致的“本月”判定：优先施行日期，否则公布日期
        date_to_check = rec.effective_date or rec.publish_date
        if not any(date_to_check.startswith(p) for p in month_prefixes):
            print(f"DEBUG: 跳过记录 '{rec.title[:40]}' - 日期 {date_to_check} 不在 {month_prefixes} 中")
            continue
        records.append(rec)
    return records


def run_mcp(
    keyword: str,
    out_jsonl: Path,
    out_json: Optional[Path],
    max_items: int,
    filter_keywords: Optional[List[str]] = None,
    month: Optional[str] = None,
    fulltext: bool = False,
    days: Optional[int] = None,
) -> List[Record]:
    """通过北大法宝 MCP 服务检索法规：原始结果全字段落盘 JSONL，并派生融合进统一 法规.csv。

    默认仅按标题（title）检索；传入 --fulltext 时追加正文（fulltext）检索，
    均由 _mcp_search_month 做自适应日期窗拆分，规避服务端 20 条上限。
    days（--days）启用增量模式：仅检索最近 N 天（月初自动跨入上月尾部），
    供每日任务节省积分；指定 --month 时忽略 days，始终整月检索。"""
    _migrate_mcp_csv_to_jsonl(MCP_LEGACY_CSV_PATH, MCP_JSONL_DEFAULT_PATH)
    token = os.environ.get(MCP_TOKEN_ENV, "").strip()
    if not token:
        print(f"未设置环境变量 {MCP_TOKEN_ENV}（北大法宝 MCP 授权码），请先设置后重试。")
        raise SystemExit(EXIT_MCP_UNAVAILABLE)

    current_month_prefix = month or now_cn().strftime("%Y.%m")
    today = now_cn()
    if days and not month:
        segments = _recent_windows(today, days)
        print(f"目标月份: {current_month_prefix}（增量模式：最近 {days} 天，分段 {segments}）")
    else:
        segments = [(current_month_prefix, 1, None)]
        print(f"目标月份: {current_month_prefix}")
    print(f"正在通过 MCP 检索关键词: {keyword}（标题{' + 正文' if fulltext else ''}）")

    try:
        _mcp_handshake(token)
        title_items: List[dict] = []
        fulltext_items: List[dict] = []
        covered_by_segment = {}
        for prefix, day_start, day_end in segments:
            items, _, truncated, covered = _mcp_search_month(
                token, keyword, prefix, "title", day_start=day_start, day_end=day_end,
            )
            title_items.extend(items)
            covered_by_segment[prefix] = covered
            if truncated:
                print(f"WARNING: 分段 {prefix} 标题检索被截断，按已解析日区间部分入账。")
            if fulltext:
                items, _, _, _ = _mcp_search_month(
                    token, keyword, prefix, "fulltext", day_start=day_start, day_end=day_end,
                )
                fulltext_items.extend(items)
    except McpUnavailableError as e:
        print(f"ERROR: MCP 当日不可用（积分不足或授权问题）: {e}")
        raise SystemExit(EXIT_MCP_UNAVAILABLE)
    except Exception as e:
        print(f"ERROR: MCP 检索失败: {e}")
        raise SystemExit(1)

    # 原始 items 全字段落盘 JSONL（读旧合并去重后整文件重写）
    write_mcp_jsonl(
        out_jsonl,
        [_with_mcp_meta(it, keyword, "title") for it in title_items]
        + [_with_mcp_meta(it, keyword, "fulltext") for it in fulltext_items],
    )

    month_prefixes = tuple(s[0] for s in segments)
    all_records = _records_from_mcp_items(title_items + fulltext_items, month_prefixes)
    all_records = deduplicate_records_by_title(all_records)

    # 标题二次过滤只约束「标题检索」来源的记录；
    # 仅由正文命中的记录（标题不含关键词）是全文检索的预期产出，直接保留。
    title_keys = {
        title_dedup_key((item.get("Title") or "").strip())
        for item in title_items
    }
    title_sourced = [r for r in all_records if title_dedup_key(r.title) in title_keys]
    fulltext_only = [r for r in all_records if title_dedup_key(r.title) not in title_keys]

    effective_filter_keywords = filter_keywords if filter_keywords else [keyword]
    title_sourced = filter_records_by_keywords(title_sourced, effective_filter_keywords)
    all_records = title_sourced + fulltext_only
    all_records = deduplicate_records_by_title(all_records)
    if max_items > 0:
        all_records = all_records[:max_items]
    print(
        f"过滤后剩余 {len(all_records)} 条记录"
        f"（标题命中 {len(title_sourced)} 条，仅正文命中 {len(fulltext_only)} 条）"
    )

    # 从 JSONL 全量派生 Record，融合进统一 法规.csv（浏览器数据已在文件中作为 base）
    fused_records = _fuse_jsonl_into_csv(out_jsonl)
    print(f"已将 JSONL 中 {len(fused_records)} 条 MCP 记录融合进 {MERGED_CSV_PATH}")
    if out_json:
        write_json(out_json, all_records)

    # 本次扫描的“确定覆盖”日区间并入覆盖账本，回填模式据此跳过已调研窗口；
    # 截断分段按已解析的日区间部分入账，剩余补集窗口留待回填补扫
    coverage = _load_backfill_state(MCP_BACKFILL_STATE_PATH)
    recorded = False
    for prefix, day_start, day_end in segments:
        for s, e in covered_by_segment.get(prefix, []):
            iv = _definitive_interval(prefix, s, e, today)
            if iv:
                _record_coverage(coverage, prefix, keyword, [iv])
                recorded = True
    if recorded:
        _save_backfill_state(MCP_BACKFILL_STATE_PATH, coverage)
    return all_records


# === MCP 历史回填（积分预算制，断点续扫） ===

def _month_range_desc(start_prefix: str, end_prefix: str) -> List[str]:
    """生成 [start, end] 闭区间的 'YYYY.MM' 月份列表（倒序，最近月份在前）。"""
    year, month = (int(p) for p in start_prefix.split("."))
    end_year, end_month = (int(p) for p in end_prefix.split("."))
    months: List[str] = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year}.{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return list(reversed(months))


def _merge_intervals(intervals) -> List[list]:
    """区间并集合并：[[1,15],[29,31],[14,20]] -> [[1,20],[29,31]]。"""
    if not intervals:
        return []
    ordered = sorted([list(iv) for iv in intervals])
    merged = [ordered[0]]
    for s, e in ordered[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _complement_intervals(covered, last_day: int) -> List[list]:
    """[1, last_day] 中未被 covered 覆盖的区间列表（回填只需扫这些窗口）。"""
    out: List[list] = []
    cur = 1
    for s, e in _merge_intervals(covered):
        if cur < s:
            out.append([cur, s - 1])
        cur = max(cur, e + 1)
    if cur <= last_day:
        out.append([cur, last_day])
    return out


def _load_backfill_state(path: Path) -> dict:
    """读取覆盖账本 {month: {keyword: [[d1,d2],...]}}；兼容旧版 {"scanned":[...]} 并迁移为整月覆盖。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Warning: 读取回填状态文件失败: {e}")
        return {}
    if data.get("version") == 2:
        return {
            m: {k: [list(iv) for iv in v] for k, v in kws.items()}
            for m, kws in (data.get("coverage") or {}).items()
        }
    coverage: dict = {}
    for combo in data.get("scanned", []):
        try:
            month, kw = combo.split("|", 1)
            last_day = calendar.monthrange(*(int(p) for p in month.split(".")))[1]
            coverage.setdefault(month, {})[kw] = [[1, last_day]]
        except Exception:
            continue
    return coverage


def _save_backfill_state(path: Path, coverage: dict) -> None:
    path.write_text(
        json.dumps({"version": 2, "coverage": coverage}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_coverage(coverage: dict, month_prefix: str, keyword: str, intervals) -> None:
    kw_map = coverage.setdefault(month_prefix, {})
    kw_map[keyword] = _merge_intervals(kw_map.get(keyword, []) + [list(iv) for iv in intervals])


def _definitive_interval(
    month_prefix: str, day_start: int, day_end: int, today: datetime
) -> Optional[list]:
    """扫描窗口 [day_start, day_end] 中“确定覆盖”的部分（不晚于扫描日的日期）。

    未来日期之后还可能有新发布条目落入（施行日期在未来），不计入确定覆盖。"""
    year, month = (int(p) for p in month_prefix.split("."))
    last_day = calendar.monthrange(year, month)[1]
    if (year, month) < (today.year, today.month):
        end = min(day_end, last_day)
    elif (year, month) == (today.year, today.month):
        end = min(day_end, today.day)
    else:
        return None
    if day_start > end:
        return None
    return [day_start, end]


def run_mcp_backfill(
    keywords: List[str],
    start_month: str,
    points_budget: int,
    out_jsonl: Path,
    out_json: Optional[Path],
    fulltext: bool = False,
    state_path: Path = MCP_BACKFILL_STATE_PATH,
    refresh: bool = False,
) -> None:
    """按积分预算回填漏扫的法规（当月补漏优先，随后 start_month 起的历史月份倒序）。

    原始结果逐段落盘 JSONL，回填结束后统一派生融合进 法规.csv。
    覆盖账本（state 文件，随仓库提交）记录每个 月份×关键词 已确定覆盖的日区间，
    回填只扫未覆盖的补集窗口；每段扫描成功即时入账，断点续扫粒度精确到日区间。
    refresh=True 时忽略覆盖账本、全月重扫（用于用富字段原地富化历史数据），
    扫描成功后仍照常记账。
    points_budget=0 表示不限预算，直到积分耗尽（McpUnavailableError 时优雅停止）。"""
    _migrate_mcp_csv_to_jsonl(MCP_LEGACY_CSV_PATH, MCP_JSONL_DEFAULT_PATH)
    token = os.environ.get(MCP_TOKEN_ENV, "").strip()
    if not token:
        print(f"未设置环境变量 {MCP_TOKEN_ENV}（北大法宝 MCP 授权码），请先设置后重试。")
        raise SystemExit(EXIT_MCP_UNAVAILABLE)

    now = now_cn()
    current_month = f"{now.year}.{now.month:02d}"
    prev_year, prev_month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    prev_month_prefix = f"{prev_year}.{prev_month:02d}"

    # 回填范围：当月（每日增量可能因缺积分产生空洞，最优先）+ start_month ~ 上月（倒序）
    months: List[str] = []
    if start_month <= current_month:
        months.append(current_month)
    if start_month <= prev_month_prefix:
        months += _month_range_desc(start_month, prev_month_prefix)
    if not months:
        print(f"回填范围为空（起始月份 {start_month} 晚于当月 {current_month}），无需回填。")
        return

    coverage = _load_backfill_state(state_path)
    budget_calls = points_budget // MCP_POINTS_PER_CALL if points_budget > 0 else None
    if budget_calls is None:
        print("积分预算: 不限（持续到积分耗尽为止，进度实时保存）")
    else:
        print(f"积分预算 {points_budget} ≈ {budget_calls} 次检索调用（约 {MCP_POINTS_PER_CALL} 积分/次）")
    print(f"回填范围: 当月补漏 + {start_month} ~ {prev_month_prefix}，共 {len(months)} 个月 × {len(keywords)} 个关键词")
    if refresh:
        print("refresh 模式：忽略覆盖账本，全月重扫（成功后照常记账）")

    try:
        _mcp_handshake(token)
    except McpUnavailableError as e:
        print(f"MCP 不可用（积分不足或授权问题）: {e}，本次回填未开始，进度无变化。")
        return
    except Exception as e:
        print(f"WARNING: MCP 连接失败: {e}，本次回填未开始。")
        return
    calls_used = 0
    stop = False
    for month_prefix in months:
        if stop:
            break
        year, mo = (int(p) for p in month_prefix.split("."))
        last_day = calendar.monthrange(year, mo)[1]
        # 当月只补“确定可覆盖”的部分（截至今天）；未来日期由每日任务捕获
        effective_last = min(last_day, now.day) if month_prefix == current_month else last_day
        for kw in keywords:
            if stop:
                break
            todo = (
                [[1, effective_last]]
                if refresh
                else _complement_intervals(
                    coverage.get(month_prefix, {}).get(kw, []), effective_last
                )
            )
            if not todo:
                continue
            for d1, d2 in todo:
                if budget_calls is not None:
                    remaining = budget_calls - calls_used
                    if remaining <= 0:
                        stop = True
                        break
                    cap = min(MCP_MAX_WINDOW_CALLS, remaining)
                else:
                    cap = MCP_MAX_WINDOW_CALLS
                print(f"回填 {month_prefix} [{d1}-{d2}] 关键词 '{kw}'...")
                try:
                    title_items, calls, truncated, covered = _mcp_search_month(
                        token, kw, month_prefix, "title",
                        max_calls=cap, day_start=d1, day_end=d2,
                    )
                    calls_used += calls
                    fulltext_items: List[dict] = []
                    if fulltext and not truncated:
                        if budget_calls is None:
                            cap = MCP_MAX_WINDOW_CALLS
                        else:
                            remaining = budget_calls - calls_used
                            cap = min(MCP_MAX_WINDOW_CALLS, remaining)
                        if cap > 0:
                            ft_items, calls, _, _ = _mcp_search_month(
                                token, kw, month_prefix, "fulltext",
                                max_calls=cap, day_start=d1, day_end=d2,
                            )
                            fulltext_items = ft_items
                            calls_used += calls
                except McpUnavailableError as e:
                    print(f"MCP 不可用（积分不足或授权问题）: {e}")
                    stop = True
                    break
                except Exception as e:
                    print(f"WARNING: MCP 调用失败: {e}")
                    stop = True
                    break

                # 原始 items 逐段落盘 JSONL（断点续扫时已落盘数据不丢）
                write_mcp_jsonl(
                    out_jsonl,
                    [_with_mcp_meta(it, kw, "title") for it in title_items]
                    + [_with_mcp_meta(it, kw, "fulltext") for it in fulltext_items],
                )

                records = _records_from_mcp_items(title_items + fulltext_items, month_prefix)
                records = deduplicate_records_by_title(records)
                # 标题二次过滤只约束标题来源；仅正文命中的记录直接保留
                title_keys = {
                    title_dedup_key((it.get("Title") or "").strip())
                    for it in title_items
                }
                title_sourced = [r for r in records if title_dedup_key(r.title) in title_keys]
                fulltext_only = [r for r in records if title_dedup_key(r.title) not in title_keys]
                title_sourced = filter_records_by_keywords(title_sourced, [kw])
                records = deduplicate_records_by_title(title_sourced + fulltext_only)

                if truncated:
                    # 调用上限导致该窗口未扫完：结果已落盘，已解析的日区间即时入账，
                    # 剩余补集窗口由下次触发的 _complement_intervals 续扫（不再整体中止，
                    # 避免单个高热组合永久阻塞后续关键词/月份）。
                    partial = []
                    for s, e in covered:
                        iv = _definitive_interval(month_prefix, s, e, now)
                        if iv:
                            partial.append(iv)
                    if partial:
                        _record_coverage(coverage, month_prefix, kw, partial)
                        _save_backfill_state(state_path, coverage)
                    print(
                        f"WARNING: {month_prefix} [{d1}-{d2}] '{kw}' 未扫完，"
                        f"已入账 {len(partial)} 个已解析日区间，剩余窗口留待下次续扫。"
                    )
                    continue
                iv = _definitive_interval(month_prefix, d1, d2, now)
                if iv:
                    _record_coverage(coverage, month_prefix, kw, [iv])
                    _save_backfill_state(state_path, coverage)
                print(
                    f"回填 {month_prefix} [{d1}-{d2}] '{kw}': +{len(records)} 条"
                    f"（累计调用 {calls_used} 次 ≈ {calls_used * MCP_POINTS_PER_CALL} 积分）"
                )

    print(
        f"回填结束：共调用 {calls_used} 次 ≈ 消耗 {calls_used * MCP_POINTS_PER_CALL} 积分"
        + (
            f"，预算剩余约 {points_budget - calls_used * MCP_POINTS_PER_CALL} 积分"
            if budget_calls is not None
            else ""
        )
    )
    if stop:
        print("进度已实时保存至状态文件，下次触发将从断点继续。")

    # 回填结束后统一从 JSONL 派生并融合进 法规.csv
    fused_records = _fuse_jsonl_into_csv(out_jsonl)
    print(f"已将 JSONL 中 {len(fused_records)} 条 MCP 记录融合进 {MERGED_CSV_PATH}")
    if out_json:
        write_json(out_json, fused_records)


def _with_mcp_meta(item: dict, keyword: str, search_field: str) -> dict:
    """给 MCP 原始 item 附加 __meta 检索上下文（retrieved_at 为北京时间）。"""
    enriched = dict(item)
    enriched["__meta"] = {
        "retrieved_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "keyword": keyword,
        "search_field": search_field,
    }
    return enriched


def _mcp_item_dedup_key(item: dict) -> str:
    """JSONL 去重键：优先 URL path，否则标题（去全部空白）。"""
    return url_path_key(_extract_mcp_url(item)) or title_dedup_key(
        (item.get("Title") or "").strip()
    )


def load_mcp_jsonl(path: Path) -> List[dict]:
    """逐行读取 JSONL（utf-8），坏行跳过并告警到 stderr。"""
    items: List[dict] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"Warning: {path.name}:{lineno} JSON 解析失败，已跳过: {e}", file=sys.stderr)
                continue
            if isinstance(obj, dict):
                items.append(obj)
    return items


def _merge_mcp_meta(old: Optional[dict], new: Optional[dict]) -> dict:
    """合并同一条目多次检索的 __meta：新 meta 覆盖同名键，但检索关键词累积进
    keywords 列表（融合时的标题过滤任一命中即保留），且一旦有过 title 命中
    就固定 search_field="title"（标题命中是更强的相关性证据，不被后续
    fulltext 命中覆盖）。"""
    old = old or {}
    new = new or {}
    merged = {**old, **new}
    keywords: List[str] = []
    for meta in (old, new):
        for kw in (meta.get("keywords") or []):
            if kw and kw not in keywords:
                keywords.append(kw)
        kw = (meta.get("keyword") or "").strip()
        if kw and kw not in keywords:
            keywords.append(kw)
    if keywords:
        merged["keywords"] = keywords
    fields = [m.get("search_field") for m in (old, new)]
    if "title" in fields:
        merged["search_field"] = "title"
    return merged


def write_mcp_jsonl(path: Path, new_items: Iterable[dict]) -> None:
    """把 MCP 原始 item 合并进 JSONL：读旧 → 按 _mcp_item_dedup_key 去重
    （同 key 新记录覆盖旧记录，__meta 经 _merge_mcp_meta 合并）→ 整文件重写。
    无 key 的行追加保留。写入经临时文件 + os.replace 原子替换，避免中断截断数据。"""
    new_items = [it for it in new_items if isinstance(it, dict)]
    old_items = load_mcp_jsonl(path)
    if not old_items and not new_items:
        return
    # 防误伤：已存在文件的首个非空行不是合法 JSON（如误把 CSV 路径当 --out 传入）
    # 时拒绝覆写——load_mcp_jsonl 会静默跳过坏行，不能据此判断文件为空。
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    first = json.loads(line)
                except json.JSONDecodeError:
                    first = None
                if not isinstance(first, dict):
                    raise SystemExit(
                        f"ERROR: {path} 已存在且不是 JSONL 格式（--out 是否误传了 CSV 路径？），拒绝覆写。"
                    )
                break
    merged: dict = {}
    nokey: List[dict] = []
    for item in old_items + new_items:
        key = _mcp_item_dedup_key(item)
        if key:
            if key in merged:
                item = dict(item)
                item["__meta"] = _merge_mcp_meta(merged[key].get("__meta"), item.get("__meta"))
            merged[key] = item
        else:
            nokey.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        for item in list(merged.values()) + nokey:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(tmp_path, path)


def _migrate_mcp_csv_to_jsonl(csv_path: Path, jsonl_path: Path) -> None:
    """一次性迁移：若 JSONL 不存在而旧版 法规_mcp.csv 存在，把 CSV 行转成 JSONL 行
    （MCP 风格字段名 + __meta{"migrated_from": ...}），使派生逻辑统一从 JSONL 出 Record。"""
    if jsonl_path.exists() or not csv_path.exists():
        return
    items: List[dict] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                items.append({
                    "Title": row.get("title", ""),
                    "Url": row.get("url", ""),
                    "IssueDate": row.get("publish_date", ""),
                    "ImplementDate": row.get("effective_date", ""),
                    "IssueDepartment": [
                        d for d in (row.get("issuing_authority") or "").split("；") if d
                    ],
                    "EffectivenessDic": [
                        h for h in (row.get("legal_hierarchy") or "").split("；") if h
                    ],
                    "Category": row.get("category", ""),
                    "__meta": {"migrated_from": csv_path.name},
                })
    except Exception as e:
        print(f"Warning: 迁移 {csv_path.name} 到 JSONL 失败: {e}")
        return
    write_mcp_jsonl(jsonl_path, items)
    print(f"已将 {csv_path.name} 的 {len(items)} 条历史记录迁移到 {jsonl_path.name}")


def _fuse_jsonl_into_csv(jsonl_path: Path, csv_path: Path = MERGED_CSV_PATH) -> List[Record]:
    """从 JSONL 全量派生 Record 并融合进统一 CSV，返回派生的记录列表。
    标题检索来源的记录沿用标题二次过滤（标题须含任一历史检索关键词）；
    仅正文命中的记录与迁移历史记录直接保留。"""
    records = []
    for item in load_mcp_jsonl(jsonl_path):
        meta = item.get("__meta") or {}
        if meta.get("search_field") == "title":
            keywords = meta.get("keywords") or []
            if not keywords and meta.get("keyword"):
                keywords = [meta["keyword"]]
            title = (item.get("Title") or "").strip().lower()
            if keywords and not any(kw.strip().lower() in title for kw in keywords):
                continue
        r = _record_from_mcp_item(item)
        if r:
            records.append(r)
    write_csv(csv_path, records)
    return records


def write_csv(path: Path, rows: Iterable[Record]) -> None:
    # 读取已有数据进行合并（使用双 key：URL path + 标题去全部空白）
    by_title: dict = {}
    by_url: dict = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    r = Record(
                        category=normalize_category(row.get("category", "")),
                        title=row.get("title", ""),
                        url=row.get("url", ""),
                        publish_date=row.get("publish_date", ""),
                        issuing_authority=row.get("issuing_authority", ""),
                        legal_hierarchy=row.get("legal_hierarchy", ""),
                        effective_date=row.get("effective_date", ""),
                        # 兼容旧版 7 列 CSV：缺 source 列默认 browser；更旧的文件缺富字段列时默认空串
                        source=row.get("source", "") or "browser",
                        timeliness=row.get("timeliness", ""),
                        document_no=_clean_document_no(row.get("document_no", "")),
                        subject_tags=row.get("subject_tags", ""),
                    )
                    if not (title_dedup_key(r.title) or url_path_key(r.url)):
                        continue
                    _merge_into_maps(r, by_title, by_url)
        except Exception as e:
            print(f"Warning: 读取现有CSV合并失败: {e}")

    # 合并新查询到的数据
    for r in rows:
        if not (title_dedup_key(r.title) or url_path_key(r.url)):
            continue
        _merge_into_maps(r, by_title, by_url)

    # 通过 id 去重得到唯一 Record 列表（多个 key 可能指向同一对象）
    seen = set()
    unique_records: List[Record] = []
    for rec in list(by_title.values()) + list(by_url.values()):
        if id(rec) in seen:
            continue
        seen.add(id(rec))
        unique_records.append(rec)

    # 按 publish_date 降序排序（由新到旧）
    sorted_records = sorted(
        unique_records,
        key=lambda x: x.publish_date,
        reverse=True
    )

    # 写回文件
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["category", "title", "url", "publish_date",
                        "issuing_authority", "legal_hierarchy",
                        "effective_date", "source",
                        "timeliness", "document_no", "subject_tags"],
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

    def _needs_enrich(r: Record) -> bool:
        # 「法律动态」详情页不含“效力位阶”，只要制定机关（映射自“新闻来源”）已补全即可。
        if normalize_category(r.category) == "法律动态":
            return not r.issuing_authority
        return not (r.issuing_authority and r.legal_hierarchy)

    to_enrich = [r for r in existing.values() if _needs_enrich(r)]

    if not to_enrich:
        print("所有现有记录已包含完整的制定机关/效力位阶信息，无需补全。")
        return list(existing.values())

    print(f"共 {len(existing)} 条现有记录，其中 {len(to_enrich)} 条需要补全详情信息。")

    _require_playwright()
    async with async_playwright() as p:
        context, browser = await new_stealth_context(
            p, headless=headless, slow_mo=slow_mo, user_data_dir=user_data_dir
        )
        page = await context.new_page()

        try:
            for r in to_enrich:
                print(f"补全详情: {r.title[:50]}...")
                detail = await fetch_detail_info(page, r.url)
                r.issuing_authority = r.issuing_authority or detail.get("issuing_authority", "")
                r.legal_hierarchy = r.legal_hierarchy or detail.get("legal_hierarchy", "")
                await page.wait_for_timeout(DETAIL_PAGE_DELAY_MS)
        finally:
            await close_browser_context(context, browser)

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
    month: Optional[str] = None,
) -> List[Record]:
    _require_playwright()
    async with async_playwright() as p:
        context, browser = await new_stealth_context(
            p, headless=headless, slow_mo=slow_mo, user_data_dir=user_data_dir
        )
        page = await context.new_page()

        try:
            all_records: List[Record] = []

            # 加载已有数据，用于跳过已抓取详情的记录
            existing_data = load_existing_records(out_csv)

            # 使用当月作为Python端过滤；--month 可指定历史月份用于回填
            current_month_prefix = month or now_cn().strftime("%Y.%m")
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

            waf_blocked = False
            for cat_key, cat_label, nav_needed, sub_tabs in categories:
                try:
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
                    all_seen_titles = set(title_dedup_key(r.title) for r in all_records if title_dedup_key(r.title))

                    found_recs = await click_load_more_until_done(
                        page, all_seen_titles, cat_label,
                        max_items=items_needed, month_prefix=current_month_prefix,
                    )

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

                        all_seen_titles = set(title_dedup_key(r.title) for r in all_records if title_dedup_key(r.title))
                        sub_recs = await click_load_more_until_done(
                            page, all_seen_titles, sub_label,
                            max_items=items_needed, month_prefix=current_month_prefix,
                        )
                        all_records.extend(sub_recs)
                        print(f"为 {sub_label} 找到 {len(sub_recs)} 条记录")
                except WafBlockedError as e:
                    # WAF 限流是日常可预期情况：跳过剩余分类，照常写盘已收集记录，
                    # 缺失的详情字段留待下次运行（或 --enrich-existing）补全。
                    print(
                        f"WARNING: 关键词 '{keyword}' 在分类 '{cat_label}' 被 WAF 拦截"
                        f"（{e}）。已收集 {len(all_records)} 条，跳过剩余分类。"
                    )
                    waf_blocked = True
                    break

            # 第五步: 访问每条记录的详情页，获取制定机关、效力位阶
            all_records = deduplicate_records_by_title(all_records)
            print(f"按标题去重后待补全详情记录数: {len(all_records)}")
            if waf_blocked:
                print("WARNING: 因 WAF 拦截跳过详情补全，缺失字段留待后续运行补全。")
            else:
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
            await close_browser_context(context, browser)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="查询法规信息并带交互式过滤")
    ap.add_argument("--keyword", default="智能", help="检索词（默认：智能）")
    ap.add_argument(
        "--out",
        default=None,
        help=f"输出路径：browser 模式默认 {MERGED_CSV_PATH}（CSV）；"
        f"mcp 模式默认 {MCP_JSONL_DEFAULT_PATH}（JSONL 原始数据，派生后融合进 {MERGED_CSV_PATH}）",
    )
    ap.add_argument("--out-json", default=None, help="输出 JSON 路径（可选）")
    ap.add_argument(
        "--filter-keywords",
        default=None,
        help="标题二次过滤关键词，逗号分隔（默认使用 --keyword 的值）",
    )

    ap.add_argument(
        "--month",
        default=None,
        help="抓取指定月份（格式 YYYY.MM，如 2026.06），用于回填历史遗漏；默认当月",
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

    ap.add_argument(
        "--source",
        choices=["browser", "mcp"],
        default="browser",
        help="数据来源：browser=模拟浏览器抓取（默认）；mcp=北大法宝 MCP 服务"
        f"（需设置环境变量 {MCP_TOKEN_ENV}）",
    )
    ap.add_argument(
        "--fulltext",
        action="store_true",
        help="仅 --source mcp 有效：在标题检索之外追加正文（fulltext）检索"
        "（覆盖面更广，但正文顺带提及的条目会带来噪声，默认关闭）",
    )
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="仅 --source mcp 有效：按积分预算回填 --start-month 至上月的历史数据"
        "（进度记录在 mcp_backfill_state.json，断点续扫）",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="仅配合 --source mcp --backfill 使用：忽略覆盖账本全月重扫"
        "（用富字段原地富化 JSONL 历史数据），扫描成功后仍照常记账",
    )
    ap.add_argument(
        "--start-month",
        default=None,
        help="回填起始月份（格式 YYYY.MM），--backfill 必填",
    )
    ap.add_argument(
        "--points-budget",
        type=int,
        default=0,
        help=f"--backfill 的积分预算（每次检索调用约 {MCP_POINTS_PER_CALL} 积分），"
        "0=不限（持续到积分耗尽为止，进度实时保存），默认 0",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="仅 --source mcp 有效：仅检索最近 N 天（按施行日期，向后覆盖至当月月末，"
        "月初自动跨入上月尾部），用于每日增量扫描以节省积分；默认整月",
    )
    ap.add_argument(
        "--fuse-only",
        action="store_true",
        help="仅 --source mcp 有效：不发起检索，只做旧 CSV 一次性迁移 + 把 JSONL "
        "全量融合进 法规.csv（用于从 JSONL 真相来源重建统一数据集）",
    )

    return ap.parse_args()


def main() -> None:
    args = parse_args()
    headless = True
    if args.headed:
        headless = False

    out_json = Path(args.out_json) if args.out_json else None
    user_data_dir = Path(args.user_data_dir) if args.user_data_dir else None
    filter_keywords = (
        [kw.strip() for kw in args.filter_keywords.split(",") if kw.strip()]
        if args.filter_keywords
        else None
    )

    month = None
    if args.month:
        if not re.fullmatch(r"\d{4}\.\d{2}", args.month):
            raise SystemExit(f"--month 格式应为 YYYY.MM，收到: {args.month!r}")
        month = args.month

    if args.days is not None and args.days < 1:
        raise SystemExit(f"--days 应为正整数，收到: {args.days!r}")

    if args.refresh and not (args.source == "mcp" and args.backfill):
        raise SystemExit("--refresh 仅配合 --source mcp --backfill 使用")

    if args.enrich_existing:
        out_csv = Path(args.out) if args.out else MERGED_CSV_PATH
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

    if args.source == "mcp":
        # MCP 模式下 --out 语义为 JSONL 原始数据路径，派生记录统一融合进 法规.csv
        out_jsonl = Path(args.out) if args.out else MCP_JSONL_DEFAULT_PATH
        if out_jsonl.suffix.lower() == ".csv" or out_jsonl.resolve() in (
            MERGED_CSV_PATH.resolve(),
            MCP_LEGACY_CSV_PATH.resolve(),
        ):
            raise SystemExit(
                f"MCP 模式下 --out 应为 JSONL 路径（默认 {MCP_JSONL_DEFAULT_PATH}），"
                f"收到: {out_jsonl}（传 CSV 路径会销毁其中的数据）"
            )
        if args.fuse_only:
            # 只做一次性迁移 + 融合（JSONL 是真相来源，法规.csv 可随时由此重建）
            _migrate_mcp_csv_to_jsonl(MCP_LEGACY_CSV_PATH, MCP_JSONL_DEFAULT_PATH)
            fused = _fuse_jsonl_into_csv(out_jsonl)
            print(f"已融合 {len(fused)} 条 MCP 记录到 {MERGED_CSV_PATH.resolve()}")
            return
        if args.backfill:
            if not args.start_month or not re.fullmatch(r"\d{4}\.\d{2}", args.start_month):
                raise SystemExit(
                    f"--backfill 需要 --start-month（格式 YYYY.MM），收到: {args.start_month!r}"
                )
            keywords = [kw.strip() for kw in args.keyword.split(",") if kw.strip()]
            if not keywords:
                raise SystemExit("--backfill 需要至少一个关键词（--keyword，逗号分隔）")
            run_mcp_backfill(
                keywords=keywords,
                start_month=args.start_month,
                points_budget=args.points_budget,
                out_jsonl=out_jsonl,
                out_json=out_json,
                fulltext=args.fulltext,
                refresh=args.refresh,
            )
            print(f"完成。JSONL文件: {out_jsonl.resolve()}")
            print(f"CSV文件: {MERGED_CSV_PATH.resolve()}")
            return
        records = run_mcp(
            keyword=args.keyword,
            out_jsonl=out_jsonl,
            out_json=out_json,
            max_items=args.max_items,
            filter_keywords=filter_keywords,
            month=month,
            fulltext=args.fulltext,
            days=args.days,
        )
        print(f"完成。总记录数: {len(records)}")
        print(f"JSONL文件: {out_jsonl.resolve()}")
        print(f"CSV文件: {MERGED_CSV_PATH.resolve()}")
    else:
        out_csv = Path(args.out) if args.out else MERGED_CSV_PATH
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
                month=month,
            )
        )
        print(f"完成。总记录数: {len(records)}")
        print(f"CSV文件: {out_csv.resolve()}")
    if out_json:
        print(f"JSON文件: {out_json.resolve()}")


if __name__ == "__main__":
    main()
