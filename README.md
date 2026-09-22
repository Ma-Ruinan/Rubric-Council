# Rubric Generator v1.0

面向 AI Agent 测评数据集的题目级 Rubric 生成器。输入目录中的每道题由 `问题描述.txt` 和可选源素材组成；成功后仅把 `rubric.md` 写回题目目录，完整制作、审核、修订和裁决留痕保存在项目的 `.rubric-generator/runs/`。

## 输出接口

每道题的 Rubric 固定输出：

- 任务完成率：`0.00%—100.00%`
- 内容覆盖度：`0.00—5.00`
- 准确率·忠实度：`0.00—5.00`
- 格式合规度：`0.00—5.00`
- 结构完整度：`0.00—5.00`
- 幻觉／自洽性：`0.00—5.00`

它不生成单一的 0—100 综合总分。上述名称和量程固定，但每项具体原子、权重、事实锚点、状态函数和错误规则均由题目及源素材决定，不会强行套用同维度或同题型模板。

## 工作流

1. Author 对单题执行“解题式审题”，形成内部 `authoring record`。
2. Author 根据该记录生成结构化 `Rubric Spec`，不自由编排 Markdown。
3. 程序检查 ID、六组权重、状态函数、真实输入路径及 record/spec 对齐，然后确定性渲染候选 `rubric.md`。
4. Reviewer 独立核查题意、锚点、可计算性、开放答案公平性和自包含性。
5. Reviewer 已判定通过但仍有 suggestions 时，独立 Classification Gate 才复核其严重性；只有存在具体、可复现评分后果的项目才提升为 blocking。
6. 最多一轮定向修改；仍有阻断分歧时由 Arbitrator 作一次绑定裁决。
7. 通过最终静态检查后原子写入题目目录；中间文件留在项目运行目录。

Reviewer 只报告会造成错误评分或无法可靠执行的问题。措辞、排版偏好和“为了更完整”的新增要求不能成为阻断项。

最终 `rubric.md` 始终由结构化 Rubric Spec 确定性渲染，因此章节顺序、编号、六项结果接口和评分表结构保持一致。中间结构中的同义措辞或可自动规范化差异不会触发整份 Rubric 重写；权重、状态函数、来源、自包含性和可计算性仍是硬约束。

## 安装

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 OpenCode。进入项目目录后运行：

```bash
cd /d/mywork/rubric-generator-v1.0
uv sync
```

项目内置的 `opencode.jsonc` 使用 AI·AAA 的 OpenAI 兼容端点。生成器会优先保留进程环境变量，并在变量不存在时自动读取项目根目录中被 Git 忽略的 `.env`：

```bash
AIAAA_API_KEY=sk-你的密钥
```

也可以只在当前终端设置：

```bash
export AIAAA_API_KEY="sk-你的密钥"
```

五个 Agent 默认均使用 `aiaaa/deepseek-v4.1-flash#high`。每个角色可在 `rubric-generator.toml` 中分别修改模型。Agent frontmatter 的温度均为 `0.1`。

## 运行

为整个数据集生成，最多同时处理三道题：

```bash
uv run rubric-generator generate \
  --dataset "D:/path/to/测试数据集" \
  --concurrency 3
```

筛选部分题目：

```bash
uv run rubric-generator generate \
  --dataset "D:/path/to/测试数据集" \
  --task-pattern "维度一" \
  --limit 2 \
  --concurrency 2
```

默认跳过已有 `rubric.md`。确认需要重做时加 `--force`；原版本会备份到当次运行留痕中。

只读审计已有 Rubric：

```bash
uv run rubric-generator audit --dataset "D:/path/to/测试数据集"
```

审计不会修改文件。已有 Rubric 可用时直接报告 `USABLE`；只有确定的结构阻断才报告 `BLOCKING`，启发式观察只显示为 `ADVISORY`。

隔离回归不会修改参考数据集：

```bash
uv run rubric-generator regression \
  --source "D:/path/to/参考数据集" \
  --limit 1 \
  --concurrency 1
```

## 文件边界

- 数据集题目目录：原有 `问题描述.txt`、原有源素材、最终 `rubric.md`。
- `.rubric-generator/runs/<run-id>/`：输入快照、素材提取、authoring record、Rubric Spec、审核、裁决、模型事件及状态。
- `regression/runs/`：隔离回归副本与比较结果。

最终 Rubric 只能引用题目目录中真实存在且会随数据集打包的文件，或题目允许使用的实时外部来源。它不能依赖 `material-manifest.json`、authoring record、审核记录或项目内部路径。

实时或外部来源以结构化表格写入 Rubric：每条来源独立记录发布方、标题、日期/版本、支持内容、完整 HTTPS 链接、核验状态和备用证据。裸域名、占位地址、猜测 URL，以及无备用证据却支撑关键锚点的不可访问页面会被阻止。

## 离线测试

测试使用假模型响应，不调用 ClawCn，也不会产生费用：

```bash
uv run python -m unittest discover -s tests -v
```

`opencode.jsonc` 与运行产物已被 `.gitignore` 排除；可提交 `opencode.example.jsonc` 和 `rubric-generator.toml`，供其他使用者配置自己的密钥和模型。
