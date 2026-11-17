# 团队网页版部署

网页由 GitHub Pages 托管，账号、作品索引、评分和私有图片由 Supabase 保存。
每人使用自己的账号登录，并用团队邀请码加入。成员可以查看团队评分，只能修改自己的记录。
所有已加入团队的成员均可导入作品、追加页面、调整共用评分项及满分，也可导出个人评分和恢复备份。满分变化按比例换算所有人的分数；恢复备份会替换自己的评分记录，并将备份内的评分标准应用到全团队。

## 初始化

1. 创建免费 Supabase 项目，在 SQL Editor 执行 schema.sql。
2. 运行 python cloud/prepare_workspace.py --owner-email 管理员登录邮箱。
3. 执行生成的 cloud/bootstrap.sql，将 cloud/private-workspace.json 中的邀请码私下提供给同事。
4. 设置 SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY、WORKSPACE_ID 后运行 python cloud/publish_library.py，迁移现有作品图片。该操作不迁移本地个人评分。
5. GitHub 仓库设置三个 Actions Variables：SUPABASE_URL、SUPABASE_ANON_KEY、WORKSPACE_ID。前端只允许使用 publishable / anon 公钥。
6. Pages 选择 GitHub Actions，运行 Deploy review website。Supabase Auth 的 Site URL 和 Redirect URLs 使用网站完整网址。

也可使用临时 Supabase Management API 令牌进行命令行部署；保存到被 Git 忽略的 cloud/private-token.txt，禁止提交。
当前已部署的网站为 https://pwmpro-a.github.io/guanye-ppt-review/ 。本机 cloud/private-login-info.txt 保存初始管理员、独立评审账号和团队邀请码；勿提交或整体群发。
cloud/manage.py 支持 inventory、create、status、configure、upload、pages 六个步骤；create_accounts.py 提供初始账号，verify_live.py 验证线上权限并清理测试记录。
管理令牌和 service-role 密钥只在本机部署时使用，不应进入网页、GitHub 变量或构建产物。

## 使用与限制

- 在线版导入图片和 PDF，PDF 在当前浏览器转换后上传私有存储。PPT / PPTX 请先另存为 PDF；本机版可直接用 PowerPoint 转换。
- 汇总均分只统计所有评分项均已填写的成员记录，未完成记录仍显示各项分数。
- 每套作品可切换整套评分或逐页评分，两种记录分别保留。逐页模式的作品均分为所有页面、所有评分项的等权平均；全部页面完成后计入团队均分。追加页面后需补评新页面。逐页笔记、分数随 JSON 备份保存，CSV 包含每页明细。
- 修改团队评分标准后，其他已打开的页面会在保存时提示刷新，避免旧标准覆盖新标准。
- 原图访问链接有效期一小时，长时间使用后可点“刷新图片库”重新生成。
- 本机版数据在 data/，云端版数据在 Supabase。两者不会自动同步评分。
- Supabase 免费套餐有容量和流量限制，休眠项目需要恢复后才能访问。

## 验证

python test_workflows.py 验证本机作品库、评分、设置、备份、图片导入与移动布局。
python test_documents.py 验证实际 PDF / PPTX / PPT 文档转换。
安装 npm install --prefix .test-output/sql @electric-sql/pglite 后，node test_cloud_sql.cjs 验证 PostgreSQL 函数和行级权限。
