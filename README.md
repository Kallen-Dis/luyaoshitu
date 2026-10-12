# 路遥识途——基于百度地图的生活圈智能规划

[![CI](https://github.com/Kallen-Dis/luyaoshitu/actions/workflows/ci.yml/badge.svg)](https://github.com/Kallen-Dis/luyaoshitu/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

> 路虽遥，可识途。输入一个社区中心点，基于百度地图的**真实路网**计算 15 分钟步行等时圈，统计圈内民生设施，
> 逐格实测居民步行 1 公里能否走到菜市场、药店、小学，自动标注灰色区域、诊断成因、开出处方，输出可视化体检报告。

## 为什么需要真实路网

「15 分钟便民生活圈」的现有评估多用「以中心点画圆」的直线缓冲区，但道路并非直线：铁路、河道、红绿灯、施工围挡
都会压缩实际可达范围。上海市普陀区两个样例的实测：

| 样例社区 | 真实路网可达面积 | 直线画圆面积 | 真实 / 直线 | 平均绕行系数 |
| --- | --- | --- | --- | --- |
| 曹杨新村街道 | 1.413 km² | 3.66 km² | 39% | 1.31 |
| 桃浦镇 | 1.181 km² | 3.66 km² | **32%** | **2.07** |

面积只是起点，设施可达性的偏差要大得多：

| 样例社区 | 直线圆内设施 | 等时圈内设施 | 直线法判为覆盖、实测走不到的居民点（圈内 100 米格心） |
| --- | --- | --- | --- |
| 曹杨新村街道 | 70 处 | 34 处 | 小学 **12.5%**（144 格里 18 格） |
| 桃浦镇 | 10 处 | **0 处** | 小学 **100%**（17 / 17）、菜市场 73.5%、药店 60.7% |

桃浦的两种口径给出的不是精确度差异，而是相反的结论：一个说配套尚可，一个说六个品类在真实可达范围内全部缺失。
完整对比可由脚本离线生成到本地 `reports/straight-line-comparison.md`，零 API 消耗。这张表由对比脚本对直线圆和等时圈
用同一份检索结果计数，逐格比较取 100 米格心；体检报告按现行买菜规则统计（曹杨生鲜采买 8 处，对比脚本 5 处），
盲区占比按 50 米细分后的面积计，所以下文的数字和这里略有不同。

## 能做什么

- **真实路网等时圈**：36 方向 × 7 档半径扇形采样，252 个点打包成批量算路（步行每请求最多 50 个点对，冷缓存 6 次），沿射线插值出边界；
  每个方向补一条步行路线，把批量算路漏掉的红绿灯与过街等待补回来。5 / 10 / 15 分钟三圈，可切骑行、驾车（畅通 / 实时路况）。
- **设施覆盖**：六类民生设施、20 个检索关键词，多源去重、误召回清洗；小学按校门而不是地图上的一个点测距。
- **服务盲区**：15 分钟圈内每 100 米一格，逐格实测步行 1 公里能否到达菜市场、药店、小学；
  和邻格结论不同的边界格、最近一家贴近 1 公里的临界格，再切成 4 个 50 米小格分别实测（中心在圈外的小格不测、不计；
  新增点对有硬上限，放不下的小格继承父格）。占比、灰色区域和评分按面积计，子格替代父格、不重复统计。
  相邻盲区连成灰色区域，逐类区分「路网阻隔」（直线近、走不到）与「供给缺口」（附近就没有）。
- **网格交叉校核**：用另一组网格样本核对射线等时圈。圈内超时的格加测子点（默认 50 / 25 米，随网格间距缩放），
  只有确认超过 15 分钟的地方才画成斜线「缺口」，自己的步行路线和批量算路结论相反的记「存疑」不画；
  相邻两格路网距离之差远超直线距离时标「绕行线索」，并直接复测近格 → 远格；
  射线点和网格点一起做不跨绕行线索的局部线性插值，给出「校核估计面积」。射线圈仍是主结果，评分不受它影响。
- **体检评分与诊疗**：五维评分雷达；阻隔开打通处方、缺口做最大覆盖选址；备选点路网核验、模拟新建；
  打开结果就给出四段综合结论，全部由算法结果生成，同一份数据永远是同一段话。
- **AI 二次核对**：用百度地图 Agent Plan 的语义地点检索，再找一遍灰色区域附近缺的菜场、药店、小学，
  和已收录的设施逐个比对，列出疑似漏收录的；可以按补录模拟一次，或到现场确认后共享为补录标注。
- **施工围挡与共享标注**：临时围挡、复测巡检、工地线索；四类共享标注（围挡、设施失效、补录设施、灰色区域），
  管理员核实后对所有人生效，结果可在「纯算法 / 含标注」之间即时切换。
- **省配额、不撒谎**：直线下界剪枝 + 够近就停 + 分组装箱，曹杨盲区判定从 19584 个点对降到 746 个；按点对缓存，重跑零消耗。
  接口失败一律记「未知」，不伪装成盲区。
- **居民出行**：地图工具条点“出行”，找已检索设施中的最近 5 家，或按所选顺序走 2~3 站；
  可换起点、固定邻站换一家，显示步行路线、过街与围挡提示。结果写明验证、快照范围与估算口径；
  多站行程先设置再规划，地图与编号时间线同步；支持全程/单段预览，缺失路段单独补取，失败保留原行程。
  出行有独立预算，点选起点仅内存缓存，离线模式也可演示。
- **交互**：搜地址、输坐标（BD09 / GCJ02 / WGS84）或点地图自定义中心；两地对比；3D 倾斜旋转、卫星底图、全屏；
  Markdown / CSV / GeoJSON / ZIP 导出。

## 两个样例的体检结果

首屏默认打开这两份预生成快照（`data/samples/`，2026-10-10 生成），不消耗配额。

| | 曹杨新村街道 | 桃浦镇 |
| --- | --- | --- |
| 15 分钟步行圈（射线法，主结果） | 1.41 km² | 1.18 km² |
| 校核估计面积（网格交叉校核） | 1.41 km²（−0.2%） | 1.29 km²（+9.6%） |
| 射线圈里确认走不到的缺口 | 7 片，约 0.033 km² | 6 片，约 0.037 km² |
| 圈内民生设施 | 37 处，六类都有 | 0 处 |
| 缺菜市场 / 药店 / 小学（按面积） | 0% / 0% / 11.2% | 92.5% / 79.7% / 100% |
| 灰色区域 | 4 片，全是小学的路网阻隔 | 1 片覆盖全圈，以供给缺口为主 |
| 处方 | 1 条打通（西南的朝春中心小学） | 3 条补设 + 1 条打通 |
| 体检总分 | **84.4（良）** | **35.4（弱）** |

格数按 100 米格折算：50 米细分出的小格各算 1/4 格，所以会出现「12.25 格」这样的数。小格中心落在射线圈外的不测、不计
（曹杨 12 个、桃浦 5 个），和 100 米格「格心在圈内才算」是同一条规则。曹杨有 43 个格做了细分，其中 17 个圈内小格
「格心缺小学、这一角走得到」，14 个反过来；只看格心会把边界判偏。
缺口只画确认超过 15 分钟的地方：取过自己步行路线的点，路线耗时也要超过 15 分钟，和批量算路结论相反的记「存疑」不画
（曹杨 3 个点）；一个点都没取过路线的片，再取最远的一点核验。桃浦的校核估计比射线圈大，是因为射线之间用直线连接，
切掉了 41 个实测 15 分钟内走得到的点（过街等待多数是插值来的，是估计不是确认；另有 10 个点自己的路线说超时，记存疑不计）。

评分口径、灰色区域的成因判定与处方规则见本地设计文档 `docs/design.md` 第 6~8 节，网格交叉校核见第 3.6 节，
50 米边界细分见第 5.4 节（不随仓库提交）。

## 快速开始

源码托管：[`Kallen-Dis/luyaoshitu`](https://github.com/Kallen-Dis/luyaoshitu)

```bash
git clone https://github.com/Kallen-Dis/luyaoshitu.git
cd luyaoshitu
```

### 环境要求

- 64 位 Python 3.11–3.13，推荐 3.13（CI 与 Docker 镜像同版本）
- Node.js 22.18+（22.x）或 24+（前端算法测试使用原生 TypeScript 类型移除）
- Docker 与 Docker Compose 2.24+（只在一键演示时需要）
- 百度地图开放平台账号（[控制台](https://lbsyun.baidu.com/apiconsole/key)）

当前后端锁定 `pydantic==2.10.4`，不要直接使用 Python 3.14 创建项目环境；Pydantic 从 [2.12 才开始支持 Python 3.14](https://pydantic.dev/articles/pydantic-v2-12-release)。Windows 启动脚本会优先使用兼容的 `.venv`，其次寻找 Python 3.13 / 3.12 / 3.11，不会改动已有的全局 Python。

### 1. 申请两个 AK

本项目需要**两个** AK，分工不同，不能混用：

| 类型 | 用途 | 控制台配置 |
| --- | --- | --- |
| 服务端 AK | 后端调用 Web 服务 API（地理编码、坐标转换、地点检索、批量算路、路线规划） | 建议配置 IP 白名单 |
| 浏览器端 AK | 前端加载 JS API GL 渲染地图 | Referer 白名单填 `localhost/*` 及正式域名 |

> Referer 白名单的格式是 `域名/*`，**不接受端口通配符**。写 `127.0.0.1:*` 会报格式错误，
> 应写 `127.0.0.1/*` 或 `localhost/*`，端口会被自动忽略。

### 2. 配置环境变量

```bash
cp .env.example .env        # Windows PowerShell：Copy-Item .env.example .env
```

`.env` 已被 `.gitignore` 排除，**不要提交**。各项都有注释，常用的是这几项：

| 变量 | 是否必填 | 说明 |
| --- | --- | --- |
| `BAIDU_SERVER_AK` | 实时计算必填 | 服务端 AK。只在后端读取，不会下发前端 |
| `VITE_BAIDU_BROWSER_AK` | 显示地图必填 | 浏览器端 AK。由后端 `/api/config` 运行时下发，换 Key 不用重新构建 |
| `BAIDU_MAP_AUTH_TOKEN` | 可选 | 百度地图 Agent Plan 的 Token（[申请](https://lbs.baidu.com/apiconsole/agentplan)），开启「AI 二次核对」；不填则不显示这个按钮 |
| `ADMIN_TOKEN` | 可选 | 共享标注的管理员审核口令，至少 12 位；不填则审核关闭，标注只作为建议 |
| `BAIDU_MAX_QPS`、`BAIDU_MATRIX_CONCURRENCY` 等 | 通常不改 | 限速、并发、重试、缓存有效期，默认值取自实测（本地 `docs/api-optimization.md` 第 4.1 节） |
| `GRIDCHECK_MAX_PAIRS`、`GRIDCHECK_MAX_ROUTES` | 可选 | 网格交叉校核每次分析最多新增的点对与步行路线，默认 400 / 40；命中缓存不计，设为 0 即不再新测 |
| `TRIP_DAY_PAIRS`、`TRIP_HOUR_PAIRS`、`TRIP_REQUEST_PAIRS` 等 | 可选 | 出行自己的日 / 小时 / 单次预算及请求限流、下界容差，默认 500 / 120 / 120 点对，见 `.env.example` |

`.env` 可以带 BOM（Windows 记事本默认如此）、值两侧可以加引号、行尾可以写 `# 注释`。
用户标注与照片存在 `data/user/`（已 gitignore，Docker 部署时挂载为数据卷），请单独备份。

### 3. 启动

**方式一：Windows 一键安装与启动（不需要 Docker）**

下载完整源码后，双击根目录的 **`start.bat`**，自动安装所需依赖并启动前后端。启动成功后，窗口会提供两个选项：**1 停止前后端并退出窗口、0 退出窗口（服务继续运行）**。也可以在 PowerShell 中执行：

```powershell
.\start.bat
```

首次运行会检查 64 位 Python 3.11–3.13 和 Node.js 22.18+（22.x）或 24+；缺失时尝试用 Windows 的 WinGet 启动官方安装器，需要你处理安装许可或 UAC 提示。若 WinGet 不可用，脚本会给出官方下载地址，安装后再次运行即可。无需更改系统执行策略，`.bat` 只为本次 PowerShell 进程设置执行选项。

脚本随后创建项目 `.venv`、安装后端锁定依赖、用 `npm ci` 安装前端锁定依赖，并在 `.env` 不存在时从模板创建它。pip / npm 下载缓存放在项目 `.cache/launcher/`，不修改全局缓存配置；Windows 的中文依赖文件按 UTF-8 读取。**已有 `.env` 不会覆盖，密钥值不会打印。** 正常使用仍需填好上文的两个 AK；没填时可先查看样例报告与离线模拟，健康检查会明确报告功能受限。

前后端只监听本机 `127.0.0.1`，页面地址为 <http://localhost:5173>，后端文档为 <http://127.0.0.1:8000/docs>。浏览器沿用 `localhost` 访问，与现有 Referer 白名单一致。脚本等待后端、前端页面与代理健康检查全部就绪，再打开默认浏览器；不会为了启动调用百度算路或 Agent 服务。重复启动会复用本脚本已启动的进程。其他程序或手动开发终端占用 8000 / 5173 时会明确退出，不会强行关闭它们或偷偷换端口。

服务在后台运行，关闭窗口不会停止。结束使用时，在启动后的窗口选择 **1 停止前后端并退出窗口**；若已退出窗口，可以再次双击 **`start.bat`**（复用已运行的服务），再选择停止。启动和停止共用这一个入口；停止只处理本启动器记录且身份核验一致的进程，保留 `.env`、`.venv`、缓存与 `data/user/`。日志在 `.cache/launcher/`。修改 `.env` 或后端代码后，先停止再启动；前端修改由 Vite 自动刷新。

常用选项：

```powershell
.\start.bat -Check                 # 只检查环境；不安装、不写配置、不启动服务
.\start.bat -SetupOnly             # 只安装环境与依赖
.\start.bat -DevDependencies       # 安装 pytest / ruff，方便运行一键验收
.\start.bat -NoRuntimeInstall      # 不通过 WinGet 安装 Python / Node.js
.\start.bat -NoBrowser             # 启动服务但不自动打开浏览器
.\start.bat -Reinstall             # 先停止，再强制重装项目依赖
.\start.bat -Stop                  # 直接停止前后端
```

下载依赖失败时，修复网络后直接重试；不要删除 `.env` 或用户数据。该入口面向 Windows 10 / 11 与 Windows PowerShell 5.1+，macOS / Linux 使用下面的 Docker 或手动开发方式。

**方式二：Docker 一键演示**

```bash
docker compose up --build
```

浏览器打开 http://localhost:8080 。首屏读取预生成快照，不消耗配额。
没配 AK 时后端照样能启动（`.env` 缺失也可以），样例、离线模拟与导出都可用，但不会悄悄降级：
后端启动日志、`/api/health`（`status: degraded`）和页面顶部都会写明缺的是哪个 AK、影响哪些功能、怎么补；
实时计算此时返回 `503 missing_server_ak`。停止用 `docker compose down`。

> 国内网络拉不到 Docker Hub 的基础镜像（报 `failed to fetch anonymous token`）时，先从镜像站拉下来、打回原名，
> 再执行上面的命令（也可以在 Docker Desktop → Settings → Docker Engine 里配置 `registry-mirrors`）：
>
> ```bash
> for img in python:3.13-slim node:22-alpine nginx:1.27-alpine; do
>   docker pull docker.m.daocloud.io/library/$img && docker tag docker.m.daocloud.io/library/$img $img
> done
> ```
>
> 缓存是几千个小文件，Docker 在 Windows 上通过绑定挂载读 `.cache/`，比本机直接读慢：
> 2026-10-04 实测同一次全部命中缓存的实时分析，本机直跑约 17 秒，容器里约 30 秒。

**方式三：手动本地开发（Windows / macOS / Linux）**

```bash
python -m venv .venv
.venv\Scripts\activate                          # macOS / Linux：source .venv/bin/activate
pip install -r backend/requirements-dev.txt    # 只运行、不开发时装 requirements.txt 即可
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

接口文档：http://127.0.0.1:8000/docs

另开一个终端：

```bash
cd frontend
npm install
npm run dev
```

访问 http://localhost:5173 。前端通过 Vite 代理访问后端。

### 4. 一键验收

```bash
python scripts/verify.py                  # 密钥扫描 + ruff lint / 格式 + pytest + 前端 lint / 类型检查 / 构建
python scripts/verify.py --skip-frontend  # 没装 Node.js 时只查后端
```

和 GitHub Actions（`.github/workflows/ci.yml`）跑同一套检查，不联网、不花配额：后端测试全部用假客户端。
其中 `scripts/check_secrets.py` 扫描会进仓库的文件，发现写死的 AK / Token 就失败；本机有 `.env` 时还会逐个核对
里面的真实值有没有出现在仓库文件里。

### 5. 生成样例快照（会消耗配额）

```bash
python scripts/run_isochrone.py --lat 31.284817 --lng 121.369523 --id taopu --name "上海市普陀区桃浦镇"
python scripts/run_isochrone.py --lat 31.247979 --lng 121.416775 --id caoyang --name "上海市普陀区曹杨新村街道"
# 标注施工围挡（纬度,经度[,半径米]，可重复）：
python scripts/run_isochrone.py --lat 31.284817 --lng 121.369523 --id taopu-closure --closure 31.2860,121.3710,60
```

缓存为空时，等时圈、设施和 100 米盲区约 500~1000 个批量算路点对（2026-10-04 实测：桃浦 486、曹杨 982）、
36 次步行路线规划、20~45 次地点检索。2026-10-09 起再加两项：50 米边界细分（缓存为空时桃浦 178、曹杨 493 个点对，
硬上限 1500，每批请求前按缓存缺口计成本）、
网格交叉校核（每次最多 400 个点对、40 条路线，受 `GRIDCHECK_MAX_*` 限制）。不需要交叉校核时加 `--no-gridcheck`。
结果按点对缓存，同一地点重跑零消耗；每个环节实发多少写在快照的 `properties.api_usage` 里。
配额中途用完时不覆盖原快照，另存为 `.partial.geojson`（`--allow-partial` 才覆盖）。

## 配额与降级

- **批量算路是最紧的约束**：日配额按点对计，实测约 2500 个点对/日（约 140 次请求、2479 个点对时触发 `302`），
  当前步行单次请求限制按官方文档取起点数 × 终点数 ≤ 50（历史实测曾接受 100）。按个人开发者额度，一个新地点约 1000~1800 个点对
  （含 50 米细分与交叉校核），每天只够 1~2 个新地点；额度提升后按比例增加。
- 所以首屏走快照、实时计算要用户显式触发，所有结果按点对缓存，补测只请求缺的点对。
- `302` 配额耗尽不重试，该接口当天熔断、北京时间零点自动恢复；`401` 限流带随机抖动退避重试。
- 报错写清哪个服务、为什么、停在哪一步、何时恢复；测距失败的格子记「未知」，检索失败的品类记「查询失败」，都不算盲区、不按 0 计分。

细节保存在本地 `docs/design.md` 第 2 节与 `docs/api-optimization.md`。

## 文档

`docs/` 与 `reports/` 为本地资料，已加入 `.gitignore`，克隆仓库不会包含这些目录。
设计资料需另行保存；实测报告可通过相应脚本生成。仓库保留更新记录、贡献说明与安全策略。

| 文档 | 内容 |
| --- | --- |
| `docs/project-summary.md`（本地） | v2 项目总结报告：改了什么、做了哪些优化、验证情况与后续建议 |
| `docs/design.md`（本地） | **技术设计文档**：架构、API 调用策略、等时圈生成算法、POI 清洗、盲区识别、评分、处方、标注、测试 |
| `docs/trip-planner-design.md`（本地） | 居民出行：一期实现、经验下界验证、校门约束、预算、位置缓存与验收 |
| `docs/api-optimization.md`（本地） | API 调用优化与容错：批量矩阵与并发、对照实验、接口能力边界实测、答辩问答 |
| `docs/crowd-markings-design.md`（本地） | 用户共享标注的设计方案，以及灰色区域识别的后续改进方向 |
| `reports/straight-line-comparison.md`（本地生成） | 真实对比测试：直线缓冲区 vs 真实路网（曹杨、桃浦） |
| `reports/api-benchmark.md`（本地生成） | 调用策略对照实验：回放真实缓存，零配额 |
| `reports/school-gates.md`（本地生成） | 小学按校门测距与按坐标点测距的对照 |
| `reports/poi-crosscheck.md`（本地生成） | 两份样例的 AI 二次核对（Agent Plan 第二通道召回）与补录实测 |
| [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | 更新记录、参与贡献、安全策略 |

## 项目结构

```
backend/
  requirements.txt       运行依赖（Docker 镜像只装这些）
  requirements-dev.txt   开发依赖：运行依赖 + pytest + ruff
  app/
    main.py              FastAPI 入口与分析流水线（SSE 渐进推送）
    config.py            运行时配置，默认值取自实测
    baidu/               百度接口客户端：连接池、令牌桶、重试、熔断、按点对缓存、错误码三分类
    isochrone/           扇形采样 + 射线插值、过街等待校正与围挡截断、复测巡检、球面几何、
                         网格交叉校核（gridcheck.py）与受约束三角网插值（tin.py）
    poi/                 品类词表、多关键词采集与清洗、校门入口、工地候选、Agent Plan 二次核对
    report/              网格盲区判定与 50 米边界细分、细分视图（subgrid.py）、灰色区域与成因、评分、处方、核验选址、模拟新建
    markings/            共享标注：校验、SQLite 存储、信任与审核、照片去元数据、分析时叠加、接口
    trip/                居民出行：候选、固定站序 DP、全快照验证、原子预算与位置缓存
    samples.py           预生成快照仓库
    diagnostics.py       出错与降级说明：停在哪一步、哪个服务、配额何时重置
    export.py            Markdown / CSV / GeoJSON / ZIP 导出
    narrate.py           综合结论：按模板由算法结果写成四段话，零外部调用
  tests/                 pytest，全部用假客户端，不联网
frontend/src/
  App.tsx                页面与交互状态
  api.ts                 后端接口客户端（含 SSE 解析）
  baiduMap.ts            JS API GL 按需加载器
  components/            地图、体检报告、评分雷达、导出、对比
  components/map/        地图就地卡片、图层面板、图例、视角栏（3D / 旋转 / 卫星 / 全屏）
  components/markings/   共享标注：标注模式、详情与投票、照片、影响对比、审核抽屉
  components/trip/       出行抽屉、最近设施、行程、分段说明与地图联动
  lib/                   网格判定读取（含 50 米子格展开）、交叉校核图层、耗时栅格、标注工具、纯算法结果还原
scripts/
  run_isochrone.py       命令行完整分析，兼快照生成
  verify.py              一键验收（与 CI 同一套检查）
  preview_trip.py        独立 8001 假接口（前端 npm run dev:preview → 5174），零真实算路配额
  check_secrets.py       密钥扫描（CI 也跑）
  benchmark_api.py       调用优化对照实验：回放真实缓存、虚拟时钟，零配额
  compare_straight_line.py    直线圆与等时圈的对比报告，零配额
  check_coverage_offline.py   离线核查圈内计数、估算配额消耗，零配额
  crosscheck_poi.py      两份样例逐片做 AI 二次核对并按补录实测，写成核对报告
  probe_facility_entries.py   小学按校门 vs 按坐标点的对照实验
  probe_*.py             接口能力边界探测（配额、矩阵形状、网络链路、单接口、Agent Plan）
  pick_sample_area.py    两阶段样例社区选址
  _singleton.py          单实例锁，防止重复启动烧光配额
data/samples/            预生成样例快照（设施名称与坐标用于打点，不含电话与街道地址）
docs/                    本地技术设计文档（不入库）
reports/                 本地实测与对照报告（不入库）
.github/                 CI、Issue 与 PR 模板
docker-compose.yml       一键演示：后端 + Nginx 托管的前端
```

## 参与贡献与安全

提交前跑一遍 `python scripts/verify.py`。配额相关的规矩、代码约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；
安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 Issue。

## 数据来源与许可

地图数据与路径规划能力由[百度地图开放平台](https://lbsyun.baidu.com/)提供，前端保留百度地图 Logo 与版权标识。

代码以 MIT License 开源，详见 [LICENSE](LICENSE)。
