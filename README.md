# WorkBuddy 积分到期日历

把多个 WorkBuddy 账号的积分**按到期日**摊开：什么时候到期、每天会走掉多少、余额还够不够烧完，一页看完，避免积分到期作废。

**在线看**：https://zhenkunyang555.github.io/workbuddy-points-calendar/

- 首页 `index.html` —— 完整报告（账号总览、每日消耗、调度、到期日程、逐批明细、成长任务）
- [`calendar.html`](https://zhenkunyang555.github.io/workbuddy-points-calendar/calendar.html) —— 极简柱状图版：一天一根柱，看哪天砸下来最多

本仓库有两种用法：

| 用法 | 你要的东西 | 数据新鲜度 |
|---|---|---|
| **静态快照**（GitHub Pages，就是上面那个链接） | 仓库根的 `index.html` / `calendar.html` / `data.json` | 抓取那一刻的切片 |
| **自托管采集容器**（`docker compose up`） | 本仓库 + 一个 magpie 实例 | 每 15 分钟自己刷新，页面就是实时的 |

## 页面里有什么

| 板块 | 回答的问题 |
|---|---|
| 账号总览 | 每个号还剩多少分、最近哪天到期、30 天内到期多少 |
| 每日消耗 · 近 30 天 | 分花在哪天了，日均多少，有没有哪天空白 |
| 今天每个账号花了多少 | 当天各号消耗与占比（自托管时每 5 秒刷新，快照里是静态的） |
| 消耗调度 | 谁在排名位全速烧、谁当登录位、兜底开关状态 |
| 到期日程 · 每天会走掉多少 | 按天合并后的到期表，45 天内逐日列出，更远的长效包单列 |
| 逐账号批次明细 | 每个号的全部积分批次：到期时间、还剩多少、来源、是否计入 |
| 成长任务 | 徽章 / 成长计划进度，哪些动作还没做 |

## 数据从哪来

采集端（`collector/`）只读地从 magpie 那边拿三样东西：

- **逐次记账**：网关侧每次请求的扣分流水（`usage.jsonl`，增量读）
- **余额差**：定时抓的余额快照（`credits-daily.json`）
- **账号令牌**：`plugin-auth.json`（向 WorkBuddy 查批次与到期日，令牌只在本机内存里用，不落盘不出网）

每天取前两路里更大的那个：逐次记账会漏掉客户端直接花的分，余额差会漏掉账本停更之后的日子，两边都只会少算。

仓库根那三份页面是**静态快照**（2026-11-13 更新），不是实时数据：原页面的自动刷新脚本已去掉，
因为线上没有 `today.json` / `report.json`，留着只会每秒报错。

## 自托管：连 Docker 一起

```bash
git clone https://github.com/zhenkunyang555/workbuddy-points-calendar.git
cd workbuddy-points-calendar
cp .env.example .env          # 填 MAGPIE_WEB_KEY 和宿主机上 magpie 配置目录的真实路径
docker compose up -d --build
# 打开 http://<nas>:3431/report.html
```

镜像基于 `python:3.12-alpine`，**零第三方依赖**（全部标准库），构建不需要联网装包。
容器里跑的就是 `collector/run.sh` 那个循环：

| 节奏 | 脚本 | 作用 |
|---|---|---|
| 每 5 分钟 | `guard.py` / `valve.py` | 顺序守护（把路由组成员顺序按预期摆回去）+ 安全阀 |
| 每 15 分钟 | `claim.py` / `history.py` / `policy.py` / `report.py` | 领徽章、记消耗曲线、账号开关策略、**生成 `report.html`** |
| 每 1 小时 | `extra.py` | 徽章墙 / 抽奖 / 能量 |
| 每 3 小时 | `tasks.py` | 任务中心、派猫、连登兑换 |
| 每 5 秒 | `today.py` | 当天每个账号花了多少 → `today.json`（页面轮询它） |

端口 3431 后面是容器内 `python -m http.server` 托管的 `/app`：
`/report.html` 是报告页，`/report.json` 是生成它的原始数据。

**两行挂载缺一不可**（`docker-compose.yml` 里）：

```yaml
- ./data:/app                       # 状态与产物：report.json / report.html / today.json / state.json / web.key
- /path/to/magpie/config:/cfg:ro    # 只读；令牌从 /cfg/magpie/plugin-auth.json 读
```

升级：改完代码 `docker compose up -d --build`。入口脚本每次启动都会把新代码覆盖进 `/app`，
状态 json 不动，历史消耗曲线不会丢。

只要页面不要采集：把 `index.html` / `calendar.html` / `data.json` 丢到任何静态服务器即可。

## 仓库结构

| 路径 | 作用 |
|---|---|
| `index.html` | 完整报告快照（单文件、无外部依赖、轮询脚本已移除） |
| `calendar.html` | 柱状图版快照（单文件、无依赖）：一天一根柱，按账号分段堆叠 |
| `data.json` | 柱状图背后的数据：一行一天，`d` 日期、`tot` 当天到期合计、各账号分项 |
| `collector/` | 采集与报告生成脚本（Python 标准库，容器里跑的就是它） |
| `tools/make-snapshot.py` | 用一份 `report.json` / `report.html` 刷新快照三件套（数据、柱子、报告页），顺手做脱敏 |
| `tools/aliases.example.json` | 脱敏映射的示例；真实映射放 `tools/aliases.json`（已在 `.gitignore`，别提交） |
| `Dockerfile` / `docker-compose.yml` / `docker/entrypoint.sh` | 自托管采集容器 |

## 本地查看

```bash
python3 -m http.server 8080
# 浏览器打开 http://localhost:8080
```

## 更新静态快照

```bash
# 1) 从采集端拿一份最新的 report.json（自托管时：curl http://<host>:3431/report.json -o report.json）
# 2) 重算柱状图数据（默认从今天起 45 天，中间没到期的日子补 0）
python3 tools/make-snapshot.py --report report.json
# 3) 完整报告页：把同一份 http://<host>:3431/report.html 存成 index.html，
#    删掉里面轮询 today.json 的 <script> 块、补一行 viewport（线上没有 today.json，留着只会每秒报错）
```

纯前端渲染，没有构建步骤。

## 隐私

页面与快照里账号一律显示为「账号 1 / 账号 2 …」，真实昵称与打码号码只留在本地 `tools/aliases.json`（已在 `.gitignore`）。仓库不含密码、Cookie、token 或任何密钥。
**钥匙不进仓库**：`web.key` 由容器启动时从环境变量 `MAGPIE_WEB_KEY` 写入；`report.json`（含账号批次明细）、
`state.json` 等运行时文件都写进了 `.gitignore`；部署时 `.env` 也别提交。

## 许可证

MIT，见 [LICENSE](LICENSE)。换数据源、加账号筛选、做深色主题都随意，不用打招呼。

## 说明

- 柱状图颜色按账号固定，堆叠起来就是当天「总共要掉多少分」；单日超 1000 分的柱子加了描边。
- 完整报告是某个时间点的切片，页面标题里带生成时间。
