# ZooFanCrawler 项目说明

## 项目目标与当前范围

ZooFanCrawler 是一个小型、礼貌的多来源动物园新闻文章采集器。它以
`config/zoos.yaml` 为唯一的站点注册表：当前登记了 Zoo Berlin、Kölner Zoo
和 GaiaZOO 三家动物园，共八个来源。程序从官方站点的 RSS/Atom、Sitemap 或
服务器渲染的新闻归档页发现候选链接，抓取文章页并抽取内容，随后保存到
SQLite；内置的 Flask Dashboard 用于只读查看结果。

项目的设计目标是让不同来源在共享的 URL 政策、抓取礼貌规则、标准化、去重
和持久化流程中运行，同时让单个来源或单篇文章失败不会中断整批采集。它不是
通用网页爬虫：当前不进行 JavaScript 渲染式来源发现，不绕过 robots.txt、限速
或站点访问限制，也不保证官方站点故障时仍能产出文章。

## 架构与数据流

```text
config/zoos.yaml
        │ load_config
        ▼
Zoo / Source records → SourceRegistry → DiscoveryEngine → URLPolicy
                                           │
                      RSS / Sitemap / SSR archive candidates
                                           ▼
                                      Fetcher
                         （robots、域名限速、重试、重定向）
                                           ▼
                                     parse_article
                                           ▼
                                 Crawler orchestration
                                           ▼
                                  SQLiteStorage (SQLite)
                                           ▼
                                  Flask Dashboard（只读）
```

`zoofan.config` 只把 YAML 映射为领域对象，不发网络请求也不初始化数据库。
`zoofan.discovery` 只负责发现候选 URL，不持久化；`zoofan.crawler.Crawler` 负责
按动物园和来源编排、抓取文章、隔离失败并记录运行统计；`zoofan.storage` 是
SQLite schema、迁移与写入的边界。Dashboard 直接以只读查询组织展示数据，
不调用 Crawler 的写入 API。

每次 crawl 先把整个注册表写入数据库（包括 `registry_only` 来源），再按来源
优先级执行：`rss`/`atom` 最先、`sitemap` 其次、`archive` 再后、其他类型最后。
来源发现成功后，候选 URL 会先标准化并经过官方域名与文章路径政策；文章页的
`canonical_url` 会在解析后再次经过同一政策。来源错误记录为该来源的运行统计，
后续来源仍会继续；单篇文章错误则使该来源状态为 `partial`，而不会使整个批次
立刻停止。

### 目录与核心模块

| 路径 | 职责 |
| --- | --- |
| `cli.py` | `crawl`、`scheduler`、`dashboard`、`endgoal` 命令入口。 |
| `config/zoos.yaml` | 当前动物园、官方域名、来源 URL、选择器和 URL 规则。 |
| `zoofan/models.py` | 与存储无关的 `Zoo`、`Source`、`Article`、运行记录等领域数据类。 |
| `zoofan/config.py` | YAML 加载与 archive 选择器配置解析。 |
| `zoofan/discovery.py` | 来源注册、RSS/Sitemap/archive 发现，以及 `URLPolicy`。 |
| `zoofan/fetcher.py` | HTTP 获取、robots.txt、域名间隔、重试与重定向处理。 |
| `zoofan/parsers.py` | RSS/Atom、Sitemap、归档页和文章页解析。 |
| `zoofan/crawler.py` | 单次批处理编排、错误隔离、文章 recheck 和运行统计。 |
| `zoofan/storage.py` | SQLite schema、幂等增量迁移、去重与数据访问。 |
| `zoofan/scheduler.py` | 基于 APScheduler 的周期运行包装。 |
| `zoofan/dashboard.py` 与 `zoofan/templates/` | Dashboard 应用和服务端渲染页面。 |
| `zoofan/endgoal.py`、`Makefile` | 联网端到端验收及报告/截图产物。 |
| `tests/` | parser、策略、抓取、存储、CLI、Dashboard 与验收辅助测试。 |

## 配置、来源与 URL 规则

顶层配置可设置 `user_agent`、`timeout`、`request_delay`、`max_retries`；未设置时
分别使用 `ZooFanCrawler/0.1`、20 秒、1 秒、3 次。每个 `zoos` 项包含稳定的
`id`、`slug`、显示名、官方 `website_url`，以及一个或多个 `sources`。来源常用
字段为 `id`、`name`、`kind`、`url`、`enabled` 与 `config`。

当前配置的来源分布如下：

| 动物园 | 活动发现来源 | 仅登记/健康检查来源 |
| --- | --- | --- |
| Zoo Berlin | `archive` 新闻归档 | `registry_only` 新闻稿页 |
| Kölner Zoo | `sitemap` 新闻 Sitemap | `registry_only` 新闻归档页 |
| GaiaZOO | `rss`、`archive` | 两个 `registry_only`（Sitemap、新闻稿页） |

`rss`、`atom` 和 `feed` 用 XML 解析；`sitemap` 支持 URL set 和 sitemap index，并以
`max_sitemaps`（默认 20，包含根 sitemap）限制继续展开的数量；`archive`、`html`
和 `news_archive` 用配置的 CSS selector 解析服务器渲染 HTML，可由
`pagination_selector` 与 `max_pages` 追踪分页（实现上限为 100 页）。
`registry_only`/`health_check` 类来源只请求并检查状态码，不产生候选文章。

`URLPolicy` 只接受 HTTP(S) 的官方主机（来源 `config.official_host`，或 zoo 的
`website_url`）。`article_allow`/`allow_regex` 可把路径收紧为明确文章路径；
`exclude_regex` 始终拒绝匹配 URL。默认还会排除首页、来源页、票务、商店、餐饮、
联系、隐私/条款、招聘和泛事件/日历等导航 URL。显式允许规则可覆盖默认导航
排除，但不能覆盖明确的排除规则或来源配置的 `navigation_patterns`。

`Fetcher` 默认遵守 robots.txt。若 robots.txt 无法获取或返回错误，会 fail closed；
每个域名有最小请求间隔，网络错误及 429/若干 5xx 会按指数退避重试，重定向目标
也要重新通过 robots 检查。

## 运行方式

先安装运行依赖（Python 3.9+）：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

`crawl` 和 `scheduler` 的默认数据库是 `:memory:`，退出后数据不会保留。因此，
若要以后查看采集结果，请始终显式使用持久路径。Crawl 与 Dashboard 必须指定
**同一个** `--db` 路径：

```sh
python3 cli.py crawl all --db data/zoofan.db
python3 cli.py dashboard --db data/zoofan.db --host 127.0.0.1 --port 8000
```

### 本地 Dashboard 与控制服务

根目录的 `./start.sh` 适用于日常本地查看：它以脚本所在目录定位配置和数据库，
优先使用 `PYTHON_BIN`，然后是 `.venv/bin/python`，最后才是 `python3`。它会检查
Python 版本和已声明的依赖，但不会自行安装；缺依赖时按提示执行
`python3 -m venv .venv` 与 `.venv/bin/python -m pip install -r requirements.txt`。

脚本仅启动两个绑定在 `127.0.0.1` 的服务，且两者都连接同一个持久数据库
`data/zoofan.db`：Dashboard 默认地址为 `http://127.0.0.1:8000`，控制服务默认地址
为 `http://127.0.0.1:8001`。可通过环境变量同时调整端口：

```sh
DASHBOARD_PORT=8100 CONTROL_PORT=8101 ./start.sh
```

启动后会在 macOS 用 `open`、Linux 用 `xdg-open` 尝试打开控制页；设置
`ZOOFAN_NO_OPEN=1` 可禁用。`data/` 不存在时会创建，但脚本不删除、不重建也不自动
crawl 数据库，不会启动 scheduler 或自动 crawl。按 `Ctrl-C` 会停止这两个服务；
控制服务的内存状态会随其重启丢失，SQLite 中的已持久化数据不会丢失。

可按已配置的 slug（或别名 `berlin`、`koeln`、`gaia`）运行单个动物园：

```sh
python3 cli.py crawl zoo-berlin --db data/zoofan.db
python3 cli.py crawl koeln-zoo --db data/zoofan.db
python3 cli.py crawl gaiazoo --db data/zoofan.db
```

全局 CLI 参数必须位于子命令之前，例如自定义注册表应写成：

```sh
python3 cli.py --config config/zoos.yaml crawl all --db data/zoofan.db
```

调度器默认每 6 小时执行一次所有已启用来源；`--once` 只执行一轮并退出：

```sh
python3 cli.py scheduler --db data/zoofan.db --once
python3 cli.py scheduler --db data/zoofan.db --hours 6
```

Dashboard 提供 `/`、`/articles`、`/articles/<article_id>`、`/sources` 与 `/runs`。
其中文章列表支持 `zoo` 和 `source_type` 查询过滤；它不写 crawler 业务表，仅做
SQLite 查询。传入不存在的数据库路径时，底层 SQLite 打开行为仍可能创建一个空
文件。若 Dashboard 和 crawler 同时运行，SQLiteStorage 对文件数据库启用 WAL，并
设置 5 秒 busy timeout；它仍是面向本地、小规模使用的实现。

`articles.raw_html` 是从外站保存的原始、不可信证据。当前 Dashboard 不输出该字段；
后续页面也不得将它直接渲染，或以 `safe` 标记绕过模板转义，否则会引入存储型 XSS。
Dashboard 生成外链时只接受带 `netloc` 的 HTTP(S) URL，并不会重新用 `URLPolicy`
核验官方域名；采集写入时已有政策校验，但展示层不能被视为额外的官方域安全边界。

## SQLite 数据、标准化与去重

`SQLiteStorage` 打开数据库时创建或以增量、幂等方式迁移 schema（当前版本为 3）。
业务数据位于以下六张表：

| 表 | 保存内容 |
| --- | --- |
| `zoos` | 动物园的标识、名称、官方站点、启用状态和 metadata。 |
| `sources` | 每个来源的 URL、类型、配置、检查状态与最近错误/HTTP 状态。 |
| `articles` | canonical/normalized/source URL、文章元数据、正文、原始 HTML 和 hash。 |
| `article_discoveries` | 文章与发现它的来源之间的关系及发现 URL/时间。 |
| `crawl_runs` | 一次批处理的 batch、开始/结束时间、状态和 metadata。 |
| `crawl_run_stats` | 每次运行中每个 zoo/source 的发现、抓取、保存、错误计数和错误详情。 |

`normalize_url` 会统一 scheme 和主机大小写、移除 fragment、移除常见追踪参数（含
`utm_*`）、排序其余 query 参数，并移除非根路径末尾 `/`。来源按
`(zoo_id, normalized_url)` 唯一；文章按 `canonical_url`、`normalized_url`、
`content_hash` 依次查找并以唯一索引约束。`content_hash` 是原始 HTML 的 SHA-256，
而 `html_hash` 为兼容字段。每次发现同时 upsert `article_discoveries`，其唯一键是
`(article_id, source_id, discovered_key)`，所以同一文章可保留多个来源，但重复
发现不会重复插入。

正常第二次 crawl 会通过已存在的 normalized URL 跳过文章页抓取，只更新发现关系。
来源或全局的 `recent_recheck`/`recent_recheck_days` 大于零（或为真）时会重新抓取，
并用新的非空字段补充既有文章。需要注意：文章正文内容哈希并非语义去重；两个
不同 URL 若返回完全相同的原始 HTML，会按该第三层身份合并。

## 测试与端到端验收

快速、离线的回归测试：

```sh
python3 -m pytest -q
```

完整验收入口为：

```sh
make endgoal
```

它先运行完整 pytest 套件，随后加载 `config/zoos.yaml`，删除并重建**指定的**
`data/acceptance.db` 及其 `-wal`、`-shm` sidecar，针对真实官方站点连续运行两次
`crawl all`。验收检查三家配置动物园、每家至少 3 篇有效文章、官方域/URL 政策、
原始 HTML/hash、来源成功状态，以及两轮之间的 canonical URL 去重；另会运行
失败隔离测试，启动同一 acceptance DB 的 Dashboard，并用真实 Playwright/Chromium
检查 `/`、`/articles`、`/sources`、`/runs` 和渲染数量。Playwright 缺少浏览器时，
流程最多尝试一次安装 Chromium。

该命令会联网、耗时较长，并有明确的数据库重建副作用；不要把
`data/acceptance.db` 当作需要保留的本地采集库。流程会在 `finally` 中尽力写出根目录
`artifacts/endgoal-report.json`；只有 Dashboard smoke 成功到达首页时，才会生成
`artifacts/dashboard-acceptance.png`。两者都可用于定位网络、站点或展示层问题，但失败
时不保证存在。

## 已知风险与扩展指引

- `archive` 依赖站点专用 CSS selector；CMS 改版、分页结构变化或客户端渲染会降低发现率。当前没有浏览器作为来源发现的回退方案。
- Sitemap 展开和 archive 分页有上限；很大的站点可能只覆盖一部分历史内容。
- 来源按顺序处理，注册表扩大时，网络时延、站点限流、允许/排除规则维护和验收时间都会线性增加。
- SQLite 适合该项目当前的本地单进程规模；高并发写入、分布式调度、队列重试和生产级观测尚未实现。
- `registry_only` 的成功表示该端点可访问，不表示它产生文章；来源为 `partial` 时，文章错误需从 `crawl_run_stats.errors_json` 或 Dashboard 的运行页诊断。
- Dashboard 的外链过滤只校验 HTTP(S) 与主机是否存在，未重跑官方域策略；若未来导入非 crawler 写入的数据，应先补充展示层的域名校验。
- 端到端验收依赖实时站点与浏览器环境，站点临时故障、robots 变化或页面改版会造成 `make endgoal` 失败，这不应通过把实时 crawl 替换为 fixture 来掩盖。

新增动物园时，先在 `config/zoos.yaml` 中添加稳定的 zoo `id`/`slug`、官方
`website_url` 和窄范围来源规则，再对该 zoo 使用持久数据库作单独 crawl 验证。
为新来源优先选择 RSS 或明确文章路径的 Sitemap；若使用 archive，补充最小必要的
选择器和分页上限。随后新增相应的解析/策略/抓取测试。当前 `endgoal` 明确要求
配置中恰好三家动物园，增加第四家还必须同时有意识地更新验收策略与测试预期。
