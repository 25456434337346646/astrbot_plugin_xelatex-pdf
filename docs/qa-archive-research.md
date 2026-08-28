# 答疑内容归档入题库 · 研究方案

> 关联 Issue：[#2 答疑内容归档入题库](https://github.com/25456434337346646/astrbot_plugin_xelatex-pdf/issues/2)
> 分支：`research/qa-archive`
> 状态：研究方案（不含代码改动，供评审）
> 日期：2026-08-28

---

## 0. 一句话结论

题库侧的「答疑 → 归档 → 审阅 → 建题」管道**已全部就绪**，缺的只是插件侧这一环。本方案给出插件侧的最小接入设计：在现有 system prompt 上追加一段「归档要求」，让 LLM 在回答末尾顺便输出一个 `[ARCHIVE]` 结构化块（规范题干 / 答案 / 题型 / 难度 / 知识点），插件解析剥离后异步回写题库。**零额外模型调用、零新依赖、失败不影响答疑主流程。**

---

## 1. 背景与现状

### 1.1 用户在做什么

用户在 QQ 群向机器人发题（文字或拍照），本插件（`astrbot_plugin_xelatex_pdf`，v0.5.0）调用 LLM 生成 ElegantBook 排版的 PDF 解答。这些「问题 + 回答」是有价值的题目资产，希望沉淀进题库网站（question-bank-web）。

### 1.2 已核实的事实（本地 + 服务器只读核查）

**题库侧（服务器 `/opt/question-bank-web`，HEAD `3a1ec35`）管道全部就绪：**

| 接口 | 作用 | 关键字段 |
|---|---|---|
| `POST /api/oauth/token` | client_credentials 换 Bearer 令牌（Basic auth） | `grant_type=client_credentials` |
| `POST /api/open/v1/qa/requests` | 提交提问 | `external_id`（幂等键）、`question_text`、`question_images[]`、`channel`（默认 `qq_bot`）、`channel_subject`/`qq`（qq_bot 必填）、`meta`（任意 JSON） |
| `POST /api/open/v1/qa/requests/{id}/answer` | 回传答复（追加式） | `answer_text`（必填）、`analysis_text`、`source_ref`、`external_ref` |
| `POST /api/open/v1/archive/entries` | 提交归档台账 | `qa_request_id` 或 `external_id` 二选一；`draft` 覆盖白名单见下 |
| 站内审阅板 | 人工通过 → 建题 | 自动写 `questions.source = "qq_bot:{external_id}"` |

**归档 `draft` 覆盖字段白名单**（`archive/entries` 路由 `parseDraft`）：

```
stem / answer / analysis / type / difficulty / images[] / tags[]
```

- `type` ∈ `single | multiple | blank | proof | calc | answer`（六选一）
- `difficulty` ∈ `1..5` 整数
- `origin`（渠道事实）**不可**被客户端覆盖
- 幂等：重复提交一律 `200 + deduplicated:true`，**不覆盖已存在行**

**插件侧是空白**：容器内 `/AstrBot/data/plugins/astrbot_plugin_xelatex_pdf/main.py`（2234 行）grep `open/v1|qa/requests|qbk|172.17` 零命中——没有任何回写代码。文档（题库仓库 `outputs/qa-writeback-integration.md`）记载的 A2 回写并未落在此插件里。**缺的就是插件这一环。**

### 1.3 插件已有的可复用基础

- **结构化标记模式**：system prompt（`main.py:726-742`）已要求输出 `[TITLE]`/`[SUMMARY]`，解析器 `_parse_structured_answer`（`main.py:1010-1030`）按行前缀剥离。归档块完全沿用此模式。
- **最佳截获点**：`handle_event` 编译成功分支（`main.py:359-363`），此处 `question_text`、structured body、QQ 号、`base_name`、PDF 路径、`event`（含 message_id）全部齐备。
- **网络栈**：插件已用 `urllib`（`_post_chat_completion`，`main.py:779-793`），回写客户端沿用 `urllib`，不引新依赖。
- **测试基建**：`tests/test_plugin.py` 用 `unittest` + `bare_plugin()` 工厂，解析用例可直接补进去。

### 1.4 用户已拍板的三个决策

| 决策点 | 选择 |
|---|---|
| 分支位置 | **插件仓库**（题库侧 API 已就绪，改动集中在插件） |
| 产出形态 | **研究方案文档**（不动代码、不部署） |
| 提示词方案 | **单轮附加标记**（现有 system prompt 追加归档块，零额外调用） |

---

## 2. 全链路数据流

```
QQ 发题
  → 插件 generate_answer（system prompt 含归档要求）
  → LLM 返回：[TITLE] / [SUMMARY] / 正文 / [ARCHIVE 块]
  → _parse_structured_answer 扩展：剥离归档块 → 渲染 PDF（PDF 不受污染）
  → 编译成功（main.py:359）
  → 异步 fire-and-forget 回写（asyncio.create_task）：
      ① POST /api/oauth/token          （client_credentials，令牌缓存 + 401 自动续期）
      ② POST /api/open/v1/qa/requests  （external_id = event.message_id，
                                          question_text，question_images = 文件服务 URL，
                                          qq，meta = {model, base_name, pdf_url}）
      ③ POST .../qa/requests/{id}/answer（answer_text = [ANSWER]，analysis_text = body）
      ④ POST /api/open/v1/archive/entries（external_id + draft 覆盖：
                                          stem/answer/analysis/type/difficulty/tags）
  → 站内审阅板人工通过 → 入 questions 表（source = qq_bot:{msg_id}）
```

### 失败隔离三原则（沿用题库仓库 `qa-writeback-integration.md` §5 既定要求）

1. **回写失败只记日志，不影响答疑主流程**——回写是旁路，答疑成功与否不依赖它。
2. **令牌 401 自动续期**——缓存的令牌过期时重新走 client_credentials。
3. **`external_id` 用 QQ 消息 id 保幂等**——重发不产生重复台账。

---

## 3. 提示词设计（本文档核心）

### 3.1 「AI 回答时顺便加上」的信息，按可行性分四档

| 档位 | 信息 | 去向 |
|---|---|---|
| **A. 直接可收**（draft 白名单内） | 清洗后规范题干、最终答案、解析、题型（6 选 1）、难度（1-5）、知识点标签 | `[ARCHIVE]` 块 → `draft` 覆盖 |
| **B. 嵌入文本字段** | 多解法、易错点、选项文本（选择题选项写进 STEM，因白名单无 `options` 字段） | 写入 `[STEM]` / `[ANSWER]` / analysis 文本 |
| **C. 插件自带，无需用提示词要** | QQ 号、消息 id、模型名、时间戳、图片、PDF 链接 | `external_id` / `channel_subject` / `meta` |
| **D. API 不收，需题库侧扩展**（列缺口，不在本期） | `options` 独立字段、`knowledge_points` 关联、来源卷名 `title`/`year`/`region` | 审阅时人工补；缺口单列 §6 |

**核心判断**：A 档是提示词真正要「顺便产出」的部分——它们是题库建题必需、且只有 LLM 在理解题目后才能给出的结构化信息。C 档插件自己就有，不必浪费提示词预算。D 档受 API 白名单限制，本期不强求。

### 3.2 具体提示词文本（定稿）

追加在现有 system prompt（`main.py:726-742`）的「内容要求」之后：

```
归档要求（供程序自动读取）：如果你判断本次回答完整解答了一道可收录的题目
（题干条件充分、解答完整），请在正文最后空一行，输出如下归档块，块内每行一个
标记、顺序固定：
[ARCHIVE]
[STEM] 规范题干：把用户口语化提问或图片转述整理为正式题目表述，保留全部已知
条件与数学符号（行内公式用 $...$），不遗漏条件，不混入解答内容；选择题须把
选项完整写入（A. … B. …）
[ANSWER] 最终答案：一行给出（选择题写选项字母，填空题写结果，证明/论述题写"见解析"）
[TYPE] 题型：single（单选）/ multiple（多选）/ blank（填空）/ proof（证明）/
calc（计算）/ answer（解答）六选一
[DIFFICULTY] 难度：1-5 的整数（1 最易）
[TAGS] 知识点：至多 5 个标签，用英文逗号分隔，例如 极限,洛必达法则,中值定理
[/ARCHIVE]
若本次内容不是完整题目（闲聊、工具命令、绘图请求、条件不全、追问讨论），
不要输出归档块。归档块会被程序剥离、不排版进 PDF；标记格式必须严格一致。
```

**设计要点**：

- **「如果你判断…」前置条件**——让模型自己过滤非题场景（闲聊、绘图、追问），避免把无意义内容塞进待审队列。
- **`[STEM]` 要求「整理为正式表述 + 保留全部条件」**——这是最有价值的一步：把口语化/图片转述的原始提问清洗成可入库的规范题干，正是人工录题最费时的环节。
- **明确「会被程序剥离、不排版进 PDF」**——给模型一个「这些标记不影响用户看到的正文」的心智模型，降低它把归档块写进正文的倾向。

### 3.3 格式选型理由

| 方案 | 结论 | 理由 |
|---|---|---|
| **围栏式 `[ARCHIVE]…[/ARCHIVE]` 块** | ✅ 采用 | 正文若偶然出现 `[ANSWER]` 字样不会误解析；边界明确，剥离干净 |
| 裸行前缀（无围栏） | ❌ | 正文里任何一行以 `[ANSWER]` 开头都会被误吞 |
| JSON 块 | ❌ | 与现有 `[TITLE]`/`[SUMMARY]` 行解析器不同构；模型输出 JSON 更易漏引号/逗号，整体坏则全丢 |
| 二轮独立抽取调用 | ❌ | 每次答疑多一次模型调用、多一份成本；且单轮上下文最完整，二轮反而拿不到原图/原提问 |

**为什么选围栏 + 行标记的混合**：围栏保证边界，行标记保证逐行容错——单行坏只丢该字段，不会整体作废。这与现有 `[TITLE]`/`[SUMMARY]` 的行解析器同构，改造成本最低。

### 3.4 解析容错策略

| 情况 | 处理 |
|---|---|
| 无 `[ARCHIVE]` 块 | 跳过归档（非题场景，正常） |
| 块内单行缺失/非法 | 该字段回落 API 默认（`buildArchiveDraft` 本就宽容：`type→answer`、`difficulty→3`） |
| `[ARCHIVE]` 块整体 | 在 `build_tex_source` **之前**剥离，PDF 零污染 |
| `[TYPE]` 给了非法值 | 丢弃该字段，回落默认 `answer`（API 层会再校验一次） |
| `[DIFFICULTY]` 非 1-5 整数 | 丢弃，回落默认 3 |

---

## 4. 字段映射表

| AI 标记 | 回写字段 | 备注 |
|---|---|---|
| `[STEM]` | `archive.draft.stem` | 覆盖原始 `question_text` 草稿 |
| `[ANSWER]` | `answer.answer_text` + `draft.answer` | `answer_text` 必填，缺失时回落 `[SUMMARY]` |
| 正文 body | `answer.analysis_text` + `draft.analysis` | 即现有 `structured["body"]` |
| `[TYPE]` | `draft.type` | 白名单校验在 API 层 |
| `[DIFFICULTY]` | `draft.difficulty` | 1-5 整数 |
| `[TAGS]` | `draft.tags` | 逗号分隔 → 数组，至多 20（API 上限） |
| 图片附件 | `qa.question_images` / `draft.images` | 插件文件服务 URL，非 AI 产出 |
| 消息 id / QQ / 模型 / 基名 | `external_id` / `qq` / `meta` | 插件产出，不经提示词 |

---

## 5. 插件改造点（供后续实施，本期不写码）

| # | 位置 | 改动 |
|---|---|---|
| 1 | `_conf_schema.json` | 增 4 项：`qbk_base_url`（默认 `http://172.17.0.1:3000`）、`qbk_client_id`、`qbk_client_secret`（或凭据文件路径挂载）、`archive_enabled`（开关，默认关） |
| 2 | `main.py:726-742` | system prompt 追加 §3.2 归档段 |
| 3 | `_parse_structured_answer`（`main.py:1010`） | 扩展返回 `archive: dict`，并在 body 中剥离 `[ARCHIVE]` 块 |
| 4 | 新增 `QaWritebackClient` | `urllib` 实现（与现有网络栈一致，不引新依赖）：令牌缓存 + 三步 POST + 重试 |
| 5 | `handle_event`（`main.py:359` 成功分支） | `asyncio.create_task` fire-and-forget 挂钩 |
| 6 | `tests/test_plugin.py` | 补解析用例：正常块 / 缺块 / 块内坏行 / 非题跳过 / 剥离后 body 无残留 |

---

## 6. 接线与部署前置（后续实施时）

- 凭据 `/opt/bots/.qbk-open-api-client` 以**只读 volume** 挂进 astrbot 容器（600 权限，不入库）。
- `archive_enabled` 灰度开关：先关，单测 + 探针通过后再开。
- 改配置后 `docker restart astrbot`（运维手册既有纪律）。
- 网络基址用 `http://172.17.0.1:3000`（docker0 网关）；**勿用** `172.19.0.1`（那是 `bots_botnet` compose 网络网关，重建时网段会漂移）。

---

## 7. 风险与缺口

| 风险/缺口 | 说明 | 缓解 |
|---|---|---|
| **`options` 缺口** | draft 白名单无 `options` 字段 | 选择题选项暂嵌 `[STEM]`，审阅者拆分；根治需题库侧扩 draft schema（列成后续 Issue 候选） |
| **图片可达性** | `question_images` 存的是插件文件服务 URL（宿主 8765 端口），题库前端渲染时浏览器须可达 | 服务器在境外，需确认端口暴露；否则后续考虑题库侧图片上传接口（当前开放 API 无此接口，缺口） |
| **message_id 可得性** | AstrBot `MessageEvent` 的 `message_id` 需实施时实测 | 缺失时回落 `时间戳 + 题干哈希` 作幂等键 |
| **标记漂移** | 模型偶发不守格式 | 逐行容错 + 缺块跳过兜底，主流程不受影响 |
| **多轮追问** | 仅编译成功的完整答疑触发回写 | 追问轮次由幂等键（同一消息 id）天然去重 |

---

## 8. 后续实施与验证路线（仅列步骤，本期不执行）

1. **实施**：按 §5 改造点顺序落地（配置 → prompt → 解析器 → 回写客户端 → 挂钩 → 测试）。
2. **单测**：`tests/test_plugin.py` 全绿，重点覆盖 §3.4 容错矩阵。
3. **服务器接线**：凭据挂载、`archive_enabled` 开启、`docker restart astrbot`。
4. **真实答疑探针**：发一道真实题，确认答疑正常 + 后台收到回写。
5. **查库验证**：`qa_requests` / `qa_answers` / `archive_entries` 三表有对应行，`draft_json` 字段正确。
6. **审阅板目视**：待审条目展示规范题干 / 答案 / 题型 / 难度 / 标签。
7. **approve 建题**：人工通过，验证 `questions.source = qq_bot:{msg_id}`、`stem`/`answer`/`analysis` 落库正确。

---

## 9. 附：关键文件索引

| 文件 | 用途 |
|---|---|
| `main.py` | prompt（726-742）、解析器（1010-1030）、截获点（359） |
| `_conf_schema.json` | 配置项现状 |
| `tests/test_plugin.py` | 测试写法参照 |
| 题库 `src/lib/open/archive-entries.ts` | draft 契约、幂等、approve 流程 |
| 题库 `src/app/api/open/v1/archive/entries/route.ts` | draft 白名单校验 |
| 题库 `src/app/api/open/v1/qa/requests/route.ts`、`[id]/answer/route.ts` | 上报字段契约 |
| 题库 `outputs/qa-writeback-integration.md` | 插件侧三条既定要求、基址定案 |
