# AGENTS.md

本文件用于指导 AI 编码助手（Claude / Cursor / Trae / Codex 等）在本仓库中协作。请在执行任何修改前阅读以下约定。

## 1. 项目概览

- **名称**：law_scraper（law_query）
- **目标**：使用关键词在「北大法宝」(https://www.pkulaw.com) 检索法律法规、立法资料、法律动态等，导出为 CSV / JSON / RSS，并通过 GitHub Pages 提供网页浏览。
- **运行方式**：本地命令行执行 `query.py`，或由 GitHub Actions 每日定时抓取后自动提交。

## 2. 技术栈

- **语言**：Python 3.10+（CI 使用 3.10，本地建议 3.12）
- **核心依赖**：`playwright>=1.41.0`（Chromium，无头/有头均支持）
- **前端展示**：单文件 `index.html`（静态页，直接读取 `法规.csv` 与 `meta.json`）
- **CI/CD**：GitHub Actions（`.github/workflows/update_law.yml`）

> ⚠️ 除 `requirements.txt` 中已声明的依赖外，**不要**擅自引入新的第三方库；如确有需要，请先在回复中说明理由并征求用户同意。

## 3. 目录结构

```
law_scraper/
├── .github/workflows/update_law.yml  # 每日定时抓取并提交
├── query.py                          # 主抓取脚本（Playwright）
├── generate_rss.py                   # 根据 CSV 生成 feed.xml
├── index.html                        # GitHub Pages 静态展示页
├── 法规.csv                          # 抓取结果（持久化数据，按标题去重）
├── feed.xml                          # 生成的 RSS 2.0
├── meta.json                         # 最近更新时间（北京时间）
├── requirements.txt
└── README.md
```

## 4. 数据契约

`法规.csv` 列定义（修改时务必保持一致，否则会破坏 `index.html` 与 `generate_rss.py`）：

| 列名 | 含义 | 示例 |
| --- | --- | --- |
| `category` | 中央法规 / 地方法规 / 立法资料 / 法规解读 / 法律动态 | 中央法规 |
| `title` | 法规标题（用于去重，使用 `normalize_title` 规整空白） | 关于…的通知 |
| `url` | 详情页链接 | https://www.pkulaw.com/... |
| `publish_date` | `YYYY.MM.DD` 或 `YYYY.MM` | 2026.06.01 |
| `issuing_authority` | 制定机关 | 国务院 |
| `legal_hierarchy` | 效力位阶 | 行政法规 |

`meta.json` 形如 `{"updated_at": "2026-06-12 08:00"}`（北京时间）。

## 5. 常用命令

```bash
# 安装依赖（首次）
pip install -r requirements.txt
playwright install chromium

# 抓取单个关键词
python query.py --keyword 智能 --out 法规.csv

# 抓取并输出 JSON
python query.py --keyword 智能 --out 法规.csv --out-json results.json

# 仅补全已有条目的制定机关 / 效力位阶
python query.py --enrich-existing --out 法规.csv

# 调试（有头浏览器 + 放慢）
python query.py --headed --slow-mo 200 --max-items 50

# 重新生成 RSS
python generate_rss.py
```

CI 默认依次抓取的关键词列表（位于 workflow 中）：
`智能 / 数智化 / AI / 高质量数据 / 大模型 / 算力 / 工业互联网 / 新兴产业 / 未来产业 / 数字化`

## 6. 代码风格与约束

- **Python 风格**：遵循现有代码（`dataclass` + `async/await`）。模块顶部已定义常量（如 `DETAIL_PAGE_DELAY_MS`、`_LABEL_MAX_CHILDREN`、`CATEGORY_NAME_MAP`），新增魔法值时优先抽为常量。
- **注释**：保持现有简洁风格；除非用户明确要求，不要新增大段注释或文档字符串。
- **请勿格式化整个文件**：避免无关 diff，仅修改必要的行。
- **抓取礼貌性**：调整爬取节奏时，请保留或调大 `DETAIL_PAGE_DELAY_MS`（默认 1000ms），不要使站点负载激增。
- **类别归一化**：所有写入 CSV 的 `category` 必须经过 `normalize_category()` 处理；新增类别时同步更新 `CATEGORY_NAME_MAP`（`query.py` 与 `generate_rss.py` 两处）。
- **去重**：以 `normalize_title(title)` 作为唯一键；合并逻辑见 `merge_record_fields()`，新字段需在此函数中决定合并策略。

## 7. 测试与验证

仓库**目前未配置自动化测试**。完成修改后，至少执行以下手动验证：

1. `python -c "import query, generate_rss"` —— 语法 / 导入检查。
2. 小规模真实运行：`python query.py --keyword 智能 --max-items 5 --headed`，确认无异常并能写入 CSV。
3. 若改动了 CSV 列或 RSS 逻辑：`python generate_rss.py` 后查看 `feed.xml` 是否仍为合法 XML。
4. 若改动了 `index.html`：在浏览器中本地打开（或 `python -m http.server`）确认能正常加载 `法规.csv` 与 `meta.json`。

## 8. CI / 自动化注意事项

- Workflow 触发：每天 UTC 22:30（北京时间次日 06:30）；也可在 Actions 页手动 `workflow_dispatch`，支持 `keyword` 与 `enrich_existing` 输入。
- Workflow 会 `git add 法规.csv meta.json feed.xml` 并自动 commit & push，**请不要**让脚本写入其他需要提交的文件，除非同步更新 workflow。
- 提交者身份固定为 `youngfish42@users.noreply.github.com`，不要修改。

## 9. 协作准则（给 AI 的硬性约束）

1. **最小变更原则**：仅完成用户当前要求，不顺手「优化」无关代码。
2. **不要新增文件**，除非任务确有必要；优先编辑现有文件。
3. **不要主动创建** `*.md`、`README`、示例脚本等文档/演示文件，除非用户明确要求。
4. **不要提交** (`git commit/push`)，除非用户明确要求。
5. **不要泄露**任何密钥、Cookie、登录态；`user_data/` 等持久化目录已在 `.gitignore`。
6. 若用户需求模糊（例如「优化一下」「改一下样式」），先用提问工具澄清，再动手。
7. 对中文文件名（如 `法规.csv`）的路径操作，在 Windows / Linux 下均需使用 UTF-8；命令行示例请使用引号包裹。

## 10. 免责声明

本项目仅用于学习研究与合规的信息获取，所有抓取行为需遵守北大法宝的服务条款及相关法律法规。
