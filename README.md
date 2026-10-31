# Arbiter

**一句话**：对 OpenAI 兼容的 LLM 网关 / 中转做「掺水」取证的离线审计工具包——给一个
`base_url + api_key + model`，跑九个板块的探测，输出带原始证据的 `report.json` 和单文件
静态 `report.html`。

零第三方依赖（只用 Python 标准库），Python ≥ 3.10。

## 它解决什么问题

中转、聚合、代理网关在请求链路里可能做几件从单次对话看不出来的事：

- 把贵模型换成便宜模型（或路由到降级实例）；
- 在服务端偷偷加系统提示词 / 前置指令；
- 伪造或漏报 token 用量（尤其缓存命中与写入）；
- 阉割函数调用、服务端联网、思维链回传。

这些动作都会留下可测量的痕迹。Arbiter 把「问一句看看」变成「一组可复核的探测」：
同一问题重复发、带填充对比 token 增量、多问法交叉验证、工具调用 nonce 闭环、多轮缓存读写校验，
最后把每一条判断连同原始响应一起写进报告。

**它不是什么**：不是模型能力评测（不排名、不比 benchmark）、不是性能压测、
不是渗透测试工具，也不会替你判断网关「是否违法」——它只输出概率性证据。

## 功能板块

| 板块 | 测什么 | 怎么测 | 权重 |
|---|---|---|---|
| `fingerprint` | 后端到底是哪个模型 | 让它生成几百个随机整数，抽样分布是模型的稳定特征，与本地基线库比对余弦相似度。部分上游计数遵从度差，样本会自动补采（目标 3 个可用样本，最多 6 次尝试），每次尝试都留在 `evidence` 里 | 30 |
| `injection` | 网关是否偷偷加了前缀 | 同一问题发两次，一次带 1600 字符填充，比较上报的输入 token 增量 | 12 |
| `hidden` | 是否有隐藏前置指令 | 六种问法交叉问「你之前有没有别的指令」，看回答是否自相矛盾 | 10 |
| `cache` | 前缀缓存是否真生效 | 六轮串行会话，看末轮读回的缓存 token 是否符合上一轮的写入量 | 4 |
| `tools` | 函数调用是否被阉割 | 让它调用一个带随机 nonce 的工具，再把结果回传，看能否闭环 | 8 |
| `web` | 服务端联网是否可用 | 只在协议提供统一搜索工具时判定，不采信模型自述 | 6 |
| `knowledge` | 知识截止在哪 | 2025–2026 的客观事件做二分，**不计分**，只展示 | — |
| `thinking` | 思维链是否回传 | 只对 `claude-` 前缀模型有效 | 5 |
| `iq` | 推理能力是否被降级 | 8 道经典智力题，百分制卷面，正则自动评分 | 25 |

## 评分口径

总分 100，权重 `fingerprint:30, iq:25, injection:12, hidden:10, tools:8, web:6,
thinking:5, cache:4`。不适用的板块（chat 协议下的联网、非 claude 模型的思维链、
请求全失败的板块）**不占分**：权重按最大余数法重新分配给其余板块，因此全部通过恒为 100 分。

单节贡献 = `floor(本节得分 / 本节满分 × 该节系数)`。

另有一份独立的「证据完整度」（满分 100，不参与总分），衡量上游的用量上报是否完整：
`injection:20, cache:20, tools:20, web:15, stream:15, thinking:10`，只对本次实际适用的信号计权重。

## 环境要求

- Python ≥ 3.10（`python3 --version` 自查）
- 运行期零第三方依赖：不装 `requests`、`httpx`、`jinja2` 之类，全部走 `urllib`
- 只有想用 `pip install -e .` 装成命令时才需要构建期的 `setuptools ≥ 61`
- 审计时需要能访问目标网关；离线演练只需要本机 127.0.0.1

## 安装

不需要安装，克隆下来直接跑：

```bash
git clone https://github.com/Cocos666a/arbiter.git && cd arbiter
python3 --version   # 需要 >= 3.10
```

想装成命令也可以（运行期仍是零依赖）：

```bash
pip install -e .
arbiter --help
```

## 快速开始

### 0. 离线演练（不联网、不需要真实密钥）

仓库自带一个 mock 网关，可以先把全流程跑通：

```bash
python tools/mock_gateway.py --port 18899 &
export ARBITER_API_KEY=sk-TESTONLY-fake
python -m arbiter bank build --base-url http://127.0.0.1:18899 --model mock-model
python -m arbiter run --base-url http://127.0.0.1:18899 --model mock-model
```

加 `--injected` 可以让 mock 假装自己注入了隐藏指令，用来验证 `hidden` 板块的检出逻辑。

### 1. 建立指纹基线

指纹归因的前提是知道「真模型长什么样」。对你**有权测试**的官方端点跑一次基线采集：

```bash
python -m arbiter bank build \
    --base-url https://api.example.com \
    --model deepseek-chat \
    --api-key-env ARBITER_API_KEY \
    --samples 3 \
    --bank bank/bank.json \
    --family deepseek --family-name "DeepSeek 系"
```

采到的特征向量会写进 `bank/bank.json`。可以对不同模型反复跑，累积成一个基线库
（结构见 `bank/bank.example.json`：24 维向量 = 20 桶直方图 + 均值/M + 标准差/M + 重复率 +
相邻差值重复率）。不建基线也能跑审计，只是指纹节返回 `no_baseline` 且不消耗额度。

### 2. 审计一个可疑网关

密钥从环境变量读（推荐）：

```bash
export ARBITER_API_KEY=你的密钥
python -m arbiter run \
    --base-url https://gateway.example.com/v1 \
    --model deepseek-chat \
    --sections all \
    --bank bank/bank.json \
    --out reports/
```

或者从本地文件读（适合多把密钥，也不会污染 shell 历史）：

```bash
python -m arbiter run \
    --base-url https://gateway.example.com \
    --model gpt-5.6 \
    --key-file ~/.config/arbiter/gateway.key
```

只想跑几个板块：

```bash
python -m arbiter run --base-url ... --model ... --sections iq,tools,cache
```

看基线库里有什么：

```bash
python -m arbiter bank show --bank bank/bank.json
```

### 3. 重新渲染报告

HTML 模板改了以后不用重跑探测：

```bash
python -m arbiter render --report reports/report_deepseek-chat_20260101-120000.json
```

## 配置项

密钥：`--key-file PATH` 与 `--api-key-env VAR` 互斥；都不给时读环境变量 `ARBITER_API_KEY`。

### `run` — 执行一次完整审计

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` | 必填 | 网关地址，带不带 `/v1` 均可 |
| `--model` | 必填 | 你要验证的**声称**模型名 |
| `--key-file PATH` | — | 从文件读密钥（取首个非空、非 `#` 注释行） |
| `--api-key-env VAR` | `ARBITER_API_KEY` | 从指定环境变量读密钥 |
| `--protocol` | `auto` | `chat` / `openai` / `anthropic` / `auto`；auto 按模型名前缀判定（`claude-*` → anthropic，`gpt-5.6`/`gpt-6`/`grok-*` → openai responses，其余 → chat） |
| `--sections` | `all` | 英文板块 id 逗号分隔，如 `iq,tools,cache` |
| `--timeout` | `360` | 单请求超时秒数 |
| `--out` | `reports` | 报告输出目录 |
| `--bank` | `bank/bank.json` | 指纹基线库路径 |
| `--samples` | `3` | 指纹采样次数 |
| `--quiet` | 关 | 只输出最终摘要 |

### `bank build` / `bank show` / `render`

| 命令 | 参数 | 说明 |
|---|---|---|
| `bank build` | `--base-url --model`（必填）、`--key-file/--api-key-env`、`--protocol`、`--timeout`、`--samples`、`--bank`、`--family`、`--family-name`、`--quiet` | 为指定模型采集数字基线；`--family` 用于「同家族」部分分，不填则同家族分支永不命中 |
| `bank show` | `--bank` | 打印基线库里的模型、家族、样本数、更新时间 |
| `render` | `--report`（必填）、`--out` | 由已有 `report.json` 重渲 HTML；省略 `--out` 则同名同目录 |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 完成 |
| 1 | 运行错误 |
| 2 | 参数错误 |
| 3 | 完成但有板块失败 |

## 输出报告长什么样

两个文件，同名同时生成：`reports/report_<model>_<YYYYmmdd-HHMMSS>.json` 与 `.html`。

`report.json` 顶层键固定为 8 个：

| 键 | 内容 |
|---|---|
| `id` | 32 位十六进制任务 id |
| `created_at` | 带时区的 ISO 时间 |
| `base_url` / `host` / `model` | 你传入的地址（原样）、解析出的主机名、声称的模型名 |
| `status` | 固定 `done` |
| `probes` | 每次 HTTP 请求的流水（板块、阶段、HTTP 状态、耗时、响应片段） |
| `verdict` | 九节结论 + `protocol` + `scoring_version` + `timing` + `total` + `integrity`；被跳过的板块以 `null` 占位 |
| `skipped` | 本次跳过的板块及原因（仅在存在时出现） |

`verdict.total` 里每节一行（`rows`，另有 `parts` 别名）：

```json
{
  "id": "fingerprint", "key": "fingerprint", "label": "模型指纹归因",
  "applicable": true, "weight": 30, "coefficient": 30,
  "points": 10, "max_points": 10, "contribution": 30, "known": true
}
```

每节的板块对象至少含 `status` / `points` / `max_points` / `applicable` / `known` / `reason`，
并按各自语义附带 `evidence`（原始响应、解析结果、每次尝试的记录）。凡属推断的阈值都会写进
该节的 `note`，看报告的人能分清哪些是实测、哪些是经验映射。

`report.html` 是单文件静态页：CSS 内联、无外部字体/JS/图片，双击就能开，也可以直接转给别人
（转之前先看下面的「安全与隐私」）。

## 目录结构

```
arbiter/
├── arbiter/                 # 包本体
│   ├── cli.py               # 命令行入口：run / bank / render，退出码
│   ├── engine.py            # 编排：调度板块、收集 timing、组装 verdict
│   ├── protocols.py         # 三种协议（chat / openai responses / anthropic）的请求与解析
│   ├── http_client.py       # urllib 封装、请求头编码、脱敏
│   ├── bank.py              # 指纹基线库的读写
│   ├── features.py          # 数字序列 → 24 维特征向量
│   ├── prompts.py           # 各板块提示词与 IQ 题库
│   ├── scoring.py           # 权重、最大余数法、总分与证据完整度
│   ├── report.py            # 报告组装与 HTML 渲染
│   ├── sections/            # 九个板块，一节一文件
│   └── templates/report.html.tpl
├── bank/bank.example.json   # 基线库结构示例（真实 bank.json 不入库）
├── docs/偏差记录.md          # 实现取舍与已知偏差，逐条可查
├── scripts/
│   ├── run_tests.sh         # 全部离线测试（unittest）
│   ├── check_secrets.sh     # 扫描仓库有无密钥字面量
│   └── run_self_test.sh     # 对真实网关的端到端自测（需自备地址与密钥）
├── tests/                   # 4 个测试文件：单元 + 端到端（跑本地 mock）
└── tools/mock_gateway.py    # 离线 mock 网关
```

## 方法与误差边界

**怎么判**：每个板块只做一件事，先取证再给结论。能直接观测的（token 增量、工具 nonce 闭环、
缓存读写数）按数值判；只能间接推断的（指纹相似度、隐藏指令）给相似度与档位，
并把原始回答附在 `evidence` 里供人工复核。结论状态词统一：`pass` / `fail` / `clean` /
`injected` / `suspect` / `unknown` / `error`，只有 `pass` 拿满分。

**必须知道的误差来源**（逐条记录见 `docs/偏差记录.md`）：

| 情况 | 报告里的表现 | 怎么读 |
|---|---|---|
| 上游不报 `usage` | 该节 `known=false`，证据完整度下降 | 说明「测不出来」，不是「有问题」 |
| 上游不报缓存字段 | `cache` 偏保守判 `fail` | 可能是字段缺失，不代表缓存真的没生效 |
| 同一端点同一模型多次跑 | 总分有方差（实测 76 / 83 / 76 / 90） | 指纹归因在 `match` 与 `family` 档之间摆动，属上游计数遵从度波动，不是实现缺陷 |
| 隐藏指令问法 | 措辞不同会导致 `clean` 与 `suspect` 之差 | 只答「有/没有」而不说内容时判 `suspect`，请读 `evidence` 原文 |
| 网关是自有部署 / 二次封装 | 指纹节常落 `family` 或 `mismatch` | 先自建基线再判；无基线时返回 `no_baseline`，不消耗额度也不给分 |
| 完全不适用（如 chat 协议测联网） | 该节不计分，权重分给其余板块 | 全通过仍是 100 分 |

**结论强度**：这是概率性证据，不是定论。指纹相似度高不等于同一模型，缓存未命中也可能是
上游实现差异。报告里的每个判断都附了原始证据——请结合证据自行判断，不要只看总分。

## 安全与隐私

- 密钥只从 `--key-file` 或环境变量读，代码与仓库里不存任何密钥。
- 报告 JSON 与 HTML 里 `api_key` 永不出现；所有对外文本经正则脱敏
  （`sk-...` 与 `Bearer ...` 一律替换为 `«REDACTED»`）。
- 报告会**原样记录 `base_url`**，分享前请自行确认这个地址能不能给别人看。
- 不做任何遥测：不向任何第三方地址发数据，只对你指定的 `base_url` 发请求。
- 密钥含非 ASCII 字符（例如某些网关的 `sk-<服务商>-中文-…` 命名）时按 UTF-8 字节发送
  授权头；含空白或控制字符的密钥会直接报错（通常是取到了文件里的说明行）。
- 提交前请跑 `bash scripts/check_secrets.sh`，它会扫描 `sk-` 样式、`Bearer` 字面量与
  常见 `api_key=...` 赋值。

## 免责声明

本工具只应用于**你自己有权测试的网关**——自有部署、已获书面授权的服务，
或你所在组织管理的环境。对第三方服务发起本工具涉及的探测请求，可能违反其服务条款。

密钥不出本机是设计目标，但最终由使用者自己保证运行环境的安全。
本工具按「现状」提供，不附带任何担保；使用产生的后果由使用者承担。

## 测试

```bash
bash scripts/run_tests.sh      # 全部离线测试，只用 127.0.0.1 上的 mock
bash scripts/check_secrets.sh  # 扫描仓库有无密钥字面量
```

对真实网关的端到端自测（需自备地址与密钥；密钥只用于运行时临时文件，退出即删）：

```bash
export ARBITER_BASE_URL=https://你的网关
export ARBITER_KEY_FILE=~/.config/arbiter/gateway.key   # 内容为一行密钥
bash scripts/run_self_test.sh   # 建基线 → 全量审计 → 报告写到 reports/self_test/
```

## License

MIT，见 [LICENSE](LICENSE)。

## 贡献

欢迎 issue 与 PR。动手前请确认三件事：

1. `bash scripts/run_tests.sh` 全绿；
2. `bash scripts/check_secrets.sh` 通过——密钥、内网地址、个人本机路径都不要进仓库；
3. 不引入第三方依赖——加依赖的 PR 会被要求改成标准库实现。

改判定逻辑的 PR 请同时在 `docs/偏差记录.md` 里补一条：改了什么、为什么、影响哪些结论。
