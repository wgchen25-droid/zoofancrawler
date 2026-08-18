# Zoofan 数据库具体实施计划

> 状态元数据
>
> - authoritative plan: true
> - saved_at: 2026-08-14
> - current execution scope: Phase 0 only
> - Phase 1/2: pending
>
> 本文是实施计划的权威存档；保存计划不代表 Phase 0 已完成。

## 一、这轮改造的最终目标

本轮不迁移 PostgreSQL，继续使用 SQLite。完成后应达到以下状态：

1. `data/zoofan.db` 是唯一正式主数据库。
2. crawl 不会再因为漏写 `--db` 而把数据保存到 `:memory:`。
3. 当前临时验收库中的 31 篇文章能够安全合并到正式库。
4. 所有数据库都有唯一身份，报告能够追踪到具体数据库和 crawl run。
5. 数据库支持备份、检查、合并和回滚。
6. 为后续文章版本、原始抓取证据、AI 摘要和小红书内容做好结构准备。
7. `reports/latest` 始终由正式数据库生成，不再来自无法恢复的临时库。

本轮按照下面的顺序实施：

```text
保护现有数据
   ↓
Schema v8：数据库安全和可追溯
   ↓
合并临时验收数据库
   ↓
Schema v9：原始证据和文章版本
   ↓
Schema v10：摘要、翻译和发布内容
```

---

# 二、阶段 0：保护当前数据

这一阶段不修改 schema，先确保两个数据库不会丢失。

## 0.1 立即归档临时数据库

将：

```text
/tmp/zoofancrawler-final-fresh.lSmQrT/final.db
```

复制为：

```text
data/imports/acceptance-final-2026-08-14.db
```

不要继续依赖 `/tmp` 路径。

推荐目录：

```text
data/
├── zoofan.db
├── imports/
│   └── acceptance-final-2026-08-14.db
├── backups/
│   ├── pre-migration/
│   └── pre-import/
└── manifests/
```

使用 SQLite Backup API 或 `.backup`，不要在数据库仍有写入连接时直接复制文件。

---

## 0.2 备份当前正式数据库

备份：

```text
data/zoofan.db
```

到：

```text
data/backups/pre-import/
  zoofan-before-acceptance-merge-2026-08-14.db
```

同时为两个数据库生成 manifest：

```json
{
  "file": "acceptance-final-2026-08-14.db",
  "created_at": "2026-08-14T00:00:00Z",
  "schema_version": 7,
  "sha256": "...",
  "zoos": 73,
  "sources": 116,
  "articles": 31,
  "article_discoveries": 34,
  "crawl_runs": 2,
  "integrity_check": "ok"
}
```

---

## 0.3 对两个数据库执行基线检查

需要检查：

```sql
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

并导出以下计数：

```text
zoos
sources
articles
article_discoveries
crawl_runs
crawl_run_stats
crawl_zoo_results
article_zoo_identities
```

### 阶段 0 验收标准

* 临时数据库已经移出 `/tmp`。
* 当前正式数据库有可恢复备份。
* 两个数据库的 SHA-256 已记录。
* 两个数据库 `integrity_check` 均为 `ok`。
* 当前数据库基线计数已经保存为 JSON。
* 此阶段不修改 `data/zoofan.db`。

---

# 三、阶段 1：Schema v8——数据库安全和可追溯

这一阶段优先解决数据路径、数据库身份、迁移历史和管理命令。

## 1.1 修复数据库路径逻辑

新增统一的数据库路径解析函数，例如：

```text
zoofan/db_path.py
```

解析顺序：

```text
1. CLI --db
2. 环境变量 ZOOFAN_DB
3. config/zoos.yaml 中的 storage.path
4. 否则报错退出
```

配置文件增加：

```yaml
storage:
  path: data/zoofan.db
```

正式 crawl 不再自动使用 `:memory:`。

只有以下两种方式可以使用内存数据库：

```bash
python3 cli.py crawl --all --ephemeral
```

或者：

```bash
python3 cli.py crawl --all --db :memory:
```

使用内存数据库时必须输出明显警告：

```text
WARNING: Ephemeral in-memory database.
All crawl data will be lost when this process exits.
```

Dashboard 和 crawl 必须调用同一个数据库路径解析函数。

---

## 1.2 新增 `database_meta`

每个数据库文件拥有唯一身份。

```text
database_meta
──────────────────────────
instance_id        TEXT
created_at         TEXT
application_name   TEXT
```

其中：

```text
instance_id = UUID
```

每个数据库只允许有一条 `database_meta` 记录。

启动时输出：

```text
Database path: /absolute/path/data/zoofan.db
Database instance: 1f6a...
Schema version: 8
Mode: read-write
```

Dashboard 顶部显示：

```text
Database: 1f6a…
Schema: v8
Mode: read-only
Last run: ...
```

---

## 1.3 新增 `schema_migrations`

当前 `schema_meta` 可以继续保存最新版本，但增加完整迁移历史：

```text
schema_migrations
──────────────────────────
version            INTEGER
name               TEXT
checksum           TEXT
applied_at         TEXT
```

例如：

```text
8 | database_identity_and_import_support | abc123 | 2026-08-14...
```

要求：

* 每个 migration 在事务中执行。
* 同一个 migration 不允许重复执行。
* migration 文件被修改后，checksum 不一致必须报错。
* migration 前自动创建备份。
* migration 失败必须完整回滚。

建议将迁移代码从 `zoofan/storage.py` 中拆出：

```text
zoofan/
├── storage.py
├── storage_migrations.py
├── db_admin.py
└── db_merge.py
```

`storage.py` 保持原有接口，避免一次性大规模重构。

---

## 1.4 给核心表增加稳定 ID

以下表增加：

```text
public_id TEXT UNIQUE NOT NULL
```

目标表：

```text
zoos
sources
articles
crawl_runs
```

规则：

* 新记录由 Python 生成 UUID。
* 已有记录在 v7 → v8 migration 中补齐。
* SQLite 内部关系仍然使用整数主键。
* API、报告、跨数据库导入使用 `public_id`。

建议 YAML 同时增加稳定 key：

```yaml
zoos:
  - key: zoo_berlin

    sources:
      - key: zoo_berlin_news
```

数据库中增加：

```text
zoos.zoo_key
sources.source_key
```

以后动物园名称或 URL 修改，不影响对象身份。

---

## 1.5 新增导入审计表

新增：

```text
import_jobs
──────────────────────────
id
public_id
source_database_instance_id
source_database_sha256
source_schema_version
target_database_instance_id
status
dry_run
started_at
finished_at
summary_json
error_message
```

状态：

```text
running
succeeded
failed
rolled_back
skipped_already_imported
```

唯一约束建议使用：

```text
source_database_sha256 + target_database_instance_id
```

这样同一份数据库不会被重复导入。

另外新增 ID 映射记录：

```text
import_record_maps
──────────────────────────
import_job_id
entity_type
source_record_id
source_public_id
target_record_id
action
```

`action`：

```text
inserted
matched
updated
skipped
conflict
```

---

## 1.6 报告增加来源信息

所有 `run-summary.json` 增加：

```json
{
  "database_instance_id": "...",
  "database_schema_version": 8,
  "crawl_run_public_id": "...",
  "generated_at": "...",
  "config_hash": "...",
  "code_revision": "...",
  "report_format_version": 2
}
```

报告目录调整为：

```text
reports/
├── runs/
│   └── {crawl_run_public_id}/
│       ├── run-summary.json
│       └── report.html
└── latest/
    ├── run-summary.json
    └── report.html
```

更新 `latest` 时先写临时目录，再执行原子替换，避免报告写到一半被 Dashboard 或用户读取。

---

## 1.7 实现数据库管理命令

需要新增以下命令：

```bash
python3 cli.py db inspect --db data/zoofan.db
```

输出：

```text
绝对数据库路径
instance_id
schema version
文件大小
WAL 状态
各表行数
最近一次 crawl run
```

验证命令：

```bash
python3 cli.py db verify --db data/zoofan.db
```

检查：

```text
integrity_check
foreign_key_check
孤儿 discovery
孤儿 stats
重复 canonical URL
重复 normalized URL
缺失 public_id
缺失 zoo/source identity
```

备份命令：

```bash
python3 cli.py db backup \
  --db data/zoofan.db \
  --output data/backups/manual/zoofan.db
```

备份必须使用 SQLite Backup API，并自动生成 manifest。

### 阶段 1 验收标准

* 未指定持久数据库时，正式 crawl 拒绝启动。
* `--ephemeral` 可以正常运行测试。
* v7 数据库可以自动升级到 v8。
* 重复执行迁移不会重复修改数据库。
* migration 失败时原数据库内容不变。
* 每个数据库拥有唯一 `instance_id`。
* 每个正式报告可以定位到数据库和 crawl run。
* `db inspect`、`db verify`、`db backup` 均可使用。

---

# 四、阶段 2：正式合并 31 篇验收数据

本阶段采用“合并”，不采用“替换”。

## 2.1 实现 dry-run

命令：

```bash
python3 cli.py db merge \
  --source data/imports/acceptance-final-2026-08-14.db \
  --target data/zoofan.db \
  --dry-run
```

dry-run 必须执行完整匹配逻辑，但最后回滚事务。

输出示例：

```json
{
  "source": {
    "articles": 31,
    "discoveries": 34,
    "runs": 2
  },
  "result": {
    "new_zoos": 0,
    "matched_zoos": 73,
    "new_sources": 0,
    "matched_sources": 116,
    "new_articles": 13,
    "matched_articles": 18,
    "new_discoveries": 16,
    "matched_discoveries": 18,
    "new_runs": 2,
    "conflicts": 0
  }
}
```

上述数字只是格式示例，实际值必须由 dry-run 计算，不能预设最终文章一定是 31 或 49。

---

## 2.2 合并顺序

必须按照依赖顺序处理：

```text
1. zoos
2. sources
3. crawl_runs
4. articles
5. article_discoveries
6. crawl_run_stats
7. crawl_zoo_results
8. article_zoo_identities
```

整个正式合并必须在一个事务中完成。

任何一步失败：

```text
ROLLBACK
```

正式数据库不能处于“只合并了一半”的状态。

---

## 2.3 Zoo 匹配规则

顺序：

```text
1. public_id
2. zoo_key
3. 官方网站规范化域名
4. 国家 + 城市 + 规范化名称
5. 无法确定时记录 conflict
```

禁止仅凭数据库整数 ID 匹配。

临时库中的 zoo ID 为 `5`，正式库中的同一个 zoo 可能是 `12`，因此必须建立：

```text
source_zoo_id → target_zoo_id
```

映射。

---

## 2.4 Source 匹配规则

顺序：

```text
1. public_id
2. source_key
3. target zoo + source_type + normalized URL
4. 无法确定时记录 conflict
```

配置字段处理：

* 正式库中的当前配置优先。
* 临时库只补充正式库缺失的配置字段。
* HTTP 状态和错误只在临时记录更新于正式记录时才考虑更新。
* 不允许旧错误覆盖更新的成功状态。

---

## 2.5 Article 合并规则

调用现有统一去重服务，不能在 merge 中重新写一套不同逻辑。

顺序：

```text
1. canonical URL
2. normalized URL
3. content hash + normalized title
4. zoo + title identity + 日期范围
```

结果分为：

```text
new
matched_identical
matched_missing_fields
content_conflict
identity_conflict
```

处理方式：

### `new`

创建新 article。

### `matched_identical`

使用目标 article，建立 discovery 和 ID 映射。

### `matched_missing_fields`

只补充目标库为空的字段，不用旧数据覆盖非空字段。

### `content_conflict`

同一文章身份但 `content_hash` 不同。

这一情况不能静默覆盖。处理规则：

```text
如果 dry-run 出现 content_conflict：
先停止正式合并；
完成阶段 3 的 article_versions 后，再把两个内容保存为不同版本。
```

### `identity_conflict`

例如 zoo + 标题相同，但 URL、正文和日期明显不同。

处理规则：

```text
默认保留为两篇文章，不自动合并；
将冲突写入 merge 报告供人工检查。
```

---

## 2.6 Discovery 合并规则

`article_discoveries` 必须保留来源关系。

唯一判断建议：

```text
article_id + source_id + discovery method + discovered URL
```

若当前 schema 只允许：

```text
article_id + source_id
```

则合并时：

* 保留最早 `discovered_at`
* 保留最近 `last_seen_at`
* 合并不同发现方式
* 不重复创建完全相同的关系

必须记录文章是通过什么方式匹配的：

```text
matched_by =
canonical_url
normalized_url
content_hash_title
zoo_title_identity
```

---

## 2.7 Run 合并规则

crawl run 不能仅按整数 ID 判断。

使用：

```text
source database instance_id
+
source crawl run public_id
```

对于原 v7 run，可以生成稳定的导入身份：

```text
UUIDv5(
  source_database_instance_id,
  "crawl_runs:{legacy_id}"
)
```

同一个来源数据库再次导入时，不会重复创建 run。

---

## 2.8 执行正式合并

只有 dry-run 满足以下条件才允许执行：

```text
hard conflicts = 0
foreign key mapping failures = 0
source records fully accounted for
```

正式命令：

```bash
python3 cli.py db merge \
  --source data/imports/acceptance-final-2026-08-14.db \
  --target data/zoofan.db
```

合并前命令自动创建：

```text
data/backups/pre-import/
```

备份。

合并后自动执行：

```text
PRAGMA integrity_check
PRAGMA foreign_key_check
db verify
```

---

## 2.9 验证幂等性

再次运行同一命令：

```bash
python3 cli.py db merge \
  --source data/imports/acceptance-final-2026-08-14.db \
  --target data/zoofan.db
```

预期：

```text
new_articles = 0
new_discoveries = 0
new_runs = 0
status = skipped_already_imported
```

---

## 2.10 重新生成正式报告

从正式数据库中的对应 run 重新生成：

```bash
python3 cli.py report \
  --db data/zoofan.db \
  --run-id <public-id>
```

然后更新：

```text
reports/latest
```

旧的、来自临时数据库的 `reports/latest` 应当归档，不直接覆盖而不留记录。

### 阶段 2 验收标准

* 日常库原有 18 篇文章全部可以找到。
* 临时库中的 31 篇文章全部被插入或映射。
* 34 条 discovery 全部被插入或识别为精确重复。
* crawl run 全部被插入或识别为已存在。
* 无孤儿外键。
* 重复导入不产生任何新增记录。
* 新的 `reports/latest` 来自 `data/zoofan.db`。
* Dashboard 与 crawl 使用同一数据库实例。
* 临时数据库和合并前备份继续保留。

---

# 五、阶段 3：Schema v9——原始抓取证据和文章版本

完成正式数据合并后，再解决 `raw_html` 与当前文章内容混在一起的问题。

## 3.1 新增 `raw_documents`

```text
raw_documents
──────────────────────────
id
public_id
crawl_run_id
source_id
requested_url
final_url
fetched_at
http_status
content_type
encoding
response_headers_json
content_hash
byte_size
storage_type
compressed_html
storage_uri
created_at
```

Phase 0/1 继续使用 SQLite 存储：

```text
storage_type = sqlite_blob
compressed_html = gzip(raw_html)
```

未来切换 MinIO/S3 时使用：

```text
storage_type = object_storage
storage_uri = raw/...
```

Dashboard 和静态报告仍然禁止输出 `compressed_html`。

---

## 3.2 新增 `article_versions`

```text
article_versions
──────────────────────────
id
public_id
article_id
raw_document_id
version_number
title
body_text
body_html_clean
language_code
author_text
published_at
source_updated_at
content_hash
parser_version
observed_at
created_at
```

`articles` 增加：

```text
current_version_id
first_seen_at
last_seen_at
```

更新规则：

```text
同一文章重新抓取
   │
   ├── content_hash 相同
   │      └── 只更新 last_seen_at
   │
   └── content_hash 不同
          ├── 创建 article_version
          ├── 更新 current_version_id
          └── 将旧派生内容标记为 stale
```

---

## 3.3 新增 `article_urls`

```text
article_urls
──────────────────────────
id
article_id
source_id
url
normalized_url
url_hash
url_type
is_canonical
first_seen_at
last_seen_at
```

用于保存：

```text
canonical URL
RSS URL
Sitemap URL
重定向 URL
带参数 URL
历史 URL
```

以后去重首先查询 `article_urls.url_hash`。

---

## 3.4 新增 `crawl_attempts`

每一个被抓取或解析的 URL 都产生一条记录：

```text
crawl_attempts
──────────────────────────
id
crawl_run_id
zoo_id
source_id
discovery_method
discovered_url
normalized_url
started_at
finished_at
duration_ms
http_status
fetch_status
parse_status
store_status
article_id
raw_document_id
matched_by
error_type
error_message
retry_count
```

这样 `crawl_run_stats` 不再是唯一证据。

统计流程调整为：

```text
crawl_attempts + article_discoveries
            ↓
重新计算 crawl_run_stats
            ↓
重新计算 crawl_zoo_results
```

---

## 3.5 拆分 Source 配置和运行状态

新增：

```text
source_runtime_state
──────────────────────────
source_id
last_attempt_at
last_success_at
last_http_status
last_error_code
last_error_message
consecutive_failures
etag
last_modified
next_crawl_at
updated_at
```

`sources` 只保存配置：

```text
URL
source type
enabled
language
crawl config
```

配置同步不再覆盖运行状态。

---

## 3.6 迁移现有文章

v8 → v9 migration 对每篇现有 article 执行：

```text
1. 如果 raw_html 非空：
   创建一条 raw_documents

2. 创建 article_versions version 1

3. 设置 articles.current_version_id

4. 将 canonical 和 normalized URL 写入 article_urls

5. 保留 articles 中旧 title/body/raw_html 列
```

第一阶段采用双写：

```text
articles 旧字段
+
raw_documents/article_versions 新字段
```

确认新读写路径稳定后：

```text
停止写旧字段
```

暂时不删除旧列，以便快速回滚。

### 阶段 3 验收标准

* 现有每篇文章至少有一个 `article_version`。
* 有 `raw_html` 的文章有对应 `raw_document`。
* 同一内容重复抓取不会增加版本。
* 内容变化会新增版本。
* RSS、Sitemap、Archive 仍然只产生一篇 article。
* 每个失败 URL 都可以在 `crawl_attempts` 中查到。
* run 统计可以从事实记录重新计算。
* Dashboard 不读取或渲染原始 HTML。

---

# 六、阶段 4：Schema v10——翻译、摘要和小红书内容

这部分在抓取数据库稳定以后实施。

## 4.1 新增 `derived_contents`

```text
derived_contents
──────────────────────────
id
public_id
article_id
story_id
content_type
language_code
title
body
structured_data_json
source_content_hash
generator_type
model_name
prompt_version
review_status
reviewed_at
created_at
updated_at
```

`content_type`：

```text
translation
short_summary
long_summary
fact_list
xhs_draft
seo_description
editorial_note
```

当文章最新版本 hash 发生变化：

```text
derived_contents.source_content_hash
!=
article_versions.content_hash
```

派生内容自动标记：

```text
stale
```

---

## 4.2 新增 `article_zoos`

支持一篇文章涉及多个动物园：

```text
article_zoos
──────────────────────────
article_id
zoo_id
relation_type
is_primary
confidence
identified_by
```

关系：

```text
publisher
subject
mentioned
origin
destination
partner
```

---

## 4.3 小红书发布数据

新增：

```text
social_posts
social_post_sources
```

保存：

```text
标题
正文
标签
审核状态
发布时间
平台文章 ID
平台 URL
引用的 article/story
引用时的 source_content_hash
```

这样每篇小红书内容都可以追踪到原始新闻及其具体版本。

### 阶段 4 验收标准

* 原文、翻译、摘要、小红书稿件完全分开。
* 每条 AI 内容记录模型和 prompt 版本。
* 原文更新后，旧稿件自动变成 stale。
* 一篇文章可以关联多个动物园。
* 小红书稿件能够反查来源文章。

---

# 七、建议按六个独立 PR 实施

## PR 1：数据路径与备份安全

包含：

```text
统一数据库路径解析
禁止隐式 :memory:
--ephemeral
数据库启动信息
SQLite Backup API
db inspect / db verify / db backup
```

## PR 2：Schema v8

包含：

```text
database_meta
schema_migrations
public_id
zoo_key/source_key
import_jobs
报告 provenance
```

## PR 3：数据库合并

包含：

```text
db merge --dry-run
ID 映射
冲突报告
事务合并
幂等导入
正式合并 31 篇数据
重新生成 reports/latest
```

## PR 4：Schema v9 数据结构

包含：

```text
raw_documents
article_versions
article_urls
crawl_attempts
source_runtime_state
现有数据 backfill
```

## PR 5：Crawler 双写和读取切换

包含：

```text
新抓取写入 raw_documents
文章版本判断
crawl_attempts
从 current_version 读取
Dashboard 兼容
stats 重算
```

## PR 6：Schema v10 内容工作流

包含：

```text
derived_contents
article_zoos
social_posts
stale 判断
```

每个 PR 必须可以独立回滚，不要把六个阶段放在一个超大提交中。

---

# 八、必须覆盖的自动化测试

至少增加以下集成场景：

| 场景                     | 预期结果                           |
| ---------------------- | ------------------------------ |
| crawl 未指定 DB           | 拒绝启动                           |
| crawl 使用 `--ephemeral` | 正常运行并输出警告                      |
| v7 数据库升级 v8            | 数据行数不变                         |
| migration 执行两次         | 第二次不产生修改                       |
| migration 中途失败         | 完整回滚                           |
| 18 篇目标库合并 31 篇源库       | 结果等于两库去重后的并集                   |
| 第二次合并相同源库              | 新增记录全部为 0                      |
| 两库整数 ID 冲突             | 通过 ID 映射正确导入                   |
| RSS 和 Sitemap 发现同一文章   | 1 article，多条 discovery         |
| 标题相同但日期明显不同            | 两篇 article                     |
| 同 URL 正文更新             | 一个 article，两个 version          |
| 同 URL 内容不变             | 不新增 version                    |
| 损坏的源数据库                | 合并拒绝，目标库不变                     |
| Dashboard              | 只读且不输出 raw HTML                |
| 报告                     | 包含 DB instance 和 run public ID |

---

# 九、本轮完成定义

本轮数据库工作只有同时满足以下条件才算完成：

```text
data/zoofan.db 是唯一正式主库
crawl 不再隐式使用 :memory:
31 篇验收数据已经正式沉淀
两库所有记录都有导入处理结果
合并过程可重复且幂等
数据库有备份、检查和恢复手段
报告能够追踪到正式数据库和 crawl run
原始网页和文章版本可以追溯
文章更新不会覆盖历史
AI 摘要和小红书稿件不会覆盖原文
Dashboard 始终保持只读
```

## 当前最先执行的范围

第一轮只做：

```text
阶段 0
阶段 1
阶段 2
```

也就是先完成：

> **保护数据库 → 升级到 v8 → 实现安全合并 → 把 31 篇文章正式沉淀 → 从正式库重新生成报告。**

`raw_documents`、`article_versions` 和 AI 内容表放到下一轮，避免在当前数据尚未统一之前同时改造抓取主流程。
