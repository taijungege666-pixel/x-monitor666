# X 博主推文 → 上市公司影响分析（自动化）

每小时抓取 X（Twitter）上你关注的博主的推文，自动识别推文中提到的**上市公司**，用 a-stock-data 技能拉取对应公司行情/财务数据，生成"该推文对公司的影响分析"报告。

## 架构

```
┌───────────────────────── 云端（免费） ─────────────────────────┐
│  GitHub Actions（每小时整点）                                   │
│    monitor.py：抓 RSS → 去重 → 写入 pending.json（待分析队列） │
│    → 自动 commit 回仓库                                        │
└─────────────────────────────┬──────────────────────────────────┘
                              │ WorkBuddy 自动化每小时拉取
┌─────────────────────────────▼──────────────────────────────────┐
│  你的电脑（本地，WorkBuddy 每小时自动化任务）                   │
│    analyze_runner.py：拉取新推文 → 去重                        │
│    AI 识别推文中的上市公司                                     │
│    a-stock-data skill：拉行情/K线/财务（akshare/baostock）     │
│    生成影响分析报告 → reports/tweet_impact_*.md               │
└─────────────────────────────────────────────────────────────────┘
```

> 为什么分两层？X 的 RSS 源被国内网络屏蔽，抓取必须放 GitHub Actions（海外服务器）；而 a-stock-data 技能和 A 股数据源（东方财富/新浪）在你本地完全可达，所以分析在本地完成。两者通过 GitHub 仓库的 `pending.json` 中转。

## 项目结构

```
X博主监控推送/
├── monitor.py               # 云端抓取脚本（抓 RSS → 去重 → 写 pending.json）
├── analyze_runner.py        # 本地辅助脚本（拉取新推文 → 去重 → 标记已分析）
├── accounts.json            # 【要改】博主名单 + RSS 源实例
├── requirements.txt         # Python 依赖
├── pending.json             # 待分析队列（GitHub Actions 自动生成）
├── state.json               # 云端去重状态（GitHub Actions 自动生成）
├── analyzed.json            # 本地已分析记录（analyze_runner.py 自动生成）
├── reports/                 # 影响分析报告输出目录（自动生成）
└── .github/workflows/
    └── x-monitor.yml        # GitHub Actions 定时任务（每小时）
```

---

## 第一步：部署云端抓取（GitHub Actions，约 10 分钟）

1. GitHub 右上角 **「+」→「New repository」**，仓库名随意（如 `x-monitor`）
2. 建议选 **Public**（公共仓库 Actions 免费额度无限制）
3. 上传本文件夹内容，**注意不要上传**：`analyzed.json`、`reports/`、`sent.json`（本地生成物）
4. 编辑 `accounts.json`，填入你关注的博主 X 用户名（可留 1-5 个）
5. 进入仓库 **Actions** 页，左侧选 **X Monitor**，点 **Run workflow** 手动触发一次
6. 运行成功后仓库里应出现 `pending.json`（含抓到的推文）

## 第二步：配置本地辅助脚本（一次性）

在**你的电脑**上设置环境变量（Windows CMD）：

```cmd
set GITHUB_RAW_URL=https://raw.githubusercontent.com/你的用户名/x-monitor/main/pending.json
```

验证：

```cmd
python analyze_runner.py
```

能输出推文清单即成功。

## 第三步：自动化任务已就绪

WorkBuddy 中已创建每小时自动化任务 **「X博主推文-上市公司影响分析」**，每小时的整点后自动执行：

1. `analyze_runner.py` 拉取新推文（已分析过的自动跳过）
2. AI 识别推文中提到的 A 股上市公司
3. 加载 a-stock-data 技能，拉取实时行情、近 10 日 K 线、板块/财务数据
4. 撰写影响分析（正面/负面/中性 + 推理逻辑，每条 200-400 字）
5. 输出报告到 `reports/tweet_impact_日期_时间.md`

报告会在对话中生成，同时存为本地 MD 文件。

---

## 常见问题

| 问题 | 处理 |
|---|---|
| 运行报"推文队列为空" | 正常，说明尚未抓到新推文。确认 GitHub 仓库已部署 + Actions 跑成功 |
| 博主一个都抓不到 | 到 https://status.d420.de/ 找可用 Nitter 实例，更新 `accounts.json` 的 `nitter_instances` |
| 想改分析频率 | 在 WorkBuddy 自动化管理里改（当前每小时） |
| 想改抓取频率 | 修改 `x-monitor.yml` 的 cron（如 `*/30` = 每 30 分钟） |
| 推文提到的是港股/美股/未上市公司 | 只做备注，不拉数据（当前只分析 A 股） |

---

## 隐私与风险提示

- 本方案不需要你的 X 账号密码或 Cookie，只抓取公开推文，X 侧无封号风险
- 公共 RSS 实例（Nitter/RSSHub）可能不稳定，届时需更换实例（免费方案的唯一维护成本）
- GitHub 仓库若设为 Public，`pending.json` 中的推文内容对所有人可见（推文本就是公开的，风险可接受）；介意可设 Private 并留意 Actions 每月 2000 分钟额度

## 升级路径（可选）

- 想实现分钟级抓取：改 cron 为 `*/5`，并把自动化频率同步调高
- 免费源全部失效时：自建 RSSHub（Docker + X 账号 Cookie）或改用商业服务（RSS.app / Mp2RSS，月付约 ¥60）
