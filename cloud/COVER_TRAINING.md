# 观页封面训练试点（M35 分支）

首版只支持**单页封面**。已有素材用于评审校准；生成仍走原工作台、Windows Worker、私有存储和正式评分。没有微调，不会根据低分自动调用生图，也不改正式站。

## 正式交付标准

用户要验收的是**能在网页里完成真实工作的一版**，不是预览站，也不是待配置页面。隔离试点只是发布前验证，不是最终交付。正式交付前应在目标工作区完成以下实测：

- 真实评分账号登录后能读取既有已授权素材和逐页分，完成 Sol 独立评分、人工纠错，并在下一次评审中看到确认过的案例被使用；报告保留独立来源留出组结果和失败例。
- 真实图像渠道生成首稿；人工先评分、再看 Sol 的建议、再确认只改一版。两版都可预览，人工能对比并选用，通过内容核对后可入库。
- 有明确的发布入口、可用的评分角色、私有存储和回退步骤。不得用登录页可达、模拟流程通过或 AI 自评分代替真实验收。

## Debian HTTPS 预览入口

- 2026-11-23 发布目录：`/opt/guanye/training/releases/20261123-r1`。训练版静态站挂在 `/training/`，`/training/api/` 反向代理到独立的 `guanye-training-gateway.service`（仅监听 `127.0.0.1:8788`），原 `/api/` 不变。使用 `GUANYE_COVER_TRAINING_SITE=1` 启动该网关；仅注册训练和必要的创作接口，不注册普通提交生成和 Worker 入口。独立 venv 提供 Pillow、PyMuPDF；网页模板资料使用 PDF，当前服务器未装 LibreOffice。
- 用 `cloud/build.py` 构建静态页面，`AI_GATEWAY_URL` 必须为含端口的同源 HTTPS origin（不追加 `/training`）。构建输入只提供 Supabase URL、公开 anon key、工作区 ID、站点 origin 和训练标记；不复制服务端 env 到静态目录。打开页面后用现有评分师身份登录，从「策划 / 生图」进入「封面训练」；读取素材盘点后才允许创建策划，缺迁移时显示待配置提示。
- 当前预览沿用现有账号服务，**不打开现有工作区的训练开关**。正式试用前，配置一份独立的试点 `EnvironmentFile`（权限 `0600`），使该服务和静态构建使用同一个隔离 Supabase 项目与工作区，再执行下面的数据库启用流程。服务器有现有网关的运行凭据，但它们并非迁移管理凭据。数据库和 Edge Function 未部署前不启动试点生图 Worker。
- 冒烟：原首页和 `/healthz` 返回 `200`，试点 `/training/`、`/training/healthz` 返回 `200`，未登录的盘点请求返回 `401`。这些只验证入口、路由与登录门槛，不验证 Sol 推理和真实成图。回退时，确认站点配置未有其他改动后恢复 `/opt/guanye/training/guanye-nginx-before-20261123-r1.conf`（若有其他改动只移除训练的 `include`），执行 `nginx -t` 后重载 nginx，再停用 `guanye-training-gateway.service`。不停止原网关、评分 Worker 或 xray。

### 2026-11-23 · r2 接通隔离库

- 训练入口切到 `/opt/guanye/training/releases/20261123-r2` 与独立的 `/etc/guanye/training-gateway.env`（root `0600`）；原站、原网关与 xray 未切换。原训练配置备份在 `/opt/guanye/training/guanye-training-before-20261123-r2.conf` 和 `/opt/guanye/training/guanye-training-service-before-20261123-r2.service`。如需回退，仅恢复这两处训练文件、`systemctl daemon-reload`、重启训练网关、`nginx -t` 后重载 nginx。
- 已创建独立评分工作区、绑定评分角色并启用训练；使用与原评分账号相同的邮箱、不同的测试密码。同一 Windows 用户可运行 `./cloud/cover_pilot_auth.ps1 -Action Login`，只在本机窗口查看密码，不在终端回显。真实浏览器登录、评分角色校验和素材盘点均返回 `200`；盘点为空不是迁移或登录失败。
- 试点 `single-judge-image-worker` 已部署并配置独立 Worker 令牌。实测拒绝匿名和缺 Worker 令牌的请求；正确认证可达租约校验，随机不存在的页面被拒绝。未启动 Windows 生图 Worker，未领取任务。
- 现有线上工作区只读盘点：188 套 AI 已授权上传作品，128 条旧评分记录，0 条正式评分提交，0 条非空逐页评分记录，0 套有原始来源摘要。该工作区暂不能作为正式训练样本来源。未搬运图片或把整套分转成封面分。用户已确认历史评分就在这个云端素材库；现有整套分只用于挑选参考，未证实逐页分前不把它当作封面标签。

## 隔离启用

2026-11-23：已创建独立免费项目 `guanye-cover-training-pilot`（Project Ref：`etorcisrfcbtagnxrpau`，东京），以只读元数据确认空库后初始化基表并应用 37 个增量。只读回查为 `managed_complete`，最新版本 `20261123_39_cover_after_human`。原项目未写入；此步骤没有导入素材或创建评审账号，也没有切换预览网关。

1. 使用独立的 Supabase 项目和测试工作区，确认本机专业版配置指向它，而不是正式站。按现有迁移流程应用 `20261123_38_cover_training` 与 `20261123_39_cover_after_human`（迁移目录中第 36、37 个增量）和更新后的 `single-judge-image-worker` Edge Function。迁移不会替任何工作区打开试点。
   Windows 可在 PowerShell 7 运行 `./cloud/cover_pilot_auth.ps1 -Action Authorize`，在无回显提示中录入临时令牌。它只保存在当前 Windows 用户的 DPAPI 加密文件中；同一用户运行 `./cloud/cover_pilot_auth.ps1 -Action Plan -ProjectRef PILOT_REF` 可使用该授权进行只读预检。授权不用时运行 `-Action Clear`，并在 Supabase Dashboard 撤销令牌。

   对已有基础库，先用项目管理员的 `SUPABASE_ACCESS_TOKEN` 跑只读预检（令牌不进入网页、服务器环境或命令行参数）：

   ```powershell
   python -B cloud/manage.py migration-plan --project-ref PILOT_REF --cover-pilot --production-project-ref PRODUCTION_REF
   ```

   核对项目标识、迁移历史和待执行版本，备份后才按照预检返回的命令迁移。普通 `migrate` / `configure` 默认不纳入 M38/M39；两项目标识一致时，试点命令在联网前就会拒绝。空库须先初始化基础架构，不可只运行 M38。若预检要求人工审计，不自动接管旧迁移历史。

   部署 Edge Function 时显式指定同一个试点项目：`supabase functions deploy single-judge-image-worker --project-ref PILOT_REF`。在该项目的函数环境中配置 `GUANYE_WORKSPACE_ID` 与独立的 `GUANYE_IMAGE_WORKER_TOKEN`；不要把生图渠道密钥放进函数或 Linux 网关。原站的已安装函数不改动。评分账号需要先是试点工作区成员，再按 M34 的 `single_judge_activate_owner` 绑定；账号能够登录并不等于已具评分权限。

2. 仅在隔离库，用数据库管理账号在 `guanye_internal.cover_training_settings` 为该工作区插入 `enabled=true` 的记录。工作区须有专职评分师、`maxScore=5`，已上传作品的现行评分项须包含 `visual` 和 `layout`。正常回 loopback 本机工作台，启动前在已验证的 PowerShell 7 会话中设 `$env:GUANYE_COVER_TRAINING='1'`，沿用本项目原本的本机专业版启动入口。
3. 关闭时先将该隔离工作区的 `enabled` 设为 `false`，再移除本机变量并按原入口重启。本机旧 Worker 不会领取试点页。已领取的请求若已发出，不能用关闭开关撤回；未发出前会在租约下重查参考授权。

## 旧分校准与留出对照

1. 打开「策划 / 生图 → 策划 → 封面训练 · 素材盘点与 Sol 对照」，读完分页。仅将**现行口径、同一评分师、5 分制、明确授权、有可信原作分组摘要的逐页人工分**纳入正式对照。没有页型标签时，以第一张预览作为封面代理（结果会标注）；如果第一张不是封面，不应拿来声称封面效果。整套分只用于原作参考筛选。历史逐页分若无法证明评分口径或来源独立，只显示为排除或待确认，不重打、不静默折算。
2. **首次跑对照前复核同源。** 完全相同的原文件天然同组；不同摘要不等于不同来源。数据库管理员用隔离库的 `guanye_internal.cover_training_source_review(workspace_id,original_sha256,family_sha256)` 把已知同源但摘要不同的版本直接指向同一个代表摘要（不要建立别名链），把不能确认来源独立或封面身份的原件用 `family_sha256=NULL` 排除。首次保存对照后不再允许改归组；发现错组需用新隔离工作区重新划分、重新跑。有效分组摘要首字节对 5 取余，0 为留出，其余为校准。预算允许时，每点一次「运行 Sol 对照」启动两次独立 Sol 视觉调用。baseline 只看同一待评图和统一标准；assisted 额外看最多三个不同源的授权封面、已确认纠错和当前团队规则。两路都不传待评图人工分。对照会记录人工评分快照、两边的分与理由、实际用到的案例 ID 和团队规则版本。
3. 误放行（人工 ≤3、AI ≥4）优先，其次为至少 1 分的差异。可在校准组确认纠错，并留下原因；留出组不能确认成训练案例。负责人可以发布一份 Markdown 或恢复已经发布的旧版。发布前不要用留出图或留出评语写规则。规则或样本状态改变后，重新做同一版本的对照，不把不同版本混算。
4. 下载盘点和预测 JSON（含人工分，只留在当前授权环境），用 `python -B -m cloud.cover_training cover-inventory.json --predictions cover-predictions.json` 出报告。默认只标记为探索结果；确认试验前已复核封面、独立来源和留出隔离，才加 `--confirm-reviewed-sources` 出正式试点结论。报告使用来源组等权的视觉和排版 MAE，列误放行和样本数。完整配对且至少有 5 个独立留出来源，才可能通过首版试点阈值；这不是通用效果证明。

## 手动改稿与渠道验收

- 策划一张「封面」，确认逐页文案，然后从策划确认版选择一张已授权的高分原作首张预览。没有可用参考不要走无图降级。首稿完成后，用现有专业评分页面提交该页人工分。只有该笔评分解锁了对应 AI 预评，才可让 Sol 给「保留 / 改哪里 / 不得改」建议。检查并编辑建议后再点「按建议改一版」；确认之前不会提交生图。一次确认只出一张，最多首稿加两版。保留首稿、失败候选和已确认版本，不重用重试作为改稿。改稿是用同文案、同原参考**重新生成**，不是编辑上次的图片。
- 并排复核后，核对错漏字、内容偏离和裁切，再选用。内容匹配正式分至少 4；视觉分高不能跳过。选用后回到该候选的现有正式评分页执行入库。仅记录了盲比选项不等于完成严格盲测：做小批效果验收时，比较者不应先看版本顺序或 AI 推荐。记录首稿／改稿人工分、选择和理由。
- 先运行本地测试：`python -B -m unittest test_cover_training test_professional_creation_browser test_professional_image_client` 和 `node test_single_judge_creation_sql.cjs`。二者分别覆盖本地页面与假模型通道、真实 PostgreSQL 角色和租约逻辑（PGlite），都不会调用真实模型。
- 渠道首验需先确认**只调用 1 张**的预算，轮换曾出现在聊天的密钥。使用隔离工作区公开配置，先运行 `python -B -m cloud.professional_image_worker configure-cover-worker --config <隔离工作区配置绝对路径>` 录入试点评分账号和专用 Worker 令牌。训练 Worker 不依赖本地创作 EXE 或评审模型密钥。再运行 `python -B -m cloud.professional_image_worker configure-cover --config <隔离工作区配置绝对路径>`，从交互无回显输入新密钥。它只保存在该 Windows 用户隔离的 DPAPI 配置；不写前端、日志、仓库，不交给 Sol。默认是 `gpt-image-2.6-sunburst`；若管理员另行决定切换，可在配置时明确指定 `--cover-model gpt-image-2.5-sunburst`。
- 队列中只放一张用**合成参考图**的试点封面，确认当前没有别的试点 Worker（本机入口也有单实例锁）。运行 `python -B -m cloud.professional_image_worker once --config <隔离工作区配置绝对路径> --max-pages 1 --cover-training`，限领一张，普通 Worker 不发试点请求。要看到实际可解码成图、私有入库和浏览器预览才算通过。据渠道方提供的接入约定，服务支持纯文本生图、参考图上传和整图改版。试点仅走参考图上传，同文案和原参考重生成；发送浏览器兼容请求头及以候选页 ID 为值的幂等请求头，multipart 到 `/v1/images/edits`，只接收可解码的 `data[0].b64_json` PNG，不跟随重定向或下载远端 URL。2026-11-23 已验收参考图请求，实际输出为 1536×1024（3:2），不作为正式 PPT 的 16:9 验收。若出现 `403`、超时、不可确定的状态或不可解码响应，停在当前验收，不自动换模型或重发。读本机 Worker 状态定位，不能凭通路 200 宣称可用。

- 渠道单批最多 20 张，生成和改稿共享 20 路额度。试点每次仍只领取一张；同一 Supabase 项目内，所有试点工作区和 Worker 共用 20 个有效领取名额，且不提高现有工作区上限。不要拿同一渠道凭据在多个项目独立运行来绕过共享额度。单页上传失败只重传该页本机缓存，不重发生成；不能确认是否出图的请求不自动重试，服务端是否支持所传的幂等头不是放宽此规则的前提。

- 单张合成参考图首验通过后，另行确认完整闭环的调用预算。闭环至少需要两次成功的成图请求：首稿一张、人工确认后的改稿一张。通过渠道首验不等于通过闭环验收。

## 正式发布

隔离闭环通过后，先确认正式工作区、评分账号和获授权素材。发布前备份并记录改动前的数据库、网关、Worker 和网页版本；先发布默认关闭的变更，核对原评分与普通生成流程，再仅为目标工作区启用训练。在正式工作区用真实评分账号重验私有预览、独立评分、改稿和入库后，才向用户交付正式入口。出现异常先关闭训练和暂停训练任务领取，保留已提交的评分及候选版本，不覆盖原始记录。

### 2026-11-23 · 单张真实出图验收通过（隔离站）

- 正式云端素材库只读统计：5 分制，视觉和排版两个维度；127 套有完整历史整套分，30 套整套均分至少 4。尚未发现可直接作为封面人工标签的逐页评分。正式库未写入，未将正式库素材或评分送给模型。
- 真实评分师从网页提交锁定策划「湿地观测计划 · 单张渠道验收」，使用一张私有合成参考。用于筛选该参考的测试分不是人工评分，已排除在评审对照外。前两次提交在下单前被字段校验拦截（均未发送模型请求）；适配器修正后返回 `202`。
- Windows 封面 Worker 只领取一页，以 `gpt-image-2.6-sunburst` 请求一次，HTTP `200`，没有重发或切模型。输出为可完整解码的 1536×1024 PNG，任务状态 `succeeded`；任务 `c7695959-65f0-42b4-837c-a6e8f8e11024`，候选页 `d679fbbf-2b5b-4dcf-9ab1-65893152fcfb`，成品 SHA-256 `7ba0b46b7b63eb0d23f90aa8dab314bb79e80af3c8737dd6a0a73bc5616a8bfd`。
- 授权账号从私有对象存储取回了与任务记录哈希一致的原图，匿名读被拒。修复了训练站「生成进度」列表误请求正式 API 的路径问题（静态回退备份 `/opt/guanye/training/20261123-before-cover-history`）。重新登录后，从「策划 / 生图 → 生成进度」打开已有首稿，确认浏览器正常解码并显示「独立打分」，没有创建新任务。
- 本次生图凭据仅加载到验收进程，未新增持久化渠道凭据、未启动无人值守生图。长期配置仍须用轮换后的密钥，通过上面的 `configure-cover` 本机加密入口录入。

### 2026-11-24 · 单页训练闭环与学习验证（隔离站）

- 前端改为可操作的五步工作流：宽屏训练工作台、步骤条、单页评分自动选中唯一封面、只填一次三项分数、隐藏无关的整库补档与双队列入口；盲比选择现在必须填写理由。入口文案由「策划 / 生图」改为「封面训练」。
- 后端新增 `20261123_39_cover_after_human`：人工分确认后才创建逐页 Sol 任务，Sol 不读取人工分作为答案；晚到预测只与首次人工提交配对，后续改分保留历史。隔离 Worker 只领取封面训练逐页任务，不消费上传或旧整套队列。迁移与服务端测试通过，原正式项目未执行。
- 首稿任务 `c7695959-65f0-42b4-837c-a6e8f8e11024` / 页 `d679fbbf-2b5b-4dcf-9ab1-65893152fcfb`：人工首评视觉 4.25、排版 4.25、匹配 5；Sol 独立评分视觉 4.6、排版 4.5、匹配 5。人工复核 Sol 指出的右侧边距后改为 4.5 / 4.5 / 5，保留首次分与改分记录。
- Sol 建议只把右侧水纹缩至约 90%，保持中心、配色与文案。人工确认后生成改稿页 `06f087fc-4c9d-4542-922d-696027c39efc`，真实生图请求 HTTP 200，PNG SHA-256 `d0271b8cbdadc3675775aa6d0458bf0fb60b7e0df25ee6028ea2773f8a2e949d`。改稿新增了未要求的上下横线并移动标题，人工评分 4 / 4 / 5；Sol 给 4.6 / 4.5 / 4.7，说明模型评分不能替代人工约束核对。
- 两版通过隐藏版本顺序的盲比比较，人工选择首稿并填写理由。首稿最新正式分 4.65（93%），完成入库，素材条目 `3251321e-4c08-4c9b-b4fe-4e4e77dc6206`。
- 学习证据：盲比选择以 choice `2` 写入；新主题「潮汐带观测计划 · 学习验证」的策划上下文记录 `choice_ids=[2]`。随后发布团队风格 Markdown v1，包含安全边距、禁止额外装饰和人工选版原则。该计划生成的新主题图任务 `fabac5a7-54d2-446a-8c63-881133633af5` / 页 `2f5788ea-cf26-47e3-ac97-449d51a95834` 人工评分 4.5 / 4.5 / 5，未出现上版的额外横线，说明已选案例和规则进入了下一次策划。
- 第二张图的 Sol 评审最初三次租约尝试分别返回 429、502、429，人工分与图片完整保留。约 16 分钟冷却后，只对这一条失败任务做了一次受控重排队；第 4 次尝试成功，Sol 给视觉 4.1、排版 4.2、匹配 5，人工为 4.5、4.5、5。人工确认选版理由后完成入库，素材条目 `3a4ba754-327a-4536-9501-709e789f6e12`。
- 前端第二轮继续优化为可见的五步阶段条、明确“下一步”提示、主操作按钮高亮、选中态、3:2 大预览，并把侧栏入口统一为「封面训练」。截图为 `guanye-training-ui-final-v2.png`。独立样本的统计改善仍需至少 5 个独立来源的留出组人工分，当前证据只能证明单页闭环和学习上下文确实生效。

**当前结论：** 隔离站已经能完成真实生成、人工先评、Sol 评审、人工纠错、确认改稿、两版盲比和入库；已确认的选择与风格规则会被后续策划读取。还不能宣称评审准确率或生成质量已普遍提升；正式站、批量和自动改稿仍保持关闭。

### 2026-11-24 · 素材库视觉参考（隔离站 r8）

- 训练站已切到 `/opt/guanye/training/releases/20261124-r8`，原站、原网关和 xray 未切换。r8 修复了浏览器读取 `cover-references` 的 GET 白名单、策划请求遗漏 `reference_deck_id`、`selectedCoverReference` 作用域错误，以及重新打开草稿时丢失 `coverStyleEvidence` 的问题。
- 选页后会先显示真实私有原图，再提交策划；服务端把同一张图交给 Sol 读图，并锁定 `deckId/path/sha256`。若主策划 JSON 缺少完整 `coverStyleEvidence`，服务端会用同一张图补做一次只返回视觉依据的受控分析；两次都不完整才失败。
- 已在隔离库打开既有草稿「潮汐带生态监测｜克制型科学观测封面」，页面重新显示实际学习的素材原图、SHA-256 和 Sol 记录的字体、构图、留白依据。截图：`E:/guanye-ppt-review/.test-output/guanye-training-cover-visual-r8.png`，SHA-256 `B8E2DB1B01E3D1757CF9242FFA0AF30BE3399BD6D9AAB3A699FD758B25E01CA0`。
- 本轮还修正了 hosted 浏览器夹具对 `/training/api/` 的路径模拟，并补充了参考图加载、策划请求携带参考 ID、草稿重开保留视觉证据的回归测试。
- 模型通道根因已定位并处理：`/etc/guanye/training-gateway.env` 原先含 4 处 CRLF，模型 URL 和凭据尾部携带 `\r`；现已备份原文件、统一为 LF、保持 `0600`，并重启训练网关与评审 Worker。非 Python 的 `curl` 直连探针已分别验证纯文本请求 `200`，以及带 1440×960 原图的 PNG 视觉请求 `200`、耗时约 117 秒、返回完整 `coverStyleEvidence`。
- 画图/长文本策划超过原先 180 秒模型超时。训练环境已设置 `GUANYE_MODEL_TIMEOUT_SECONDS=420`；随后从网页重新选择素材库原图并完成一次真实策划，页面显示「本次实际学习的素材库原图」、SHA-256 和 Sol 的字体/构图/留白依据。新策划标题为「潮汐带生态监测｜克制型海岸观测封面」。截图：`E:/guanye-ppt-review/.test-output/guanye-training-cover-visual-new-r8.png`，SHA-256 `A3A58E09E98D307A7F6C1932BAE7F05E4E97F26E101308F2887CD54766061374`。
- 这一步验证了真实读图策划；尚未在本轮发起新的计费生图，正式站仍未切换。

## Node-only operations

The migration, function deployment, and formal-library import paths can run with Node.js only; no Python is required.

```powershell
# Apply one migration through the Supabase Management API
$env:SUPABASE_ACCESS_TOKEN = '<management token>'
$env:PROJECT_REF = '<target project ref>'
$env:MIGRATION_FILE = 'cloud/migrations/20261125_42_ppt_training_scope.sql'
node cloud/apply_migration.mjs

# Deploy one Edge Function
$env:FUNCTION_SLUG = 'single-judge-import'
$env:FUNCTION_DIR = 'supabase/functions/single-judge-import'
node cloud/deploy_function.mjs

# Import the scored formal library (source may be read-only via service role)
$env:SOURCE_SERVICE_ROLE_KEY = '<source service role key>'
node cloud/import_formal_library.mjs
```

`SOURCE_SERVICE_ROLE_KEY` replaces `SOURCE_ANON_KEY`, `SOURCE_EMAIL`, and `SOURCE_PASSWORD` for read-only source access. The target still requires a workspace member account (`TARGET_EMAIL` / `TARGET_PASSWORD`) because the import function validates a user JWT. Secrets must stay in process environment variables and must never be committed.