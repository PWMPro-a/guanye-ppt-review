# 团队网页版与 AI Gateway 部署

网页由 GitHub Pages 托管，账号、作品索引、评分和私有图片由 Supabase 保存。
每人使用自己的账号登录，并用团队邀请码加入。成员可以查看团队评分，只能修改自己的记录。
普通素材库与 AI 生成作品分开进入：启用评审功能时普通作品库优先调用 `get_review_library(w)`，只显示已上传作品及正式入库的生成整套/单页版本；未正式入库的生成作品留在 AI 工作台。

## 初始化

### 新库：命令行初始化

1. 创建 Supabase 项目；本机临时 Management API 令牌保存到被 Git 忽略的 `cloud/private-token.txt`，不要提交或回显。
2. 运行 `python -B cloud/manage.py create`，只创建或记录项目。
3. 运行 `python -B cloud/manage.py configure`。该命令在一次作业中准备私有 workspace、安装 `cloud/schema.sql` 与 `cloud/bootstrap.sql`、按 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08 → 09 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18 → 19 → 20 → 21 应用全部 AI migrations，并配置 Auth URL 与前端所需公钥。
4. 将 `cloud/private-workspace.json` 中的邀请码私下提供给同事。
5. 设置 `SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`、`WORKSPACE_ID` 后运行 `python -B cloud/publish_library.py`，迁移现有作品图片。该操作不迁移本地个人评分。
6. GitHub 仓库设置 Actions Variables：`SUPABASE_URL`、`SUPABASE_ANON_KEY`、`WORKSPACE_ID`。需要启用 AI 时再增加可选变量 `AI_GATEWAY_URL`。未设置 `AI_GATEWAY_URL` 时，网页隐藏 AI 工作台入口。
7. Pages 选择 GitHub Actions，运行现有 `.github/workflows/pages.yml`。Supabase Auth 的 Site URL 和 Redirect URLs 使用网站完整网址。

数据库迁移历史保存在 `guanye_internal.schema_migrations`（未暴露的专用 schema；已撤销 `PUBLIC`/`anon`/`authenticated` 权限并启用 RLS，客户端不能读取或伪造历史）。基座记为 `0000_base_schema`，每个 AI migration 的 SQL 与历史记录在同一个 `DO` 语句中执行；任一步失败都不会留下该迁移记录。迁移与 schema 的 SHA256 按 UTF-8 文本、统一 LF 换行计算，因此 Git 的 CRLF/LF 检出差异不会误报漂移。重复运行 `migrate` 只补未记录项；已有 01 → 20 的库运行 `python -B cloud/manage.py migrate` 时只补 21；历史必须是 `0000 → 01 → 02 …` 的有序前缀，缺号、乱序或已记录文件内容变化都会拒绝继续。`configure` 中的本机 `schemaInstalled` 只是部署 checkpoint，不作为迁移依据。

### 已有库与手工 SQL

手工 SQL 仍保留用于新建库、审计和恢复。全新库手工初始化按以下顺序执行。

1. 在 Supabase SQL Editor 执行 `cloud/schema.sql`。
2. 从仓库根目录运行 `python cloud/prepare_workspace.py --owner-email 管理员登录邮箱`。
3. 在 SQL Editor 执行生成的 `cloud/bootstrap.sql`，将 `cloud/private-workspace.json` 中的邀请码私下提供给同事。
4. 严格按 01 → 21 执行 `cloud/migrations/20261121_*.sql`，不能只跑最新一版。`05_review_queue.sql` 文件本身没有外层事务。在 SQL Editor 手工执行时，用 `begin; ... commit;` 包住整份文件。

已有基础库但没有迁移历史时，不要直接重跑 01 → 21，也不要自动补写记录：

- 若只存在老基础 schema，且 02 → 21 的关键对象均不存在：先备份，再显式运行 `python -B cloud/manage.py migrate --upgrade-unmanaged-base`。该命令只接受 base-only 状态，先原子记录基座，再按序执行 01 → 21。若发现任何 02 → 21 对象，它会拒绝。
- 若已手工执行部分或全部 01 → 21：先按实际 schema、约束、函数、触发器、权限与 RLS 策略逐项审计，补齐缺失 SQL；确认整套状态后，才可运行 `python -B cloud/manage.py migrate --adopt-existing`。该命令的 probes 只检查少量关键对象，不等价于验证完整 schema 或权限；它不会重跑 migration SQL，只写入基座与 01 → 21 历史记录。
- 迁移历史只接受从 `0000_base_schema` 开始的有序前缀。仅有 08、09、10、11、12、13、14、15、16、17、18、19、20 或 21、跳过中间版本或历史与文件 checksum 不一致时，必须停下人工审计，不能继续自动应用。

`cloud/manage.py` 支持 inventory、create、status、configure、migrate、upload、pages 七个步骤；`create_accounts.py` 提供初始账号，`verify_live.py` 验证线上权限并清理测试记录。管理令牌和 service-role 密钥只在本机部署时使用，不应进入网页、GitHub 变量或构建产物。
## AI Gateway

静态 Pages 不运行 Python。`cloud/ai_gateway.py` 是独立、仅用标准库的可部署 HTTP gateway；启动时导入并调用以下模块的 `configure(router)`：

- `cloud/ai_reviews.py`
- `cloud/ai_planning.py`
- `cloud/ai_generation.py`

路由是精确的 `method + path`。`handler(ctx)` 可使用：

- `ctx.user_id` / `ctx.workspace_id` / `ctx.role`
- `ctx.body`（JSON object）与 `ctx.query`（`dict[str, str]`）
- `ctx.rpc(name, params, privileged=False)`
- `ctx.select(table, query, privileged=False)`
- `ctx.storage_get(path)`
- `ctx.storage_put(path, bytes, content_type)`
- `ctx.chat_json(messages)`（OpenAI Chat Completions，支持图文多模态）

启动示例：

```powershell
$env:GUANYE_SUPABASE_URL='https://PROJECT.supabase.co'
$env:GUANYE_SUPABASE_ANON_KEY='sb_publishable_...'
$env:GUANYE_SUPABASE_SERVICE_ROLE_KEY='仅在 gateway 主机保存'
$env:GUANYE_WORKSPACE_ID='11111111-1111-4111-8111-111111111111'
$env:GUANYE_ALLOWED_ORIGINS='https://pwmpro-a.github.io'
$env:GUANYE_MODEL_API_KEY='模型网关密钥'
python -B cloud/ai_gateway.py
```

### 必需环境变量

| 变量 | 用途 |
| --- | --- |
| `GUANYE_SUPABASE_URL` | Supabase 项目 URL；兼容读取 `SUPABASE_URL` |
| `GUANYE_SUPABASE_ANON_KEY` | Supabase Auth / Storage 公钥；兼容 `SUPABASE_ANON_KEY` |
| `GUANYE_SUPABASE_SERVICE_ROLE_KEY` | 校验成员、执行服务端 RPC；兼容 `SUPABASE_SERVICE_ROLE_KEY` |
| `GUANYE_WORKSPACE_ID` | 唯一允许的 workspace UUID；不读取浏览器传来的 workspace |
| `GUANYE_ALLOWED_ORIGINS` | 逗号分隔的精确站点 origin；不配置则不返回 CORS 许可 |

### 模型与限制

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GUANYE_MODEL_GATEWAY_URL` | `https://internal.api.aiwelink.cc/v1` | 独立模型网关地址；兼容 `GUANYE_MODEL_BASE_URL` |
| `GUANYE_MODEL_ID` | `gpt-6.2-sol` | 可覆盖为 `gpt-6.1-sol` 或其它兼容 Chat Completions 的模型标识 |
| `GUANYE_MODEL_API_KEY` | 空 | 模型网关密钥；兼容 `GUANYE_AI_API_KEY`，不写日志 |
| `GUANYE_MODEL_TIMEOUT_SECONDS` | `180` | 单次模型请求超时 |
| `GUANYE_MODEL_MAX_CONCURRENCY` | `8` | 并发模型请求上限 |
| `GUANYE_MAX_BODY_BYTES` | `167772160`（160 MiB） | 请求体上限；允许约 100 MiB 原始文件经 base64 后约 133 MiB 的 JSON |
| `GUANYE_MAX_STORAGE_BYTES` | `26214400`（25 MiB） | 单文件写入上限，与评审/生成图片合同一致 |
| `GUANYE_REQUEST_TIMEOUT_SECONDS` | `30` | Supabase / Storage 请求超时 |

模型通道始终直连，禁用代理和重定向，避免 bearer / API key 被转发到其它主机。`AI_GATEWAY_URL` 是浏览器访问 gateway 的地址；`GUANYE_MODEL_GATEWAY_URL` 是 gateway 访问模型的地址，两者互不复用。

### 预评状态与 review worker

- 成员可读取 `GET /api/ai/reviews?action=pre-evaluation-status&kind=uploaded|generated&deckId=ID`，响应恰好为 `{"preEvaluationStatus":"..."}`。workspace 主响应顶层也带 `preEvaluationStatus`。
- 状态只会是 `queued`、`running`、`retry_scheduled`、`completed`、`failed`、`not_queued`。只返回文字状态，不返回模型标识、预评结果、分数或错误正文；其他成员也能查看进度。
- 状态只匹配同 workspace、同 `source_kind`、同 deck/rubric 版本和同作品的整套任务。上传轨优先读取 `baseline`，同版本缺少该变体时读取历史 `assisted`。生成轨读取 `assisted`。旧版本和逐页任务不参与状态查询，缺少匹配任务时为 `not_queued`。
- `failed` 任务若仍有余次，且 `available_at` 是可解析的正常时间而非 `infinity`，显示为 `retry_scheduled`；余次耗尽或终止时间无限时显示 `failed`。变量来自 `20261121_05_review_queue.sql`。
- 在 gateway 同一组 `GUANYE_*` 服务端环境变量（不是浏览器公开变量）下，另开一个进程消费预评队列：

```powershell
python -B cloud/ai_gateway.py --review-worker
```

- review worker 只使用 workspace 已指派的 `dedicated_judge_id` 领取任务；未指派评委时不会执行。它和 HTTP gateway 分开运行，不占用浏览器路由。
- `--review-worker` 默认启动两条相互隔离的监听线程：`uploaded` 与 `generated` 各最多一个任务，合计最多两个；两轨复用同一个 `Gateway` 的 `model_max_concurrency` semaphore。`GUANYE_MODEL_MAX_CONCURRENCY=1` 时保持旧的单线程交替消费（先 uploaded、再 generated，循环切换），不启用双轨。
- 任一轨为空按 5 秒、异常按 15 秒独立退避，不阻塞另一轨。KeyboardInterrupt 保持原行为；停止事件会唤醒两轨，并在发起下一轮查询或 RPC claim 前退出。
- `--review-worker` 只在 CLI 主线程注册最小 SIGTERM 处理：handler 仅写入停止意向，不做 `Event` / `Condition` 锁操作；正常控制路径再设置已有停止事件。停止意向会在工作区查询后以及每次实际 `ai_review_claim_jobs` RPC 前复查，因此查询间隙、首轨返回空和 parallel 的 0.1 秒轮询窗口都不会再开始新 claim；已经进入 claim RPC、模型请求或 finish RPC 的尝试允许完成。`GUANYE_MODEL_MAX_CONCURRENCY=1` 的串行循环使用同一检查。
- 领取与完成仍只通过 `20261121_05_review_queue.sql` 的原子 RPC：`ai_review_claim_jobs` 使用 `FOR UPDATE SKIP LOCKED`，`ai_review_finish_attempt` 校验 running / attempt / lease 后行锁完成。本卡不改 API、SQL 或环境既有默认值。
- 旧库有私有预览但没有人工评分的作品可通过 `backfill` 准备整套预评。已经有人工评分的旧记录仍按原整套或逐页粒度补档。排队使用对应轨道的变体规则，人工评分记录由人评流程保存。
- `20261121_07_review_calibration.sql` 新增 service-role-only RPC `ai_review_calibration(w, actor, p_kind, p_limit, p_deck_id, p_scope)`：严格分离 `uploaded` / `generated`，排除当前作品和 bucket 0 的全部 holdout；先按输入作品匹配同粒度，再优先同分类/style 等可用元信息，最后以时间、作品号和样本号稳定排序。若同粒度不足，只允许退到同轨其他粒度，并在每条样本和顶层 `scopeFallback` 中明确实际 `scope`。
- 校准的新样本只取 `ai_review_submissions` 中当前 deck/rubric 版本、维度完整的人工评分；纠偏只取专职评委已确认的正式 `ai_review_decisions`，校验 submission / record / decision 的 `deck_id`、`maxScore` 一致且存在具体 match，按作品/粒度/单元去重，不读取提交里的分类纠偏字段。`uploaded` 的 legacy 评分按 `reviews.data.scores`（整套）或 `reviews.data.pages[unitId].scores`（逐页）读取，只有对当前 `workspaces.settings.dimensions` 完整且落在当前 `maxScore` 内才可用；样本返回真实私有存储 `previewPath`，由服务端读取，不收前端 URL。
- 选择法参考 LangChain `SemanticSimilarityExampleSelector` 的输入条件 few-shot 选择；PostgREST 继续使用现有 Functions-as-RPC 面，不需要升级 PostgREST 版本。旧 `ai_review_training_bundle` 保留用于升级兼容，gateway 主链路应只调用新 RPC。
- `20261121_08_review_comparison.sql` 新增 service-role-only 稳定 RPC `ai_review_comparison(w uuid)`。函数为 `security invoker`、`set search_path=''`，只返回聚合 JSON（不暴露 submission / record / user ID、单条样本或分数），`w` 为 null 时返回空结果；调用侧继续执行现有 workspace 成员鉴权，直接 `anon` / `authenticated` 不可执行。它严格按同一 submission、`match.variant = record.variant`、bucket 0 holdout，以及 deck/scope/unit/source/deck_version/rubric_version/max_score 全部一致配对 baseline / assisted；共同数字分项按 `abs(AI-human)/max_score` 计算，先以提交单元求总体 MAE，再对提交等权平均，分项各自平均，并按 `uploaded` / `generated` 与总计保持现有 shape。08 迁移探针检查函数存在且仅 `service_role` 可执行；已有 01 → 07 部署运行 `python -B cloud/manage.py migrate` 时只会补 08。
- 08 的留出组比较也直接读取真实旧评分：只纳入当前 deck/rubric 版本、baseline/assisted 成对且维度完整的样本；同一评分若存在现代 `ai_review_submissions`，优先按其去重。
- 观页固定留出 MAE 使用人工基准分：有现代盲评提交时取 `ai_review_submissions.entry.scores`；该评分单元无有效现代盲评替代时，沿用旧库 `reviews.data` 的历史人工评分。正式纠偏分（`ai_review_decisions.formal_scores`）不参与 MAE，只用于素材库展示及其他正式评级/业务投影。
- `20261121_09_review_backfill.sql` 新增 service-role-only RPC `ai_review_backfill(w, actor, p_kind, p_offset, p_limit)`：再次校验 dedicated judge；先按 workspace 与 `uploaded|generated` 轨道过滤，再以 `deck.id` 稳定排序并 `offset/limit`，其中 `p_offset >= 0`、`p_limit` 为 1..20。
- 该 RPC 只调用现有 `ai_review_prepare` 排队，绝不调用 `ai_review_submit`，也不写 `reviews` / `ai_review_submissions` / `ai_review_decisions`。上传轨从同 workspace/deck 全部作者存量 `reviews` 中收集有效整套与页级分数：页评只补当前且真实私有预览中的对应页，无页评至少补整套，整套与页评并存时两类都补；生成轨忽略旧 reviews，有私有预览时补整套。`preparedUnits` 按覆盖的整套/页面粒度计数，不按 baseline/assisted 队列变体计数；重复调用沿用现有唯一约束保持幂等。09 迁移探针检查函数存在、`security definer` 且仅 `service_role` 可执行；已有 01 → 08 部署运行 `python -B cloud/manage.py migrate` 时只补 09。
- `20261121_10_review_queue_listing.sql` 新增三个仅 `service_role` 可执行的标量 JSONB RPC：`ai_review_queue_decks(w,p_kind,p_after,p_limit)` 以 `id` 升序键集分页，`p_after=null` 表示首页、`p_limit` 为 1..50，并多取一行生成 `nextCursor`；直接调用时只拒绝空字符串，其他游标文本（包括纯空格作品 ID）按原值比较、不裁剪；`ai_review_queue_counts(w)` 在库内按 `uploaded` / `generated` 与四种状态精确计数（含零值，不受 PostgREST 行上限影响）；`ai_review_queue_jobs(w,p_kind)` 在 `p_kind` 为空时合并双轨，按 `created_at,id` 升序限 50，只返回现有 `_queue_state` 的字段投影。三个函数均为 `security invoker`、`set search_path=''`，并配套 workspace+轨道分页与任务排序索引。工作区成员可分页浏览两轨的作品简表，任务明细和精确计数仍限专职评委。三种查询的 workspace 均取自网关认证上下文。已有 01 → 09 部署运行 `python -B cloud/manage.py migrate` 时只补 10。
- `20261121_11_planning_history.sql` 新增仅 `authenticated` 可执行的标量 JSONB RPC `ai_planning_list_my_plans(w,p_after,p_limit)`。函数为 `security definer`、`set search_path=''`，先校验 `auth.uid()` 的 workspace 成员身份，再强制 `created_by=auth.uid()`；管理员也只列出自己创建的方案。结果按 `(created_at,id)` 倒序稳定键集分页，`p_after` 必须是当前 workspace 当前用户自己的方案 ID，否则统一返回同一游标错误；`p_limit` 为 1..50；额外取一行只判断是否还有下一页，`nextCursor` 固定返回本页最后一条 visible 记录的 ID，不会跳过额外行。`pageCount` 从 `draft.pages` 为数组时的 `jsonb_array_length` 计算，旧或畸形数据回退为 0，不对旧 `pageCount` 文本做强转。返回仅为 `{items:[{planId,title,mode,pageCount,status,revision,createdAt}],nextCursor}`，不返回草稿正文、参考、上传路径或同组他人方案。迁移同时新增 `(workspace_id,created_by,created_at desc,id desc)` 索引，不改变既有 RLS、表授权或 `ai_planning_get_plan` 读取策略。已有 01 → 10 部署运行 `python -B cloud/manage.py migrate` 时只补 11。
- `20261121_12_library_classification.sql` 重写 `ai_review_effective_meta(w,target_id)` 但保持原返回键和 `security definer` / `set search_path=''` 属性。它只从当前 deck/rubric 版本、维度完整的整套人工提交中读取非空 `classificationCorrection` 分类/style，字符串和既有对象字段都先 trim，分类与风格分别按 `created_at,id` 稳定取最后一个非空值；页级提交不再升级为整套元数据。当前专职评委的整套正式确认/纠偏优先于普通提交，生成单页正式分类不能覆盖整套。普通提交后依次回退到作品原有 metadata 和同版本同评分标准的 AI 分类；没有有效整套提交时 AI 值保持隐藏，单页提交也不能解锁。函数改为仅 `service_role` 可直接执行，`get_review_library` 与校准 RPC 仍通过各自的 definer 调用；公开生成作品是否出现继续只由既有入库门禁决定。迁移探针检查该版本标记、`security definer`、审计路径和 service-only 权限。`ai_review_decisions.expires_at` 仍只约束正式入库审批，不限制已确认分类的持久有效期。已有 01 → 11 部署运行 `python -B cloud/manage.py migrate` 时只补 12。
- `20261121_13_formal_correction.sql` 以 `CREATE OR REPLACE` 重写 `ai_review_confirm(w,actor,payload)`，保持参数、service-only 权限、版本/匹配/入库门槛和返回键。正式分先用 `ai_review_scores_complete` 校验当前轨道全部维度；上传轨只保留 `workspaces.settings.dimensions` 当前评分项参与平均，生成轨固定保留 `visual/layout/match`，额外维度不会抬高正式分。已匹配 AI 记录的任一当前维度数值不同即标记 `corrected`；AI 分缺失或非数字不会被当成数字纠偏。非空分类按字符串/对象 label 及 style 归一比较，显式 `corrected` 继续兼容。空 `reasons`、`null` 或可选人工理由本身不再作为模型分错的证据，但原因仍随正式分保存。命中既有幂等键时响应补回实际 `status`，幂等键生成规则不变；迁移不回写历史 decision，也不自动补造过去纠偏。已有 01 → 12 部署运行 `python -B cloud/manage.py migrate` 时只补 13。
- `20261121_14_library_formal_scores.sql` 重写 `review_score_summary(w)` 但保持原上传团队 `count/mean`、返回 JSON 对象和 authenticated-only 权限。新增 service-only 的稳定表函数 `ai_review_uploaded_formal_scores(w)`，上传轨只选当前专职评委、当前 deck/rubric 版本、当前评分维度完整且 `max_score` 等于当前满分的 `confirmed/corrected` decision；每个 `scope/unit` 按 `created_at,id` 稳定取最新，页级 decision 必须仍对应当前私有预览页。`expires_at` 只约束入库审批，不限制正式分展示。生成轨只读取 `library_visible` entry，复用 `get_review_library` 的整套/单页可见判定并排除未入库作品、未入库单页和非当前预览页；正式分投影为 `decision.formal_total / decision.max_score × 当前 maxScore`，按两位小数保留当前分制精度，不改后台 decision 原值。有正式分但没有旧均分的对象返回 `count=0, mean=null`，正式字段独立存在且输出不含评委 ID、decision/record ID 或原始记录。已有 01 → 13 部署运行 `python -B cloud/manage.py migrate` 时只补 14。
- `20261121_15_planning_original_references.sql` 以 `CREATE OR REPLACE` 重写仅 `service_role` 可执行的 `ai_planning_reference_candidates(w uuid,max_items integer default 12) returns jsonb`。函数为 `security definer`、`set search_path=''`，只从当前 workspace 的上传作品中选择策划参考：团队分只统计当前评分维度完整的旧 `reviews`，再用 `ai_review_uploaded_formal_scores(w)` 的整套正式分覆盖团队均分；排除 bucket 0 holdout，按分数、完整评分数降序排列，同分按 deck ID 升序稳定排列，`max_items` 限制在 1..20。返回不含生成作品，只带作品标题/style/分数来源、页数、bucket 和最多两张当前私有预览路径。已有 01 → 14 部署运行 `python -B cloud/manage.py migrate` 时只补 15。

- `20261121_16_uploaded_baseline.sql` 重写 `ai_review_prepare(w,actor,payload)` 的变体路由。常规上传只排 `baseline`，常规生成保留 `assisted`，两轨的 bucket 0 留出集都排两种变体。`backfill` 复用该函数。迁移保留既有任务、结果和幂等键，RPC 仍仅允许 `service_role` 调用。已有 01 → 15 部署运行 `python -B cloud/manage.py migrate` 时只补 16。

- `20261121_17_review_workspace_batch.sql` 新增仅 `service_role` 可执行的标量 JSONB RPC `ai_review_workspace_batch(w, actor, p_deck_id, p_submission_ids)`。函数为 `security invoker`、`stable`、`set search_path=''`，接收当前请求已认证并已加载的 submission ID 批次，最多 1000 个；服务端仍先用现有 `_workspace_state` 的 `scores_complete` 生成公共 `scoresComplete`，RPC 再次校验 workspace、deck、actor 本人提交和全部 match/record/job/decision/entry 工作区关系。只有完整评分的本人 submission 才汇入预测；records 按 record ID 去重，decisions 保持原有排序和公共字段，`libraryVisible` / `entryId` 仍由同 workspace 的 entry 决定，旧 decision 不因预测版本过旧而消失。一次 RPC 返回 `{records, decisions}`，替代逐 submission 的 matches、records、decisions、entries 读取；60 页逐页评分场景的详情读取为 1 个 RPC、4 类详情表 0 个 GET。仅在 PostgREST 明确返回 `PGRST202`（或同义 schema-cache 缺函数消息）时兼容回退 16 的旧读取路径，权限、RLS、业务和格式错误继续上抛。已有 01 → 16 部署运行 `python -B cloud/manage.py migrate` 时只补 17。

- `20261121_18_review_backfill_classification.sql` 重写 `ai_review_effective_meta(w,target_id)` 与 `ai_review_backfill(w,actor,p_kind,p_offset,p_limit)`：保留已有逐页回填任务，只补历史上只有页级评分、但缺少 deck AI 记录导致分类无法生效的缺失 deck 任务；批次读取保持有界、确定性和幂等。已有 01 → 17 部署运行 `python -B cloud/manage.py migrate` 时只补 18。

策划历史 API 合同：

- `GET /api/ai/planning/history?cursor=<planId>` 返回当前登录用户在当前 workspace 的轻量方案历史，顺序与 `nextCursor` 语义同上述 RPC。HTTP 合同只接受 `cursor`，每页固定 20 条；网关以 `PLANNING_HISTORY_PAGE_SIZE=20` 调用 SQL RPC，RPC 自身的 `p_limit` 仍支持 1..50。首页省略 `cursor`；后续页面把上一页 `nextCursor` 原样作为 `cursor`。只允许本人方案的 cursor，陌生、他人或其它 workspace 的 cursor 都按同一无效游标错误处理。
- `POST /api/ai/planning/save` 供策划编辑复用既有 `ai_planning_save_draft`：请求必须带 `planId`、`expectedRevision` 和 `draft`，服务端从鉴权上下文确定 workspace 与 owner，只有创建者可以保存 draft；revision 不一致时拒绝覆盖，状态已变为 `confirmed` 后禁止保存。Gateway 接入该路由时不新增平行保存实现。

### 身份、worker 与 private storage

- 每个请求都必须带 Supabase `Authorization: Bearer <access_token>`；gateway 再用 Supabase Auth 校验用户，并从配置 workspace 的 `members` 行读取角色。
- 浏览器传来的 `X-Workspace-Id`、`workspace_id`、`role`、`user_id` 均不作为身份依据。
- 生成 worker 路由 `/api/ai/generation/worker/*` 同时校验专用成员的 Supabase access token 和独立 worker secret。Windows 主机与 Gateway 使用同一个 `GUANYE_WORKER_TOKEN`。
- storage bucket 固定为 `ppt-library`，与 `cloud/schema.sql` 的 `team_images_*` policy 完全一致。任何其它 bucket 配置都会拒绝启动。
- 私有对象路径固定以 `<workspace_uuid>/` 为首段，与 `public.is_member((storage.foldername(name))[1]::uuid)` 一致。路径包含 `..`、反斜杠、绝对路径或其它 workspace 前缀时拒绝。
- Storage 读写使用用户 bearer 和 anon key，让现有 RLS 生效；service role 只用于受路由约束的 RPC / select。

### CORS 与错误

- 只回显 `GUANYE_ALLOWED_ORIGINS` 中的精确 origin；不支持的 Origin 返回 403。
- 未登录或 bearer 无效返回 401；不是 workspace 成员、角色不允许或 worker 身份无效返回 403；业务 `ValueError` 返回 400。
- 请求体超过上限返回 413；上游超时返回 504；其它上游故障返回 502。
- 日志只记录 method、路径和状态码，不记录 Authorization、worker token、请求体或模型密钥。

- AI 工作台给 `GuanyePlanning.mount` 传入 `onGenerationSubmitted`；策划提交生成后自动切换“生成进度”，并把回执中的 `jobId/planId` 传给 `GuanyeGeneration.mount`，不需要手抄任务号。
- 生成进度从 `GET /api/ai/generation?action=recent` 加载当前成员在本工作区最近提交的 30 项任务。重新登录或刷新网页后可继续查看，切换任务后页面读取所选任务的进度。
- 已完成页可以预览并下载原图，下载通过当前成员的访问凭据读取私有图片。每张失败页可以单独重试。
- Windows Worker 优先从 EXE 的 `image-versions` 接口获取唯一标记为当前的原图版本，并校验下载地址。桌面代理要求该接口返回原图版本。老版直连接口可以使用项目返回的图片地址。

## Windows 生图 Worker

启动 HN PPT2 后，用其创作设置配置 GPT Image 模型与创作凭据。`GUANYE_EXE_DATA_DIR` 指向该实例的数据目录。Worker 读取这里的 `desktop-ready.json`，通过本机接口创建项目、逐页发起创作并下载图片。

```powershell
python -B cloud/windows_image_worker.py --preflight
```

`--preflight` 依次检查已运行服务的三个只读 GET 接口。每个阶段通过后才继续。预检的付费请求数为零。

- 使用 `desktop-ready.json` 时，先检查 `/api/image2ppt/jobs?limit=1` 是否返回 `data.jobs` 数组。空数组有效，预检只输出摘要。
- 设置 `IMAGE2PPT_BASE_URL` 时，该地址优先用于直连。直连先检查 `/health` 是否返回 `status: ok` 的 JSON 对象。
- `/api/image2ppt/config` 必须返回 `worker_exists: true`，且 `max_files` 字段必须存在，值为正整数或 `null`。`null` 表示无文件数上限，文件大小和总字节数限制仍然适用。
- `/api/image2ppt/settings` 必须返回非空 `vision_model`。

响应可直接返回，也可包在 `{"data": ...}` 中。HTTP 200 本身不算通过：响应结构或能力检查失败时，退出码为 `2`，`httpProtocol.stage` 标出 `jobs`、`health`、`config` 或 `settings`。

成功时 `httpProtocol.verified: true` 仅证明已运行服务的 HTTP 协议与能力兼容；`runtimeImage.verified: false` 且 `sha256: null`，因为 HTTP 响应不能识别 EXE 文件或进程映像。PyInstaller 程序的摘要必须对确切 EXE 文件测量，并与发布来源信息分别核对：

```powershell
$exe = 'C:\path\to\HN PPT2 对话体验版.exe'
Get-FileHash -Algorithm SHA256 -LiteralPath $exe
```

实际出图需要 `GUANYE_API_BASE`、`GUANYE_WORKSPACE_ID` 和 `GUANYE_WORKER_TOKEN`。通过 `GUANYE_WORKER_STATE` 可指定独立状态目录。工作区并发默认为 4，数据库在所有 worker 实例之间统一限制活动页数。

常驻 worker 通过 `GUANYE_SUPABASE_URL`、`GUANYE_SUPABASE_ANON_KEY`、`GUANYE_WORKER_EMAIL`、`GUANYE_WORKER_PASSWORD` 登录专用成员账号。凭据由 Windows 进程的秘密配置提供。进程会在内存中共享会话并自动续期，各生图线程共用同一个刷新过程。设置 `GUANYE_TOKEN` 时优先使用该短期 access token，适合单页验收。

```powershell
python -B cloud/windows_image_worker.py --once --max-pages 1
```

以上命令只领取一次，至多处理一页，使用该任务的实际生图额度。退出码 `0` 表示已回传图片，`2` 表示失败，`3` 表示没有可领取的任务。确认图片可下载后，用 `python -B cloud/windows_image_worker.py` 持续处理队列。

## 普通素材库隔离

- 启用 `AI_GATEWAY_URL` 时，普通作品库优先调用 authenticated `get_review_library(w)`；服务端返回 `{decks:[{id,data}],entries}`，已完成正式整套与正式单页筛选。
- 只有 `get_review_library` 不存在（PostgREST `PGRST202` / PostgreSQL `42883`）才回退旧查询；401、403、RLS、业务错误或格式错误都会直接显示，不静默吞掉。
- AI 功能关闭或旧 schema 保持原普通作品库查询；启用但尚未应用 02 migration 时，回退后用 `decks.source_kind='uploaded'` 做服务端过滤。
- 生成作品只从 AI 工作台进入独立队列；正式确认后才以 `generated` 进入生成评审和独立统计。
- 02 SQL 的 `review_score_summary` 已通过 `ai_review_review_allowed` 排除未正式入库的生成作品；`public/cloud.js` 再按 `get_review_library` 可见 deck ids 过滤汇总键，作为 UI 兼容兜底。
- 所有库内预览继续使用 `window.Cloud.imageUrl(path)`；它统一通过 `ppt-library` 的 `createSignedUrls` 为评审图、正式整套图和正式单页筛选图生成短期签名 URL。生成卡只需返回存储路径。
- 普通 UI 不应把 generated 作品送进旧评分操作；生成作品的评分只能走生成评审/正式确认链路。

## 策划方法来源
- [`alchaincyf/huashu-design`](https://github.com/alchaincyf/huashu-design)（MIT License）：参考其 [视觉评审指南](https://github.com/alchaincyf/huashu-design/blob/e8d1148a80d5a281d4247cffe320add3df017928/references/critique-guide.md) 中可观察的信息层级、对齐与间距、功能性和载体密度检查，以及 [幻灯片模块](https://github.com/alchaincyf/huashu-design/blob/e8d1148a80d5a281d4247cffe320add3df017928/references/modules/deck.md) 中按叙事角色与数据关系选择版式的方法。
- [`Duoasa/Duoasa-skill`](https://github.com/Duoasa/Duoasa-skill)（MIT License）：参考其 [SKILL.md](https://github.com/Duoasa/Duoasa-skill/blob/1844606181b18501c40cf5256d02a4c3a13aae8e/skills/duoasa-skill/SKILL.md) 中从内容、受众和使用目的解释设计选择，允许有理由的非常规处理，并以具体问题而非统一画风作判断的方法。
- 观页将这两套方法写进策划与 assisted 预评的系统提示词。策划先从页面用途、数据核心关系和读者应理解的结论出发选择形式，并结合受众、资料和参考作品说明风格取舍；资料不足不补造数字，模板样板用明确占位符。辅助预评会给出可从预览图核对的评分理由。运行时由观页自己的服务提供。
- baseline 保持既有提示词与评分语义不变，上述方法只加到 assisted。旧记录若跨版本或跨方法，比较需谨慎；现有 MAE 指标语义不变，且其中的变化不能归因于单一因子。

## 上传整套评分的离线校准

现有历史报告只提供 `idSha256`。脚本会检查 deck ID 唯一性和训练集与留出集不重叠。它不能证明原始套件独立，汇总报告会标记 `source_groups_not_provided`。

旧报告可继续使用现有命令。

```powershell
python -B -m cloud.verify_uploaded_rating_calibration train.json holdout.json --output calibration.json
```

如果输入行带 `sourceGroupSha256`，两份报告的每一行都必须提供该字段。每个原始套件在每份报告中最多一个代表，训练集与留出集不能有相同原始套件。指纹必须来自实际原始文件。不得用标题、路径或 deck ID 生成或替代。

下一轮前瞻验收使用严格模式。缺少 `sourceGroupSha256` 时脚本拒绝运行且不写输出。

```powershell
python -B -m cloud.verify_uploaded_rating_calibration train.json holdout.json --require-source-groups --output calibration.json
```

固定分数和偏差校正也参与比较。单调校准的斜率由训练样本交叉验证选择。已查看过基线的留出集用于回顾分析，推广新策略需要在独立的新人工评分样本中验证。工具在本地读取 JSON 并写入汇总报告。

## 使用与限制

- 在线版普通素材库的“导入作品”在配置 AI Gateway、未指定追加目标且只选择一份不超过 25 MiB 的 PPT/PPTX 时，直接调用 `POST /api/ai/reviews/import`。该 handler 继续使用 `document_import.convert` 生成逐页 PNG，写入原有 `ppt-library` 私有桶；单份文件按服务端实际解码字节计算 SHA256，并调用 service-role-only `ai_import_review_deck(...)` 原子写入来源记录和作品。客户端提交的同名字段被忽略。未配置 AI Gateway 时保留“请另存为 PDF 后导入”的旧行为。
- 现有 AI handler 会把同一次请求中的多份 PPT/PPTX 合并为一套作品，因此普通素材库不会把多文件直接透传或逐套循环请求。多文件合并继续走旧 `add_review_deck`，来源组记为未知，不写 provenance，也不从标题、路径或 deck ID 猜测指纹。多份或混合文档会明确提示：AI 工作台会合并为一套，想各自成套请逐份导入或先转 PDF；追加 PPT/PPTX 只提示先另存为 PDF。原有 PDF/图片流程不变。
- 部署顺序固定为先执行数据库 M19，再部署调用 `ai_import_review_deck` 的 gateway。数据库函数缺失时单文件导入失败关闭并提示先执行迁移；不得降级调用旧 RPC 或伪造来源。
- AI 工作台原有 PPT/PPTX 上传入口继续支持 1–10 份文件、单份 25 MiB、合计 100 MiB 和每套 1–600 页；这些限制由现有 handler 执行，本卡不扩展后端校验。
- 汇总均分只统计所有评分项均已填写的成员记录，未完成记录仍显示各项分数。
- 每套作品可切换整套评分或逐页评分，两种记录分别保留。逐页模式的作品均分为所有页面、所有评分项的等权平均；全部页面完成后计入团队均分。追加页面后需补评新页面。逐页笔记、分数随 JSON 备份保存，CSV 包含每页明细。
- 修改团队评分标准后，其他已打开的页面会在保存时提示刷新，避免旧标准覆盖新标准。
- 原图访问链接有效期一小时，长时间使用后可点“刷新作品库”重新生成。
- 本机版数据在 `data/`，云端版数据在 Supabase。两者不会自动同步评分。
- Supabase 免费套餐有容量和流量限制，休眠项目需要恢复后才能访问。

### 在线 PPT 导入实现参考

素材库 PDF 仍在浏览器内由 PDF.js 逐页预览；单份 PPT/PPTX 则交由现有 AI 导入 handler，服务端 `document_import.convert` 调用 `powerpoint_pdf`，优先 PowerPoint COM，失败后调用 LibreOffice `--headless --convert-to pdf --outdir`。以下上游源码固定在本次核对提交，用于核对服务端命令行转换和浏览器 PDF 预览语义：

- LibreOffice `cmdlineargs.cxx`（`--headless`、`--convert-to`、`--outdir` 解析）：<https://github.com/LibreOffice/core/blob/6ca1666c2c05b28b944b9dbe51c42c723e95c59b/desktop/source/app/cmdlineargs.cxx#L559-L603>
- LibreOffice `dispatchwatcher.cxx`（命令行转换属性与 `storeToURL` PDF 导出）：<https://github.com/LibreOffice/core/blob/6ca1666c2c05b28b944b9dbe51c42c723e95c59b/desktop/source/app/dispatchwatcher.cxx#L652-L720>
- PDF.js `getDocument`、`numPages` 与 `page.render`：<https://github.com/mozilla/pdf.js/blob/62466e5b8f75e14f288abdc10fc2deda94c86c26/src/display/api.js#L230-L240> 和 <https://github.com/mozilla/pdf.js/blob/62466e5b8f75e14f288abdc10fc2deda94c86c26/src/display/api.js#L1542>
- PDF.js workerSrc 与逐页渲染示例：<https://github.com/mozilla/pdf.js/blob/62466e5b8f75e14f288abdc10fc2deda94c86c26/examples/webpack/main.mjs#L8-L24>

### 上传来源指纹 V19

`cloud/migrations/20261121_19_uploaded_source_provenance.sql` 在私有的 `guanye_internal.uploaded_source_provenance` 表记录 `deck_id`、`workspace_id` 与严格 64 位小写 SHA256。表对 `public`、`anon`、`authenticated`、`service_role` 全部撤销直接读写权限；只有 service-role-only、`SECURITY DEFINER`、空 `search_path` 的 `public.ai_import_review_deck(uuid,uuid,jsonb,text)` 可以写。RPC 先锁 workspace、校验 actor 的实际成员关系、只允许新建 uploaded 作品，并在插入 deck 前写 provenance；`deck_id` 外键为 `DEFERRABLE INITIALLY DEFERRED`，删除 deck 时级联删除 provenance。延后约束触发器另外校验 provenance 与 deck 的 workspace 一致。

本迁移不反填旧作品：旧库中已有 deck 没有来源记录，不能根据旧标题、路径或 ID 推断。当前采集只覆盖 gateway 服务端实际接收的单份 PPT/PPTX 原始解码字节；多文件合并和旧上传仍为未知。M19 与 M20 应在开启新 gateway 导入前连续迁移，避免 M19 已产生来源记录后、M20 尚未安装时写入旧桶队列。语义依据为 [scikit-learn `GroupShuffleSplit`](https://github.com/scikit-learn/scikit-learn/blob/f85f0584d5d7a66068bfe50b395ac855b2289dc2/sklearn/model_selection/_split.py#L2066-L2188)。延后 FK/约束实现依据 PostgreSQL [`parse_utilcmd.c`](https://github.com/postgres/postgres/blob/207b5293b8d9a217bb918b5b44774b3b4f1e2f89/src/backend/parser/parse_utilcmd.c#L3966-L4027) 与 [`tablecmds.c`](https://github.com/postgres/postgres/blob/207b5293b8d9a217bb918b5b44774b3b4f1e2f89/src/backend/commands/tablecmds.c#L14845-L15080)。

### 上传来源分组 V20

`cloud/migrations/20261121_20_uploaded_source_groups.sql` 增加私有的 service-role-only helper，把 M19 可信 source SHA256 当作 GroupShuffleSplit group：同一 SHA 的不同 deck ID 进入同一稳定 5 桶，bucket 0 仍为 holdout。没有来源指纹的旧上传、多文件合并和 generated 轨继续使用原 `md5(deck_id)` 首字节回退，不把未知误标为已知来源。helper 不对 `anon` / `authenticated` 暴露，provenance 表仍不可直接读取；source SHA 只参与内部分桶和同源比较，不进入 snapshot、校准、规划、比较或任务 JSON。

M20 用 `CREATE OR REPLACE` 更新 `get_ai_review_sample_snapshot`、`ai_review_calibration`、`ai_review_comparison`、`ai_planning_reference_candidates`、`ai_review_prepare`，保持签名、返回结构和原服务角色 ACL。校准样本与正式纠偏排除目标同源组和 holdout；规划参考只取 uploaded 高分非 holdout；snapshot、prepare、holdout comparison 使用同一 helper。迁移在事务内锁住 decks/provenance/jobs/records，若 M19 已写入的来源 deck 存在旧算法 bucket/split 不一致的 queue 或 record，直接失败并回滚，不修改旧评分、历史或队列。M19/20 必须先连续完成，再允许新 gateway 处理单文件导入。`ai_review_comparison` 是 security-invoker RPC，因此 service_role 只获得 bucket / boolean helper execute；完整 hash key helper 仅供 security-definer 校准函数内部使用，不直接授予 service_role。

M20 的表锁只能阻止锁获取后的新写入，不能撤销迁移开始前已经取得 workspace/事务快照、正等待 job 写锁的旧 prepare 会话。若没有额外的持久路由 guard，发布窗口必须按以下顺序：停止旧 gateway 与 review worker，等待在途导入/预评结束或明确取消，执行 M19/M20，再启动使用 M20 的新 gateway 与 worker。不要把 SHARE ROW EXCLUSIVE 锁描述成能覆盖全部旧会话竞态的保证。

M20 不重写历史评分或队列，也不做 MAE 加权去重；M21 只处理比较观测的同源去重，不改变 M20 的评分、队列、校准或存储。

### 比较来源去重 V21

`cloud/migrations/20261121_21_uploaded_comparison_source_dedup.sql` 只替换 `public.ai_review_comparison(uuid)`，保留 M20 的 holdout/source-group 路由、`security invoker`、空 `search_path`、service-role-only ACL 以及已有字段语义。新增的私有 `SECURITY DEFINER` helper 只返回上传作品的 `deck_id`、是否来自可信 M19 provenance、以及是否保留；不返回 source hash 或完整组键，`service_role` 仍不能直接读取 provenance 表。

比较先按现有配对与 `abs(AI-human)/max_score` 的共通 numeric 分项形成合格样本；只有至少有一条共通分项的 uploaded deck 才进入来源代表选择。同一可信 SHA 组按 `decks.created_at` 最早者保留，同刻按 `id COLLATE C` 打破平局；较早但过时、不配对或没有共同数值的 deck 不占位。代表作品保留自己全部合格单元，同组其余 deck 从维度统计、全局统计和 `byKind.uploaded.holdoutUnits` 中排除；代表选择不看 baseline/assisted 误差或得分高低。无 M19 provenance 的原库 deck 每套仍按原规则保留，generated 轨完全不变。

响应在 `byKind.uploaded.sourceDedup` 增加 `knownGroups`、`unknownDecks`、`excludedDuplicateDecks` 三个非负整数；generated 不增加该字段，且 JSON 不包含来源 hash、组键、样本或用户信息。该 RPC 只读，不写作品、评分、审校、队列或存储，也不根据标题、路径或旧数据猜测来源组。

## 验证

```powershell
python -B test_ai_gateway.py
python test_workflows.py
python test_documents.py
node test_cloud_client.cjs
```

安装 `npm install --prefix .test-output/sql @electric-sql/pglite` 后：

```powershell
node test_cloud_sql.cjs
node test_ai_review_sql.cjs
node test_ai_review_queue_sql.cjs
node test_ai_generation_sql.cjs
node test_ai_review_calibration_sql.cjs
node test_ai_review_comparison_sql.cjs
node test_ai_review_backfill_sql.cjs
node test_ai_review_queue_listing_sql.cjs
node test_ai_planning_history_sql.cjs
node test_uploaded_source_provenance_sql.cjs
node test_uploaded_source_groups_sql.cjs
node test_uploaded_comparison_source_dedup_sql.cjs
```

配置 Playwright 和 Edge 后可运行 `python -B -m unittest -v test_ai_generation_browser`。此测试验证按页码预览、授权下载、失败页单独重试和刷新后切换历史任务。

`test_ai_gateway.py` 使用注入 transport / mock，仅启动本机 loopback 测试 server，不向 Supabase、模型或 storage 发真实请求。它覆盖精确路由、401/403/400、CORS、请求体上限、private bucket/路径、worker token、特权 RPC、模型地址/标识覆盖、模型并发上限，以及 review worker 的双轨重叠、单轨异常退避、停止不再发起 claim 和 `model_max_concurrency=1` 串行交替策略。

---

## 容器化 staging：Linux gateway + review worker

本节是增量 staging 入口，不改变上面的生产说明。前端仍由 GitHub Pages 托管；Linux 容器只运行 HTTP gateway 与 `--review-worker`。现有 Windows EXE 生图 Worker 继续留在 Windows，通过 HTTPS gateway 接入，不进入本镜像。

### 运行边界

- `gateway` 与 `review-worker` 使用同一镜像、同一非 root UID，分别启动 HTTP 服务和无监听队列消费者。镜像入口固定为 `python -B -m cloud.ai_gateway`；review worker 只追加 `--review-worker`。
- `gateway` 的 Compose `stop_grace_period` 保持 20 秒；`review-worker` 独立配置为默认 5 分钟。这个窗口只用于有界排空，不是模型超时或数据库租约；若环境里的模型/存储步骤可能更久，可在部署时覆盖为更大的值。窗口耗尽后 Compose 会发送 SIGKILL，未完成尝试继续按既有队列租约恢复，不能在窗口内假设任务一定成功。
- gateway 只在主机发布 `127.0.0.1:8787`；HTTPS 由宿主机现有反向代理终止，不把端口直接暴露到公网。
- 容器内强制 `GUANYE_HOST=0.0.0.0`、`GUANYE_PORT=8787`。服务端其余配置沿用工程现有的 `GUANYE_*` 名称。
- `cloud/private-gateway.env` 由测试环境运营方在目标主机私下创建，并被 `cloud/private*` 规则忽略；仓库中不存在该文件是预期状态。Compose 允许它缺失以便静态解析，但 gateway 缺少必需变量时会在启动阶段失败关闭。
- 该 env 文件不会进入镜像或构建上下文；`cloud/Dockerfile.dockerignore` 采用 deny-by-default allowlist，只放行 `Dockerfile` 实际复制的运行文件。
- 该环境面向独立的 Supabase 测试项目，不应复用生产 service-role key、生产账号或生产作品。

### 运营方配置

在部署主机的 `cloud/private-gateway.env` 中按现有启动契约提供以下变量；不要提交、打印或粘贴到文档：

```text
GUANYE_SUPABASE_URL
GUANYE_SUPABASE_ANON_KEY
GUANYE_SUPABASE_SERVICE_ROLE_KEY
GUANYE_WORKSPACE_ID
GUANYE_ALLOWED_ORIGINS
GUANYE_MODEL_API_KEY
GUANYE_WORKER_TOKEN
```

按需增加 `GUANYE_MODEL_GATEWAY_URL`、`GUANYE_MODEL_ID`、超时和并发限制。Windows 侧的 `GUANYE_API_BASE` 指向反向代理的 HTTPS 地址，`GUANYE_WORKER_TOKEN` 必须与容器环境一致。

### staging 命令

从仓库根目录执行。需要支持 `env_file.required` 的 Docker Compose 2.24+，并使用 BuildKit：

```bash
docker compose -f cloud/compose.yml config --quiet
docker compose -f cloud/compose.yml up -d --build
docker compose -f cloud/compose.yml ps
curl --fail --silent --show-error http://127.0.0.1:8787/healthz
docker compose -f cloud/compose.yml logs --tail=100 gateway review-worker
```

### 公共镜像站

默认值保持通用流程，并且与 `cloud/Dockerfile` 一致：

- `GUANYE_PYTHON_IMAGE=python:3.12-slim-bookworm`
- `GUANYE_PIP_INDEX_URL=https://pypi.org/simple`

Dockerfile 只允许该变量替换仓库前缀和镜像名，`@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df` 始终固定在 `FROM` 中，因此 amd64/arm64 仍使用同一 Python 多架构摘要。Docker Hub 不可达时，在仓库根目录一次性设置下列公开变量后再构建；PyPI 索引二选一：

```bash
export GUANYE_PYTHON_IMAGE='public.ecr.aws/docker/library/python:3.12-slim-bookworm'
export GUANYE_PIP_INDEX_URL='https://pypi.org/simple'
# 或：export GUANYE_PIP_INDEX_URL='https://mirrors.aliyun.com/pypi/simple'

docker compose -f cloud/compose.yml config --quiet
docker compose -f cloud/compose.yml build --pull
docker compose -f cloud/compose.yml up -d --no-build
```

Compose 通过 `build.args` 传入同名参数；`--require-hashes` 和 `cloud/gateway-requirements.txt` 中的 PyMuPDF 锁不变。两个参数只接受不带凭据的公开 HTTPS 镜像地址；不要把用户名、token、签名参数、云厂商凭据、模型/Supabase 凭据或代理凭据放进 build args。运行时凭据仍只从 `cloud/private-gateway.env` 读取，任何凭证环境都不要传给构建参数。

停止环境：

```bash
docker compose -f cloud/compose.yml down
```

在 GitHub Pages 的 staging 构建中，将 `AI_GATEWAY_URL` 设为反向代理提供的 HTTPS origin。反向代理到 `127.0.0.1:8787`；不要把容器端口发布为 `0.0.0.0`。

### 验证层级不同

1. **存活**：`GET /healthz` 只证明 gateway 进程已监听且该公开 liveness 路由可响应，不证明 Supabase 登录、权限、队列、模型或 Windows EXE 可用。
2. **账号与权限**：用独立测试 Supabase 项目的真实成员经 Pages 登录并打开 AI 工作台，或携带该成员 access token 调用受保护路由；这证明 Auth、workspace、角色与 CORS，不证明模型上游。
3. **真实模型**：使用非敏感测试内容提交一次最小真实模型请求，确认返回内容、用量与日志；这会消耗真实额度。Windows EXE 链路还应先运行 `python -B cloud/windows_image_worker.py --preflight`，再按需执行受控的 `--once --max-pages 1`。本仓库的容器配置检查不发起这类请求。

### Review worker 停机排空 smoke

GitHub Actions 在构建同一 `guanye-ai-gateway:smoke` 镜像后运行 `python -B cloud/verify_review_worker_drain.py --image guanye-ai-gateway:smoke`。脚本以镜像原 exec-form entrypoint、Compose 同款 `--init` 和 `--network none` 启动 worker，用 `docker exec` 在同一隔离网络命名空间启动固定 loopback fixture，实际执行 `docker stop`，确认两轨在释放前均已进入在途尝试、drain marker 被消费、各写回一条 completed finish RPC、退出码为 0 且停机后没有新增 claim。Windows 可用 `--local-fixture-check` 复用同一 fixture 和真实 CLI 主函数做协议检查，但该模式是进程内 SIGTERM 注入，不等于 Linux 容器/PID 1 证据。fixture 只模拟 Supabase REST/RPC、storage 和模型，不联系真实上游；Docker smoke 依赖 Linux Docker 运行时。上游语义参考 [Docker stop](https://docs.docker.com/reference/cli/docker/container/stop/)、[Moby stop tests](https://github.com/moby/moby/blob/master/integration/container/stop_test.go) 和 [Tini signal forwarding](https://github.com/krallin/tini#tini-signal-handling)。

### 并发调度参考

以下上游只用于核对调度语义，本项目不引入 Celery 或其它依赖：

- Celery `-Q` 按队列启动独立 worker 的路由示例：<https://github.com/celery/celery/blob/main/docs/userguide/routing.rst>
- Celery worker warm shutdown（TERM 完成当前任务、等待完成后退出）：<https://github.com/celery/celery/blob/main/docs/userguide/workers.rst#worker-warm-shutdown>
- Compose `stop_grace_period`（等待容器处理停止信号后发送 SIGKILL）：<https://github.com/compose-spec/compose-spec/blob/main/spec.md#stop_grace_period>
- Celery consumer 的队列消费与 pool 边界：<https://github.com/celery/celery/blob/main/celery/worker/consumer/consumer.py>
- CPython `threading.Event` / `Thread` 的唤醒、启动与 join 语义：<https://github.com/python/cpython/blob/main/Lib/threading.py>
- CPython `_thread` lock 的有界等待与释放语义：<https://github.com/python/cpython/blob/main/Modules/_threadmodule.c>

### 官方参考

- Docker Compose v2.40.3 发布：<https://github.com/docker/compose/releases/tag/v2.40.3>
- Compose `env_file` 与服务健康检查规范：<https://github.com/compose-spec/compose-spec/blob/master/05-services.md>
- Compose `build.args` 规范：<https://docs.docker.com/reference/compose-file/build/#args>
- Docker Build 的 `ARG`、`FROM` 交互与凭证边界：<https://docs.docker.com/build/building/variables/>
- Python 3.12 Slim Bookworm 官方 Dockerfile：<https://github.com/docker-library/python/blob/master/3.12/slim-bookworm/Dockerfile>
- Supabase Compose 健康检查与依赖顺序：<https://github.com/supabase/supabase/blob/master/docker/docker-compose.yml>
- FastAPI full-stack Compose 的后端进程与健康检查分离：<https://github.com/fastapi/full-stack-fastapi-template/blob/master/compose.yml>
- PyMuPDF 1.26.5 发布：<https://github.com/pymupdf/PyMuPDF/releases/tag/1.26.5>
- Debian Bookworm LibreOffice Impress：<https://packages.debian.org/bookworm/libreoffice-impress>
- Debian Bookworm Noto CJK 字体：<https://packages.debian.org/bookworm/fonts-noto-cjk>
