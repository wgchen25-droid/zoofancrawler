# ZooFanCrawler 项目说明

## 项目目标与当前范围

ZooFanCrawler 是一个小型、礼貌的多来源动物园新闻文章采集器。它以
`config/zoos.yaml` 为唯一的站点注册表，已完成 Phase0 v0.1 导入：73 个启用
动物园行、116 个来源行、63 个启用来源检查（44 个可产文章来源和 19 个
`registry_only` 健康检查）。程序从官方站点的 RSS/Atom、Sitemap、JSON/custom
adapter framework 或服务器渲染的新闻归档页发现候选链接，抓取文章页并抽取内容，
随后保存到 SQLite；内置的 Flask Dashboard 用于只读查看结果。

名单唯一权威输入是
[`data/zoofan_phase0_zoo_list_v0_1.md`](data/zoofan_phase0_zoo_list_v0_1.md)，完整
文件 SHA-256 为
`6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd`。
其中四行没有核验到官方官网，仍作为 `SOURCE_NOT_FOUND`/`not_found` 行保留，
没有用目录、社交、地方政府或搜索结果 URL 填充。完整计数、分组和 provenance
见 [`reports/zoo-list-summary.md`](reports/zoo-list-summary.md)。

当前验收边界是完整 73 行 Phase0 registry；外部站点限制按明确分类记录，不被
冒充为代码故障，也不通过 robots、认证或 JavaScript 浏览器回退规避。

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
                      RSS / Sitemap / SSR archive / JSON-custom candidates
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
优先级执行：`rss`/`atom` 最先、`sitemap` 其次、`archive`/JSON/custom 再后、
其他类型最后。JSON/custom 是可测试的共享 framework；Phase0 没有新增
site-specific adapter。
来源发现成功后，候选 URL 会先标准化并经过官方域名与文章路径政策；文章页的
`canonical_url` 会在解析后再次经过同一政策。来源错误记录为该来源的运行统计，
后续来源仍会继续；单篇文章错误则使该来源状态为 `partial`，而不会使整个批次
立刻停止。

### 目录与核心模块

| 路径 | 职责 |
| --- | --- |
| `cli.py` | `crawl`、`scheduler`、`validate-config`、`build-acceptance-report`、`dashboard`、`control`、`endgoal` 命令入口。 |
| `config/zoos.yaml` | 当前动物园、官方域名、来源 URL、选择器和 URL 规则。 |
| `zoofan/models.py` | 与存储无关的 `Zoo`、`Source`、`Article`、运行记录等领域数据类。 |
| `zoofan/config.py` | YAML 加载与 archive 选择器配置解析。 |
| `zoofan/discovery.py` | 来源注册、canonical adapter 发现分派，以及 `URLPolicy`。 |
| `zoofan/fetcher.py` | HTTP 获取、robots.txt、域名间隔、重试与重定向处理。 |
| `zoofan/parsers.py` | RSS/Atom、Sitemap、归档页和文章页解析。 |
| `zoofan/crawler.py` | 单次批处理编排、错误隔离、文章 recheck 和运行统计。 |
| `zoofan/storage.py` | SQLite schema、幂等增量迁移、去重与数据访问。 |
| `zoofan/scheduler.py` | 基于 APScheduler 的周期运行包装。 |
| `zoofan/dashboard.py` 与 `zoofan/templates/` | Dashboard 应用和服务端渲染页面。 |
| `zoofan/reporting.py` | 从单一只读 projection 生成五个报告文件。 |
| `zoofan/endgoal.py`、`Makefile` | 联网端到端验收及 JSON/截图产物。 |
| `tests/` | parser、策略、抓取、存储、CLI、Dashboard 与验收辅助测试。 |

## 配置、来源与 URL 规则

顶层配置可设置 `user_agent`、`timeout`、`request_delay`、`max_retries`；未设置时
分别使用 `ZooFanCrawler/0.1`、20 秒、1 秒、3 次。每个 `zoos` 项包含稳定的
`id`、`slug`、显示名、官方 `website_url`、`country_code`、`language`，以及一个
或多个 `sources`。可选 zoo 字段包括 `region`、`city`、`groups`、`enabled`、
`metadata`、`source_status` 和 `list_provenance`。其中 `groups` 保留多重归属，
`source_status` 是登记/研究证据状态，`list_provenance` 用于追溯名单来源；它们
不能替代实时 crawl 结果。来源常用字段为 `id`、`name`、
`kind`/`adapter`、`url`、`language`、`enabled` 与 `config`。

当前三条 generic article adapter kinds 与健康检查类型如下；完整 73 行来源表见
[`reports/zoo-list-summary.md`](reports/zoo-list-summary.md)：

| 类型 | 用途 |
| --- | --- |
| `rss`、`sitemap`、`archive` | 实际复用的 3 个 generic article kinds；不添加 site-specific adapter。 |
| `registry_only` | 健康/登记检查，不产生文章候选；与前三者合计 4 个运行时 source shapes。 |

`rss`、`atom` 用 XML 解析；`sitemap` 支持 URL set 和 sitemap index，并以
`max_sitemaps`（默认 20，包含根 sitemap）限制继续展开的数量；`archive`、`html`
和 `news_archive` 用配置的 CSS selector 解析服务器渲染 HTML，可由
`pagination_selector` 与 `max_pages` 追踪分页（实现上限为 100 页）。
`registry_only`/`health_check` 类来源只请求并检查状态码，不产生候选文章。

canonical adapter/status vocabulary is `rss`、`atom`、`sitemap`、`archive`、
`json_api`、`custom_adapter`、`registry_only`、`unsupported`（8 类）。没有新增
site-specific adapter；`rss`、`sitemap`、`archive` 是实际复用的 3 个 generic
article kinds，`registry_only` 是第四个 health shape。JSON/custom 是共享
framework 并有确定性测试。
`html`/`news_archive` 是 archive 的兼容别名。运行状态区分成功、无条目、partial、
failed、robots、HTTP、timeout、DNS/TLS、parse、blocked、unsupported、configuration、
`date_parse_error`、`content_parse_error` 和 `javascript_required` 等稳定分类；最终完成报告再映射为大写
终结类别。

`URLPolicy` 只接受 HTTP(S) 的官方主机（来源 `config.official_host`，或 zoo 的
`website_url`）。`article_allow`/`allow_regex` 可把路径收紧为明确文章路径；
`exclude_regex` 始终拒绝匹配 URL。默认还会排除首页、来源页、票务、商店、餐饮、
联系、隐私/条款、招聘和泛事件/日历等导航 URL。显式允许规则可覆盖默认导航
排除，但不能覆盖明确的排除规则或来源配置的 `navigation_patterns`。

`Fetcher` 默认遵守 robots.txt。若 robots.txt 无法获取或返回错误，会 fail closed；
每个域名有最小请求间隔，网络错误及 429/若干 5xx 会按指数退避重试，重定向目标
也要重新通过 robots 检查。
当前请求使用默认 30 秒 TTL 的 bounded response/page cache；每域序列化为 1，
全局也保持串行。重复发现仍依靠 normalized URL、content identity 和 storage 唯一约束
去重，并继续执行 robots、delay、重试/退避和重定向检查。

## Phase0 live evidence and acceptance boundary

Fresh temporary live runs covered all 73 enabled zoos in both rounds:

| 轮次 | Run ID | Zoo coverage | Articles | Discovery relations | Inserted | Updated |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | `1f0f213ae0d34af9836c3a6fe0e20d3b` | 73/73 | 31 | 34 | 31 | — |
| 2 | `c9735cb5d87f4a5f918e2ce1f4530c47` | 73/73 | 31 | 34 | 0 | 0 |

The second round inserted zero new articles and updated zero existing rows.
The zoo-level classifications are `SUPPORTED` 25,
`SUPPORTED_NO_CURRENT_NEWS` 12, `ROBOTS_DISALLOWED` 19,
`SOURCE_NOT_FOUND` 11, `UNSUPPORTED` 4, `BLOCKED` 2, and zero for
`JAVASCRIPT_REQUIRED`, `PARTIALLY_SUPPORTED`, and `FAILED` (73 total).
There were 13 source errors (12 robots decisions plus one ZSL HTTP 403) and
eight site failures; every one is an external limitation. There are no parser,
configuration, storage, runtime, or quality blockers.

The existing Zoo Berlin/Kölner Zoo regression evidence remains stored. GaiaZOO
is represented by the external no-current-items result. These statements do
not claim that a limited site was crawled successfully; they record the
fail-closed acceptance boundary. The fresh double crawl is equivalent live
evidence in a temporary database, not a claim that `make endgoal` regenerated
the historical root artifacts.

Static acceptance browser smoke generation is
`c49257ece8394a83ae072175f9c05483`: actual DOM 73 rows, `Zoo Berlin` search 1,
`SUPPORTED` filter 25, working detail view and safe links, with no `raw_html`,
overflow, console errors, or page errors. The final offline evidence is
**588 passed, 0 skipped** under `PYTHONWARNINGS=error`. The full decision and
section matrix are in [`reports/final-completion-report.md`](reports/final-completion-report.md).

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
python3 cli.py --config config/zoos.yaml crawl all --db data/zoofan.db
python3 cli.py --config config/zoos.yaml dashboard --db data/zoofan.db --host 127.0.0.1 --port 8000
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

全局 CLI 参数（如 `--config`、`--log-level`）必须位于子命令之前，例如：

```sh
python3 cli.py --config config/zoos.yaml crawl all --db data/zoofan.db
```

对同一持久数据库生成只读报告：

```sh
python3 cli.py --config config/zoos.yaml build-acceptance-report \
  --db /path/to/acceptance.db --output-dir reports
find reports/latest -maxdepth 2 -type f -print | sort
```

报告构建会先拒绝空文件、无效 SQLite、缺少 `zoos`/`articles`/`crawl_runs` 表或没有
任何 zoo/run evidence 的数据库。`data/zoofan.db` 若是零字节占位文件，不会被当作
成功报告；请显式传入可用数据库。生成报告不会执行 crawl，也不会替换数据库。

严格校验配置（只读、不联网）：

```sh
python3 cli.py --config config/zoos.yaml validate-config --json
```

该命令把五个文件写到 `reports/latest/`：`run-summary.json`、
`run-summary.md`、`zoo-status.csv`、`articles.json` 和
`acceptance/index.html`。它使用单一只读 projection，保留无关文件，并且不把
`articles.raw_html` 输出到报告；另写入 metadata-only 的 `manifest.json` 作为带五个
payload 哈希的完整生成提交标记（不计作第六个报告视图）。

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

`SQLiteStorage` 打开数据库时创建或以增量、幂等方式迁移 schema（当前版本为 **7**，
同时写入 `PRAGMA user_version` 与 `schema_meta`）。迁移会兼容旧数据库、回填必要
字段并使用 `IF NOT EXISTS` 建立索引，不删除用户数据库。业务数据位于以下表：

| 表 | 保存内容 |
| --- | --- |
| `zoos` | 动物园的标识、名称、官方站点、启用状态和 metadata。 |
| `sources` | 每个来源的 URL、类型、配置、检查状态与最近错误/HTTP 状态。 |
| `articles` | canonical/normalized/source URL、raw URL/timestamps、文章元数据、正文、原始 HTML 和 hash。 |
| `article_discoveries` | 文章与发现它的来源之间的关系及发现 URL/时间。 |
| `crawl_runs` | 一次批处理的 batch、开始/结束时间、状态和 metadata。 |
| `crawl_run_stats` | 每次运行中每个 zoo/source 的发现、抓取、保存、错误计数和错误详情。 |
| `article_zoo_identities` | 文章与 zoo 的标题身份关系和唯一约束。 |
| `crawl_zoo_results` | 每次 run 的 zoo 级结果、状态、计数和错误类别。 |
| `schema_meta` | schema 版本元数据。 |

`normalize_url` 会统一 scheme 和主机大小写、移除 fragment、移除常见追踪参数（含
`utm_*`）、排序其余 query 参数，并移除非根路径末尾 `/`。来源按
`(zoo_id, normalized_url)` 唯一；文章按 `canonical_url`、`normalized_url` 和
`content_identity_key` 以唯一索引约束。后者是 `content_hash` 与规范化标题组合后
计算的稳定 key；`content_hash` 是规范化 parsed article identity content 的哈希，
而 `html_hash` 是原始 response bytes 的哈希。`content_hash` 不再作为全局唯一身份，
而 `html_hash` 为兼容字段和原始响应证据。每次发现同时 upsert
`article_discoveries`，其唯一键是 `(article_id, source_id, discovered_key)`，所以同一
文章可保留多个来源，但重复发现不会重复插入。

正常第二次 crawl 会通过已存在的 normalized URL 跳过文章页抓取，只更新发现关系。
来源或全局的 `recent_recheck`/`recent_recheck_days` 大于零（或为真）时会重新抓取，
并用新的非空字段补充既有文章。需要注意：文章正文内容哈希并非语义去重；两个
不同 URL 若解析出完全相同的 identity content 和规范化标题，会按该第三层身份合并；
原始 HTML 的哈希只作为响应证据保存。

## 测试与端到端验收

快速、离线的回归测试：

```sh
PYTHONWARNINGS=error python3 -m pytest -q
```

当前 quality gate evidence 为 **588 passed, 0 skipped**（`PYTHONWARNINGS=error`）；
Ruff lint、mypy、strict config validation、compileall 和 `git diff --check` 也通过。可分别运行
`make config-tests`、`make fixture-tests`、`make integration-tests`、
`make golden-tests` 和 `make quality-tests`；严格配置命令为
`python3 cli.py --config config/zoos.yaml validate-config --json`。这些数字和结果是
记录时点的 evidence，代码变化后应重新执行。

测试分组是显式且互不重叠的：

| Make target | Test files |
| --- | --- |
| `config-tests` | `tests/test_config.py` |
| `fixture-tests` | `tests/test_fetcher.py`、`tests/test_normalization.py`、`tests/test_parsers.py`、`tests/test_url_policy.py` |
| `integration-tests` | `tests/test_cli.py`、`tests/test_control.py`、`tests/test_crawler.py`、`tests/test_dashboard.py`、`tests/test_failure_isolation.py`、`tests/test_live_smoke.py`、`tests/test_module_cli.py`、`tests/test_start_script.py`、`tests/test_storage.py` |
| `golden-tests` | `tests/test_endgoal.py`、`tests/test_reporting.py` |
| `quality-tests` | `tests/test_packaging.py`、`tests/test_quality_gates.py` |

它们共同覆盖每个 `tests/test_*.py` 文件一次；`make test` 仍然运行完整套件。
`offline-gates` 是不递归调用 Make 的离线总闸门，直接运行 validate、lint、typecheck、
完整 pytest 和 report build，但必须显式提供非空且含有效 zoo/run evidence 的
`OFFLINE_DB`：

```sh
make offline-gates \
  OFFLINE_DB=/path/to/acceptance.db \
  REPORT_DIR=/tmp/zoofancrawler-reports
```

test taxonomy 包括 `tests/test_live_smoke.py` 的 bounded integration coverage 和
`tests/test_packaging.py` 的 wheel/package-data contract；这些是当前 scope 的
确定性门禁。最终完整套件结果为 588 passed、0 skipped，且 warnings treated as errors。

等价的 fresh Phase0 双轮 live 复现、报告构建和静态 HTML 服务命令如下；使用临时
数据库不会覆盖仓库内的数据库或历史 root artifacts：

```sh
LIVE_DIR="$(mktemp -d /tmp/zoofancrawler-phase0-live.XXXXXX)"
LIVE_DB="$LIVE_DIR/acceptance.db"
python3 cli.py --config config/zoos.yaml crawl all --db "$LIVE_DB" | tee "$LIVE_DIR/run-1.json"
python3 cli.py --config config/zoos.yaml crawl all --db "$LIVE_DB" | tee "$LIVE_DIR/run-2.json"
python3 cli.py --config config/zoos.yaml build-acceptance-report \
  --db "$LIVE_DB" --output-dir reports
python3 -m http.server 8765 --directory reports/latest
```

本次 evidence 的 run IDs 为
`1f0f213ae0d34af9836c3a6fe0e20d3b` 与
`c9735cb5d87f4a5f918e2ce1f4530c47`；每轮均有 73/73 zoo results。浏览器 smoke
generation 为 `c49257ece8394a83ae072175f9c05483`：actual DOM 73 rows，搜索
`Zoo Berlin` 得 1 行，`SUPPORTED` 得 25 行，detail works、safe links 存在，且无
`raw_html`、overflow、console errors 或 page errors。

完整验收入口为：

```sh
make endgoal
```

它先运行完整 pytest 套件，随后加载 `config/zoos.yaml`，删除并重建**指定的**
`data/acceptance.db` 及其 `-wal`、`-shm` sidecar，针对真实官方站点连续运行两次
`crawl all`。验收按 73 个启用 zoo 检查有效文章、官方域/URL 政策、
原始 HTML/hash、来源成功状态，以及两轮之间的 canonical URL 去重；另会运行失败
隔离测试，启动同一 acceptance DB 的 Dashboard，并用真实 Playwright/Chromium 检查
`/`、`/articles`、`/sources`、`/runs`、静态 acceptance view 和渲染数量。Playwright 缺少浏览器时，流程
最多尝试一次安装 Chromium。后续 roster 版本变化时，验收覆盖范围随配置变化，并需
同步评估相应的数量、来源和策略预期。

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

后续 roster 变更必须先发布新的权威版本与 provenance，再在
`config/zoos.yaml` 中更新稳定 ID、官方 URL、分组和窄范围来源规则；四个当前
no-site 行在有明确官方 endpoint 前不得填入猜测 URL。为新来源优先选择 RSS 或
明确文章路径的 Sitemap；若使用 archive，补充最小必要的选择器和分页上限。随后
新增相应的解析/策略/抓取测试，并更新验收策略所需的来源、文章质量和 Dashboard
预期。`reports/zoo-list-blocker.md` 仅保留为历史缺失名单审计，不再是 Phase0
覆盖的阻塞条件。
