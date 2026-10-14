# OpenCode CLI 兼容配置

[返回 README](../README.md)

本文说明本项目如何选择 OpenCode CLI 的调用模式、配置 API，以及排查调用兼容性问题。模式适配由现有 `rubric_generator/opencode_client.py` 实现，不改变 Rubric 的制作和判别规则。

## 选择调用模式

| 项目 | `legacy` 兼容模式 | `standalone` 模式 |
|---|---|---|
| 适用 CLI | 支持本项目调用参数的 OpenCode 1.x | 支持 `run --standalone` 的 OpenCode CLI |
| `OPENCODE_RUN_MODE` | `legacy` | `standalone`，也是未设置时的默认值 |
| 配置模板 | `opencode.v1.example.jsonc` | `opencode.example.jsonc` |
| 提供商配置层级 | `provider` → `npm` / `options` | `providers` → `package` / `settings` |
| `#high` 的处理 | 模型后缀转为 `--variant high` | 保留模型引用，交给对应 CLI 解析 |
| CLI 状态目录 | 隔离至项目 `.rubric-generator/opencode-legacy/` | 不设置 legacy 的隔离目录，沿用对应 CLI 的状态位置 |

先检查实际可执行文件：

```bash
opencode --version
opencode run --help
```

不要只根据版本号猜测参数和请求字段是否兼容；不同小版本及不同中转服务的支持情况可能不同。模型服务还必须能处理工具调用结果并继续返回，程序会在运行前实际检查这条链路。

## 配置兼容模式

在项目根目录执行：

```bash
cp opencode.v1.example.jsonc opencode.jsonc
```

在 `opencode.jsonc` 修改 `provider.custom.options` 内的字段：

```jsonc
"options": {
  "baseURL": "https://your-api.example/v1",
  "apiKey": "{env:API_KEY}"
}
```

在同目录的 `.env` 设置：

```dotenv
API_KEY=sk-your-api-key
OPENCODE_RUN_MODE=legacy
```

地址与密钥均须替换为实际值。`custom` 是通用的本地提供商标识，不代表具体服务商。

## 配置 standalone 模式

只有所用 CLI 支持 `run --standalone` 时才使用此模式：

```bash
cp opencode.example.jsonc opencode.jsonc
```

在 `opencode.jsonc` 修改 `providers.custom.settings`：

```jsonc
"settings": {
  "baseURL": "https://your-api.example/v1",
  "apiKey": "{env:API_KEY}"
}
```

`.env` 可显式写出模式：

```dotenv
API_KEY=sk-your-api-key
OPENCODE_RUN_MODE=standalone
```

本项目允许的模式值只有 `standalone` 和 `legacy`。配置模板与模式应成套使用；不能仅复制另一版本的 JSON 配置，而保留不匹配的模式。

## 旧配置兼容

项目启动时会兼容旧的 `AIAAA_API_KEY`：仅设置旧变量时，自动补充 `API_KEY`；仅设置新变量时，也会补充旧变量以支持旧配置。已有变量不会被覆盖；两者同时存在时，各配置使用自己引用的变量。这个兼容处理适用于通过本项目命令启动的 OpenCode 子进程。

旧配置中显式指定的 `aiaaa/...` 模型引用仍可使用。迁移提供商标识时，须同步修改 OpenCode 提供商定义、默认模型、TOML 各角色模型和 `.opencode/agents/` 角色文件中的模型引用，不要只改一处。

## 指定 OpenCode 可执行文件

`OPENCODE_BIN` 可在 `.env` 中指定，适合同时安装多个 CLI 版本，或 OpenCode 不在 PATH 的情况：

```dotenv
OPENCODE_BIN=D:/tools/opencode/opencode.exe
```

路径需指向自己机器上的真实可执行文件。Linux/macOS 可使用相应路径，例如 `/usr/local/bin/opencode`。

未设置时，程序优先在 PATH 查找 `opencode.exe`，其次查找 `opencode`。Windows 建议使用原生 `.exe`，以减少 npm 包装脚本的命令行长度和引号解析问题；程序只对已识别的包装脚本安装结构尝试寻找原生文件，不保证能自动识别所有安装布局。

兼容模式会分别将数据、状态、缓存和全局配置目录隔离到：

```text
.rubric-generator/opencode-legacy/
├── data/
├── state/
├── cache/
└── config/
```

项目根目录的 `opencode.jsonc` 仍是项目配置入口；隔离是为了避免 legacy CLI 迁移或覆盖用户其他版本 OpenCode 的会话状态。

## 模型、变体与温度

各角色在 `rubric-generator.toml` 中配置模型。更换模型时，确认以下引用一致：

1. OpenCode 配置中的提供商 ID、模型 ID 和变体定义。
2. OpenCode 配置中的默认 `model`。
3. TOML 中各角色的模型引用。

当前默认角色模型为 `custom/deepseek-v4.1-flash#high`。若服务商不支持该模型或 `high` 变体，应改成实际支持的定义，不能仅替换 URL 后继续使用不存在的模型名。

`--temperature 0` 通过本次子进程的 `OPENCODE_CONFIG_CONTENT` 覆盖角色温度，不修改共享角色文件；不传该参数时沿用角色配置。进程环境变量优先于 `.env`。若自己设置了 `OPENCODE_CONFIG_CONTENT`，应提供合法 JSON。

## 运行前检查与调用恢复

- **工具续写检查**：使用一个小型读取任务验证“模型发起工具调用 → 工具返回 → 模型继续输出”。失败时不启动题目任务，但检查本身会调用模型。
- **长提示**：超过内部阈值时以提示文件传给 CLI；程序负责附件和消息参数的分隔，不要求手工拼接长命令。
- **有限调用重试**：超时、返回不完整或输出长度上限会保留诊断；取得会话 ID 且有剩余重试次数时，复用已有会话。完整阶段结果仍须通过内容校验。
- **JSON 格式修复**：满足条件的局部语法错误交给修复角色；格式修复不等于评分规则已经正确，随后仍检查结构与一致性。
- **数据集自动恢复**：使用 README 中的 `run` 入口，保留有效检查点，只续跑未完成题目。`generate` 和 `resume` 是单轮入口，没有同样的父调度跨轮恢复。

兼容模式只改变 CLI 调用和配置适配；不会改变评分公式、职责边界或通过条件。

## 常见问题

| 现象 | 检查位置与处理 |
|---|---|
| 提示 `--standalone` 不支持 | 核对实际 CLI 的 `run --help`；使用适配的模板和 `OPENCODE_RUN_MODE` |
| 提示配置字段不识别 | 核对 `provider/options` 与 `providers/settings` 是否符合当前模式 |
| 找不到 OpenCode，或 Windows 包装脚本调用失败 | 检查 PATH，必要时设置原生可执行文件的 `OPENCODE_BIN` |
| 认证失败 | 核对 `.env` 的密钥、`apiKey` 环境变量引用，以及终端中是否已有旧值覆盖 `.env` |
| API 地址迁移、404 或请求到错误路径 | 修改本地 `opencode.jsonc` 的 `baseURL`，使用服务商当前有效 Base URL；程序不会自动猜测新地址 |
| 模型或变体不存在 | 同步修改 OpenCode 模型定义和 `rubric-generator.toml` 的引用 |
| 工具续写检查失败 | 查看当前模式、模型和服务是否支持工具调用及其后续消息；该失败不能通过跳过检查来当作成功 |
| 单次调用很久，终端仍显示等待 | 查看阶段日志与 `--timeout`；一次 Agent 调用可包含多轮模型/工具处理，超时按整次调用计算 |
| 字段不一致、补丁或审核未通过 | 查看题目 `status.json` 及 `artifacts/`；这属于内容校验，不一定是 CLI 不兼容。使用同一 `run` 让程序在预算内恢复 |

API 私密配置不进入示例文件或日志分享材料。`opencode.jsonc`、`.env`、`.rubric-generator/` 均被 Git 忽略；公开文档中的路径、地址和密钥都是占位示例。
