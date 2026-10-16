# 路遥识途

[![CI](https://github.com/Kallen-Dis/luyaoshitu/actions/workflows/ci.yml/badge.svg)](https://github.com/Kallen-Dis/luyaoshitu/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

输入社区中心点，用百度地图真实路网估计 **15 分钟步行生活圈**，逐格检查步行 **1 公里**能否到达菜场、药店和小学，再标出服务盲区、分析成因、提出改进建议。

## 主要功能

- **生活圈分析**：射线采样、过街等待校正、路线约束融合与水域裁剪；保留原射线范围作对照。
- **设施与盲区**：六类设施检索、买菜能力筛选、小学校门测距、100 米网格与局部 50 米细分。
- **体检报告**：五维评分、灰色区域、自动处方、选址核验与规划模拟。
- **居民出行**：附近设施排序、按顺序多站规划、路线展示与当前位置重规划。
- **共享标注**：围挡、设施失效、补录设施和人工灰色区域；照片、审核与版本追溯。
- **查看与导出**：样例、历史、两地对比、离线模拟，以及 Markdown、CSV、GeoJSON、JSON、ZIP 和打印 PDF。

## 设计文档

| 文档 | 内容 |
| --- | --- |
| [算法设计](docs/algorithm-design.md) | 采样、数学公式、融合求解、孔洞与水域、盲区、配额及验收 |
| [共享标注设计](docs/shared-markings.md) | 数据结构、生效规则、审核、版本、照片与隐私 |
| [居民出行设计](docs/resident-travel.md) | 最近设施、多站动态规划、通行核验、定位与预算 |
| [报告设计](docs/report-design.md) | 正式统计范围、评分公式、成因诊断、处方与导出 |

## 快速开始

### Windows

需要 **64 位 Python 3.11–3.13** 和 **Node.js 22.18+（22.x）或 24+**。

```powershell
git clone https://github.com/Kallen-Dis/luyaoshitu.git
cd luyaoshitu
Copy-Item .env.example .env
# 按下表填写 .env 后启动
.\start.bat
```

脚本检查运行环境、安装项目依赖并启动前后端；缺少运行环境时会提示安装。启动后可选 **1 停止服务并退出**，或 **0 关闭窗口、保留服务**。已有 `.env` 不会被覆盖。

- 页面：<http://localhost:5173/>；后端接口文档：<http://127.0.0.1:8000/docs>。
- 环境检查：`.\start.bat -Check`；停止服务：`.\start.bat -Stop`。
- 安装开发检查依赖：`.\start.bat -DevDependencies`。

### Docker

安装 Docker 和 Compose 2.24+，在项目目录配置 `.env` 后运行：

```bash
docker compose up --build
# 停止：docker compose down
```

页面：<http://localhost:8080/>。缓存和用户标注通过目录挂载保留。

### 配置

| 变量 | 用途 |
| --- | --- |
| `BAIDU_SERVER_AK` | 实时地理编码、设施检索、测距与路线规划 |
| `VITE_BAIDU_BROWSER_AK` | 浏览器地图底图 |
| `BAIDU_MAP_AUTH_TOKEN`（可选） | AI 二次核对设施线索 |
| `ADMIN_TOKEN`（可选，至少 12 位） | 共享标注管理员审核 |

其余预算、缓存和 OSM 水域配置见 [.env.example](.env.example)。按百度控制台要求配置各自的密钥权限；服务端密钥只留在后端。

未配置 AK 仍可启动并查看样例报告、离线模拟和导出；地图底图需要浏览器 AK，真实计算需要服务端 AK。读取历史和导出不调用百度，重新计算、出行和补测可能消耗配额。

## 开发与验证

技术栈：FastAPI / Python、React / TypeScript / Vite、百度地图 JS API GL；几何处理使用 Shapely。开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
python scripts/verify.py
```

执行密钥扫描、后端 lint / 格式 / 测试，以及前端 lint / 测试 / 类型检查 / 构建。需先安装开发依赖；自动测试使用假客户端，不消耗百度配额。

## 结果的边界

绿色范围是有限采样下的路网模型估计。基础检查通过的融合结果可作为默认范围；完整抽样核验另行标识，硬冲突则回退射线。坐标吸附、设施漏收录和水域数据缺失仍可能影响结果，不能据此保证任意地点完全准确。

接口失败与证据不足记为“未知”，不当作盲区。样例保留生成时的数据与算法版本，不会因代码更新自动重算。`.env`、`.cache/` 和 `data/user/` 不入库，用户标注及照片需单独备份。

[更新记录](CHANGELOG.md) · [安全说明](SECURITY.md) · [MIT 许可证](LICENSE)
