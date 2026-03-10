# law_query

进行检索并查询法律法规结果列表（标题、URL、公布日期）。

## 目标
使用检索词 **“智能”**（可通过参数修改），在标题检索后分别查询：

1. **法律法规 → 中央法规**：筛选 **“本月生效”**
2. **法律法规 → 地方法规**：筛选 **“本月生效”**

并导出相应结果的：**title / url / publish_date**。

## 环境准备

- Python 3.10+（建议 3.12）
- 安装依赖：

```bash
pip install -r requirements.txt

## GitHub Pages 部署

本项目包含自动更新 GitHub Pages 的功能。

1.  **启用 GitHub Pages**：
    - 进入仓库 **Settings** -> **Pages**。
    - 在 **Source** 下选择 **Deploy from a branch**。
    - 在 **Branch** 下选择 `main` 分支（或你当前的分支），文件夹选择 `/ (root)`。
    - 点击 **Save**。

2.  **自动更新**：
    - GitHub Actions 会在每天北京时间 08:00 自动运行查询脚本。
    - 若有新法规，`法规.csv` 会被更新，网页也会随之更新。
    - 你也可以在 **Actions** 标签页手动运行 "Update Law Query Data" 工作流。

3.  **访问网页**：
    -设置完成后，网页地址通常为 `https://<你的用户名>.github.io/law_query/`。

```

## 快速运行

```bash
python query.py --keyword 智能 --out results.csv
```

同时输出 JSON：

```bash
python query.py --keyword 智能 --out results.csv --out-json results.json
```

## 常用参数

- `--keyword`：检索词（默认：智能）
- `--headless / --headed`：无头/有头模式（默认无头）
- `--max-items`：最多查询多少条（0 表示不限制，默认 0；对中央/地方都生效）
- `--slow-mo`：调试用，放慢浏览器操作（毫秒）
- `--user-data-dir`：使用持久化浏览器目录保存 cookie（可用于需要登录的情况）

示例（有头 + 放慢 + 限制 200 条）：

```bash
python query.py --headed --slow-mo 200 --max-items 200
```

## 登录/权限说明

- 本脚本只查询**列表页**信息，通常无需登录。
- 若你在本地运行时遇到“需要登录/验证码/无法翻页”等情况：
  1. 用 `--headed --user-data-dir ./user_data` 运行一次
  2. 在弹出的浏览器里手动登录
  3. 以后继续用相同的 `--user-data-dir` 运行即可复用登录态

## 输出格式

CSV 列：

- `category`：central 或 local
- `title`
- `url`
- `publish_date`：YYYY.MM.DD

## 免责声明

仅用于学习研究与合规的信息获取，请遵守网站服务条款与相关法律法规。
