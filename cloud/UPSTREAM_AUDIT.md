# 上游素材库只读审计

审计日期：2026-11-22（Asia/Shanghai）。本次只执行登录、读取和计数，没有写入、评分、修改或删除任何生产数据。

## 连接状态

- 公开页面 `https://pwmpro-a.github.io/guanye-ppt-review/` 返回的 `cloud-config.js` 指向原 Supabase 项目。
- 工作区 ID 与仓库专业版配置一致：`972acea4-4b87-4857-986c-7bee423307ff`。
- 使用现有评分师账号完成 Auth 登录；凭据未写入本文件、日志或提交。
- 旧 RPC `get_review_state` 返回成功：满分 5，评分维度 2 项。
- 旧 RPC `review_score_summary` 返回成功：有评分汇总的作品 127 套。
- `get_review_library` 返回 HTTP 404。
- 新专业版入口 `single_judge_access` 返回 HTTP 404。

## 素材库读取结果

通过 Auth 用户的 PostgREST 只读查询读取 `decks`：

- 作品：188 套。
- 含预览图的作品：188 套。
- 预览图：2,012 张。
- 原始分类/分类纠偏字段：0 套。
- 现有评分汇总覆盖：127 套。

风格字段非空的主要分布：

| 风格 | 套数 |
|---|---:|
| 科技 | 37 |
| 中国风 | 25 |
| 卷心木木 | 14 |
| 商务简约 | 8 |
| 翠色生态 | 4 |
| 碧野清新 | 4 |
| 橙光暖调 | 3 |
| 棕韵国风 | 3 |
| 翠绿科技 | 2 |
| 浅蓝科技 | 2 |

## 结论

1. 原素材库确实是现有生产数据，不是演示空库；188 套、2,012 张预览都应作为初始可信样本保留。
2. 当前生产库尚未应用本仓库的 M01–M35 迁移，至少缺少 `get_review_library` 和专业身份校验函数。
3. 本地专业版代码和 M35 final2 发布候选包不能直接宣称已上线；必须先在原项目完成 M01 → M35 迁移、Edge Functions 部署、真实模型与干净 Windows 全链路验收。
4. 因为分类字段当前为 0，第一轮 AI 任务应优先对这批原始样本生成分类预评，再由指定评分师和原有人工评分校准。

## 上线前置

已验证 YLKpro 的 Management API `/v1/organizations` 与 `/v1/projects` 均为空列表，看不到目标项目；该会话已退出。当前新的 CLI 登录会话正在等待拥有目标项目/组织权限的所有者账号授权。仍不能执行 `migration-plan`、迁移或部署。

拿到项目级访问权限后，先在目标项目执行：

```powershell
python -B cloud/manage.py migration-plan --project-ref aytnpevbqkdhwzkbhkvw
```

迁移前必须只读核对计划、备份数据库和私有文件并完成恢复演练；确认后再按 M01 → M35 顺序迁移。不要在聊天中发送访问令牌、数据库密码或 Service Role Key。

## 本地验证边界

截至 2026-11-22，最新本地与隔离验证结果：

- Python：309 项通过（含安装器专项 15 项）。
- Node/SQL：25 项通过。
- Edge Functions：66 项通过。
- 浏览器：9 个模块已纳入 workflow。
- CI：新增 `core-tests.yml` 三个 job；尚未在 GitHub 实际运行，需 PR 后验证。

上述结果只证明当前工作区、本地构建和隔离夹具通过，不代表生产迁移、Edge Function 部署、真实模型调用或干净 Windows 全链路已完成。

## 最新发布候选包（M35 final2）

- 文件：`E:/guanye-delivery-m35-final2/观页专业版-Setup.exe`
- 大小：393,927,864 bytes
- 安装包 SHA256：`5B38D00EBC496BD612DC4715B4B4DB474A540B056DA5C7364DA6718BE5384A78`
- payload SHA256：`636F68C43007D1E16FB91EF72EC7575979BBDD508E5237F5CAFD4092000A1018`
- 主 EXE SHA256：`FADBF62B8B65CB647F8D36CCB35C7A362FA9FD59A243E45A828F5AA3F987904C`
- 安装器已通过首次安装、连续升级/修复、按精确安装路径停止运行中旧进程、`.old` 无残留、切换失败回滚注入测试、卸载退出码 0 和受限 PATH 无 Python smoke；安装器 bootstrap 失败回滚与 Windows MessageBox 已纳入验收。安装器专项 15 项、Python 309 项全部通过。
- 安装包包含 HN PPT2 headless runtime、16 路生图 Worker、`single_judge_intake` Worker、观页本地服务和单账号登录壳。
- 该安装包仍是本地发布候选包：原 Supabase 生产项目仍未应用 M01–M35，也未部署 Edge Functions，等待项目所有者授权；登录入口所依赖的 `single_judge_access` 当前仍为 404；生产迁移、Edge Functions 部署、真实模型和干净 Windows 全链路验收均未完成，不能视为生产可用。
