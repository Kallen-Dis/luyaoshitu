# 参与贡献

欢迎提 Issue 和 Pull Request。下面是这个项目特有的几条规矩，大多和「百度地图的日配额很紧」有关。

## 开发环境

按 [README「快速开始」](README.md#快速开始) 配好后端、前端和 `.env`。后端开发装
`backend/requirements-dev.txt`（运行依赖 + pytest + ruff）；`requirements.txt` 只放运行依赖，Docker 镜像只装它。
新增依赖写死版本号。没有 AK 也能开发：样例快照、离线模拟、导出都不调接口，后端测试全部用假客户端。

## 提交前

```bash
python scripts/verify.py                  # 密钥扫描 + ruff lint / 格式 + pytest + 前端 lint / 类型检查 / 构建
python scripts/verify.py --skip-frontend  # 只改了后端时
```

和 CI（`.github/workflows/ci.yml`）跑的是同一套检查，本机通过了再推。

## 配额规矩

- **改调用策略，先用回放实验量，不要拿真实接口反复跑。** `python scripts/benchmark_api.py`
  用本地缓存回放百度的真实回答，零配额；它的数字和真实运行逐个对得上（见 `reports/api-benchmark.md` 第 1 节）。
- 会调真实接口的脚本要持有单实例锁（`scripts/_singleton.py`），避免误开多个实例烧光当天配额。
- PR 里如果用真实接口验证过，写明花了多少批量算路点对、多少次地点检索。
- 测试不得联网：用假客户端或 `httpx.MockTransport`。

## 代码约定

- Python：ruff 负责 lint 和格式（行宽 100，配置在 `backend/ruff.toml`），类型标注写全。
  提交前在 `backend/` 下运行 `python -m ruff format app tests ../scripts`，CI 用 `ruff format --check` 把关。
- 前端：oxlint + TypeScript 类型检查（`tsconfig.app.json`）。新组件要能用键盘操作，按钮有文字或 `aria-label`。
- 注释写「为什么」，不写「做了什么」。
- **不把「没测到」写成「没有」**：接口失败、检索失败一律记「未知」，不能进盲区、不能按 0 计分。
  这是本项目最重要的一条原则，相关改动请附测试。

## 文档

- README 只放入口信息（简介、样例结果、快速开始、配置、索引）；算法与设计写进 `docs/design.md`，
  API 调用策略与接口实测写进 `docs/api-optimization.md`。
- 改了会影响数字的逻辑，同步更新 README 和 `docs/` 里引用这些数字的地方。
- `reports/` 下的报告都由脚本生成（`benchmark_api.py`、`compare_straight_line.py`、`probe_facility_entries.py`、
  `crosscheck_poi.py`），不要手改，改脚本后重跑。

## 安全

密钥只放 `.env`（已被 `.gitignore` 排除）。发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 Issue。

## 许可

提交即表示你同意以 [MIT 许可证](LICENSE) 授权你的贡献。
