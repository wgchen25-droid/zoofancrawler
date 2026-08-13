# ZooFanCrawler 开发指南

## 项目定位与入口

ZooFanCrawler 是一个面向已配置动物园官网的、礼貌抓取新闻文章的小型 Python 爬虫：配置提供动物园和来源；发现层产出候选 URL；Crawler 获取并解析文章、去重并写入 SQLite；Flask Dashboard 只读展示抓取结果。

关键路径：

- `config/zoos.yaml`：生产站点注册、官网/来源 URL、selector 和 source 定义的唯一事实来源。现有 CLI/crawler 兼容 alias 和配置集成测试可以显式列出当前 zoo；不要在 crawler、scheduler 或解析逻辑中硬编码生产站点、来源 URL 或 selector。
- `cli.py`：命令行入口；`zoofan/config.py` 和 `zoofan/models.py`：配置加载与领域记录。
- `zoofan/discovery.py`、`zoofan/parsers.py`：候选 URL 发现和 RSS/Atom、sitemap、SSR archive 解析。
- `zoofan/fetcher.py`：robots、按域名限速、重定向和重试。
- `zoofan/crawler.py`：批次编排、失败隔离、文章获取和持久化调用。
- `zoofan/storage.py`：SQLite schema、迁移和唯一写库 API。
- `zoofan/dashboard.py` 与 `zoofan/templates/`：只读浏览界面。
- `zoofan/endgoal.py`：真实联网端到端验收；不是普通单元测试替代品。

运行环境为 Python 3.9+。常用检查只使用已声明的命令：

```sh
python3 -m pytest -q
python3 cli.py crawl all --db data/zoofan.db
python3 cli.py scheduler --db data/zoofan.db --once
python3 cli.py dashboard --db data/zoofan.db --host 127.0.0.1 --port 8000
make endgoal
```

`--config` 和 `--log-level` 是 CLI 全局参数，必须放在子命令之前，例如：

```sh
python3 cli.py --config config/zoos.yaml --log-level DEBUG crawl berlin --db data/zoofan.db
```

`crawl`、`scheduler` 的 `--db` 是子命令参数，可放在其子命令之后。Dashboard 必须连接到实际 crawl 使用的同一个持久数据库路径；它不是 crawl 的写入入口。

## 站点与网络规则

新增或修改生产动物园、来源、selector、allow/exclude 规则时，先更新 `config/zoos.yaml`，由 `load_config` 加载、`Crawler` 编排 `SourceRegistry` 注册并经 storage 持久化。每个来源需要稳定 ID、kind、官方 host 和收窄的文章规则；不要用宽泛路径或第三方域名“提高命中率”。当前发现 kind 为 `rss`/`atom`、`sitemap`、`archive` 和仅健康检查的 `registry_only`；新增或变更 kind 时必须同时实现发现分派、对应解析/限制，并新增配置加载和行为测试（当前没有通用的严格 config validation 层）。

- 来源 URL、候选 URL、canonical URL 及用于 identity 的 URL 都必须经过 `normalize_url`。不要自行比较未规范化 URL，也不要绕开 `SourceRegistry` 的规范化注册。
- 发现出的文章 candidate 以及抓取后文章 canonical URL 必须通过 `URLPolicy.from_zoo_source(...).accepts(...)`。它要求 http(s) 官方域名，执行 allow/exclude 与导航误报过滤，并拒绝首页和来源页。显式 allow 只能覆盖内置导航过滤，不能覆盖域名、scheme 或显式 exclude。
- 正常网络请求走 `Fetcher`。保持 robots 默认启用且 **fail-closed**：robots 不可读取/不可解析、目标不允许、或重定向目标不允许时都不得继续抓取。保留每域限速、有限重试/退避、`Retry-After` 和最大重定向限制；重定向目标同样要重新检查 robots。现有 `URLPolicy` 不会对 source URL、sitemap 子文件或 archive 分页等中间请求逐一做官方域校验；修改这些遍历或重定向逻辑时不得扩大到非官方域，并以 fake transport 覆盖该边界。
- 不要用 Playwright、浏览器自动化或禁用 robots 作为隐藏的 source 抓取回退。archive 只解析服务端渲染 HTML 和配置的 CSS selector。
- 当前来源发现/文章抓取的网络或解析失败，会记录 source/run stats 后继续其他来源和动物园；source 状态更新也受保护，不会阻断后续来源。不要把这项隔离扩展性假定为所有 storage 写入错误都可继续；改变写入错误处理时须明确其批次语义并补测。

涉及网络行为、URL 策略或新增/变更 source kind 的提交，必须在 `tests/` 增加可注入 transport/fetcher 的确定性测试，覆盖允许路径和至少一个拒绝、跨域防护或失败路径；测试不能依赖真实官网、时间或网络。

## 分层与存储

- `config.py`、`models.py` 只处理数据和验证，不发网络请求、不写 SQLite。
- `DiscoveryEngine` 与 `parsers.py` 不持久化：前者将一个 `Source` 转为 `DiscoveryCandidate`，后者只负责解析。`Crawler` 负责编排注册、文章页获取、解析、去重和通过 storage 写入。
- `Crawler` 是发现、抓取和写入的协调边界；通过现有 storage 方法（如 `upsert_*`、`record_discovery`、run/stat API）写入。不要从 parser、fetcher、dashboard 或新 source adapter 直接执行 SQLite 写操作。
- `SQLiteStorage` 是 schema 和持久化唯一边界。文章 identity 由 canonical URL、normalized URL 和 content hash 的唯一约束保护；跨来源发现关系写入 `article_discoveries`，不要复制文章记录来规避去重。

任何 schema 变更必须在 `SQLiteStorage.create_schema()`/`_migrate_schema()` 实现为可重复执行的增量迁移：兼容已有数据库、补齐/回填必要旧列和数据、在数据清理后建立 `IF NOT EXISTS` 索引。不得假设 fresh DB、不得删除用户数据库、不得将不可恢复 schema 重建藏在启动流程。必须补充 legacy schema migration 的幂等测试，以及相关 upsert/去重/关系写入测试。

## Dashboard 与测试

Dashboard 对 SQLite 仅执行 SELECT，视图层必须保持只读；不要在路由、模板或页面动作中触发 crawl、修改来源或写入运行状态。新增页面/字段必须保持缺失的可选表或列不会 500，且使用 Flask/Jinja 自动转义的普通展示路径。

`articles.raw_html` 是抓取和验收的原始证据，**绝不能在任何 Dashboard 页面渲染或标记为安全 HTML**。需要展示内容时使用已解析、转义后的字段；当前外链仅在 URL 为 http(s) 且有 netloc 时输出，并带安全的 `rel` 属性，Dashboard 不会执行 `URLPolicy` 官方域校验。若未来收紧外链政策，必须为允许/拒绝 URL 补测试。Dashboard 改动必须覆盖全部相关路由、过滤/空数据/不存在记录，以及 `raw_html` 不出现于响应的测试。

测试按行为就近放在 `tests/test_config.py`、`test_parsers.py`、`test_crawler.py`、`test_failure_isolation.py`、`test_storage.py`、`test_dashboard.py`、`test_cli.py` 或 `test_endgoal.py`。使用 `SQLiteStorage(':memory:')` 和 injected fake fetcher/transport；对 crawler 改动至少覆盖成功、去重及来源失败隔离。提交前运行 `python3 -m pytest -q`。

## 真实验收与仓库卫生

`make endgoal` 会运行测试、对 `config/zoos.yaml` 中所有已启用动物园下的已启用来源执行两轮真实联网 crawl，并在真实 Playwright 浏览器中 smoke-test Dashboard。网络、官网可用性和已安装的可用依赖是 PASS 条件；首次需要时可能会为当前 Python 环境安装 Chromium。它会**仅**删除并重建明确的 `data/acceptance.db`，不会替代日常快速测试，也不能用于清理其他数据库。

Endgoal 会在 `finally` 中尽力写入 `artifacts/endgoal-report.json`；只有 Dashboard smoke 成功到达首页时才生成 `artifacts/dashboard-acceptance.png`。两者用于诊断，不应将它们都表述为失败时必定存在。不要把 `.venv/`、`__pycache__/`、pytest/browser cache、普通本地 SQLite 数据库或其 `-wal`/`-shm` 文件提交为功能成果；除根目录要求保留的验收 report/screenshot 外，临时截图和运行副产物应留在工作区外或清理。不要删除已有 `data/` 或 `artifacts/` 内容，除非任务明确指定了精确目标。
