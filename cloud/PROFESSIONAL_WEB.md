# 观页正式网页版

用户入口是 https://pwmpro-a.github.io/guanye-ppt-review/ 。浏览器直接向 Supabase 登录、投递并保存正式分；阿里云 ECS 负责文档转换、预评、策划和图片生成。ECS 公网需要 Caddy 的 TCP 80 和 443；SSH 管理端口由实例安全组单独维护。网关只绑定 127.0.0.1:8787。生产和演练使用独立环境变量目录、Compose 项目与状态目录。

- 投递成员只能提交 PPT、PPTX 或 PDF（25 MiB）并查看本人回执，默认仅人工。管理员和评分师用各自的账号登录，可整套或逐页评分。每个评分单元以最新正式确认为准，保留作者和历史；旧团队评分不被覆盖。
- 管理员可按已有邮箱授权或停用投递成员、修改评分标准、归档或恢复作品、导出评分档案。更换分制或评分项后旧决议保留在档案里。评分档案不含私有文件，不能代替数据库和文件备份。
- ECS：同一工作区运行一个转换实例和一个预评实例；两页同时生成。每天按北京时间限制转换 30 次、预评模型请求 60 次、生图尝试 20 次。渠道超时可能已经受理，SQLite 会保留标记，重启后阻止同一页重复调用。上传失败时重用已落盘的图片。

## 部署与回退

1. 在原项目运行 python -B -m cloud.manage migration-plan。备份 PostgreSQL 和所有私有文件；单独恢复、校验数量和摘要，再安装待应用迁移。M36 只安装能力，professional_web 默认关闭。
2. 用生产项目中的独立演练工作区验证真实管理员、评分师和投递成员。核对不同网络访问、中文 PPT/PDF、个人回执、评分历史、策划、引用图生图及重启续跑。生产开启前再次确认备份与频率；定时计划应输出独立的加密快照，恢复密钥与远端存储分开保管。
3. 用 cloud/Dockerfile 构建并标记候选镜像。cloud/compose.professional.yml 从 root 专用目录读取 gateway.env、worker.env、intake.env、review.env、image.env。共享配置只包括 GUANYE_SUPABASE_URL、GUANYE_SUPABASE_ANON_KEY、GUANYE_WORKSPACE_ID 和后台评分师登录变量。预评和生图使用各自的 Edge Function worker token。网关不使用 Supabase service role key。浏览器来源设置为 GUANYE_ALLOWED_ORIGINS=https://pwmpro-a.github.io。
4. 使用供应商给出的正式 HTTPS API 入口配置 GUANYE_IMAGE_API_BASE，指定 GUANYE_IMAGE_MODEL=gpt-image-2.6-sunburst。先核对渠道授权、接口和价格，再用合成参考图验证一次。供应商返回文件地址时才配置 GUANYE_IMAGE_RESULT_ORIGINS。密钥不进入代码、日志或网页。
5. 参考 cloud/Caddyfile.public-ip.example 验证公网 IP 的 TLS、IP SAN、健康检查和 API CORS。全部验收后，通过 service role 调用 professional_web_activate，再用 GUANYE_WEB_PROFESSIONAL=1 和 HTTPS AI_GATEWAY_URL 构建并部署 GitHub Pages。现有链接不变。
6. 回退时先停正式 ECS 工作进程、恢复上一个 Pages 构建并关闭构建变量，再调用 professional_web_rollback 恢复工作区角色和并发。评分与归档记录保留。仅数据损坏时从已验证的备份恢复。

正式切换前，可在候选 PR 的干净工作树运行只读预检：

    python -B -m cloud.verify_professional_web_release --repository PWMPro-a/guanye-ppt-review --pr 3 --site https://pwmpro-a.github.io/guanye-ppt-review --gateway https://47.94.214.35 --rollback-artifact <artifact.tar> --rollback-sha256 <sha256> --backup-directory <备份目录>

预检核对当前提交的五项 GitHub 检查、旧站构建包、迁移记录、最新异地备份的恢复记录及公网 TLS。恢复记录必须对应同一份归档。主机防火墙、真实参考图生图和生产冒烟仍需单独验收。

## 备份检查

Windows 上的 `Guanye-Daily-Encrypted-Backup` 任务每天 04:00 运行。它需要备份电脑可用且当前 Windows 用户处于登录状态；关机或退出登录错过的运行在条件满足后补做。每日归档保留最近七份，本机保留切换前的独立快照，ECS 的 /var/backups/guanye-encrypted 只接收加密归档。检查本机备份目录的 daily.log 和 last-verified.json；后者仅在远端 SHA-256 一致后更新。last-restore-verification.json 记录从加密归档恢复数据库和文件的最近演练。恢复私钥单独保管，定时任务仅使用公钥加密。

## 生图受理状态核对

在镜像内运行 python -B -m cloud.ecs_worker image --pending-image-requests 查看待查页面，不会调用模型。只有供应商确认某页未受理、未计费时，先暂停 image worker，再使用 --acknowledge-unbilled-page <page-uuid> --provider-confirmed-unbilled 解锁，随后由评分师重试失败页。若本机已有待上传图片，脚本拒绝清除记录。
