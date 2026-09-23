# 部署到另一台电脑

**最方便的方式就是用 GitHub 链接**：一条 `git clone` 就能拿到代码 + 本文档。

但要清楚：**仓库里只有代码和文档**，下面三样不在仓库里，需要在新电脑上补齐——

| 不在仓库里 | 原因 | 怎么补 |
| --- | --- | --- |
| 个人文件（简历、`profile.yaml`、`greeting.txt`、`filter_rules.yaml`） | 含个人信息，已 gitignore | 第 3 步，让 AI 重新生成 |
| stealth 反检测引擎（约 240MB） | 体积大，且走独立下载 | 第 4 步 |
| Python 依赖 | 原仓库未声明，现已补 `requirements.txt` | 第 2 步 |

---

## 0. 前提

- Windows 10/11；能访问 GitHub（国内需 VPN / 代理）。
- Python 3.10+（实测 3.13.14）。
- 建议由 WorkBuddy（或任意能执行命令的 AI）来跑；扫码登录、滑块验证时人工介入。

## 1. 克隆仓库

```bash
git clone https://github.com/DerekSuu/aihawk-job-applier.git
cd aihawk-job-applier
```

## 2. 建虚拟环境 + 装依赖

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -U pip
.venv/Scripts/python.exe -m pip install -r requirements.txt
# 非 stealth 路径（企业官网 / 网申表单）还需要标准 firefox：
.venv/Scripts/python.exe -m playwright install firefox
```

## 3. 生成个人文件（让 AI 做）

1. **简历 → `profile.yaml` + `greeting.txt`**
   把简历 PDF 放到 `resume/`，让 AI 解析成 `profile.yaml`（姓名/电话/邮箱/身份证除外，
   这些在网页里自己填），再按求职方向生成 `greeting.txt` 招呼语模板
   （100 字内、含 `{title}` 占位符）。
2. **岗位要求 → `filter_rules.yaml`**
   让 AI 按 `prompts/grill-job-filters.md` 对你做一次分轮追问，生成
   `filter_rules.yaml`；或复制 `filter_rules.example.yaml` 手改。

> 这两个文件都在 `.gitignore` 里，不会入库，也不会被推到 GitHub。

## 4. 装 stealth 反检测引擎（BOSS 必需）

BOSS 直聘能识别普通 Playwright Firefox 的指纹——登录后一进搜索页就被弹回首页，
**人工操作也一样被弹**。解法是 invisible-playwright 的补丁版 Firefox（约 240MB）：

```bash
# 国内直连 GitHub CDN 会被掐断，用加速镜像 + curl 断点续传：
curl -L --retry 8 -o stealth-firefox.zip ^
  "https://ghfast.top/https://github.com/feder-cr/firefox_antidetect_patch/releases/download/firefox-26/firefox-151.0-stealth-win-x86_64.zip"

# 解压到缓存目录（路径必须精确）：
#   %LOCALAPPDATA%/invisible-playwright/invisible-playwright/Cache/
#       firefox-26_151.0_20260831121941/
# 然后用 invisible-core 写入 stamp（校验 + 登记）：
.venv/Scripts/python.exe -m invisible_core doctor   # 应显示 engine: OK
```

> 引擎解压后**不能直接启动**（会崩 0xC0000409），必须补写 `.invisible-seal.json` stamp，
> 即上面 `invisible_core doctor` 这一步。

## 5. 起 daemon → 扫码登录 → 投递

```bash
# 5.1 启动长驻浏览器（--stealth-binary 指向上面解压出的 firefox.exe）
PYTHONPATH=src .venv/Scripts/python.exe -m jobdriver.daemon \
  --profile profiles/stealth --headed --state profiles/stealth.json \
  --stealth-binary "<stealth firefox.exe 路径>"

# 5.2 弹出浏览器后暂停，人工扫码登录 BOSS 直聘，确认登录态后继续

# 5.3 按规则批量打招呼
.venv/Scripts/python.exe boss_batch.py --query "商业分析" --limit 10
```

收工时跑一次 `.venv/Scripts/python.exe clean.py --go`，清理截图/状态/日志/锁
（登录态、简历、去重记录保留）。

## 常见坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 搜不到岗位 / 被弹回首页 | 用了普通 Firefox | 必须走 `--stealth-binary` 的 stealth 引擎 |
| 引擎启动即崩 0xC0000409 | 缺 `.invisible-seal.json` stamp | 跑 `python -m invisible_core doctor` |
| GitHub 下载 502 / SSL 中断 | 走了系统代理 | 清代理 + 加速镜像 + `curl` 断点续传 |
| 打招呼发了但没验证通过 | 同时跑了两个批量脚本抢浏览器 | 只跑单进程；脚本自带发送验证钩子 |
| `git push` 连不上 | GitHub 被墙 | 开 VPN / 代理后重试 |
| 提示「未找到规则文件 filter_rules.yaml」 | 第 3 步没做 | 先跑岗位要求 grilling 生成规则文件 |

## 相关文档

- `README.md`：工具整体说明、机制与隐私边界。
- `prompts/grill-job-filters.md`：生成 `filter_rules.yaml` 的追问提示词。
- `filter_rules.example.yaml`：规则模板（每个字段的作用）。
