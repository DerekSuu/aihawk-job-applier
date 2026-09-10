# AIHawk Job Applier · 会话内驱动的校招投递助手

把 AI 助手（WorkBuddy 等）变成你的求职操作员：它读你的简历、筛岗位、
驱动一个反检测浏览器在 BOSS 直聘上批量发送**按岗位定制**的打招呼语。
人只在三个时刻出手：扫码登录、滑块验证、决定发不发简历。

**实测效果**（2026-09-03，单会话）：
- 宝洁中国网申表单：自动解析简历填充 + 定制招呼 → 成功提交
- 字节跳动校招表单：56 字段自动带入就绪
- BOSS 直聘：16 张岗位卡片自动过滤 → 6 条定制招呼全部发送并逐条验证通过
- 硬过滤拦下：要求 3-5 年经验的管理岗、标题带薪资的骗子岗、兼职实习岗

## 适合谁

- 2027 届及以后的应届生 / 留学生回国求职
- 用 WorkBuddy（或其他能执行命令的 AI 助手），但**不会编程**
- 想把"重复打开 30 个招聘网站填同一张表"这件事交给机器

## 三分钟上手（WorkBuddy 一键提示词）

把下面这段话**整段复制**给 WorkBuddy（前提：本仓库已克隆到本地）：

```
帮我运行求职投递助手。步骤：
1. 进入 aihawk-job-applier 目录，用 .venv/Scripts/python.exe，确认依赖已装。
2. 先读 prompts/grill-job-filters.md，就"我要投哪些岗位、哪些不投"对我做一次
   分轮追问（每轮给出你的推荐答案），谈拢后生成 filter_rules.yaml。
3. 读我放在 resume/ 目录里的简历 PDF，解析成 profile.yaml（姓名/电话/邮箱/
   身份证除外——这些我到网页里自己填），再按我的求职方向生成 greeting.txt
   招呼语模板（100 字内、含 {title} 占位符、突出简历里最对口的经历）。
4. 按下面命令启动 stealth daemon（注意 --stealth-binary 指向
   invisible-playwright 缓存里的 firefox.exe，路径见 README「引擎安装」）：
   PYTHONPATH=src .venv/Scripts/python.exe -m jobdriver.daemon \
     --profile profiles/stealth --headed --state profiles/stealth.json \
     --stealth-binary "<stealth firefox.exe 路径>"
5. 弹出浏览器后暂停，让我扫码登录 BOSS 直聘，确认登录态后继续。
6. 运行 boss_batch.py 批量打招呼：按 filter_rules.yaml 过滤、随机间隔 60-180 秒、
   每条发送后验证消息真的出现在对话流，验证不过就停；触发滑块或风控页立即暂停交给我。
```

## 它是怎么工作的

```
你的简历 PDF
   │  AI 解析（WorkBuddy 会话内完成）
   ▼
profile.yaml + greeting.txt          ← 个人数据，gitignore，不入库
   │
   ▼
┌─────────────────────────────────────────────┐
│ daemon（长驻浏览器，HTTP 单步驱动）           │
│   ├─ 普通 Firefox  → 企业官网 / 网申系统      │
│   └─ StealthFox    → BOSS 直聘等强风控站点    │  ← --stealth-binary
└─────────────────────────────────────────────┘
   │  boss_batch.py 批量循环
   ▼
搜索 → 详情核查（经验/薪资/管理岗过滤）→ 立即沟通
     → 填定制招呼语 → 发送 → 验证钩子 → 随机间隔 → 下一家
```

收工时执行一次 `clean.py --go`：删掉截图、状态文件、锁和日志
（约 4MB/次），登录态、简历、去重记录全部保留。

## 筛选规则：全部走 `filter_rules.yaml`（代码不含个人硬约束）

工具本身**不带任何个人偏好**——`reject`（一票否决）与 `require`（正向白名单）
全部由 `filter_rules.yaml` 决定，**留空即不限制**。

**生成方式**：让 AI 按 `prompts/grill-job-filters.md` 对你做一次「岗位要求 grilling」，
自动生成 `filter_rules.yaml`；也可照 `filter_rules.example.yaml` 手改。

| 字段 | 作用 |
| --- | --- |
| `reject.title_keywords` | 标题命中即跳过（岗位类型/黑名单词） |
| `reject.employer_keywords` | 雇主名/卡片命中即跳过 |
| `reject.industries` | 黑名单行业（查卡片） |
| `reject.required_majors` | JD 只列这些专业且未命中 `allowed_majors` → 跳过 |
| `reject.dev_keywords` | 技术岗标题关键词（留空=不限制） |
| `reject.phone_sales` / `part_time` / `scam_title` | 电销 / 兼职实习 / 骗子岗 开关 |
| `reject.min_years_experience` | 要求 ≥N 年经验即跳过（0 = 不启用） |
| `reject.experience_required` | JD 明写「需要经验」即跳过 |
| `require.industries` / `functions` | 行业 / 职能白名单（留空 = 不限） |
| `require.allowed_majors` | 允许专业（与 `required_majors` 配合） |
| `require.overseas` | 是否只要海外/跨境岗 |
| `salary.floor` / `salary.unknown` | 月薪下限；薪资不明时 `allow` / `reject` |

### 内置机制（与个人偏好无关）

- **发送前校对**：全部规则在点「立即沟通」前**独立复检一遍**，任一不过即放弃发送。
- **发送验证**：每条发送后校验输入框清空 + 消息进入对话流，验证不过即停。
- **风控**：发送失败后复查 security 页，命中即停；连续 N 次失败熔断（`--max-fail`）。
- **自动刷新**：列表扫完刷新搜索，直到 `--limit` 或 `--max-refresh`。
- **去重**：`greeted.json` 持久化已处理岗位。

> 关键词条目按**正则**处理，可写 `电商(?!支付|收款)` 这类断言来保留边界情况。
> 「1-3 年」岗位会点进详情确认 JD 没写死经验要求（写「优先」的放行）。

## 引擎安装（StealthFox 反检测内核）

BOSS 直聘的风控能识别普通 Playwright Firefox 的指纹——登录后一进搜索页就
被弹回首页，**人工操作也一样被弹**。解法是 invisible-playwright 的补丁版
Firefox（约 240MB）：

```bash
# 国内网络直连 GitHub CDN 会被掐断，用加速镜像 + curl 断点续传：
curl -L --retry 8 -o stealth-firefox.zip ^
  "https://ghfast.top/https://github.com/feder-cr/firefox_antidetect_patch/releases/download/firefox-26/firefox-151.0-stealth-win-x86_64.zip"

# 解压到缓存目录（路径必须精确）：
#   %LOCALAPPDATA%/invisible-playwright/invisible-playwright/Cache/
#       firefox-26_151.0_20260831121941/
# 然后用 invisible-core 写入 stamp（校验 + 登记）：
.venv/Scripts/python.exe -m invisible_core doctor   # 应显示 engine: OK
```

## 我们犯过的错（后来都修了，给你避坑）

| 错误 | 后果 | 改进 |
| --- | --- | --- |
| 以为上游 AIHawk 仓库还有批量投递代码 | 白研究了半天配置 | 动手前先拉文件树核实现状 |
| 用自主 agent 循环（25 轮不中断） | 没法"填完停住等人确认" | 改成长驻 daemon + 单步命令 |
| 普通 Firefox 上 BOSS | 风控拦截，人工也被弹回 | StealthFox 引擎（--stealth-binary） |
| 同时启动两个批量脚本 | 互相抢浏览器，招呼发了没验证 | 单进程 + 发送验证钩子 |
| 给"3-5年经验"经理岗发招呼 | 应届生身份投了无效还减分 | 硬过滤 + 详情核查 |
| 让下载走系统代理 | GitHub 502 / SSL 中途掐断 | 清代理 + 加速镜像 + curl 续传 |
| 引擎解压后直接启动 | 崩溃退出（0xC0000409） | 补写 .invisible-seal.json stamp |

## 隐私与安全

- **不入库**：登录态（profiles/）、简历、profile.yaml、greeting.txt、
  **个人筛选规则（filter_rules.yaml）**、投递记录（greeted.json / 日志）、
  任何截图——全部 gitignore。
- 浏览器窗口在投递期间保持打开；进程退出不丢登录态（cookies 已落盘）。
- **请遵守目标平台的服务条款**。自动化操作违反多数平台 ToS，有账号受限
  风险；本项目把频次（随机间隔）、人工闸门、过滤规则做进默认值，但风险
  自担。本项目仅供学习研究，勿用于大规模商业用途。

## 目录

```
boss_batch.py            BOSS 批量定制打招呼（规则走配置 + 校对 hook + 验证钩子）
filter_rules.example.yaml 筛选规则模板（复制为 filter_rules.yaml，或由 grilling 生成）
prompts/
  grill-job-filters.md    岗位要求 grilling 提示词（据此生成个人的 filter_rules.yaml）
clean.py                 进程结束后的产物清理（截图/状态/日志/锁，--go 执行）
src/jobdriver/
  daemon.py              长驻浏览器 daemon（--stealth-binary / --cdp）
  cli.py                 单步命令客户端
  condense.py            DOM → 精简字段表（上下文成本的关键）
  recon.py               岗位入口探测
tests/fixtures/          离线冒烟测试表单
```

## License

MIT
