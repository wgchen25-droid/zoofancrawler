# Crawler Console P0 TODO

> 这是 Crawler Console 的持续开发记录。Status 为 DONE 的条目保留为现有
> 实现和测试证据，不删除；PARTIAL 表示仓库已有子集但尚未满足完整契约；
> TODO 表示尚未实现或当前 gate 尚未通过。状态依据当前工作区代码、定向测试
> 和验收脚本的实际结果核验，不能用旧 report 数字代替当前结果；
> 不把未来控制、异常检测或 raw log 能力计入 P0。
>
> 当前验收快照（2026-08-16）：Acceptance **PASS**，Blockers **0**，完整回归
> **706 passed**、0 failed，schema 12；报告中的真实浏览器 smoke、API/UI 和
> Normal、Budget Hit、Failure / Source Isolation 均 PASS。总回归通过不等于
> 所有局部 UI 契约、Nice-to-have 或 Future/P1 已实现。
>
> 本次文档同步只修改本 TODO 和架构文档；`zoofan/`、templates、tests、scripts、
> data、reports 和既有 artifacts 不在写入范围内。

状态分类契约：Console DTO 的 `status`、`classified_status`、`status_key` 始终
是同一个规范化分类 key；`raw_status`/`run_status` 只保留原始状态，不能替代分类。
每个分类都提供对应的 `status_label`：Success、Warning、Failed、Budget Hit、
Running、Stale、Unknown。当前 JS `statusTag` 以分类 key 生成 CSS，并读取
`status_label` 显示人类可读文案；Overview、Runs、Run Detail 的分类联动已完成。

## P0 Required

| ID | Status | Priority | Description | Acceptance condition / evidence |
| --- | --- | --- | --- | --- |
| P0-R00 | DONE | P0 Required | 现有 Flask server-rendered Dashboard 基线：/、/articles、/articles/<id>、/sources、/runs，使用只读/query-only SQLite connection。 | tests/test_dashboard.py 的 route、empty/legacy、read-only 和不存在 Article 测试持续通过；Dashboard 不创建缺失 DB。 |
| P0-R01 | DONE | P0 Required | 现有 Crawler / discovery / parser / storage / Dashboard 边界已存在：Crawler 不依赖前端，parser/fetcher 不直接写 SQLite；现有 logging 保持 stderr 开发诊断。 | tests/test_crawler.py、tests/test_failure_isolation.py、tests/test_dashboard.py 和 tests/test_storage.py 证明现有边界；新增 Console 不引入反向依赖。 |
| P0-R02 | DONE | P0 Required | 建立 Console 专用 read repository、service 和 DTO 层；按现有 flat zoofan/ 命名拆分，不把复杂 SQL、聚合、HTML 拼接重新塞进 dashboard.py。 | 新 Console route 只调用 service/DTO；静态检查或测试能证明 route/template 不执行 SQLite 查询；Crawler 不 import Flask/Console 模块。Evidence: `zoofan/console_repository.py`、`zoofan/console_service.py`、`zoofan/console_api.py` 已分层；验收报告 Architecture Validation 和 `tests/test_console_api.py` 通过。 |
| P0-R03 | DONE | P0 Required | 按 SQLiteStorage.create_schema() / _migrate_schema() 增加 crawl_run_events、event indexes 和 legacy-safe migration；当前 schema 从历史版本增量升级到 12。 | fresh DB 与代表性 legacy DB 都能初始化；migration 可重复、保留旧数据、失败 rollback；event 的 run_id/id 查询走明确 index。Evidence: `SQLiteStorage.SCHEMA_VERSION == 12`，验收脚本核对 events/run-articles 表、列和索引，legacy migration、外键和回归测试通过。 |
| P0-R04 | DONE | P0 Required | 在 crawl_runs、crawl_run_stats、crawl_zoo_results 增加一等结构化 stop_reason；运行中为 NULL，旧证据为 unknown；同步领域模型和 DTO。 | Normal、budget-hit、HTTP/parse failure fixture 都能在 DB/API/UI 读取 stop reason；测试证明不依赖日志文本猜测。Evidence: Normal、Budget Hit 和 HTTP 503 Failure 的 DB/API/UI evidence 通过，真实浏览器 Run Detail 可见 HTTP/error 与 `stop_reason`。 |
| P0-R05 | DONE | P0 Required | 增加最小 crawl_run_articles 关系，保存 run_id、article_id、zoo_id、source_id、outcome、created_at、metadata_json；区分 stored、updated、already_known、duplicate，不改变 article_discoveries 的跨来源语义。 | Run Detail 的 Stored Articles 与本次 run outcome 一致；第二次相同 crawl 不把已有 Article 误报为新存储；分页和 Zoo/Source 关联可查询。Evidence: storage、service 和 crawler tests 已证明每次 run 都记录 stored、updated、already_known、duplicate outcome；幂等 upsert、重复 crawl、分页、Zoo/Source 关联和无时间推断均通过。 |
| P0-R06 | DONE | P0 Required | 增加最小 run heartbeat/progress 运行证据。可扩展 crawl_runs 或独立运行状态记录，但必须复用现有 crawler_leases，不引入第二个控制面。至少包含 heartbeat_at、progress_at、current_zoo_id/current_source_id、current_phase 和 progress counters。 | Active status 能区分 running、有活跃 heartbeat 的 current activity、无 owner/stale 和 idle；progress 更新不写模板、不依赖 HTTP；旧 DB 缺字段时返回 unknown/no current activity。Evidence: schema/DTO 暴露 heartbeat/progress、current zoo/source 和 progress；start heartbeat、stale reason、legacy unknown/no-activity 与 schema contract 测试通过。 |
| P0-R07 | DONE | P0 Required | 实现窄 EventSink / CrawlEventRecorder abstraction，由 Crawler 在 run/source/discovery/storage/budget/major failure 边界 emit 少量 operational events。 | Crawler 不知道 Flask/HTML/HTTP server；event 可注入 fake sink 测试；event metadata 只接受 JSON-safe 值，异常、response、HTML 不进入 SQLite。Evidence: `zoofan/events.py` 提供 recorder/sink，Crawler 使用可注入 hook 和 fail-soft delivery；metadata sanitizer 与 `tests/test_console_events_storage.py`、`test_console_crawler_events.py` 通过。 |
| P0-R08 | DONE | P0 Required | 提供同一个 Flask app 下的 JSON API：/api/crawler/status、/api/crawl-runs、run detail、run zoos、run articles、run events、zoos、zoo detail、zoo history。 | API tests 覆盖 200、空结果、400 参数错误、404 run/zoo/article、分页、filters 和不泄露 raw_html/堆栈/secrets；API 不创建第二个 server。Evidence: `zoofan/console_api.py` 挂在现有 Dashboard app；`tests/test_console_api.py` 覆盖 DTO、filters、pagination、404、empty 和 raw_html boundary，报告 API Validation 全 PASS。 |
| P0-R09 | DONE | P0 Required | 在现有 Overview 之外提供 Crawler Console Overview：active/current run、Run ID、started/elapsed、Zoo/Source progress、discovered/fetched/stored/duplicate/errors、Current Activity、Recent Runs。 | 有 active、idle、stale、无数据、部分失败 fixture 时页面均可渲染；指标来自 API DTO，状态文本同时表达 Running/Success/Warning/Failed/Budget Hit。Evidence: `/console` 页面、metrics/current activity/recent runs、loading/empty/error、polling 和 `status`/`classified_status`/`status_label` 联动均通过 service/UI tests 与真实浏览器 smoke。 |
| P0-R10 | DONE | P0 Required | 现有 /runs 已能显示部分 run 摘要，但需补齐 Console Runs filter 和 Run Detail：最新在前、status filter、duration、Zoo/Source totals、Zoo/Source result table、stop reason。 | 历史 run 可按 status/时间稳定分页；点击任一 run 得到 200；Running、completed、completed_with_errors、failed 和 stale 都可快速区分。Evidence: Runs filter、run detail、分页/排序、Zoo/Source result、duration/stop_reason 和统一 Success/Warning/Failed/Budget Hit presentation 已通过 service/API/UI 与浏览器 smoke。 |
| P0-R11 | DONE | P0 Required | 实现 Zoo Detail 与 crawl history：配置 name/country/status、Source enabled/HTTP/last success/last error，以及 discovered/fetched/stored/duplicate/error/duration/stop reason 历史。 | 有历史、无历史、缺失 Source、单 Source 失败和坏 metadata 的 Zoo 都返回 200；无历史显示 No crawl history 而非 500。Evidence: repository/service/API 和 UI 已提供 Zoo facts、Source health table（enabled、HTTP、last success、last error）及 history；missing-zoo/no-history safe behavior、单 Source failure 和坏 metadata 测试通过。 |
| P0-R12 | DONE | P0 Required | 现有 /articles 和 Article Detail 已有安全解析字段展示；需增加 run-scoped Article Preview，仅列本次 outcome=stored 的 Article，并显示 title、Zoo、Source、published/fetched、canonical URL、language、image count。 | API/UI 与 crawl_run_articles 一致；Article DTO 和所有页面都不含 raw_html；parsed content/summary 自动转义，invalid URL 不生成危险 link。Evidence: stored-only API、relation-aware DTO、Article stored preview 的 title/time/language/image_count、Zoo/Source、parsed preview、安全 URL 和 raw_html boundary 已由 service/API/UI tests 与报告核对通过。 |
| P0-R13 | DONE | P0 Required | 实现 Events 页面和 Run Detail Events panel，按 level、Zoo、Source、component、event type 和文本搜索过滤；展示 UTC time、message、safe metadata。 | crawl_started、source lifecycle、discovery、stored、budget、HTTP/robots/retry/parser、crawl completed/failed 至少可查询；旧 run 无 events 显示 empty state。Evidence: event table、Run Detail panel、level/Zoo/Source/component/event type/text filters、UTC/message/safe metadata、empty state 和 crawler event vocabulary 均已实现；`tests/test_console_services.py`、`test_console_api.py`、`test_console_ui.py` 与验收 smoke 通过。 |
| P0-R14 | DONE | P0 Required | 实现 2–3 秒 HTTP polling；active run 刷新 status/summary，events 使用 after_id 增量；finished run 停止 polling，客户端按 event id 去重。 | 浏览器测试证明 active run 自动刷新、连续请求不重复历史 events、API 短暂失败显示 stale/error、terminal status 后不再请求；不使用 WebSocket。Evidence: Console JS 使用 2500 ms `setInterval`、`after_id`、event-id `Set` 去重、error state 和 `clearInterval` terminal stop；`tests/test_console_ui.py` 与验收报告 Polling contract/browser smoke PASS。 |
| P0-R15 | DONE | P0 Required | 保留现有 Source/Zoo/Article failure isolation，并补齐 Console 的 stale run、event persistence failure、malformed metadata、legacy no-event/no-relation/no-heartbeat 处理。 | 一个 Source/Article 错误不会让其他 Source/Zoo 或 Console 500；HTTP/parse/retry/robots 可定位；缺少关系时显示 no data，绝不按时间推断 run 归属；run-level storage failure 不被伪装成成功。Evidence: source isolation、stale/legacy/no-relation/no-heartbeat、malformed metadata、robots/error events 已由 crawler/service/API tests 和报告覆盖；`test_event_sink_failure_is_fail_soft_and_preserves_source_isolation` 提供确定性的 event sink persistence failure 测试，核心 run-level storage failure 仍保持 failed/stale 语义。 |
| P0-R16 | DONE | P0 Required | 增加 repository、service、API、UI 和 migration 行为测试；使用 SQLiteStorage(':memory:'), temporary legacy DB, fake fetcher/transport/event sink，不依赖真实官网、真实时间或网络。 | 新增测试覆盖 active/no-active/finished/failed/stale、run summary、Zoo results、articles、heartbeat/progress、event insert/filters/after_id、坏 metadata、404/empty/UI states；`python3 -m pytest -q` 必须全绿。Evidence: Console 定向行为覆盖与完整回归通过，完整回归为 **706 passed**、0 failed，ruff/mypy 也通过。 |
| P0-R17 | DONE | P0 Required | 增加 Normal、Budget Hit、Failure 三个确定性 Crawler Observability scenario，并保留 crawler/storage/dedupe/dashboard/CLI regression。 | Scenario A 有完整生命周期 state/event；Scenario B 有结构化 stop_reason；Scenario C 使用确定性 HTTP 503，验证 `http_error`/`source_failed`、失败 source 的 `stop_reason`、recovery source 的 `article_stored`，并有 DB/API/UI evidence。Evidence: Normal、Budget Hit 和 HTTP 503 + recovery source Failure 均通过，真实浏览器 visual failure evidence 也通过。 |
| P0-R18 | DONE | P0 Required | 生成自包含验收报告，并提供固定验证入口（优先 python3 scripts/verify_crawler_console.py 或项目等价命令）。 | 报告含 PASS/FAIL、Blockers、环境、架构隔离、API/UI、三种 scenario、regression、known limitations；无浏览器时明确 Browser visual check unavailable，不得伪造 PASS。Evidence: `python3 scripts/verify_crawler_console.py` 为 PASS、Blockers=0，报告包含上述 sections，Failure browser visual check 为 PASS。 |
| P0-R19 | DONE | P0 Required | 完成最终 gate 和只读架构 review，检查 Crawler/frontend coupling、API/UI/DB coupling、God Function、migration、event overlogging、raw_html、heartbeat/progress 和 stale run。 | Architecture Validation、Verifier Safety、Failure DB/API/route/browser evidence、Required Tests、Regression、Normal、Budget Hit 和 Failure 全部 PASS；Blockers=0，允许声明 P0 Required gate complete，同时保留 Nice-to-have/Future/P1 TODO。 |

## P0 Nice-to-have

| ID | Status | Priority | Description | Acceptance condition / evidence |
| --- | --- | --- | --- | --- |
| P0-N00 | PARTIAL | P0 Nice-to-have | 展示当前已有的 budget evidence：max_candidates_per_source、max_pages、archive/sitemap limit、batch_limit、candidates_limited、remaining_estimate、since_days。现有 /sources 已有部分 metadata 展示。 | Console 对已实现字段显示值和来源，对不存在的 global fetched/runtime limit 显示 not implemented / not observable；不能把 max_pages 误标为 runtime budget。 |
| P0-N01 | PARTIAL | P0 Nice-to-have | Run 与 Zoo detail 显示相对最近一次历史 run 的 discovered/fetched/stored/duplicate/error/duration 变化；现有 reporting 已有部分 relative-change 逻辑可参考。 | 无 previous run 时显示 unknown/empty；有 previous run 时每个 delta 有明确基准 Run ID，不把累计 articles 当作本次变化。 |
| P0-N02 | PARTIAL | P0 Nice-to-have | Article Preview 提供 parsed summary/body、author、hash、image count 和 structured metadata 展开；复用现有 Article Detail 的安全字段。 | 展开/收起不请求或渲染 raw_html；body 经自动转义；缺字段显示 —，坏 metadata 不致 500。 |
| P0-N03 | TODO | P0 Nice-to-have | Event row 提供结构化 metadata 展开、safe target/status code/retry 摘要和复制 event ID；不引入原始 DEBUG log viewer。 | 任意合法 metadata 都能安全展示；HTML、脚本、userinfo、query secret 和 exception repr 不执行或泄露。 |
| P0-N04 | PARTIAL | P0 Nice-to-have | 桌面高信息密度表格、小屏横向 overflow、键盘 focus、ARIA live 更新、减少动画；现有 Dashboard CSS 的 responsive/escaped 基线可复用。 | Playwright 或现有浏览器能力可用时检查主要页面无水平布局破坏、loading/empty/error 可读、状态不只依赖颜色；无浏览器时报告限制。 |
| P0-N05 | DONE | P0 Nice-to-have | 为 Runs、Zoo results、Articles、Events 加上服务端分页上限和稳定排序，避免 active run 时下载几千条历史。 | 所有 list endpoint 对 limit 有上限；相同 DB 快照下排序稳定；after_id 与 cursor 不会遗漏或重复。Evidence: repository `MAX_PAGE_SIZE`、API per-resource limit validation、stable timestamp/id or event-id ordering、offset/after_id DTOs；`test_console_services.py`、`test_console_api.py` 和报告 after_id check 通过。 |
| P0-N06 | TODO | P0 Nice-to-have | 为 event recorder 增加有限 event volume/retention 设计，防止 article_stored 或重复 source event 无限膨胀；P0 先保证 bounded event types。 | 设计说明 event retention 与历史可见性；默认不会删除仍在验收范围内的 run evidence；达到限制时有可观察 warning。 |
| P0-N07 | TODO | P0 Nice-to-have | API/service metrics 记录 query latency、返回 item count 和 polling error，但不写入 crawler operational event 的低层噪声。 | 诊断信息不影响 API DTO、不会包含 SQL/路径/secret；能区分数据为空和查询失败。 |

## Future / P1

| ID | Status | Priority | Description | Acceptance condition |
| --- | --- | --- | --- | --- |
| P1-00 | TODO | Future / P1 | Crawler control：Run All、Run Zoo、Retry Failed、Stop、Restart；沿用独立 control boundary，不让普通 Console GET 改变状态。 | 有明确鉴权、CSRF、并发 lease、审计和错误契约；控制测试与只读 API/UI 隔离；未实现前 P0 不显示控制按钮。 |
| P1-01 | TODO | Future / P1 | stale run recovery：对进程退出后遗留的 running run 做显式 recovery/标记，并保留原始证据。 | recovery 是可审计、幂等、不会把未知状态改写成成功；Console 能区分 recovered、stale 和 active。 |
| P1-02 | TODO | Future / P1 | 更完整的 crawl budget：统一 global max fetched articles、runtime limit、pagination/page budget，并将命中原因接入结构化 stop_reason。 | budget 在 Crawler、storage、API、UI、tests 有同一语义；命中可重复验收，不能只依赖日志。 |
| P1-03 | TODO | Future / P1 | anomaly detection / infinite-crawl protection：根据历史变化、URL 发现量、重复率、HTTP 错误和 runtime 生成疑似异常提示。 | 规则、阈值、false-positive 处理和 evidence 可配置/可测试；P0 仍只展示事实计数，不自动判故障。 |
| P1-04 | TODO | Future / P1 | SSE/WebSocket 增量推送，仅在 HTTP polling 的负载和延迟证明不足后评估。 | 有明确连接生命周期、断线重连、权限、backpressure 和与 after_id 一致的事件顺序；不为 P0 提前引入基础设施。 |
| P1-05 | TODO | Future / P1 | 有 rotation/retention/权限边界的 persistent raw diagnostic log sink；不要把 stderr 无条件复制到 SQLite。 | raw log 与 operational event 分离、敏感信息脱敏、访问受控；Console 默认仍不渲染 raw_html 或未过滤日志。 |
| P1-06 | TODO | Future / P1 | 更强的历史趋势、run diff、Source health score 和 Zoo anomaly view。 | 指标明确区分 run-scoped 与 cumulative；缺历史显示 unknown/empty，不以缺失数据制造趋势。 |
| P1-07 | TODO | Future / P1 | ZooFan Admin / CMS：Source 编辑、Article 编辑/发布、审核、删除和权限。 | 先定义身份、授权、审计、并发和数据恢复；不把 P0 read-only DTO 当作写入 API。 |
| P1-08 | TODO | Future / P1 | 多用户/部署级 authentication、authorization、audit log。 | 未授权请求、只读角色、控制角色和审计记录都有确定性测试；P0 的本地 loopback 假设不被静默扩大为公网服务。 |
| P1-09 | TODO | Future / P1 | 分布式 queue/worker/metrics/logging infrastructure。 | 只有 SQLite/单进程/polling 已成为实际瓶颈并有迁移方案时评估；不以未来可扩展为理由提前引入 Kafka、Redis、Celery 等。 |

## 记录与依据

本 TODO 的状态依据 2026-08-16 对当前工作区的代码、测试和验收报告核验：

- zoofan/dashboard.py：现有 Flask app factory、query-only connection、五个只读路由和安全 Article 展示；
- zoofan/templates/：现有 Overview、Runs、Sources、Articles、Article Detail server-rendered 页面；
- zoofan/storage.py：SQLite schema、SCHEMA_VERSION 12、幂等 migration、events/run-articles、stop_reason、heartbeat/progress 和既有 storage API；
- zoofan/events.py、zoofan/crawler.py：窄 EventSink/recorder、JSON-safe operational events、Crawler lifecycle/budget/failure hooks、source isolation 和 lease；
- zoofan/console_repository.py、zoofan/console_service.py、zoofan/console_api.py：只读 repository/service/DTO、active/stale/unknown 分类、分页/过滤和同一 Flask app 下的 /api/*；
- zoofan/templates/console_*.html、zoofan/static/dashboard.css：Overview、Runs、Run Detail、Zoo Detail、Articles、Events、loading/empty/error、2500 ms polling 和 after_id UI contract；
- tests/test_console_events_storage.py、test_console_crawler_events.py、test_console_services.py、test_console_api.py、test_console_ui.py：migration、events、relations、stop reason、status、API、UI contract 和 failure isolation 覆盖；
- scripts/verify_crawler_console.py：固定验收入口；当前脚本从 pytest summary 动态读取 test count/evidence，且可通过 `CRAWLER_CONSOLE_REPORT_PATH` 将报告写到临时路径，避免核验覆盖既有 artifacts/data。当前验收为 PASS、Blockers=0，完整回归为 706 passed、0 failed，ruff/mypy 通过；Failure 场景为 HTTP 503 + recovery source，并包含 browser visual evidence。controls、WebSockets、anomaly detection、persistent raw logs 和 CMS 仍不属于当前 P0 gate。

仍需保留为 PARTIAL/TODO 的能力包括：budget / history delta / metrics / retention，
以及 P0 Nice-to-have 和 Future/P1 的 control、anomaly、persistent raw logs 和 CMS。

本次文档同步只修改本 TODO 和架构文档；没有修改 `zoofan/`、templates、tests、
scripts、Makefile、用户 `data/` 或既有 `artifacts/`。验证报告如需临时输出仍应
写入 `/tmp` 路径，避免覆盖仓库内既有产物。
