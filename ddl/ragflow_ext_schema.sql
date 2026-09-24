-- =====================================================================================
-- RAGFlow 二开扩展库建表脚本（评审版 V1.0）
-- 依据   : 《二开技术方案设计文档》RAGFLOW-EXT-HLD-001 V1.0 第 5 章（5.2/5.3 基线展开）
-- 库     : ragflow_ext —— 独立 schema，与 RAGFlow 主库(rag_flow)物理隔离，可同实例部署
-- 引擎   : MySQL 8.0+ / InnoDB / utf8mb4_0900_ai_ci
-- 范围   : 五大核心域 22 张表
--          域A 权限扩展(9) 域B 知识空间(3) 域C 审批流程(6) 域D 搜索历史(3) 域E 版本管理(2)
-- 约定   : ① 引用 RAGFlow 主库实体(user/knowledgebase/document)仅存字符串 ID(VARCHAR(64))，
--             不建跨库外键；库内亦不建外键（沿用 RAGFlow 自身无外键惯例），一致性由
--             ragflow-ext 服务应用层校验 + 定时对账任务保证；
--          ② 公共列规则：实体表= id/created_at/updated_at/created_by/updated_by；
--             事件表(追加只写)= id/created_at + 业务主体列(user_id/operator)；
--             特例 ext_audit_log = occurred_at(分区键) + ragflow_user_id；
--          ③ 生产由 Alembic 基线迁移建表；本文件供 DBA 评审/测试环境/手工部署；
--          ④ 词表封闭用 ENUM（词表变更走 Alembic ALTER），可演化结构用 JSON。
-- =====================================================================================
-- CREATE DATABASE IF NOT EXISTS ragflow_ext DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- USE ragflow_ext;

SET NAMES utf8mb4;

-- =====================================================================================
-- 域 A｜权限扩展域（R01 基础版 / R02 完善版 / R04 组织架构 / R17 页面隐藏 / R20 审计）
-- 模型：用户 → 角色(自定义) → 资源(空间/知识库/文档/字段) × 动作(read/edit/manage/share/approve)
--       叠加主体维度（用户/部门/岗位）形成 ACL；
-- 判定：deny 优先 → 用户直接授权(就近) → 部门/岗位/角色继承(空间树自节点向根聚合) → 系统角色
-- =====================================================================================

-- A1. ext_user 用户-组织架构映射 --------------------------------------------------------
-- 主库引用: ragflow_user_id → rag_flow.user.id (CHAR(32))
CREATE TABLE IF NOT EXISTS ext_user (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ragflow_user_id VARCHAR(64)  NOT NULL                COMMENT 'RAGFlow 主库 user.id',
  employee_no     VARCHAR(64)  DEFAULT NULL            COMMENT '员工工号（HR 主键，同步幂等键）',
  display_name    VARCHAR(128) DEFAULT NULL            COMMENT '姓名（同步自 HR，冗余展示，避免回查主库）',
  email           VARCHAR(255) DEFAULT NULL            COMMENT '邮箱（与主库 user.email 对账映射）',
  dept_code       VARCHAR(64)  DEFAULT NULL            COMMENT '主部门编码 → ext_department.dept_code',
  secondary_depts JSON         DEFAULT NULL            COMMENT '兼职部门编码数组，如 ["D0012","D0013"]',
  position_code   VARCHAR(64)  DEFAULT NULL            COMMENT '岗位编码（ACL 主体 position 维度）',
  position_name   VARCHAR(128) DEFAULT NULL            COMMENT '岗位名称',
  security_level  TINYINT UNSIGNED NOT NULL DEFAULT 1  COMMENT '可访问密级上限 1-5（R02 密级过滤/R20 水印联动）',
  status          TINYINT      NOT NULL DEFAULT 1      COMMENT '1=生效 0=离职停用（组织同步任务维护）',
  last_synced_at  DATETIME(3)  DEFAULT NULL            COMMENT '最近一次组织架构同步时间',
  created_by      VARCHAR(64)  DEFAULT NULL,
  updated_by      VARCHAR(64)  DEFAULT NULL,
  created_at      DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_ragflow_user (ragflow_user_id),
  UNIQUE KEY uk_employee_no (employee_no),
  KEY idx_dept (dept_code),
  KEY idx_position (position_code),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A1 用户-组织架构映射（R01/R04）：密级上限/主兼职部门/岗位';

-- A2. ext_department 部门树 --------------------------------------------------------------
-- full_path 编码全路径（如 0001/0012/0035）双重用途：
--   ① 写入 RAGFlow 文档元数据 department 字段的取值；② 检索代理 metadata_condition
--   前缀匹配（本部门+下级部门展开）的依据（见 HLD 4.3.3）
CREATE TABLE IF NOT EXISTS ext_department (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  dept_code     VARCHAR(64)  NOT NULL                COMMENT '部门编码（HR 系统主键，同步幂等 upsert 键）',
  parent_code   VARCHAR(64)  DEFAULT NULL            COMMENT '父部门编码，NULL=根部门',
  dept_name     VARCHAR(128) NOT NULL                COMMENT '部门名称',
  full_path     VARCHAR(512) NOT NULL                COMMENT '编码全路径 0001/0012/0035（prefix 检索过滤依据）',
  name_path     VARCHAR(1024) DEFAULT NULL           COMMENT '名称全路径 集团/华东大区/上海分公司（展示用）',
  level         TINYINT UNSIGNED NOT NULL DEFAULT 1  COMMENT '层级深度（根=1）',
  sort_no       INT          NOT NULL DEFAULT 0      COMMENT '同级排序',
  status        ENUM('active','merged','revoked') NOT NULL DEFAULT 'active'
                                                        COMMENT 'active=正常 merged=已合并 revoked=已撤销',
  merged_to     VARCHAR(64)  DEFAULT NULL            COMMENT 'status=merged 时指向合并后的部门编码',
  hr_updated_at DATETIME(3)  DEFAULT NULL            COMMENT 'HR 侧该部门最近更新时间（对账依据）',
  synced_at     DATETIME(3)  DEFAULT NULL            COMMENT '本表最近同步时间',
  created_by    VARCHAR(64)  DEFAULT NULL,
  updated_by    VARCHAR(64)  DEFAULT NULL,
  created_at    DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_dept_code (dept_code),
  KEY idx_parent (parent_code),
  KEY idx_full_path (full_path),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A2 部门树（R04 组织架构同步）';

-- A3. ext_org_sync_log 组织架构同步日志 ---------------------------------------------------
-- 每批次一行；降级机制判定依据：连续 3 个周期 failed → 冻结部门变更类操作并告警（HLD 4.3.3）
CREATE TABLE IF NOT EXISTS ext_org_sync_log (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  batch_no        VARCHAR(32) NOT NULL                COMMENT '同步批次号（YYYYMMDDHHmmss+序号）',
  source          ENUM('hr_api','excel','manual') NOT NULL DEFAULT 'hr_api'
                                                        COMMENT '同步来源：HR接口/Excel导入兜底/手工修正',
  trigger_type    ENUM('schedule','manual') NOT NULL DEFAULT 'schedule' COMMENT '定时/手动触发',
  dept_stats      JSON DEFAULT NULL                   COMMENT '部门统计 {"inserted":n,"updated":n,"deactivated":n}',
  user_stats      JSON DEFAULT NULL                   COMMENT '人员统计 {"inserted":n,"updated":n,"deactivated":n}',
  result          ENUM('success','partial','failed') NOT NULL COMMENT '成功/部分失败/失败',
  degraded_events JSON DEFAULT NULL                   COMMENT '降级事件明细（快照沿用/冻结操作/告警通知）',
  error_msg       TEXT COMMENT '失败原因',
  started_at      DATETIME(3) DEFAULT NULL,
  finished_at     DATETIME(3) DEFAULT NULL,
  created_by      VARCHAR(64) DEFAULT NULL,
  updated_by      VARCHAR(64) DEFAULT NULL,
  created_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_batch_no (batch_no),
  KEY idx_started (started_at),
  KEY idx_result (result)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A3 组织架构同步日志（R04，含降级事件）';

-- A4. ext_role 自定义角色 -----------------------------------------------------------------
-- 两级模型：功能权限点（本表 permissions，菜单/功能级）× 资源 ACL（A6 条目，资源级）
CREATE TABLE IF NOT EXISTS ext_role (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  code        VARCHAR(64)  NOT NULL                COMMENT '角色编码（唯一，如 space_admin）',
  name        VARCHAR(128) NOT NULL                COMMENT '角色名称',
  description VARCHAR(512) DEFAULT NULL,
  permissions JSON         NOT NULL                COMMENT '功能权限点集合，如 ["space:create","kb:read"]；["*"]=全部',
  is_system   TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '1=内置角色（super_admin/space_admin），禁止删除',
  status      TINYINT      NOT NULL DEFAULT 1      COMMENT '1=启用 0=停用',
  created_by  VARCHAR(64)  DEFAULT NULL,
  updated_by  VARCHAR(64)  DEFAULT NULL,
  created_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_code (code),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A4 自定义角色（R01/R02）';

-- A5. ext_role_user 角色-用户绑定 ----------------------------------------------------------
-- scope 绑定使 space_admin 类角色可限定在特定空间生效（scope_id=0 表示全局生效）
CREATE TABLE IF NOT EXISTS ext_role_user (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  role_id         BIGINT UNSIGNED NOT NULL           COMMENT '→ ext_role.id',
  ragflow_user_id VARCHAR(64)      NOT NULL           COMMENT '→ rag_flow.user.id',
  scope_type      ENUM('global','space') NOT NULL DEFAULT 'global' COMMENT '角色生效范围',
  scope_id        BIGINT UNSIGNED NOT NULL DEFAULT 0  COMMENT 'scope_type=space 时为 ext_space.id；global 固定 0',
  expires_at      DATETIME(3)      DEFAULT NULL       COMMENT '绑定过期时间（NULL=永久，判定时过滤）',
  granted_by      VARCHAR(64)      DEFAULT NULL       COMMENT '授权人',
  created_by      VARCHAR(64)      DEFAULT NULL,
  updated_by      VARCHAR(64)      DEFAULT NULL,
  created_at      DATETIME(3)      DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3)      DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_role_user_scope (role_id, ragflow_user_id, scope_type, scope_id),
  KEY idx_user (ragflow_user_id),
  KEY idx_scope (scope_type, scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A5 角色-用户绑定（可限定空间范围，R01/R02）';

-- A6. ext_permission_entry 权限 ACL 条目（判定核心表）---------------------------------------
-- 一条记录 = 主体 × 资源 × 动作 × 效果；判定在应用层聚合：
--   deny 优先 → 就近优先（document > dataset > space > global；用户直接 > 部门/岗位/角色继承）
--   → 同层并集；action 层级语义 manage ⊇ edit ⊇ read（判定时向上展开）
-- 主体引用：user=rag_flow.user.id / role=ext_role.id / dept=ext_department.dept_code / position=岗位编码
-- 资源引用：space=ext_space.id / dataset=rag_flow.knowledgebase.id / document=rag_flow.document.id /
--           field=元数据字段名 / global=*
CREATE TABLE IF NOT EXISTS ext_permission_entry (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  subject_type  ENUM('user','role','dept','position') NOT NULL COMMENT '授权主体类型',
  subject_id    VARCHAR(64) NOT NULL                  COMMENT '主体标识（见上方主体引用约定）',
  resource_type ENUM('global','space','dataset','document','field') NOT NULL,
  resource_id   VARCHAR(64) NOT NULL DEFAULT '*'      COMMENT '资源标识；* = 全局（resource_type=global 时固定 *）',
  action        ENUM('read','edit','manage','share','approve') NOT NULL,
  effect        ENUM('allow','deny') NOT NULL DEFAULT 'allow' COMMENT 'deny 显式拒绝（优先级最高）',
  inherit       TINYINT(1) NOT NULL DEFAULT 1         COMMENT '是否随空间树向下继承（仅 space 资源有效）',
  expires_at    DATETIME(3) DEFAULT NULL              COMMENT '过期时间（判定时不参与，NULL=永久）',
  remark        VARCHAR(255) DEFAULT NULL,
  created_by    VARCHAR(64) DEFAULT NULL,
  updated_by    VARCHAR(64) DEFAULT NULL,
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_subject (subject_type, subject_id),
  KEY idx_resource (resource_type, resource_id),
  KEY idx_action (action),
  KEY idx_effect (effect),
  KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A6 权限ACL条目（R01/R02 判定核心表）';

-- A7. ext_route_permission 角色-前端路由可见性（R17 页面隐藏）--------------------------------
-- route_key 取值 = web/src/routes.tsx 的 Routes 枚举路径（如 /files = 文件管理页）；
-- role_id=0 表示全局默认规则（对所有未单独配置的角色生效）；网关/ext-web 按角色合并输出菜单
CREATE TABLE IF NOT EXISTS ext_route_permission (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  role_id    BIGINT UNSIGNED NOT NULL DEFAULT 0   COMMENT '→ ext_role.id；0=全局默认规则',
  route_key  VARCHAR(128) NOT NULL                COMMENT '前端路由路径（Routes 枚举值，如 /files）',
  visible    TINYINT(1)    NOT NULL DEFAULT 0     COMMENT '1=可见 0=隐藏',
  created_by VARCHAR(64)  DEFAULT NULL,
  updated_by VARCHAR(64)  DEFAULT NULL,
  created_at DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_role_route (role_id, route_key),
  KEY idx_route (route_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A7 角色-路由可见性映射（R17 前端页面隐藏）';

-- A8. ext_audit_log 审计日志（按月 RANGE 分区）----------------------------------------------
-- 追加只写事件表：不设 updated_*；ragflow_user_id 即操作人（公共列规则的文档化例外）；
-- 写入来源：网关拦截(denied) / ext 写操作(success) / 登录事件 / 定时对账；
-- 分区维护：月度任务预建下月分区，并按保留策略（默认 24 个月）归档后 DROP 最旧分区
CREATE TABLE IF NOT EXISTS ext_audit_log (
  id              BIGINT UNSIGNED AUTO_INCREMENT NOT NULL,
  occurred_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '事件时间（分区键）',
  ragflow_user_id VARCHAR(64) NOT NULL           COMMENT '操作人 → rag_flow.user.id',
  user_ip         VARCHAR(64)  DEFAULT NULL,
  source          ENUM('gateway','ext_web','ext_api','sync_job') NOT NULL DEFAULT 'gateway' COMMENT '事件来源',
  action          VARCHAR(64)  NOT NULL           COMMENT '动作，如 dataset:upload / document:delete / authz:denied / user:login',
  resource_type   VARCHAR(32)  DEFAULT NULL       COMMENT '资源类型（space/dataset/document/share_link...）',
  resource_id     VARCHAR(64)  DEFAULT NULL,
  result          ENUM('success','denied','error') NOT NULL DEFAULT 'success',
  detail          JSON         DEFAULT NULL       COMMENT '上下文（请求摘要/拒绝原因/审批单号等）',
  trace_id        VARCHAR(64)  DEFAULT NULL       COMMENT '链路追踪 ID（网关 ↔ ext ↔ RAGFlow）',
  PRIMARY KEY (id, occurred_at),
  KEY idx_user_time (ragflow_user_id, occurred_at),
  KEY idx_action (action),
  KEY idx_resource (resource_type, resource_id),
  KEY idx_time (occurred_at),
  KEY idx_result (result)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A8 审计日志（按月分区，异步落库，R01/R20）'
  PARTITION BY RANGE (TO_DAYS(occurred_at)) (
    PARTITION p202609 VALUES LESS THAN (TO_DAYS('2026-10-01')),
    PARTITION p202610 VALUES LESS THAN (TO_DAYS('2026-11-01')),
    PARTITION p202611 VALUES LESS THAN (TO_DAYS('2026-12-01')),
    PARTITION p202612 VALUES LESS THAN (TO_DAYS('2027-01-01')),
    PARTITION p202701 VALUES LESS THAN (TO_DAYS('2027-02-01')),
    PARTITION p202702 VALUES LESS THAN (TO_DAYS('2027-03-01')),
    PARTITION p202703 VALUES LESS THAN (TO_DAYS('2027-04-01')),
    PARTITION p202704 VALUES LESS THAN (TO_DAYS('2027-05-01')),
    PARTITION p202705 VALUES LESS THAN (TO_DAYS('2027-06-01')),
    PARTITION p202706 VALUES LESS THAN (TO_DAYS('2027-07-01')),
    PARTITION p202707 VALUES LESS THAN (TO_DAYS('2027-08-01')),
    PARTITION p202708 VALUES LESS THAN (TO_DAYS('2027-09-01')),
    PARTITION p202709 VALUES LESS THAN (TO_DAYS('2027-10-01')),
    PARTITION p202710 VALUES LESS THAN (TO_DAYS('2027-11-01')),
    PARTITION p202711 VALUES LESS THAN (TO_DAYS('2027-12-01')),
    PARTITION p202712 VALUES LESS THAN (TO_DAYS('2028-01-01')),
    PARTITION pmax    VALUES LESS THAN MAXVALUE
  );

-- A9. ext_user_setting 用户偏好设置 --------------------------------------------------------
-- incognito：无痕检索开关（R12 隐私控制：开启后本会话检索不写 D1 搜索历史）
CREATE TABLE IF NOT EXISTS ext_user_setting (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  ragflow_user_id VARCHAR(64) NOT NULL              COMMENT '→ rag_flow.user.id',
  incognito       TINYINT(1)  NOT NULL DEFAULT 0    COMMENT '无痕检索（1=不记录搜索历史，R12）',
  settings        JSON        DEFAULT NULL          COMMENT '其他偏好扩展（键值对，向前兼容）',
  created_by      VARCHAR(64) DEFAULT NULL,
  updated_by      VARCHAR(64) DEFAULT NULL,
  created_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_user (ragflow_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='A9 用户偏好设置（R12 无痕检索等）';

-- =====================================================================================
-- 域 B｜知识空间层级域（R03）
-- 空间树：group(纯分组) / kb(挂载 Dataset 叶子) / space_group(部门空间模板)；
-- Dataset 为物理载体（rag_flow.knowledgebase），一个 Dataset 至多挂载到一个 kb 节点（uk_dataset）
-- =====================================================================================

-- B1. ext_space 空间树节点 -----------------------------------------------------------------
-- path 物化路径（/1/5/12 即根→…→本节点 id 链）：免递归查子树（WHERE path LIKE '/1/5/%'）；
-- 归档(archived)=权限判定时不可见，Dataset 保留可恢复；物理删除必须走审批流（域C）
CREATE TABLE IF NOT EXISTS ext_space (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  parent_id    BIGINT UNSIGNED DEFAULT NULL       COMMENT '父节点 id；NULL=根节点',
  name         VARCHAR(128) NOT NULL              COMMENT '空间/分组名称',
  space_type   ENUM('group','kb','space_group') NOT NULL DEFAULT 'group'
                                                    COMMENT 'group=纯分组(无载体) kb=挂载Dataset space_group=部门空间模板',
  dataset_id   VARCHAR(64) DEFAULT NULL           COMMENT 'kb 类型挂载的 rag_flow.knowledgebase.id（全局唯一）',
  path         VARCHAR(512) DEFAULT NULL          COMMENT '物化路径 /1/5/12（节点移动时子树批量重算）',
  depth        SMALLINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '层级深度（根=1）',
  description  VARCHAR(512) DEFAULT NULL,
  status       ENUM('active','archived') NOT NULL DEFAULT 'active' COMMENT 'archived=归档（不可见，Dataset 保留）',
  sort_no      INT NOT NULL DEFAULT 0,
  type_config  JSON DEFAULT NULL                  COMMENT '节点级覆盖的类型配置（覆盖 B2 默认值）',
  doc_count    INT UNSIGNED NOT NULL DEFAULT 0    COMMENT '子树文档数冗余统计（异步刷新，展示用）',
  created_by   VARCHAR(64) DEFAULT NULL,
  updated_by   VARCHAR(64) DEFAULT NULL,
  created_at   DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at   DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_dataset (dataset_id),
  KEY idx_parent (parent_id),
  KEY idx_path (path),
  KEY idx_status (status),
  KEY idx_type (space_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='B1 多层级知识空间树（R03）';

-- B2. ext_space_type_config 空间类型差异化配置 ----------------------------------------------
-- default_parser_id 取值须在 ParserType 枚举白名单内（common/constants.py:123，15 个值）
CREATE TABLE IF NOT EXISTS ext_space_type_config (
  id                         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  type_code                  VARCHAR(64)  NOT NULL     COMMENT '类型编码（group/kb/space_group 或自定义业务类型）',
  type_name                  VARCHAR(128) NOT NULL,
  icon                       VARCHAR(128) DEFAULT NULL COMMENT '前端图标标识',
  default_parser_id          VARCHAR(32)  DEFAULT NULL COMMENT '默认解析器（ParserType 枚举值，如 naive）',
  default_embd_id            VARCHAR(128) DEFAULT NULL COMMENT '默认向量模型（创建 kb 节点 Dataset 时指定）',
  default_permission_template JSON DEFAULT NULL        COMMENT '默认权限模板（创建节点时批量生成 A6 ACL 条目）',
  allow_children             TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否允许创建子空间',
  max_depth                  SMALLINT UNSIGNED NOT NULL DEFAULT 10 COMMENT '该类型节点允许的最大层级深度',
  extra_config               JSON DEFAULT NULL,
  created_by                 VARCHAR(64)  DEFAULT NULL,
  updated_by                 VARCHAR(64)  DEFAULT NULL,
  created_at                 DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at                 DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_type_code (type_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='B2 空间类型差异化配置（R03）';

-- B3. ext_space_dataset_map 空间-Dataset 挂载/迁移事件流水（追加只写）------------------------
-- 存量迁移脚本（ext-migrate spaces）与层级调整的审计依据；当前挂载关系以 B1.dataset_id 为准
CREATE TABLE IF NOT EXISTS ext_space_dataset_map (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  space_id      BIGINT UNSIGNED NOT NULL             COMMENT '→ ext_space.id',
  dataset_id    VARCHAR(64) NOT NULL                 COMMENT '→ rag_flow.knowledgebase.id',
  event         ENUM('mount','unmount','move','migrate','adopt') NOT NULL
                                                        COMMENT '挂载/卸载/移动/存量迁移/待归档认领',
  from_space_id BIGINT UNSIGNED DEFAULT NULL         COMMENT 'move：原空间 id',
  to_space_id   BIGINT UNSIGNED DEFAULT NULL         COMMENT 'move：目标空间 id',
  operator      VARCHAR(64) DEFAULT NULL             COMMENT '操作人（system=迁移脚本）',
  detail        JSON DEFAULT NULL                    COMMENT '事件上下文（迁移批次号/Dataset 名/文档数等）',
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_space (space_id),
  KEY idx_dataset (dataset_id),
  KEY idx_event (event)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='B3 空间-Dataset 挂载/迁移事件流水（R03）';

-- =====================================================================================
-- 域 C｜审批流程域（R08 基础版 / R19 完善版）
-- 「先审后执」：文件暂存 ext（MinIO ext-approval-stash 桶）→ 审批通过 → PUBLISHING 由异步
-- 执行器以服务账号调 RAGFlow /api/v1 完成写入 → PUBLISHED / FAILED（幂等 + 指数退避重试 3 次）
-- 状态机：DRAFT → REVIEWING → PUBLISHING → PUBLISHED｜FAILED；任一在途态 → CANCELLED
-- =====================================================================================

-- C1. ext_approval_flow 审批流程定义 --------------------------------------------------------
-- flow 定义可迭代（version+1 重新发布）；在途实例固化引用创建时的 flow_version，定义变更不影响存量
CREATE TABLE IF NOT EXISTS ext_approval_flow (
  id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  code        VARCHAR(64)  NOT NULL                COMMENT '流程编码（如 default_doc_upload）',
  name        VARCHAR(128) NOT NULL,
  biz_action  ENUM('upload','update','delete','share','config') NOT NULL COMMENT '适用动作',
  scope_type  ENUM('global','space','dataset') NOT NULL DEFAULT 'global' COMMENT '适用范围类型',
  scope_id    VARCHAR(64)  DEFAULT NULL            COMMENT '范围对象（space=ext_space.id / dataset=kb.id / global=*）',
  version     INT UNSIGNED NOT NULL DEFAULT 1      COMMENT '定义版本号（每次发布 +1）',
  status      ENUM('draft','active','disabled') NOT NULL DEFAULT 'active',
  is_default  TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '1=该动作兜底流程（无更精确 scope 匹配时启用）',
  description VARCHAR(512) DEFAULT NULL,
  created_by  VARCHAR(64)  DEFAULT NULL,
  updated_by  VARCHAR(64)  DEFAULT NULL,
  created_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3)  DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_code_version (code, version),
  KEY idx_scope (scope_type, scope_id),
  KEY idx_action_status (biz_action, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C1 审批流程定义（R08/R19）';

-- C2. ext_approval_node 审批节点定义 --------------------------------------------------------
-- 基础版三节点：submit(发起) → review(审核校对) → publish(自动执行)；
-- assignee_rule 例：{"type":"space_admin","fallback":"role:super_admin"}
--   type ∈ role|user|dept_leader|space_admin|initiator_leader|initiator|system
--   规则解析为空 → fallback 兜底（升级到管理员而非卡死，HLD 4.8.3③）
CREATE TABLE IF NOT EXISTS ext_approval_node (
  id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  flow_id        BIGINT UNSIGNED NOT NULL           COMMENT '→ ext_approval_flow.id',
  node_order     SMALLINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '节点顺序（从 1 起，按序执行）',
  node_type      ENUM('submit','review','publish') NOT NULL DEFAULT 'review'
                                                     COMMENT 'submit=发起 publish=自动执行(系统节点)',
  node_name      VARCHAR(128) NOT NULL,
  assignee_rule  JSON NOT NULL                      COMMENT '审批人规则 {type,value,fallback}',
  countersign    ENUM('any','all') NOT NULL DEFAULT 'any' COMMENT 'any=或签(任一通过) all=会签(全部通过)',
  timeout_hours  SMALLINT UNSIGNED DEFAULT NULL     COMMENT '超时阈值（小时，NULL=不限）',
  timeout_action ENUM('remind','escalate','auto_approve') NOT NULL DEFAULT 'remind'
                                                     COMMENT '超时动作：提醒/升级到fallback/自动通过',
  condition_expr VARCHAR(512) DEFAULT NULL          COMMENT '进入本节点的条件表达式（R19 条件分支）',
  created_by     VARCHAR(64) DEFAULT NULL,
  updated_by     VARCHAR(64) DEFAULT NULL,
  created_at     DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_flow_order (flow_id, node_order),
  KEY idx_flow (flow_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C2 审批节点定义（R08/R19）';

-- C3. ext_approval_instance 审批实例（先审后执状态机核心表）-----------------------------------
-- reject/return 后实例回 DRAFT（发起人修改重报），任务行保留 rejected/returned 状态供审计
CREATE TABLE IF NOT EXISTS ext_approval_instance (
  id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  flow_id           BIGINT UNSIGNED NOT NULL       COMMENT '→ ext_approval_flow.id',
  flow_version      INT UNSIGNED NOT NULL          COMMENT '固化引用的流程定义版本',
  action            ENUM('upload','update','delete','share','config') NOT NULL,
  biz_ref_type      ENUM('document','dataset','space') NOT NULL COMMENT '业务对象类型',
  biz_ref_id        VARCHAR(64) DEFAULT NULL       COMMENT 'update/delete=已有 rag_flow.document.id；upload 为空',
  target_dataset_id VARCHAR(64) DEFAULT NULL       COMMENT '目标知识库 → rag_flow.knowledgebase.id（发布目的地）',
  target_space_id   BIGINT UNSIGNED DEFAULT NULL   COMMENT '目标空间节点 → ext_space.id（流程匹配与权限判定）',
  stash_file_key    VARCHAR(256) DEFAULT NULL      COMMENT '暂存文件对象键（MinIO ext-approval-stash 桶）',
  meta_snapshot     JSON DEFAULT NULL              COMMENT '提交元数据快照（文件名/大小/parser_id/部门/标签/密级）',
  status            ENUM('draft','reviewing','publishing','published','failed','cancelled')
                                                   NOT NULL DEFAULT 'draft' COMMENT '状态机（见域C说明）',
  current_node_id   BIGINT UNSIGNED DEFAULT NULL   COMMENT '当前停留节点 → ext_approval_node.id',
  initiator         VARCHAR(64) NOT NULL           COMMENT '发起人 → rag_flow.user.id',
  version_no        VARCHAR(32) DEFAULT NULL       COMMENT '关联文档版本号（update/delete/rollback 联动域E）',
  published_doc_id  VARCHAR(64) DEFAULT NULL       COMMENT '发布成功后的 document.id（重试幂等与每日对账依据）',
  error_msg         TEXT COMMENT '发布失败原因',
  retry_count       TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '发布重试次数（指数退避，上限 3 后转人工）',
  submitted_at      DATETIME(3) DEFAULT NULL,
  published_at      DATETIME(3) DEFAULT NULL,
  created_by        VARCHAR(64) DEFAULT NULL,
  updated_by        VARCHAR(64) DEFAULT NULL,
  created_at        DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at        DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_flow (flow_id),
  KEY idx_status (status),
  KEY idx_initiator (initiator),
  KEY idx_target (target_dataset_id),
  KEY idx_biz_ref (biz_ref_type, biz_ref_id),
  KEY idx_publish_retry (status, retry_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C3 审批实例（R08/R19 先审后执状态机）';

-- C4. ext_approval_task 审批任务 ------------------------------------------------------------
-- 任务粒度=审批人×节点；version 乐观锁防并发审批（UPDATE ... WHERE id=? AND version=?）；
-- 加签(add_sign)=当前节点动态追加任务；转签(assignee→transferred_to)；委托=受托人代审（查 C5）
CREATE TABLE IF NOT EXISTS ext_approval_task (
  id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  instance_id    BIGINT UNSIGNED NOT NULL            COMMENT '→ ext_approval_instance.id',
  node_id        BIGINT UNSIGNED NOT NULL            COMMENT '→ ext_approval_node.id',
  assignee       VARCHAR(64) NOT NULL                COMMENT '审批人 → rag_flow.user.id（含委托代审受托人）',
  task_type      ENUM('review','countersign','add_sign') NOT NULL DEFAULT 'review'
                                                      COMMENT 'add_sign=加签产生的动态任务（R19）',
  status         ENUM('pending','approved','rejected','returned','transferred','timeout','cancelled')
                                                      NOT NULL DEFAULT 'pending',
  comment        TEXT COMMENT '审批意见（reject/return 必填，应用层校验）',
  acted_at       DATETIME(3) DEFAULT NULL            COMMENT '处理时间',
  due_at         DATETIME(3) DEFAULT NULL            COMMENT '超时截止（节点 timeout_hours 推算）',
  transferred_to VARCHAR(64) DEFAULT NULL            COMMENT '转签目标人 → rag_flow.user.id（R19）',
  version        INT UNSIGNED NOT NULL DEFAULT 0     COMMENT '乐观锁版本号（并发审批冲突控制）',
  created_by     VARCHAR(64) DEFAULT NULL,
  updated_by     VARCHAR(64) DEFAULT NULL,
  created_at     DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at     DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_instance (instance_id),
  KEY idx_assignee_status (assignee, status),
  KEY idx_node (node_id),
  KEY idx_timeout_scan (status, due_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C4 审批任务（乐观锁防并发，R08/R19）';

-- C5. ext_approval_delegate 审批委托设置（R19）---------------------------------------------
-- 生效期内命中待办 → 任务生成时 assignee 直接取受托人（detail 记录委托来源）
CREATE TABLE IF NOT EXISTS ext_approval_delegate (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  delegator     VARCHAR(64) NOT NULL                 COMMENT '委托人 → rag_flow.user.id',
  delegatee     VARCHAR(64) NOT NULL                 COMMENT '受托人 → rag_flow.user.id',
  scope_type    ENUM('all','flow') NOT NULL DEFAULT 'all' COMMENT '全部审批/指定流程',
  scope_flow_id BIGINT UNSIGNED DEFAULT NULL         COMMENT 'scope_type=flow 时 → ext_approval_flow.id',
  start_at      DATETIME(3) NOT NULL,
  end_at        DATETIME(3) NOT NULL,
  reason        VARCHAR(255) DEFAULT NULL,
  status        ENUM('active','expired','revoked') NOT NULL DEFAULT 'active',
  created_by    VARCHAR(64) DEFAULT NULL,
  updated_by    VARCHAR(64) DEFAULT NULL,
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_delegator (delegator, status),
  KEY idx_delegatee (delegatee, status),
  KEY idx_period (start_at, end_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C5 审批委托设置（R19）';

-- C6. ext_notification 站内通知 --------------------------------------------------------------
-- type：todo=待办提醒(即时) timeout=超时提醒(24h/48h) result=审批结果(发起人) failure=发布失败告警(管理员)
CREATE TABLE IF NOT EXISTS ext_notification (
  id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  user_id    VARCHAR(64) NOT NULL                   COMMENT '接收人 → rag_flow.user.id',
  type       ENUM('todo','timeout','result','failure','system') NOT NULL,
  title      VARCHAR(255) NOT NULL,
  content    VARCHAR(1024) DEFAULT NULL,
  ref_type   VARCHAR(32) DEFAULT NULL               COMMENT '跳转对象类型（approval_instance/version...）',
  ref_id     VARCHAR(64) DEFAULT NULL               COMMENT '跳转对象 ID',
  channel    ENUM('inbox','im') NOT NULL DEFAULT 'inbox' COMMENT 'inbox=站内信 im=企业IM推送(钉钉/飞书/企微)',
  is_read    TINYINT(1) NOT NULL DEFAULT 0,
  read_at    DATETIME(3) DEFAULT NULL,
  created_by VARCHAR(64) DEFAULT NULL,
  updated_by VARCHAR(64) DEFAULT NULL,
  created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_user_read (user_id, is_read, created_at),
  KEY idx_type (type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='C6 站内通知（R08/R19）';

-- =====================================================================================
-- 域 D｜搜索历史域（R12 搜索历史 / R13 热词推荐）
-- 热/冷分层：热路径 Redis（用户最近 50 条 ZSet + 热词榜 List）；MySQL 为冷存储与快照留档
-- =====================================================================================

-- D1. ext_search_history 搜索历史（追加只写事件表）-------------------------------------------
-- 写入：经 ext 检索代理 /search/proxy 的检索异步落库（不阻塞检索主链路）；
-- query_norm=trim+lower 归一化（前端去重展示 + R13 热词统计共用）；
-- 保留：默认 90 天滚动清理（ext 服务配置项）；无痕用户（A9.incognito=1）不写入
CREATE TABLE IF NOT EXISTS ext_search_history (
  id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  user_id        VARCHAR(64) NOT NULL               COMMENT '检索人 → rag_flow.user.id',
  query          VARCHAR(512) NOT NULL              COMMENT '原始查询词',
  query_norm     VARCHAR(512) NOT NULL              COMMENT '归一化查询词（trim+lower）',
  scope_snapshot JSON DEFAULT NULL                  COMMENT '检索范围快照（空间/部门过滤条件）',
  hit_count      INT UNSIGNED NOT NULL DEFAULT 0    COMMENT '命中文档数',
  entry          ENUM('proxy','suggest','direct') NOT NULL DEFAULT 'proxy' COMMENT '入口：代理检索/下拉点击/直连',
  created_at     DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  KEY idx_user_time (user_id, created_at DESC),
  KEY idx_norm_time (query_norm, created_at),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='D1 搜索历史（R12，90 天滚动清理）';

-- D3. ext_hotword 热词榜单快照与运营词（R13）-------------------------------------------------
-- score = Σ各窗口频次×衰减因子(1h/1d/3d/7d/30d → 1.0/0.6/0.4/0.25/0.1) ÷ max_score 归一化；
-- 对外输出 = 统计榜(pinned 提权) ∪ 运营手工词(source=manual) − blocked 敏感词；
-- 冷启动：source=corpus（文档标题/标签 TopN 语料模式，日检索量≥200 切换 stats 模式）
CREATE TABLE IF NOT EXISTS ext_hotword (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  word          VARCHAR(128) NOT NULL                COMMENT '热词（同义词归并后的代表词）',
  snapshot_date DATE NOT NULL                        COMMENT '榜单快照日期',
  score         DECIMAL(10,4) NOT NULL DEFAULT 0     COMMENT '加权得分（归一化 0-1）',
  rank_no       SMALLINT UNSIGNED NOT NULL           COMMENT '当日名次（1 起；rank 为 MySQL 8 保留字故更名）',
  window_stats  JSON DEFAULT NULL                    COMMENT '各时间窗频次 {"h1":n,"d1":n,"d3":n,"d7":n,"d30":n}',
  source        ENUM('stats','corpus','manual') NOT NULL DEFAULT 'stats'
                                                     COMMENT '统计模式/语料冷启动/运营手工补词',
  pinned        TINYINT(1) NOT NULL DEFAULT 0        COMMENT '置顶（运营干预，输出时提权）',
  blocked       TINYINT(1) NOT NULL DEFAULT 0        COMMENT '屏蔽（敏感词，不对外输出）',
  created_by    VARCHAR(64) DEFAULT NULL,
  updated_by    VARCHAR(64) DEFAULT NULL,
  created_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at    DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_word_date (word, snapshot_date),
  KEY idx_date_rank (snapshot_date, rank_no),
  KEY idx_pinned (pinned, blocked)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='D3 热词榜单快照与运营词（R13）';

-- =====================================================================================
-- 域 E｜版本管理域（R09 文档版本管理）
-- 面向 dataset 文档（rag_flow.document）；workspace 文件版本直接用原生 file_commit API，不在此域
-- =====================================================================================

-- E1. ext_document_version 文档版本快照（事件表：创建后仅 is_current 标记翻转）-----------------
-- 版本号 v{major}.{minor}：审批发布的内容更新/回滚 major+1；元数据/解析配置调整 minor+1；
-- 快照：源文件全量存 MinIO ext-versions 桶 version/{doc_id}/{version_no}；
--       大文档(>20MB)可选增量策略（chunk 快照 + E2 差异表）；
-- 保留：最近 N=20 个版本 + 全部 is_major=1 重大版本（桶生命周期清理配合）；
-- 回滚：以历史版本源文件发起 update 审批 → 回滚本身也是新版本（不物理删中间版本）
CREATE TABLE IF NOT EXISTS ext_document_version (
  id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  doc_id               VARCHAR(64) NOT NULL          COMMENT '→ rag_flow.document.id',
  dataset_id           VARCHAR(64) NOT NULL          COMMENT '→ rag_flow.knowledgebase.id',
  version_no           VARCHAR(32) NOT NULL          COMMENT '版本号 v{major}.{minor}',
  major                INT UNSIGNED NOT NULL         COMMENT '主版本（内容更新递增）',
  minor                INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '次版本（元数据/配置调整递增）',
  source               ENUM('approval','manual','migration','rollback') NOT NULL
                                                          COMMENT '来源：审批发布/直改/存量迁移/回滚',
  operator             VARCHAR(64) NOT NULL          COMMENT '操作人 → rag_flow.user.id',
  file_object_key      VARCHAR(256) NOT NULL         COMMENT 'MinIO ext-versions 桶对象键',
  file_name            VARCHAR(255) DEFAULT NULL     COMMENT '快照文件名',
  file_hash            VARCHAR(64) DEFAULT NULL      COMMENT '内容哈希（重复内容识别）',
  file_size            BIGINT UNSIGNED DEFAULT NULL  COMMENT '字节数',
  meta_snapshot        JSON DEFAULT NULL             COMMENT '元数据快照（名称/部门/标签/parser配置/密级）',
  chunk_snapshot_key   VARCHAR(256) DEFAULT NULL     COMMENT 'chunk 快照对象键（增量策略用）',
  chunk_count          INT UNSIGNED DEFAULT NULL     COMMENT 'chunk 总数（快照时点）',
  summary              VARCHAR(512) DEFAULT NULL     COMMENT '变更摘要（时间线展示）',
  is_major             TINYINT(1) NOT NULL DEFAULT 0 COMMENT '重大版本（保留策略永久保留）',
  is_current           TINYINT(1) NOT NULL DEFAULT 0 COMMENT '当前生效版本（每 doc 至多一条，见 uk_current_doc）',
  current_doc_key      VARCHAR(64) GENERATED ALWAYS AS (IF(is_current = 1, doc_id, NULL)) STORED
                                                          COMMENT '唯一约束辅助列：保证每 doc 至多一条 current',
  approval_instance_id BIGINT UNSIGNED DEFAULT NULL  COMMENT '来源审批单 → ext_approval_instance.id',
  created_at           DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  updated_at           DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_doc_version (doc_id, version_no),
  UNIQUE KEY uk_current_doc (current_doc_key),
  KEY idx_doc_order (doc_id, major, minor),
  KEY idx_dataset (dataset_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='E1 文档版本快照（R09）';

-- E2. ext_version_chunk_diff chunk 级版本差异（追加只写事件表）--------------------------------
-- 版本对比视图数据源：两版本间新增/删除/修改的 chunk 清单（前端渲染文本 diff）
CREATE TABLE IF NOT EXISTS ext_version_chunk_diff (
  id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  doc_id          VARCHAR(64) NOT NULL                COMMENT '→ rag_flow.document.id',
  from_version_id BIGINT UNSIGNED NOT NULL            COMMENT '旧版本 → ext_document_version.id',
  to_version_id   BIGINT UNSIGNED NOT NULL            COMMENT '新版本 → ext_document_version.id',
  chunk_id        VARCHAR(64) NOT NULL                COMMENT 'RAGFlow chunk ID（原生 chunks API 拉取）',
  change_type     ENUM('added','removed','modified') NOT NULL,
  chunk_hash      VARCHAR(64) DEFAULT NULL            COMMENT 'chunk 内容哈希（快速比对）',
  content_preview VARCHAR(1024) DEFAULT NULL          COMMENT '内容预览（diff 视图用，超长截断）',
  created_at      DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uk_diff_chunk (from_version_id, to_version_id, chunk_id),
  KEY idx_doc (doc_id),
  KEY idx_to_version (to_version_id),
  KEY idx_change (change_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='E2 chunk 级版本差异（R09）';

-- =====================================================================================
-- 附注：① 库内/跨库均不建外键（应用层校验 + 每日对账任务）；
--       ② 生产建表由 Alembic 基线迁移执行（等价本 DDL），演进规则见《扩展数据库设计文档》§11；
--       ③ 配套种子数据见 ragflow_ext_seed.sql。 —— 评审版结束
-- =====================================================================================
