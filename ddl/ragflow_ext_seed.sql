-- =====================================================================================
-- RAGFlow 二开扩展库种子数据（评审版 V1.0）
-- 前置 : 先执行 ragflow_ext_schema.sql
-- 幂等 : 全部使用 INSERT ... ON DUPLICATE KEY UPDATE，可重复执行
-- 内容 : ① 内置角色 ② 默认审批流模板（三节点 × upload/update/delete）
--        ③ 空间类型默认配置 ④ R17 路由默认隐藏规则
-- 注意 : 生产首启由 ragflow-ext 服务 bootstrap 幂等执行本脚本等价逻辑
-- =====================================================================================

-- ① 内置角色（is_system=1 的两条禁止删除；permissions 为功能权限点集合）
INSERT INTO ext_role (code, name, description, permissions, is_system, created_by) VALUES
('super_admin', '超级管理员', '系统内置：权限判定无条件放行；持有全部功能权限点', '["*"]', 1, 'system'),
('space_admin', '空间管理员', '系统内置：空间范围内管理空间/知识库/成员/审批', '["space:read","space:edit","space:manage","kb:read","kb:edit","kb:manage","doc:read","doc:edit","approval:approve","approval:config","share:submit","version:rollback:submit"]', 1, 'system'),
('knowledge_contributor', '知识贡献者', '上传/编辑文档（经审批），不可管理空间与成员', '["space:read","kb:read","doc:read","doc:edit","share:submit"]', 0, 'system'),
('knowledge_viewer', '知识只读用户', '仅检索与阅读', '["space:read","kb:read","doc:read"]', 0, 'system')
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';

-- ② 默认审批流模板（基础版三节点：发起 → 空间管理员审核(48h,或签) → 自动发布）
INSERT INTO ext_approval_flow (code, name, biz_action, scope_type, scope_id, version, status, is_default, description, created_by) VALUES
('default_doc_upload', '文档上传默认审批流', 'upload', 'global', '*', 1, 'active', 1, '基础版内置三节点模板', 'system'),
('default_doc_update', '文档更新默认审批流', 'update', 'global', '*', 1, 'active', 1, '基础版内置三节点模板：发布前自动生成旧版本快照', 'system'),
('default_doc_delete', '文档删除默认审批流', 'delete', 'global', '*', 1, 'active', 1, '基础版内置三节点模板：删除前自动生成最终版本快照', 'system')
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';

-- ②配套节点（按 flow code 关联，避免依赖自增 ID；delete 流程审核节点演示会签 all）
INSERT INTO ext_approval_node (flow_id, node_order, node_type, node_name, assignee_rule, countersign, timeout_hours, timeout_action, created_by)
SELECT f.id, n.node_order, n.node_type, n.node_name, n.assignee_rule, n.countersign, n.timeout_hours, n.timeout_action, 'system'
FROM (
  SELECT 'default_doc_upload' AS flow_code, 1 AS node_order, 'submit'  AS node_type, '发起提交' AS node_name, '{"type":"initiator"}' AS assignee_rule, 'any' AS countersign, NULL AS timeout_hours, 'remind' AS timeout_action
  UNION ALL SELECT 'default_doc_upload', 2, 'review',  '知识审核', '{"type":"space_admin","fallback":"role:super_admin"}', 'any', 48, 'escalate'
  UNION ALL SELECT 'default_doc_upload', 3, 'publish', '自动发布', '{"type":"system"}', 'any', NULL, 'remind'
  UNION ALL SELECT 'default_doc_update', 1, 'submit',  '发起提交', '{"type":"initiator"}', 'any', NULL, 'remind'
  UNION ALL SELECT 'default_doc_update', 2, 'review',  '知识审核', '{"type":"space_admin","fallback":"role:super_admin"}', 'any', 48, 'escalate'
  UNION ALL SELECT 'default_doc_update', 3, 'publish', '自动发布', '{"type":"system"}', 'any', NULL, 'remind'
  UNION ALL SELECT 'default_doc_delete', 1, 'submit',  '发起提交', '{"type":"initiator"}', 'any', NULL, 'remind'
  UNION ALL SELECT 'default_doc_delete', 2, 'review',  '知识审核', '{"type":"space_admin","fallback":"role:super_admin"}', 'all', 72, 'escalate'
  UNION ALL SELECT 'default_doc_delete', 3, 'publish', '自动发布', '{"type":"system"}', 'any', NULL, 'remind'
) n
JOIN ext_approval_flow f ON f.code = n.flow_code AND f.version = 1
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';

-- ③ 空间类型默认配置（default_parser_id 取值见 ParserType 枚举白名单）
INSERT INTO ext_space_type_config (type_code, type_name, icon, default_parser_id, allow_children, max_depth, extra_config, created_by) VALUES
('group',       '知识分组', 'folder',    NULL,    1, 10, '{"color":"#1677ff"}', 'system'),
('kb',          '知识库',   'database',  'naive', 0, 10, '{}', 'system'),
('space_group', '部门空间', 'apartment', NULL,    1, 10, '{"permissionTemplate":"dept_inherit"}', 'system')
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';

-- ④ R17 路由默认规则：/files（前端文件管理页，Routes.Files）默认隐藏；超管保留入口
INSERT INTO ext_route_permission (role_id, route_key, visible, created_by)
SELECT 0, '/files', 0, 'system'
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';

INSERT INTO ext_route_permission (role_id, route_key, visible, created_by)
SELECT r.id, '/files', 1, 'system' FROM ext_role r WHERE r.code = 'super_admin'
ON DUPLICATE KEY UPDATE updated_at = CURRENT_TIMESTAMP(3), updated_by = 'system';
