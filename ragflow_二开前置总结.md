# RAGFlow 二次开发前置总结

> **文档性质**：基于当前仓库实际代码通读后整理的二开入门参考手册，非官方宣传材料。所有结论均标注了对应代码位置，可直接按路径定位。
>
> **代码基线**：RAGFlow **0.26.4**（见 `pyproject.toml`），`main` 分支，commit `c0582b8e1`。
>
> **编写约定**：**加粗**内容为二开必须重点关注的结论或高风险点。

---

## 一、项目整体架构

### 1.1 架构总览

RAGFlow 是一个**前后端分离 + 多进程协作**的 RAG 引擎，后端同时存在 **Python 主实现**与 **Go 并行实现**两套 API 服务（通过 nginx 代理层切换），前端为 React 单页应用：

| 层 | 技术 | 职责边界 |
|---|---|---|
| **前端** | React + TypeScript + Vite（`web/`） | 知识库管理、对话、Agent 画布编排、模型管理等全部交互界面 |
| **Python 后端（默认）** | Quart（异步 Flask）+ Peewee（`api/`） | Web API、任务调度、对话、Agent 执行、数据源同步 |
| **Go 后端（并行实现）** | gin（`cmd/` + `internal/`） | 与 Python 对等的 API/摄取/解析实现，含 CGO 原生解析库，可整体替换 Python API 层 |
| **任务执行器** | Python（`rag/svr/task_executor.py`，独立进程） | 消费 Redis 队列，执行文档解析 → 分块 → 向量化 → 入库 |
| **解析服务** | Python（`deepdoc/server/deepdoc_server.py`，独立进程） | OCR / 版面识别 / 表格结构识别等视觉模型推理 |
| **基础设施** | MySQL/PG、Redis、MinIO、ES8/Infinity/OpenSearch 等 | 业务库、队列缓存、对象存储、文档向量存储 |

**关键事实**：`docker/entrypoint.sh` 中通过 `API_PROXY_SCHEME` 环境变量在 `ragflow.conf.python` 与 `ragflow.conf.golang` 两套 nginx 配置间切换，**默认走 Python 后端**。Go 实现目前处于活跃重构期（见 `AGENTS.md`），二开**优先基于 Python 侧**，除非明确需要 Go 侧能力。

### 1.2 服务组成（Docker 部署视角）

- **ragflow 主容器**：nginx(80) + Quart API Server(**9380**) + 若干 task_executor 进程；可选 `--enable-mcpserver`(9382)、`--enable-adminserver`(**9381**)
- **deepdoc 容器**：文档解析视觉服务（镜像 `deepdoc_oss`，`docker-compose.yml` 中独立定义）
- **基础服务**（`docker/docker-compose-base.yml`）：MySQL、Redis、MinIO、Elasticsearch 8（可换 Infinity/OpenSearch）

### 1.3 核心请求链路

**链路一：文档入库（摄取）**

```
前端上传 → api/apps/restful_apis/document_api.py（写元数据 + 文件存 MinIO）
        → Redis 队列投递任务（queue_rag_flow 等）
        → rag/svr/task_executor.py 消费：
            build_chunks()  —— 按 parser_id 在 FACTORY 中选 chunker（rag/app/*.py）
            chunker 内部调用 deepdoc/parser/* 与 deepdoc_server 完成解析
            embedding()     —— LLMBundle 调向量化模型
            insert_chunks() —— 经 rag/utils/*_conn.py 写入 ES/Infinity 等
```

**链路二：检索与对话**

```
前端提问 → api/apps/restful_apis/chat_api.py / search_api.py
        → api/db/services/（会话、知识库服务）
        → rag/nlp/search.py Dealer.retrieval()：
            分词加权（rag/nlp/query.py）→ 混合检索（BM25 + 向量）
            → rerank（规则加权或 rerank 模型）→ 引用插入 insert_citations()
        → LLMBundle 调用对话模型（流式）返回前端
```

**链路三：Agent 工作流**

```
前端画布保存 DSL → canvas_service 落库
执行时 api 层构造 agent/canvas.py 的 Graph/Canvas → 按 DSL 拓扑逐个执行
agent/component/* 组件（LLM/检索/分类/循环等）→ 结果流式回传
```

**职责边界小结**：**Python 管业务编排与 RAG 全流程，Go 管高性能并行的对等实现与原生解析，前端只管展示与编排交互，三者之间只通过 HTTP API + Redis/DB 交互**。

---

## 二、核心目录与代码入口

### 2.1 根目录关键文件夹

| 目录 | 作用 | 主入口文件 |
|---|---|---|
| `api/` | **Python API 服务**（Quart）：路由、服务层、数据模型、IM 渠道 | `api/ragflow_server.py` → `api/apps/__init__.py` |
| `rag/` | **RAG 引擎核心**：分块、检索、LLM 封装、任务执行器、GraphRAG | `rag/svr/task_executor.py`、`rag/nlp/search.py` |
| `deepdoc/` | **文档解析内核**：格式解析器、视觉识别（OCR/版面/表格） | `deepdoc/parser/pdf_parser.py`、`deepdoc/server/deepdoc_server.py` |
| `agent/` | **工作流引擎**：画布 DSL 执行、23 个组件、插件体系、沙箱 | `agent/canvas.py` |
| `memory/` | 记忆模块（消息记忆服务） | `memory/services/` |
| `mcp/` | MCP 协议客户端/服务端 | `mcp/server/server.py` |
| `common/` | Python/Go 共享的常量、配置、日志、异常等基础设施 | `common/settings.py` |
| `conf/` | 运行配置：`llm_factories.json`（模型厂商）、`mapping.json`（ES 索引映射）、`service_conf.yaml` | — |
| `cmd/` | **Go 程序入口** | `cmd/ragflow_server.go`（主服务）、`cmd/ragflow-cli.go`（CLI） |
| `internal/` | **Go 应用代码**：router/handler/service/dao/ingestion/parser/agent 等 | `internal/router/router.go` |
| `web/` | **React 前端** | `web/src/main.tsx`、`web/src/routes.tsx` |
| `docker/` | 部署编排与配置模板 | `docker/docker-compose.yml`、`docker/service_conf.yaml.template` |
| `sdk/python/` | 官方 Python SDK（HTTP API 封装） | `sdk/python/ragflow_sdk/` |
| `docs/` | 官方文档源（含 `docs/develop/` 开发者文档） | — |
| `tools/` | 周边工具（migrate-canvas、es-to-oceanbase-migration 等） | — |

### 2.2 各核心模块内部入口速查

**Python API（`api/`）**
- 路由自动注册：`api/apps/__init__.py` 中 `search_pages_path()` + `register_page()` —— 自动扫描 `*_app.py` 与 `restful_apis/*.py` 注册为 Blueprint
- 新式 RESTful 接口：`api/apps/restful_apis/`（30 个 `*_api.py`，URL 前缀 `/api/v1`）
- 数据模型：`api/db/db_models.py`（**Peewee 定义全部表结构**）
- 服务层：`api/db/services/`（如 `document_service.py`、`llm_service.py` 的 `LLMBundle`、`knowledgebase_service.py`）
- IM 渠道：`api/channels/`（`core/registry.py` 注册机制，已支持钉钉/飞书/企微/Discord/Telegram/LINE/QQ/WhatsApp）

**RAG 引擎（`rag/`）**
- 任务执行：`rag/svr/task_executor.py`（`FACTORY` 字典在第 111 行，映射 parser_id → chunker 模块）
- 检索核心：`rag/nlp/search.py` 的 `Dealer` 类；查询处理 `rag/nlp/query.py`；中文分词 `rag/nlp/rag_tokenizer.py`
- 分块方法：`rag/app/*.py`（naive/table/paper/qa/book/laws/manual/resume/presentation/picture/audio/email/tag/one），统一暴露 `chunk()` 函数
- 存储适配：`rag/utils/es_conn.py`、`infinity_conn.py`、`opensearch_conn.py`、`ob_conn.py`（OceanBase）、`minio_conn.py`、`redis_conn.py`
- 模型封装：`rag/llm/`（`chat_model.py`、`embedding_model.py`、`rerank_model.py`、`cv_model.py`、`tts_model.py` 等）
- GraphRAG：`rag/graphrag/`（实体解析、检索 `search.py`）
- 新 DSL 流水线：`rag/flow/`（`pipeline.py` 的 `Pipeline(Graph)`，chunker/compiler/extractor/tokenizer 子模块）

**Go 实现（`cmd/` + `internal/`）**
- 服务入口：`cmd/ragflow_server.go`；路由注册 `internal/router/router.go`（gin）
- 分层：`internal/handler/` → `internal/service/` → `internal/dao/`
- 摄取/解析：`internal/ingestion/`、`internal/parser/`、`internal/deepdoc/`（**活跃重构区**）
- 开发环境：`internal/development.md`（CGO 原生库配置必读）

**前端（`web/`）**
- 应用入口：`web/src/main.tsx`；路由表：`web/src/routes.tsx`（`Routes` 枚举集中定义）
- 页面：`web/src/pages/`（datasets/agent/next-chats/next-search/memories/admin 等）
- 服务请求：`web/src/services/`；国际化：`web/src/locales/`；画布：基于 `@antv/x6`

---

## 三、核心模块详解

### 3.1 deepdoc 文档解析引擎

**实现逻辑**：分为「格式解析器」与「视觉识别」两层。格式解析器（`deepdoc/parser/`）负责从各格式中提取文本与结构；遇到扫描件/复杂版面时，调用视觉识别层（`deepdoc/vision/`）做 OCR、版面区域划分、表格结构还原。视觉模型推理可独立为 `deepdoc_server` 服务部署（`deepdoc/server/deepdoc_server.py`），主服务通过其 endpoints 远程调用。

**关键类与文件**：
- `deepdoc/parser/pdf_parser.py` —— PDF 解析主力（与版面识别深度耦合，**复杂度最高**）
- `deepdoc/parser/docx_parser.py`、`excel_parser.py`、`ppt_parser.py`、`html_parser.py`、`markdown_parser.py`、`txt_parser.py`、`epub_parser.py`、`json_parser.py`、`figure_parser.py` —— 各格式独立解析器
- **第三方解析引擎适配层**：`docling_parser.py`、`mineru_parser.py`、`mistral_parser.py`、`paddleocr_parser.py`、`tcadp_parser.py`、`opendataloader_parser.py`、`somark_parser.py` —— 说明官方已预留「可替换解析内核」的扩展位
- `deepdoc/vision/ocr.py`（OCR）、`layout_recognizer.py`（版面识别）、`table_structure_recognizer.py`（表格结构）、`recognizer.py`（识别器基类）

**常用扩展点**：
1. **新增格式解析器**：在 `deepdoc/parser/` 新增 `xxx_parser.py`，再到 `rag/app/` 层接入 chunk 流程（参见 3.2 的 FACTORY）。
2. **切换第三方解析引擎**：通过 dataset 配置选用 docling/mineru 等，**无需改核心代码**——这是官方认可的扩展路径。
3. ⚠️ **不建议**直接修改 `pdf_parser.py` 内部版面切分逻辑与 `vision/` 的模型后处理（见第五章禁区）。

### 3.2 rag 检索引擎

**实现逻辑**：检索入口是 `rag/nlp/search.py` 的 **`Dealer` 类**，构造时注入一个 `DocStoreConnection`（由 `rag/utils/*_conn.py` 实现的存储抽象）。一次 `retrieval()` 调用内部完成：

1. **查询处理**：`rag/nlp/query.py` 对 query 分词、加权（`term_weight.py`、`synonym.py` 参与）
2. **混合检索**：BM25 全文 + 向量 KNN 并行下发到 doc store
3. **重排**：`rerank()`（关键词/向量加权融合）或 `rerank_by_model()`（rerank 模型）
4. **引用定位**：`insert_citations()` 把引用片段插回答案

**关键类与函数**：
- `Dealer.retrieval()`（`search.py` 约 549 行起）——**检索总入口**
- `Dealer.search()` / `Dealer.rerank()` / `Dealer.hybrid_similarity()` —— 可调参的排序逻辑
- `rag/utils/es_conn.py` 等 `DocStoreConnection` 实现 —— 新存储后端只需实现该接口
- 摄取侧：`rag/svr/task_executor.py` 的 `build_chunks()`（按 `FACTORY[parser_id]` 选 `rag/app/*.py` 的 `chunk()` 函数）→ `embedding()` → `insert_chunks()`
- 模型调用统一经 `api/db/services/llm_service.py` 的 **`LLMBundle`**，具体实现在 `rag/llm/`（新增模型厂商在 `chat_model.py` 等文件加类 + `conf/llm_factories.json` 登记）

**常用扩展点**：
1. 新增向量库/存储后端：新增一个 `*_conn.py` 实现 `DocStoreConnection`
2. 新增 LLM/Embedding/Rerank 厂商：`rag/llm/` 加适配类 + `conf/llm_factories.json` 注册
3. 调整分块策略：优先改 dataset 的 parser 配置，其次才是 `rag/app/` 对应 chunker

### 3.3 graph 工作流（Agent）

**实现逻辑**：前端画布保存的 DSL（components + 上下游关系）由 `agent/canvas.py` 中的 **`Graph` 类**执行：解析 DSL 拓扑 → 从 Begin 组件开始按依赖顺序驱动各组件 → 支持流式输出、循环（Loop/Iteration）、分支（Switch/Categorize）。`rag/flow/pipeline.py` 的 `Pipeline(Graph)` 复用同一套图执行框架处理数据流水线。

**关键类与文件**：
- `agent/canvas.py` —— `Graph`（DSL 执行调度核心，**1700+ 行，逻辑密集**）
- `agent/component/base.py` —— **`ComponentBase`（组件基类）** 与 `ComponentParamBase`（参数基类），所有组件必须继承
- `agent/component/__init__.py` —— **自动导入机制**：扫描目录下所有 `.py`（跳过 `__init__` 与 `base` 开头），提取类注册；`component_class(class_name)` 依次从 `agent.component` → `agent.tools` → `rag.flow` 动态查找组件类
- 现有组件（23 个）：`llm.py`、`categorize.py`、`retrieval`（在 rag.flow）、`invoke.py`（HTTP）、`browser.py`、`switch.py`、`iteration.py`、`loop.py`、`message.py`、`variable_aggregator.py`、`excel_processor.py` 等
- `agent/sandbox/` —— 代码执行沙箱；`agent/templates/` —— 官方模板

**常用扩展点**：**新增工作流组件是二开最常见的需求之一**——在 `agent/component/` 下新建文件，定义 `XxxParam(ComponentParamBase)` + `Xxx(ComponentBase)` 实现 `_run` 等方法即可被自动发现（前端画布侧需同步添加组件图标与表单，属于安全区改动）。

### 3.4 API 服务层

**实现逻辑**：入口 `api/ragflow_server.py` 启动 Quart 应用（由 `api/apps/__init__.py` 构建）。最核心的机制是**路由自动注册**：

```python
# api/apps/__init__.py
search_pages_path()  # 扫描 *_app.py、sdk/*.py、restful_apis/*.py
register_page()      # 每个文件动态加载为 Blueprint 并注册
# URL 前缀规则：
#   restful_apis/ 下 → /api/v1（对外 RESTful API，推荐）
#   其余 *_app.py   → /v1/<模块名>（前端内部接口）
```

鉴权支持 **JWT（会话）+ API Token** 双模式（`AUTH_JWT`/`AUTH_API`），装饰器在 `api/apps/__init__.py` 中定义（`login_required` 系列）。数据层为 **Peewee ORM**：`api/db/db_models.py` 定义全部表（MySQL/PG/OceanBase 三种连接池实现），`api/db/services/` 提供 `CommonService` 基类与 30+ 业务服务类。

**关键文件**：
- `api/apps/restful_apis/chat_api.py`、`dataset_api.py`、`document_api.py`、`chunk_api.py`、`openai_api.py`（OpenAI 兼容接口）等 —— **业务接口二开主战场**
- `api/apps/services/` —— 接口层聚合服务（如 `dataset_api_service.py`）
- `api/channels/core/registry.py` —— IM 渠道注册中心，新增渠道按 `api/channels/dingtalk/` 等现有目录结构照抄即可

**Go 侧对等实现**：`internal/router/router.go` 集中注册 30+ handler（gin），分层 `handler → service → dao`。若二开需要 Go 侧同步生效，**必须同时改 Go 代码**，否则切换 `API_PROXY_SCHEME=go` 后功能缺失。

### 3.5 插件体系（Plugin）

**实现逻辑**：`agent/plugin/plugin_manager.py` 的 `PluginManager` 基于 `pluginlib` 库，在启动时**递归扫描 `agent/plugin/embedded_plugins/`** 加载插件。当前**唯一支持的类型是 `llm_tools`**（供 LLM Agent 调用的工具函数），由 `ragflow_server.py` 启动时经 `GlobalPluginManager.load_plugins()` 初始化。

**关键类与文件**：
- `agent/plugin/llm_tool_plugin.py` —— **`LLMToolPlugin` 基类**与 `LLMToolMetadata` 元数据结构
- `agent/plugin/common.py` —— 插件类型常量（`PLUGIN_TYPE_LLM_TOOLS`）
- `agent/plugin/embedded_plugins/llm_tools/bad_calculator.py` —— **官方完整示例**
- `agent/plugin/README_zh.md` —— 官方中文插件开发指南（**写插件前必读**）
- 对外接口：`api/apps/restful_apis/plugin_api.py`（Python）与 `internal/handler/plugin.go`（Go）向前端暴露插件列表

**开发方式**（详见第六章）：新建文件 → 继承 `LLMToolPlugin` → 实现 `_version_`、`get_metadata()`、`invoke()` 三要素 → 放入 `embedded_plugins/llm_tools/` → 重启即加载。**这是官方明确推荐的二开首选路径，零核心代码改动**。

---

## 四、技术栈与依赖说明

### 4.1 后端 Python

- **版本**：`requires-python = ">=3.13,<3.14"`（`pyproject.toml`，**必须 3.13**）
- **包管理**：`uv`（`pyproject.toml` + `uv.lock`），安装命令 `uv sync --python 3.13 --all-extras`
- **Web 框架**：**Quart**（Flask 的异步对等实现）+ quart-cors + quart-schema（OpenAPI）+ quart-auth；⚠️ 依赖里虽含 flask 系列包，但 API 服务实际代码用的是 Quart（`from quart import Quart, Blueprint...`）
- **ORM**：**Peewee**（playhouse 连接池），非 SQLAlchemy
- **任务队列**：Redis 自研队列（`rag/utils/redis_conn.py`），无 Celery
- **LLM SDK**：litellm + 各厂商原生 SDK（openai、anthropic、dashscope、google-genai 等）
- **文档处理**：python-docx、openpyxl、PyPDF 系、pdfplumber、markdown 系等（解析场景重依赖）

### 4.2 后端 Go

- **版本**：Go **1.26.4**（`go.mod`）；实际开发参考 `internal/development.md`（clang-20 + lld + CMake 环境）
- **框架**：**gin**；LLM 编排用 cloudwego **eino**
- **构建**：⚠️ **必须用 `bash build.sh`**，不能裸跑 `go build`/`go test`——需要 CGO flags 与原生静态库（`office_oxide`/`pdfium`/`pdf_oxide`，由 `ragflow_deps/download_deps.py` 下载）
- **测试分层**：unit（无 tag）/ integration / e2e / manual（build tag 区分，详见 `AGENTS.md`）

### 4.3 前端

- **框架**：React 18 + TypeScript + **Vite**（已从 umi 迁移，见 `web/package.json` 描述）
- **UI**：antd 5 + TailwindCSS；流程画布 **@antv/x6**；富文本 Lexical
- **脚本**：`npm run dev` / `build` / `lint`（oxlint）/ `test`（jest）/ `type-check`

### 4.4 数据存储选型

| 用途 | 选型 | 说明 |
|---|---|---|
| 业务库 | **MySQL**（默认）/ PostgreSQL / OceanBase（MySQL 协议） | Peewee 三种连接池并存（`api/db/db_models.py`） |
| 文档/向量库 | **Elasticsearch 8**（默认）、Infinity、OpenSearch、OceanBase、SerenE、SeekDB | 经 `DocStoreConnection` 抽象，`conf/mapping.json` 定义索引结构 |
| 对象存储 | **MinIO**（默认）、S3、OSS、GCS、Azure Blob | `rag/utils/*_conn.py` + `storage_factory.py` |
| 缓存/队列/锁 | **Redis** | 任务队列、会话、分布式锁（`RedisDistributedLock`） |

> 配置集中在 `docker/service_conf.yaml.template`（部署）与 `conf/`（映射/模型清单），二开环境差异优先改配置而非改代码。

---

## 五、二次开发分级指引

### 5.1 🟢 安全区（推荐优先改动）

| 改动类型 | 位置 | 理由 |
|---|---|---|
| **新增业务接口** | `api/apps/restful_apis/` 新增独立 `*_api.py` | 自动注册机制直接生效，**不触碰现有文件**；遵循 router→service→dao 分层 |
| **业务服务逻辑** | `api/db/services/` 新增或扩展 service | 独立文件，冲突面小 |
| **前端页面/样式/文案** | `web/src/pages/`、`web/src/locales/` | 纯展示层，升级冲突最易解决 |
| **LLM 工具插件** | `agent/plugin/embedded_plugins/llm_tools/` | 官方插件机制，**零核心改动** |
| **新增工作流组件** | `agent/component/` 新增文件 + 前端画布配置 | 自动发现机制，独立文件 |
| **新增 IM 渠道** | `api/channels/` 新增目录 | registry 注册模式，照抄现有渠道结构 |
| **新增格式解析器** | `deepdoc/parser/` + `rag/app/` 新增文件 | 新格式走新文件，不动存量解析器 |
| **模型厂商接入** | `rag/llm/` 加类 + `conf/llm_factories.json` | 厂商间相互隔离 |
| **运行配置** | `conf/`、`docker/service_conf.yaml.template` | 本就是配置项 |

### 5.2 🟡 谨慎区（封装扩展优先，改前评估）

| 模块 | 风险点 | 建议姿势 |
|---|---|---|
| **`Dealer` 检索逻辑**（`rag/nlp/search.py`） | 全链路共用，改坏影响所有对话/搜索 | 用子类继承或参数扩展，避免改原方法签名 |
| **task_executor 流程**（`rag/svr/task_executor.py`） | 单文件 1900+ 行，摄取主链路 | 只在 `rag/app/` chunker 层做文章，不动调度框架 |
| **`rag/app/*.py` 现有 chunker** | 存量文档已按其产出入库 | 改分块逻辑需考虑**存量数据重解析** |
| **GraphRAG**（`rag/graphrag/`） | 图谱构建与检索耦合 | 优先外层封装 |
| **数据模型**（`api/db/db_models.py`） | 全服务共用；**必须同步写迁移脚本**（项目规则要求） | 只增字段不改类型，提供存量兼容迁移 |
| **Agent 画布执行**（`agent/canvas.py`） | DSL 结构兼容 + 并发逻辑复杂 | 新能力尽量做成新组件，不改编排器 |
| **`common/settings.py` 初始化链** | 全模块 import 期依赖 | 新增配置项走环境变量/配置文件 |

### 5.3 🔴 禁区（强烈不建议直接修改）

1. **`deepdoc/vision/` 视觉模型推理与后处理**：与 ONNX 模型权重强绑定（`operators.py`/`postprocess.py`），改错直接导致解析结果崩坏且难以回归测试。
2. **`deepdoc/parser/pdf_parser.py` 版面切分内核**：项目规则明确 deepdoc 底层核心优先外层封装；内部状态机复杂，官方升级高频变动区。
3. **`rag/nlp/rag_tokenizer.py` 中文分词内核**：检索与入库双侧依赖，改动会导致**新旧数据分词不一致、检索召回漂移**。
4. **Go `internal/cpp/` 与原生库接入**（`internal/deepdoc`、`internal/parser` 底层）：CGO/Rust 绑定（office_oxide/pdfium），构建环境苛刻，AGENTS.md 标注为活跃重构区。
5. **DB 连接池/迁移机制**（`RetryingPooledMySQLDatabase` 等）：影响全服务启动与事务行为。
6. **路由自动注册机制**（`api/apps/__init__.py` 的 `register_page`）与 **DSL 结构**（`agent/dsl_migration.py`）：属于框架级契约，改它等于 fork 整个项目。
7. **`docker/entrypoint.sh` / `build.sh` 启动编排**：牵动所有进程拓扑与 nginx 切换逻辑。

---

## 六、官方原生扩展机制

> 项目规则（`.clinerules`）明确：**优先通过原生 Plugin 机制扩展功能，能不改核心源码就不改**，以降低官方版本升级的兼容成本。

### 6.1 LLM 工具插件（当前唯一插件类型）

**能力范围**：向 Agent 注册可被 LLM 调用的工具函数（函数调用/Tool Calling），工具元数据同时提供给 LLM 与前端展示。

**开发步骤**（依据 `agent/plugin/README_zh.md`）：

1. 在 `agent/plugin/embedded_plugins/llm_tools/` 下新建 `my_tool.py`
2. 定义继承 `LLMToolPlugin` 的类，**三要素**：
   ```python
   class MyToolPlugin(LLMToolPlugin):
       _version_ = "1.0.0"                      # 必填版本号

       def invoke(self, a: int, b: int) -> str:  # 参数即 LLM 传入参数，必须返回 str
           return str(a + b)

       @classmethod
       def get_metadata(cls) -> LLMToolMetadata:  # 工具描述（提供给 LLM + 前端）
           return {
               "name": "my_tool",
               "displayName": "我的工具",
               "description": "给 LLM 看的用法说明",
               "displayDescription": "给前端展示的描述",
               "parameters": {
                   "a": {"type": "number", "description": "参数a说明", "required": True},
                   "b": {"type": "number", "description": "参数b说明", "required": True},
               },
           }
   ```
3. 重启服务，日志出现 `Loaded llm_tools plugin MyToolPlugin version 1.0.0` 即加载成功；前端 Agent 编排界面可直接选用该工具

**加载机制**：`PluginManager.load_plugins()` 用 `pluginlib.PluginLoader` **递归扫描** `embedded_plugins/`，因此插件可放子目录分类管理；`ragflow_server.py` 启动时初始化 `GlobalPluginManager`。

**参考示例**：`agent/plugin/embedded_plugins/llm_tools/bad_calculator.py`（官方完整可运行示例）。

### 6.2 其他官方认可的扩展位

| 扩展机制 | 入口 | 说明 |
|---|---|---|
| **工作流组件** | `agent/component/` 新建文件继承 `ComponentBase` | `__init__.py` 自动扫描注册，无需改注册表 |
| **IM 渠道** | `api/channels/<渠道名>/` | 参照 dingtalk/feishu 目录结构，`core/registry.py` 注册 |
| **解析引擎切换** | dataset 配置层 | docling/mineru/paddleocr 等已有适配层（`deepdoc/parser/*_parser.py`） |
| **MCP Server** | `mcp/server/server.py` | 通过容器参数 `--enable-mcpserver` 开启，对外暴露检索能力 |
| **RESTful API** | `api/apps/restful_apis/` 新增文件 | 文件名即路由模块，自动挂到 `/api/v1` |
| **模型厂商** | `rag/llm/` + `conf/llm_factories.json` | 加适配类 + 登记元信息 |

---

## 七、版本升级避坑指南

官方升级方式（`docs/administrator/upgrade_ragflow.mdx`）：**代码与 Docker 镜像必须同步升级**（`git pull` + 更新 `RAGFLOW_IMAGE` + `docker compose up -d`）；升级本身不删数据，但 `docker compose down -v` 会清卷，**严禁带 `-v` 操作生产环境**。

### 7.1 二开后最容易产生冲突的改动点

1. **`api/apps/__init__.py`、`agent/component/__init__.py`** 等**自动注册/扫描机制**文件——官方高频重构区，一旦改过几乎必然冲突。**对策：永远不要改机制本身，只往约定目录里加文件。**
2. **`rag/svr/task_executor.py`**——摄取主链路单文件巨无霸，官方持续演进。**对策：自定义逻辑下沉到 `rag/app/` 自己的 chunker 文件。**
3. **`api/db/db_models.py` 与 Peewee 迁移**——官方表结构变更频繁。**对策：二开新增字段用独立前缀（如 `biz_`），并保留自己的迁移脚本（项目规则要求）。**
4. **`pyproject.toml` / `uv.lock` / `go.mod`**——依赖版本冲突高发。**对策：能用运行时配置/插件解决的，不引入新依赖；必须引入时集中记录。**
5. **`web/src/locales/` 与 `routes.tsx`**——前端文案和路由官方常动。**对策：文案用新增 key，不覆盖官方 key；路由追加不插入。**
6. **`conf/mapping.json` 等索引映射**——改映射意味着存量索引不兼容。**对策：新增字段而非修改现有字段，必要时准备 reindex 方案。**
7. **deepdoc / rag 内核直接改动**——与上游 diff 越积越大，最终无法合并。

### 7.2 降低兼容成本的通用手法

- **独立文件原则**：所有二开代码尽量放新文件/新目录，利用项目的自动发现机制接入（插件、组件、restful_apis、channels 都支持）。
- **外层封装原则**：必须扩展核心行为时，用继承/组合在调用侧包装，不改原函数体与签名。
- **变更台账**：维护一份「改了什么、为什么改、涉及文件」的清单（如 `CUSTOM_CHANGES.md`），每次升级前逐项核对上游是否改动了同一区域。
- **分支策略**：fork 仓库保持自有分支，按 release tag 分批 rebase/merge，**不要积压多个大版本一次升级**。
- **升级后回归清单**：文档解析（重点 PDF/表格）、对话检索召回、Agent 执行、渠道消息、启动日志中的插件/组件加载告警。

---

## 八、快速上手路线

### 阶段一：跑起来（第 1 天）
1. 按 `docs/develop/launch_ragflow_from_source.md` 搭环境：`docker compose -f docker/docker-compose-base.yml up -d` 起基础服务 → `uv sync --python 3.13 --all-extras` → `bash docker/launch_backend_service.sh` 起后端 → `cd web && npm install && npm run dev` 起前端。
2. 在界面上完整走一遍：**建知识库 → 传文档 → 等解析 → 检索测试 → 建对话助手 → 提问**，建立对两条核心链路的直观认知。

### 阶段二：读懂两条主链路（第 2-4 天）
- **摄取链**（按调用顺序读）：`api/apps/restful_apis/document_api.py` → `rag/svr/task_executor.py`（`build_chunks`/`embedding`/`insert_chunks`）→ `rag/app/naive.py` 的 `chunk()` → `rag/utils/es_conn.py`
- **对话链**：`api/apps/restful_apis/chat_api.py` → `api/db/services/dialog_service.py` → `rag/nlp/search.py` `Dealer.retrieval()` → `api/db/services/llm_service.py` `LLMBundle`
- 辅助：`api/apps/__init__.py` 的路由自动注册、`agent/canvas.py` 的 `Graph` 执行（Agent 场景再读）。

### 阶段三：动手做低风险改动（第 1-2 周）
按优先级依次练手：
1. **写一个 LLM 工具插件**（仿 `bad_calculator.py`，30 行以内）——熟悉插件机制
2. **新增一个 RESTful 接口**（`restful_apis/` 新文件 + 前端 `services/` 调用）——熟悉分层与鉴权装饰器
3. **改前端文案/样式**（`locales/` + 页面组件）——熟悉前端结构
4. **新增一个工作流组件**（后端 `agent/component/` + 前端画布）——熟悉 DSL 与组件协议

### 阶段四：深度定制（按需）
- 自定义分块：新增/修改 `rag/app/` chunker
- 检索调优：研究 `Dealer` 的 rerank 参数与 `rank_feature`
- 接入自有模型/存储：`rag/llm/` 与 `rag/utils/*_conn.py`
- ⚠️ 触碰谨慎区/禁区前，先回到第五章对照风险，并按项目规则**先出方案经确认再动手**。

### 附：必读参考文件清单
| 文件 | 内容 |
|---|---|
| `AGENTS.md`（= `CLAUDE.md`） | 代码库操作指南：技术栈、目录约定、Go 测试分层、验证命令 |
| `.clinerules` | 项目二开行为规则（代码规范/执行原则/安全约束） |
| `agent/plugin/README_zh.md` | 插件开发中文指南 |
| `internal/development.md` | Go 开发环境搭建（CGO/原生库） |
| `docs/develop/launch_ragflow_from_source.md` | 源码启动指南 |
| `docs/administrator/upgrade_ragflow.mdx` | 官方升级指南 |

---

*本文档基于 RAGFlow 0.26.4（commit `c0582b8e1`）实际代码通读生成；后续官方版本演进后，请对照第七章重新核对关键路径是否变化。*




