# ZooFanCrawler Crawler Console P0 架构

> 文档状态：P0 架构、实现契约与当前工作区验收证据。
>
> 事实基线：以当前工作区运行时和验收脚本的实际结果为准；未通过的 gate
> 必须保留为 blocker，不能由文档改写成完成。当前 gate 已通过；局部
> PARTIAL/TODO 仍按下文和 TODO 清单保留。

当前验收快照（2026-08-16）：schema version 与 `PRAGMA user_version` 均为 **12**，
完整回归 **706 passed**、0 failed，ruff/mypy 均通过；`python3
scripts/verify_crawler_console.py` 为 **PASS**，Blockers **0**。Failure 场景使用
确定性的 HTTP 503 和 recovery source，真实浏览器 visual evidence、API/UI、Normal、
Budget Hit 和 Failure / Source Isolation evidence 均为 PASS。完整回归通过不等于
所有 Nice-to-have 或 Future/P1 已实现。

## 1. Purpose

Crawler Console 是一个面向内部运维的只读观察面。它要让使用者在浏览器中快速回答：

- 当前是否有 crawl 正在运行，运行的是哪一个 Run；
- 当前处理了哪些 Zoo / Source；
- discovered、fetched、stored、already-known、duplicate 和 error 的数量；
- 哪个 Zoo / Source 失败，失败的 HTTP、解析或策略原因是什么；
- crawl 为什么结束，尤其是是否触发了一个结构化 budget/stop reason；
- 本次 run 最终存储了哪些 Article；
- 当前 run 与最近历史 run 是否出现明显变化。

它不是日志搬运页。展示优先级为：

~~~text
Structured State → Operational Events → Developer Diagnostics
~~~

状态和统计来自持久化运行记录，结构化事件来自独立的 event sink；现有
Python logging 仍主要服务开发者诊断。

## 2. 当前仓库基线（已核对）

当前仓库没有既有 docs/ Feature 文档目录约定，因此本文件采用项目现有
Markdown 风格，并保持现有 flat zoofan/ 包的命名方式。

本节的“当前基线”指当前工作区实际可导入、可查询的实现。当前
`SQLiteStorage.SCHEMA_VERSION = 12`，验收脚本会从 fresh fixture 的
`schema_meta`/`PRAGMA user_version` 和真实表列、索引读取版本证据；本次文档
同步不替 `zoofan/`、模板或 tests 的现状宣称完成。

| 现有模块 | 当前职责与对 Console 的意义 |
| --- | --- |
| config/zoos.yaml、zoofan/config.py | 配置和来源注册的唯一事实来源；不应在 Console 中复制 Zoo、Source 或 selector。 |
| zoofan/models.py | Zoo、Source、Article、ArticleDiscovery、CrawlRun、CrawlRunStat、CrawlZooResult、ArticleReadModel 等领域记录。 |
| zoofan/discovery.py、zoofan/parsers.py | 发现候选 URL、解析 RSS/Atom、sitemap、SSR archive 和文章；不负责持久化。 |
| zoofan/fetcher.py | robots、官方域、限速、重试、Retry-After 和重定向边界。 |
| zoofan/crawler.py | 以 Zoo / Source 为边界编排发现、文章抓取、去重、错误隔离，并调用 storage 写入 run/stat/result。 |
| zoofan/storage.py | SQLiteStorage 是 schema、幂等迁移和写入唯一边界；当前 SCHEMA_VERSION = 12，包含 events、run-article outcomes、stop_reason 和 run-scoped activity 列。 |
| zoofan/dashboard.py、zoofan/templates/ | 当前 Flask server-rendered、只读 Dashboard；同一 app 已挂载 Console shell/API，保留 /、/articles、/articles/<article_id>、/sources、/runs 兼容路由。 |
| cli.py、zoofan/scheduler.py | CLI / scheduler 启动 crawl；--log-level 通过标准库 logging 配置输出，默认主要到 stderr。 |
| zoofan/control.py | 已存在的独立 loopback 控制面；它不是本次只读 Console 的控制 API。 |
| tests/ | 除 crawler、failure isolation、storage、dashboard、CLI 和 endgoal 外，工作区还包含 Console API、event storage/crawler、service 和 UI contract 测试；当前完整回归已通过，局部未完成能力见 TODO。 |

当前 SQLite 表还包括 `crawl_run_events` 和 `crawl_run_articles`；JSON Console
API、结构化 `stop_reason`、`heartbeat_at`、`progress_at`、
`current_zoo_id/current_source_id/current_phase` 与 `progress_json` 已进入
当前 schema。`article_discoveries` 仍不是 run outcome 表，Run Detail 的
run-scoped article 证据必须走 `crawl_run_articles`，不能用时间窗口猜测。
`crawler_leases` 仍是进程级 lease；是否 active 还要结合 run-scoped activity，
缺失证据必须显示 unknown/no-data。

旧 Dashboard 的 `_load_data()` 仍通过 query-only SQLite 连接读取兼容页面数据，
不会调用 Crawler，也不会渲染 `articles.raw_html`。Console 页面则通过
`console_repository` → `console_service` → `console_api` 读取 JSON；验收脚本
用 AST/导入检查确认 frontend 不执行 SQL、API/service 不拥有 SQL，且 API 仍挂在
现有 Flask app 上。当前 runtime regression 已全绿；这些是边界证据，不能把
未实现的 Nice-to-have/Future/P1 写成完成。

## 3. Scope

P0 交付的 Console 由同一个 Flask app 提供，使用与 crawl 相同的持久 SQLite
路径；浏览器只观察，不改变 crawl 状态。

### 3.1 页面范围

逻辑页面和主要内容如下。页面 URL 不是另一个 HTTP server；实现时可在现有
Dashboard 中挂载 /crawler-console 前缀，避免破坏已有 /、/runs、
/articles 和 /sources 兼容路由。

| 页面 | P0 内容 |
| --- | --- |
| Overview | 当前 run 的 status、Run ID、开始时间、elapsed、Zoo/Source 完成度、discovered/fetched/stored/duplicate/error，以及当前活动行。无 active run 时显示最近 run 和明确的 empty state。 |
| Runs | crawl_runs 最新在前；按 status 过滤；显示时间、duration、Zoo/Source 数、各类计数和 errors；可进入 Run Detail。 |
| Run Detail | run 总结、Zoo/Source 结果表、status 分类、HTTP/error、duration、结构化 stop_reason、budget evidence、Stored Articles 和 Events。 |
| Zoo Detail | Zoo 名称、国家、当前状态、Source enabled/HTTP/last success/last error，以及按时间倒序的 crawl history。无历史时显示 No crawl history。 |
| Article Preview | 本次 run 的新存储 Article 列表；至少有 title、Zoo、Source、published at、fetched at、canonical URL、language、image count（有数据时）。详情只显示解析后的、自动转义的字段和结构化 metadata。 |
| Events | 当前 run 的结构化 operational event 时间线，支持 level、Zoo、Source、component、event type 和文本搜索过滤。 |

页面必须有 loading、empty、error、active polling、last updated 状态；桌面优先
但表格在小屏上允许横向滚动。Running、Success、Warning、Failed、Budget Hit
必须有明显且一致的视觉区分。

### 3.2 数据范围

P0 复用现有运行和内容数据，不复制生产站点配置：

- Zoo / Source 名称、enabled、kind、国家和 URL 来自 registry/storage；
- run 总计从 crawl_run_stats 和 crawl_zoo_results 的明确字段聚合；
- Article 身份和跨来源关系仍由 articles、article_discoveries、
  article_zoo_identities 负责；
- stop_reason 是结构化状态，不从任意日志文本猜测；
- event 只记录有限的 operational milestones，不把所有 DEBUG logging 写入 SQLite；
- 预算仅展示当前系统实际能观察到的值；不存在的 global fetched/runtime budget
  显示为 not implemented / not observable，不为了 UI 重写现有 crawler。

### 3.3 明确 Non-goals

本 P0 不实现或扩展：

- Start、Stop、Restart、Kill、Retry Failed、Run All 等 crawler control；
- 浏览器启动 subprocess、task queue、命令通道或 WebSocket command channel；
- 把现有 zoofan/control.py 合并到 Dashboard 或让 Crawler 依赖 HTTP server；
- Kafka、Redis、Celery、Elasticsearch、Loki、Grafana、Prometheus 等重型基础设施；
- React microfrontend、第二个 web server 或为了 Console 引入新的分布式服务；
- Playwright/浏览器自动化作为 crawler source 抓取回退，或绕过 robots；
- 把 stderr 原样搬进网页、持久化全部 DEBUG/套接字/selector/redirect 细节；
- P1 CMS、Zoo 管理、Source 编辑、文章编辑、发布、删除或权限管理；
- 自动 anomaly detection 的最终判定。P0 只展示计数、budget 命中和事件证据；
- 在任意 Dashboard/Console 页面渲染或标记 articles.raw_html 为安全 HTML。

## 4. Target Architecture

~~~text
config/zoos.yaml
        │
        ▼
CLI / scheduler ──► Crawler Core ──► Fetcher / Discovery / Parser
                         │
                         ├──► SQLiteStorage（runs、stats、results、articles）
                         │          │
                         │          └──► read repositories
                         │                    │
                         └──► EventSink ───────┘
                                    │
                                    ▼
                            crawl_run_events
                                    │
                    Query repositories / DTO services
                                    │
                     Flask JSON API（只读 /api/*）
                                    │
                Flask/Jinja Console shell + browser polling
~~~

logging 是旁路开发者诊断：

~~~text
Crawler / Fetcher / Storage logging ──► stderr
Crawler operational emit             ──► EventSink ──► SQLite events
~~~

### 4.1 分层职责

| 层 | 责任 | 禁止事项 |
| --- | --- | --- |
| Crawler Core | 编排 crawl、统计、错误隔离和少量 lifecycle event emit。 | 不导入 Flask、模板、HTTP route 或浏览器代码。 |
| Event abstraction | 接收稳定 event DTO，并负责安全序列化与持久化。 | 不解析 HTML、不保存 exception/response 对象、不承担页面渲染。 |
| SQLiteStorage / repositories | schema、migration、聚合读取和写入；读写连接职责分离。 | API route 不直接拼复杂 SQL；parser/fetcher 不写 SQLite。 |
| Services | get_active_run()、get_run_summary()、get_run_zoo_results()、get_run_articles()、get_run_events()、get_zoo_history() 等单一用途查询和业务映射。 | 不返回模板对象，不把 HTML 混进 SQL。 |
| API / DTO | 校验参数、调用 service、序列化稳定 JSON、返回 200/400/404/5xx 契约。 | 不直接访问 SQLite connection，不暴露 raw HTML、堆栈或 secrets。 |
| Console UI | 只消费 /api/* JSON；显示状态、表格、过滤、polling、loading/empty/error。 | 新 Console 页面不直接查询 SQLite，也不触发 crawl/control。 |
| Existing Dashboard | 继续提供当前兼容的 server-rendered 只读页面；迁移时可复用服务层。 | 不因 P0 复制一套新的数据库读取真相。 |

当前项目没有 repositories/、services/、api/ 子包；实现时应优先沿用
zoofan/ flat package 和现有函数命名，按职责拆成小模块或 Blueprint，
而不是为了匹配概念目录建立一个新的 God File。

### 4.2 连接和进程边界

- Crawler 继续由 cli.py crawl 或 scheduler 启动；Dashboard 不启动它。
- Console API 使用 read-only connection，保留当前 PRAGMA query_only = ON
  和“缺失数据库显示空状态、不创建数据库”的行为。
- crawl 和 Dashboard 必须指向同一个持久数据库路径；:memory: 只用于测试。
- crawler_leases 的 owner/lease_until/heartbeat_at 可作为 active-run liveness
  的证据，但 API 不为了显示状态偷偷修改 lease 或 run。
- 旧数据库没有 event table 时，页面应返回空 events，而不是 500；正常 storage
  初始化仍必须执行可重复的增量 migration。

## 5. Data Model

### 5.1 现有表的使用语义

| 表 | Console 使用方式 |
| --- | --- |
| zoos | Zoo identity、name、slug、country/language、enabled；详情页的静态配置和关联来源。 |
| sources | zoo_id、kind、enabled、status、last_checked、last_success、last_error、last_http_status；Source health 摘要。 |
| crawl_runs | 一次 batch 的 run identity、started_at、finished_at、duration_ms、status、safe error、metadata_json；run list/detail 的主记录。 |
| crawl_run_stats | 一个 run 下的 Zoo/Source 统计；discovered_count、fetched_count、stored_count、already_known_count、duplicate_candidate_count、error_count、errors 和 metadata。Source 表格的主要计数来源。 |
| crawl_zoo_results | 一个 (crawl_run_id, zoo_id) 的聚合结果；discovered、parsed、inserted、updated、failed、duplicate_filtered、HTTP、error category/summary、duration。Zoo 级状态和历史来源。它没有 source_id，不能伪造 Source 明细。 |
| articles | Article identity、title、published/source/fetched timestamps、summary/content、language、image evidence、status 和 hashes。raw_html 只能保留为抓取证据，不进入普通 DTO/UI。 |
| article_discoveries | Article 与 Source 的跨来源发现关系及 first/last discovered evidence；它不是某个 run 的结果表，也不应新增隐含的 run 语义来破坏去重。 |
| article_zoo_identities | Zoo 范围内的 Article identity/标题关联，用于 Zoo 文章查询。 |
| crawler_leases | 进程级并发/heartbeat 证据；可与 run-scoped heartbeat 对照判断 liveness，但不是 current Zoo/Source/progress，也不是历史事件流。 |

计数命名必须保持以下区别：

- stored：本次处理创建了新的 Article row（created=True）；
- updated：已有 Article 的证据发生更新，不重复计为 stored；
- already_known：跨 run 已存在且没有新的 run 内重复候选；
- duplicate_candidate / duplicate_filtered：本次 run 内候选或跨 source 被去重；
- fetched：以 crawl_run_stats.fetched_count 为准；Zoo 聚合没有同名字段时，
  由关联 Source stats 聚合，不能把 parsed 静默改名为 fetched；
- error：保留明确的 error count/category/message；未知或缺失数据显示 — 或
  unknown，不伪造 0。

### 5.2 P0 新增/扩展 schema

这是当前实现必须保持的 schema 契约；当前工作区的实现版本为 12。本次只由
验收脚本读取并核对 schema，不在脚本中迁移或清理用户数据库。

#### crawl_run_events

~~~text
id              INTEGER PRIMARY KEY        -- 单库单调递增，供 after_id 使用
run_id          TEXT NOT NULL               -- FK crawl_runs(id)
zoo_id          TEXT NULL                   -- FK，允许事件只属于 run
source_id       TEXT NULL                   -- FK，允许事件只属于 Zoo/run
created_at      TEXT NOT NULL               -- UTC ISO-8601
level           TEXT NOT NULL               -- INFO | WARNING | ERROR
component       TEXT NOT NULL               -- crawler | discovery | fetcher | parser | storage
event_type      TEXT NOT NULL               -- 稳定 vocabulary，见 Event Model
message         TEXT NOT NULL               -- 有界、已脱敏的人类可读摘要
metadata_json   TEXT NOT NULL DEFAULT '{}'  -- 仅 JSON object
~~~

要求：

- run_id、id 支持 (run_id, id) 的增量查询；
- 只增加有实际查询用途的 (run_id, zoo_id, id)、(run_id, source_id, id)
  或 created_at index，避免无意义索引；
- FK delete action 遵循现有 storage 风格；删除 run 时不留下无主 event；
- metadata 序列化失败不能把非法对象写入数据库；
- event 写入在 recorder 中集中处理，不能在 crawler 各处散落 SQL。

#### Run Article Outcome relation

现有 article_discoveries 没有 run_id，因此 P0 必须增加一个最小的
crawl_run_articles run-to-article outcome 关系，至少保存：

~~~text
run_id, article_id, zoo_id, source_id, outcome, created_at, metadata_json
~~~

outcome 至少区分 stored、updated、already_known 和 duplicate。
Run Detail 的 Stored Articles 只查询该关系的 stored（必要时另列 updated），
不能用 created_at 时间窗口猜测，也不能把一篇 Article 复制成多个 Article
来表示不同来源。该关系需要 (run_id, outcome, id) 查询路径和 Article/Zoo/Source
外键。旧 DB 在该关系不存在或没有回填证据时，API 返回空 items 和
evidence_status=no_data/not_recorded；不得把时间相近的 articles 当成该 run
的准确结果。

#### Run heartbeat and progress

P0 将 run-scoped activity 作为与 lease 分开的最小增量证据，优先扩展
crawl_runs，而不是创建第二个控制面。当前 schema 契约为：

~~~text
heartbeat_at       TEXT NULL       -- 最近一次 crawler heartbeat
progress_at        TEXT NULL       -- 最近一次可见进度更新
current_zoo_id     TEXT NULL       -- 当前 Zoo，只有已持久化时才有值
current_source_id  TEXT NULL       -- 当前 Source，只有已持久化时才有值
current_phase      TEXT NULL       -- discovery | fetch | parse | store | finish
progress_json      TEXT NOT NULL DEFAULT '{}'
                                   -- bounded counters/budget evidence
~~~

Crawler 在 run start、Zoo/Source boundary、主要 phase 和 finish 前后更新一次，
不要求每个 candidate 都写 DB。progress_json 只保存 JSON-safe 的 bounded counters，
例如 processed、discovered、fetched、stored、duplicate、errors、total/complete；
它不是 event log，也不替代 crawl_run_stats。API 只有在 heartbeat/progress 和
lease 证据存在时才显示 current activity；缺失时显示 current_activity=null、
activity_status=not_recorded/unknown。

#### stop_reason

P0 的增量 migration 在 crawl_runs、crawl_run_stats 和 crawl_zoo_results 增加
nullable 的 stop_reason，并同步扩展领域对象。当前 schema 版本为 12；events、
run-article relation、heartbeat/progress 和 stop_reason 均属于这一当前版本，
文档和验收证据不得再使用旧的 future-version 数字。

允许的最小 vocabulary：

~~~text
exhausted
no_new_urls
article_limit
url_discovery_limit
page_limit
runtime_limit
retry_exhausted
robots_blocked
http_error
parser_error
disabled
unknown
~~~

运行中为 NULL；正常终止也必须有可解释的 reason（如 exhausted 或
no_new_urls）；旧库/进程异常退出且没有证据时为 unknown，而不是从日志猜。
status 和 stop_reason 是两个维度：例如 status=completed_with_errors
可以同时有 stop_reason=exhausted；status=failed 可以有 http_error。

#### Budget evidence

Console 读取并明确标注当前可见的 budget：

- Crawler/CLI 的 max_candidates_per_source；
- max_pages、archive pagination 和 sitemap 的 max_sitemaps；
- next_batch 的 batch_limit、candidates_processed、
  candidates_limited、remaining_estimate；
- since_days 等过滤条件，以及 source config_json 中实际存在的限制。

当前没有统一的 global max_fetched_articles 或 max_runtime 运行字段。
P0 不扩写 crawler 语义；UI 显示 not implemented / not observable，并在
TODO 中保留后续项。

### 5.3 Status classification contract

Repository、service、API 和 Console UI 对 run、Zoo result、Source result 和
current status 统一使用以下稳定分类。分类只读取结构化 status、stop_reason、
error/count、heartbeat 和 lease evidence，不解析日志文本。对每个 status-bearing
DTO，字段关系固定如下：

- `raw_status`/`run_status` 保留存储层或上游传入的原始状态，仅供诊断；
- `status`、`classified_status`、`status_key` 始终是同一个规范化分类 key；
- `status_label` 是该分类的稳定人类可读文案；`display_status` 保持兼容性的
  display key，不替代 `status_label`。

| 分类 key（`status` = `classified_status` = `status_key`） | `status_label` | 结构化判定 | `active` / polling 语义 |
| --- | --- | --- | --- |
| `success` | Success | terminal status 已结束，无结构化错误，且没有 budget stop reason | terminal；不再轮询。 |
| `warning` | Warning | completed/partial 结果带 error evidence，或 raw status 为 warning/partial/completed_with_errors；未知状态若有错误也归此类 | terminal/非 active；保留 error 与 stop reason。 |
| `failed` | Failed | raw status 为 failed/error/aborted/cancelled/stopped 等失败状态 | terminal；不再轮询。 |
| `budget_hit` | Budget Hit | 显式 budget_hit，或 terminal 结果命中已知 budget stop reason 且无错误 | terminal；展示 limit/observed evidence，不等同于 Failed。 |
| `running` | Running | raw status 为 running，且 heartbeat 或 run-scoped lease 新鲜 | `active=true`；Overview/Run Detail 可按 2.5 秒轮询。 |
| `stale` | Stale | raw status 仍为 running，但 lease inactive/expired 或 heartbeat 超过 stale threshold | `active=false`；不轮询，不改写为 Success。 |
| `unknown` | Unknown | 无 run、无 liveness evidence 的 running、未识别 raw status，或 legacy 缺少必要证据 | `active=false`；`status_observable=false`，current activity 为 null/unknown。 |

DTO 另外提供 `status_reason`、`status_observable` 和 `current_activity`。当前 JS
的 `statusTag` 使用分类 key 生成 CSS class，并使用 `status_label` 显示文案；
Overview、Runs 和 Run Detail 的 `active`、`poll_hint` 与 terminal stop 逻辑均以
分类 key 为准，R09/R10 的分类联动已完成。

### 5.4 Schema migration policy

新表、新列和 index 必须进入 SQLiteStorage.create_schema() /
_migrate_schema() 的同一套可重复增量迁移。P0 的最小迁移集合是：

- crawl_run_events；
- crawl_run_articles；
- crawl_runs 的 heartbeat_at、progress_at、current_zoo_id、
  current_source_id、current_phase、progress_json；
- crawl_runs、crawl_run_stats、crawl_zoo_results 的 stop_reason。

迁移规则：

1. fresh DB 创建完整 schema；
2. legacy DB 保留现有 rows，补列、补表、补 index；
3. migration 失败整体 rollback，不删除用户数据库；
4. 重复执行不改变已有 event/article/run 证据；
5. 增加 legacy migration、event pagination、run-article outcome、
   heartbeat/progress round-trip、stop_reason round-trip 和 FK/index 的
   确定性测试。

## 6. Event Model

### 6.1 Event abstraction

Crawler 只依赖一个窄接口，概念上类似：

~~~python
event_sink.emit(
    run_id=run_id,
    zoo_id=zoo_id,
    source_id=source_id,
    level="WARNING",
    component="crawler",
    event_type="crawl_budget_hit",
    message="Article candidate limit reached",
    metadata={"limit": 50, "processed": 50, "reason": "article_limit"},
)
~~~

具体类名可为 EventSink、CrawlEventRecorder 或同等 abstraction；重要的是
Crawler 不知道 Flask、HTML、API、Browser 或 SQLite SQL。Recorder 负责：

- 校验 level/component/event_type；
- 将 exception 转成 sanitize_error() 后的有限文本；
- 只接受 JSON-safe metadata（scalar/list/object），拒绝 HTML、response、
  exception object、file handle 等运行时对象；
- 对 URL 做现有安全脱敏，不写 query secret、userinfo 或完整 raw response；
- 写入 crawl_run_events 并返回单调 event id，或通过既有 logging 报告 recorder
  失败。

### 6.2 Level、component 和 event type

Level 仅有：

~~~text
INFO | WARNING | ERROR
~~~

Component 控制在：

~~~text
crawler | discovery | fetcher | parser | storage
~~~

P0 事件 vocabulary：

~~~text
crawl_started
crawl_completed
crawl_failed
source_started
source_completed
source_failed
discovery_completed
article_stored
crawl_budget_hit
retry_exhausted
robots_blocked
http_error
parse_failed
event_persistence_failed
~~~

推荐发射点：

| 事件 | 级别/组件 | 必须包含的 metadata |
| --- | --- | --- |
| crawl_started / crawl_completed | INFO / crawler | run、selection、Zoo/Source 总数和最终 totals |
| crawl_failed | ERROR / crawler | safe error category、error summary、是否有 finish evidence |
| source_started / source_completed / source_failed | INFO/ERROR / crawler | Zoo、Source、duration、counts、status |
| discovery_completed | INFO / discovery | discovered、duplicate、page/sitemap evidence、root HTTP |
| article_stored | INFO / storage | article id、Zoo/Source、outcome、canonical URL（脱敏） |
| crawl_budget_hit | WARNING / crawler | limit、observed、scope、structured reason |
| retry_exhausted / robots_blocked / http_error | WARNING/ERROR / fetcher | status code、retry count、policy category、safe target |
| parse_failed | ERROR / parser | article/source scope、stable parser category、safe detail |

不记录每个 socket/header/selector/普通 redirect DEBUG。大量重复候选使用
source_completed 的聚合 metadata，避免把 event table 变成第二个日志文件。

### 6.3 Logging 共存和 event 写失败

现有 logging 不删除、不重定向为网页数据源：

~~~text
logging              = developer diagnostics / stderr
crawl_run_events     = bounded operational observability
~~~

单个 event recorder 故障是 observability degradation，必须以现有 logging
报告，并在可行时追加 event_persistence_failed 证据；它不能把一个尚未完成的
run 显示为成功，也不能静默覆盖核心 run/stat storage failure。核心 storage
写失败仍遵循当前 Crawler 的明确批次语义：Source/Article 层可隔离的失败继续
后续工作，无法安全继续的 run-level failure 必须终止并显示 failed/stale。

## 7. API Contract

API 挂在现有 Flask app 的 /api 前缀下。新 route 通过 service/DTO 访问数据；
不创建第二个 server，也不让模板或浏览器打开 SQLite。

### 7.1 Endpoints

| Endpoint | 作用与主要响应 |
| --- | --- |
| GET /api/crawler/status | 当前是否有 active run、liveness/state、Run ID、status、started/elapsed、totals、Zoo/Source 完成度、last updated 和建议 poll_after_ms。 |
| GET /api/crawl-runs | 最新在前的历史 run；status、limit、cursor/时间过滤；返回摘要、计数和 duration。 |
| GET /api/crawl-runs/{run_id} | run summary、status、start/finish/duration、totals、budgets、stop reason、error summary 和可用的 event/article cursors。 |
| GET /api/crawl-runs/{run_id}/zoos | 该 run 的 Zoo/Source result view；Source 计数来自 stats，Zoo 聚合来自 zoo results。支持 Zoo/status 过滤和分页。 |
| GET /api/crawl-runs/{run_id}/articles | 该 run 的 Article outcome view；默认 outcome=stored，支持 limit/cursor；不返回 raw_html。 |
| GET /api/crawl-runs/{run_id}/events | 事件查询；支持 after_id、limit、level、zoo_id、source_id、component、event_type 和文本搜索。 |
| GET /api/zoos | Zoo 列表、enabled/current status、last run 摘要和可进入 detail 的 ID。 |
| GET /api/zoos/{zoo_id} | Zoo 配置摘要、Source health 和最近一次结果。 |
| GET /api/zoos/{zoo_id}/crawl-history | 按时间倒序的 Zoo 历史，至少含 discovered/fetched/stored/duplicate/error/duration/stop_reason。 |

### 7.2 JSON 约定

所有 list API 使用稳定 envelope：

~~~json
{
  "items": [],
  "next_cursor": null,
  "has_more": false,
  "last_updated": "2026-08-16T00:00:00+00:00"
}
~~~

/api/crawler/status 可返回：

~~~json
{
  "state": "running",
  "active": true,
  "run": {
    "id": "run-id",
    "status": "running",
    "started_at": "2026-08-16T00:00:00+00:00",
    "finished_at": null,
    "duration_ms": null,
    "stop_reason": null,
    "heartbeat_at": "2026-08-16T00:00:01+00:00",
    "progress_at": "2026-08-16T00:00:01+00:00",
    "current_activity": {
      "zoo_id": "zoo-id",
      "source_id": "source-id",
      "phase": "fetch"
    }
  },
  "totals": {
    "zoos_completed": 0,
    "zoos_total": 1,
    "sources_completed": 0,
    "sources_total": 1,
    "discovered": 0,
    "fetched": 0,
    "stored": 0,
    "already_known": 0,
    "duplicate": 0,
    "errors": 0
  },
  "poll_after_ms": 3000,
  "last_updated": "2026-08-16T00:00:01+00:00"
}
~~~

state=running 需要 run status、run-scoped heartbeat/progress 和未过期
lease/heartbeat 证据；run row 为 running 但缺少这些新增字段时返回
unknown/not_recorded，不能仅凭 started_at/当前时间推断 activity。lease
缺失或过期时返回 stale 或 unknown，并在 UI 明确说明“持久化记录仍为
running，但没有足够的活跃 owner/progress 证据”，不把它伪装成健康运行。
current_activity 只有在 current_zoo_id、current_source_id、current_phase
等证据已持久化时才显示；否则返回 null。

参数和错误契约：

- ID、level、component、event type、status、limit/cursor 先校验；
- 无效参数返回 400，不存在的 run/zoo/article 返回 404；
- 合法但没有数据返回 200 的空 envelope；
- 数据库/服务不可用返回不含堆栈、路径、secret 的安全错误；
- API 永远不返回 raw_html；普通 Article DTO 只返回解析后的字段、
  summary/content（按需）、hash、metadata 摘要和安全的 HTTP(S) link。

## 8. Polling Model

P0 使用 HTTP polling，不使用 WebSocket。

1. Overview 打开时先请求 /api/crawler/status 和最近 run。
2. state=running 且有 heartbeat/progress evidence 时，以约 2–3 秒间隔刷新
   status/run summary；默认建议 3 秒，由 API 的 poll_after_ms 提供可调提示。
   state=unknown、stale 或 legacy no-data 时只显示当前已知证据，不把 polling
   本身当作 crawler activity。
3. Run Detail 首次请求最近一页 events（例如 100 条），保存最大的 event.id
   为 after_id。
4. 后续请求使用：
   GET /api/crawl-runs/{run_id}/events?after_id=<last_id>&limit=200；
   只插入 id > last_id 的新 event，客户端按 id 去重，不能每次下载全部历史。
5. active run 结束为 completed、completed_with_errors 或 failed 后，
   完成本次刷新，停止 polling；历史 run 页面不轮询。
6. API 短暂失败时保留上一次数据，显示 stale/error banner，并以有限退避重试；
   不触发 crawl、不修改 run、不自行猜测 stop reason。
7. 页面始终显示 Last updated、heartbeat/progress timestamp 和 polling 状态；
   loading、空 events、无 active run、legacy no-data 和 finished run 各有独立文案。

## 9. Failure Handling

### 9.1 Crawl failure isolation

- 一个 Source 失败只影响该 Source 的 stat/result，后续 Source 和其他 Zoo 继续；
  UI 显示 source_failed、HTTP/error category、safe message 和对应事件。
- 单篇 Article fetch/parse 失败计入该 Source error/partial，不使整个 Dashboard
  500；其他 Article 继续。
- Zoo 没有历史时返回空 history；Source 没有 last success 时显示 —。
- HTTP、robots、retry exhausted、parser 和 storage 的稳定分类不依赖日志正则；
  event 和 stop_reason 分别持久化。

### 9.2 Run stopped / crashed

- 正常 budget 命中：先写 crawl_budget_hit，再在适用的 run/stat/zoo row 写
  stop_reason，UI 显示 Budget Hit 和 limit/observed metadata。
- 失败结束：写 crawl_failed，status=failed，保存脱敏 error category/summary。
- 进程在 finish 前退出：保留 crawl_runs.status=running 的事实；API 结合
  lease 和最后 heartbeat/progress 识别 stale/unknown，绝不能显示为成功。
  如果旧 DB 没有这些字段，则只报告 unknown/not_recorded，不根据时间差伪造
  stop_reason 或 current activity。后续可由 P1 recovery job 处理 stale run，
  但 P0 不在浏览器里自动修复数据库。
- robots_blocked 必须 fail-closed；Console 只展示证据，不提供绕过按钮。
- Retry-After、重试次数、redirect/domain policy 仍由现有 Fetcher 负责；
  Console 不改变网络安全边界。

### 9.3 Storage、legacy DB 和坏 metadata

- SQLiteStorage migration 必须幂等、可 rollback、保留旧数据；
  event table 缺失的旧 DB 在 migration 前后都不能让只读页面 500。
- 没有 events 的旧 run 显示 No events recorded for this run.；
  没有 crawl_run_articles 关系或关系没有回填证据时，Stored Articles 返回
  empty/no data，不用 articles.created_at、last_fetched_at 或 discovered_at
  时间窗口推断；没有 heartbeat/progress 的旧 run 显示 unknown/not recorded，
  current activity 为 null；没有 stop_reason 的旧 run 显示 unknown/not recorded。
- malformed metadata_json、errors_json 采用 graceful degradation：忽略坏
  的 metadata 或以安全 placeholder 展示，不执行其中内容、不把坏值当作 0。
- Dashboard/API 使用 read-only/query-only 数据库连接；任何 POST/控制动作不属于
  Console 路由。
- articles.raw_html 永远不进入模板、JSON 普通响应或 |safe。Article
  Preview 使用 parsed content/summary，由 Jinja 自动转义；Debug/Raw Data
  只允许结构化 evidence、hash、metadata 和安全链接，不是 raw HTML viewer。

## 10. UI information architecture

### Overview

顶部放 active/current run；无 active 时明确 Idle 和最近 run。主指标固定为
Zoo/Source progress、Discovered、Fetched、Stored、Already known、Duplicate、
Errors。Current Activity 每行显示 Zoo、Source、status、counts、duration、
stop reason/error。

### Runs / Run Detail

Run list 最新在前，status filter 和空状态明确。Run Detail 顶部显示 status、
started/finished/elapsed 和总计；表格列为 Zoo、Source、status、discovered、
fetched、stored、duplicate、error、duration、stop reason。Running、Success、
Warning、Failed、Budget Hit 的颜色和文本不能只靠颜色。

### Zoos

Zoo Detail 以配置 name/country/status 开头，Source 表显示 enabled、last HTTP、
last success、last error；history 行至少为 discovered → fetched → stored，并
保留 duplicate/error/duration/stop reason。历史缺失用 empty state。

### Articles

Run-scoped list 只显示 stored Article；字段包括 title、Zoo、Source、published
at、fetched at、canonical URL、language、image count。可展开 parsed summary/body
和 structured metadata；不要默认展示整篇，也不要展示 raw HTML。

### Events

Event row 显示 UTC 时间、level、Zoo、Source、component、event type、message；
metadata 以折叠的安全 JSON 展示。Filter 和 text search 是 query 参数，不在浏览器
端下载大量无关历史后再过滤。

## 11. Test and acceptance contract

实现阶段必须把测试放在现有 tests/ 行为文件中，不引入真实官网、真实时间或
不可控网络：

- Repository/storage：active run、run summary、Zoo results、run articles、
  event insert、after_id/pagination、filters、legacy migration；
- Service：无 active、running、finished、failed、stale、missing Zoo、旧 DB 无
  events/关系/heartbeat-progress、malformed metadata；旧 DB 的 no-data 不得被
  时间推断替代；
- API：200、400、404、invalid run ID、filters、pagination、after_id、empty；
- UI：Overview、Run Detail、Zoo Detail、Articles、Events、loading/empty/error、
  polling 不重复 events、finished run 停止 polling；
- regression：至少覆盖现有 crawler、storage、dedupe、failure isolation、
  dashboard、CLI 测试。

至少要有三种确定性验收场景：

1. Normal：crawl/source start → discovery → article stored → source/crawl
   completed，UI/DB 计数和事件完整；
2. Budget Hit：fixture 触发候选/article/page 限制，结构化 stop_reason 可读，
   不能仅靠日志猜；
3. Failure：HTTP、parser 或 retry exhausted 至少一种；一个 Source 失败不
   破坏 Console，UI 能定位 Zoo、Source、error、event。

验收入口为 `python3 scripts/verify_crawler_console.py`，报告必须是自包含的，
并明确 PASS/FAIL、Blockers、环境、架构隔离、API、UI、三种 scenario、
regression 命令及已知限制。非破坏性本地核验可设置
`CRAWLER_CONSOLE_REPORT_PATH=/tmp/crawler_console-acceptance.html`；脚本只使用
内存 acceptance DB/临时 fixture，不清理用户 `data/` 或既有 `artifacts/`。
当前 Failure fixture 明确使用确定性的 HTTP 503，验证失败 source 的
`http_error`/`source_failed`、`stop_reason=http_error`、recovery source 的
成功 `article_stored`，并分别检查 DB、API、Console route/API-backed UI evidence。
2026-08-16 当前报告确认真实浏览器页面可见 HTTP/error evidence；Failure 使用
HTTP 503 + recovery source，Browser visual check、Required Tests、Regression、
Normal、Budget Hit 和 Failure 全部 PASS。回归为 706 passed、0 failed，ruff/mypy
通过，`verify_crawler_console.py` 为 PASS、Blockers=0。该 gate 结论不覆盖 TODO
中明确的局部 UI 能力、Nice-to-have 或 Future/P1。

## 12. Implementation sequence

按以下顺序实现，避免先做漂亮 UI 再补数据语义：

1. 在 SQLiteStorage 现有 migration 边界加入 event、stop_reason 和 run-article
   outcome 的可重复 schema；
2. 建立 read repository/service/DTO，先覆盖现有 run/stat/result/article 数据；
3. 加入 API 和 API tests；
4. 加入 Console shell、Overview、Runs、Run Detail、Zoo Detail、Articles；
5. 接入窄 EventSink，在 crawler lifecycle/budget/major failure 节点发事件；
6. 加入 Events UI、filters、after_id polling 和 finished-run stop；
7. 跑全套 regression 与三种 scenario，生成 acceptance report；
8. 做一次只读架构 review，重点检查 crawler/frontend coupling、SQL/UI coupling、
   migration、raw_html、event overlogging 和 stale run 语义。

## 13. Future

P1 可在 P0 的稳定 read model/API 之上增加：

- Run All、Run Zoo、Retry Failed、Stop 等显式控制，并复用独立 control boundary；
- stale run recovery、global runtime/fetched budget 和更强的无限爬保护；
- anomaly detection、历史 diff 和趋势视图；
- 有权限控制、rotation/retention 的独立 raw diagnostic log sink；
- SSE/WebSocket（仅在 polling 的数据库/API 负载证明不足后）；
- CMS、Source 管理、Article 编辑/发布和用户权限。

这些未来项不能改变 P0 的只读、SQLite-first、现有 Fetcher 安全边界，也不能让
当前缺失的能力在 P0 页面中被伪装成已实现。
