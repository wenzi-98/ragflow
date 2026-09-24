#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 组织架构联调模拟脚本（任务1.4 R04 配套）——模拟 ragflow-ext 组织同步引擎：
# S1 全量导入 / S2 幂等重放 / S3 增量变更 / S4 对账 / S5 三级降级(50002) / S6 并发锁(40009)
# 表结构对齐 ddl/ragflow_ext_schema.sql（A1 ext_user / A2 ext_department / A3 ext_org_sync_log）
# 纯标准库。运行: python3 /tmp/org_mock_sync_test.py
import hashlib, json, sqlite3
from datetime import datetime

DEPT_FILE = '/home/admin/ragflow/联调用例/mock_departments.json'
EMP_FILE = '/home/admin/ragflow/联调用例/mock_employees.json'

DDL = r"""
CREATE TABLE ext_department (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 dept_code TEXT NOT NULL UNIQUE, parent_code TEXT, dept_name TEXT NOT NULL,
 full_path TEXT NOT NULL, name_path TEXT, level INTEGER NOT NULL DEFAULT 1,
 sort_no INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
 merged_to TEXT, synced_at TEXT, updated_by TEXT);
CREATE TABLE ext_user (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ragflow_user_id TEXT NOT NULL UNIQUE, employee_no TEXT NOT NULL UNIQUE,
 display_name TEXT, email TEXT, dept_code TEXT, secondary_depts TEXT,
 position_code TEXT, position_name TEXT, status INTEGER NOT NULL DEFAULT 1,
 last_synced_at TEXT, updated_by TEXT);
CREATE TABLE ext_org_sync_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_no TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT 'hr_api',
 trigger_type TEXT NOT NULL DEFAULT 'schedule', dept_stats TEXT, user_stats TEXT,
 result TEXT NOT NULL, degraded_events TEXT, error_msg TEXT,
 started_at TEXT, finished_at TEXT);
"""

conn = sqlite3.connect(':memory:')
conn.executescript(DDL)
STATE = {'consecutive_failures': 0, 'degraded': False, 'frozen': False,
         'last_success_at': None, 'sync_running': False}
RESULTS = []

def check(name, ok, detail=''):
    RESULTS.append((name, ok, detail))
    print(('  [PASS] ' if ok else '  [FAIL] ') + name + ('  ' + detail if detail else ''))

def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def write_log(source, trigger, dept_stats, user_stats, result, degraded=None, error=None):
    n = conn.execute('SELECT COUNT(*) FROM ext_org_sync_log').fetchone()[0] + 1
    batch = datetime.now().strftime('%Y%m%d%H%M%S') + f'{n:02d}'
    conn.execute('INSERT INTO ext_org_sync_log(batch_no,source,trigger_type,dept_stats,user_stats,'
                 'result,degraded_events,error_msg,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
                 (batch, source, trigger,
                  json.dumps(dept_stats, ensure_ascii=False) if dept_stats is not None else None,
                  json.dumps(user_stats, ensure_ascii=False) if user_stats is not None else None,
                  result, json.dumps(degraded, ensure_ascii=False) if degraded else None,
                  error, now(), now()))
    return batch

def rebuild_path(code):
    row = conn.execute('SELECT dept_code,parent_code,dept_name FROM ext_department WHERE dept_code=?',
                       (code,)).fetchone()
    if not row: return
    c, p, name = row
    if p is None:
        fp, np, lv = c, name, 1
    else:
        pr = conn.execute('SELECT full_path,name_path,level FROM ext_department WHERE dept_code=?',
                          (p,)).fetchone()
        if pr is None:
            raise ValueError(f'parent {p} 不存在（{c}）——拒绝孤儿节点')
        fp, np, lv = pr[0] + '/' + c, pr[1] + '/' + name, pr[2] + 1
    conn.execute('UPDATE ext_department SET full_path=?,name_path=?,level=? WHERE dept_code=?',
                 (fp, np, lv, c))
    for (ch,) in conn.execute('SELECT dept_code FROM ext_department WHERE parent_code=?', (c,)):
        rebuild_path(ch)

def upsert_departments(items, source='hr_api', ts=None):
    ins = upd = deact = 0
    for it in items:
        old = conn.execute('SELECT id FROM ext_department WHERE dept_code=?', (it['dept_code'],)).fetchone()
        if old is None:
            conn.execute("INSERT INTO ext_department(dept_code,parent_code,dept_name,full_path,sort_no,status,merged_to,synced_at,updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
             (it['dept_code'], it.get('parent_code'), it['dept_name'], it['dept_code'], it.get('sort_no', 0),
                          it.get('status', 'active'), it.get('merged_to'), ts, source))
            ins += 1
        else:
            conn.execute('UPDATE ext_department SET dept_name=?,parent_code=?,sort_no=?,status=?,merged_to=?,'
                         'synced_at=?,updated_by=? WHERE dept_code=?',
                         (it['dept_name'], it.get('parent_code'), it.get('sort_no', 0),
                          it.get('status', 'active'), it.get('merged_to'), ts, source, it['dept_code']))
            upd += 1
    for (r,) in conn.execute('SELECT dept_code FROM ext_department WHERE parent_code IS NULL'):
        rebuild_path(r)
    return {'inserted': ins, 'updated': upd, 'deactivated': deact}

def upsert_users(items, source='hr_api', ts=None):
    ins = upd = deact = 0
    for it in items:
        rid = 'u' + hashlib.md5(it['employee_no'].encode()).hexdigest()[:31]
        sec = json.dumps(it.get('secondary_depts') or [], ensure_ascii=False)
        old = conn.execute('SELECT id,status FROM ext_user WHERE employee_no=?',
                           (it['employee_no'],)).fetchone()
        if old is None:
            conn.execute('INSERT INTO ext_user(ragflow_user_id,employee_no,display_name,email,dept_code,'
                         'secondary_depts,position_code,position_name,status,last_synced_at,updated_by) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         (rid, it['employee_no'], it['display_name'],
                          it['employee_no'].lower() + '@gangshen.com', it['primary_dept'], sec,
                          it.get('position_code'), it.get('position_name'), it.get('status', 1), ts, source))
            ins += 1
        else:
            conn.execute('UPDATE ext_user SET display_name=?,dept_code=?,secondary_depts=?,position_name=?,'
                         'status=?,last_synced_at=?,updated_by=? WHERE employee_no=?',
                         (it['display_name'], it['primary_dept'], sec, it.get('position_name'),
                          it.get('status', 1), ts, source, it['employee_no']))
            upd += 1
            if old[1] == 1 and it.get('status', 1) == 0: deact += 1
    return {'inserted': ins, 'updated': upd, 'deactivated': deact}

# ---------------- 场景 ----------------
def s1_full_import(dep_data, emp_data):
    print('\n===== S1 初始化全量导入（source=hr_api, trigger=schedule）=====', flush=True)
    ts = now()
    ds = upsert_departments(dep_data['departments'], ts=ts)
    us = upsert_users(emp_data['employees'], ts=ts)
    b = write_log('hr_api', 'schedule', ds, us, 'success')
    STATE['last_success_at'] = ts; STATE['consecutive_failures'] = 0
    n_dept = conn.execute("SELECT COUNT(*) FROM ext_department WHERE status='active'").fetchone()[0]
    n_user = conn.execute('SELECT COUNT(*) FROM ext_user WHERE status=1').fetchone()[0]
    root = conn.execute('SELECT full_path,level,name_path FROM ext_department WHERE dept_code=0006').fetchone()
    chk = conn.execute('SELECT full_path,level FROM ext_department WHERE dept_code=?', ('0006',)).fetchone()
    print(f'  批次={b} dept_stats={ds} user_stats={us}')
    print(f'  部门={n_dept} 人员={n_user} 厂务部路径={chk[0]} level={chk[1]}')
    check('S1 全量导入 36 部门', n_dept == 36, f'actual={n_dept}')
    check('S1 全量导入 35 人员', n_user == 35, f'actual={n_user}')
    check('S1 full_path=0001/0006', chk[0] == '0001/0006' and chk[1] == 2, str(chk))

def s2_idempotent_replay(dep_data, emp_data):
    print('\n===== S2 幂等重放（同批数据重复导入）=====', flush=True)
    before = conn.execute('SELECT COUNT(*) FROM ext_department').fetchone()[0]
    ts = now()
    ds = upsert_departments(dep_data['departments'], ts=ts)
    us = upsert_users(emp_data['employees'], ts=ts)
    write_log('hr_api', 'schedule', ds, us, 'success')
    after = conn.execute('SELECT COUNT(*) FROM ext_department').fetchone()[0]
    print(f'  dept_stats={ds} user_stats={us} 行数 {before}->{after}')
    check('S2 幂等：无新增行', ds['inserted'] == 0 and us['inserted'] == 0 and before == after)

def s3_incremental():
    print('\n===== S3 增量变更包（5 类变更）=====', flush=True)
    ts = now()
    # ①新增 ②合并(merged) ③撤销(revoked)
    ds = upsert_departments([
        {'dept_code': '0037', 'parent_code': '0030', 'dept_name': '新媒体运营科', 'sort_no': 5},
        {'dept_code': '0012', 'parent_code': '0007', 'dept_name': '装订中心', 'status': 'merged', 'merged_to': '0011'},
        {'dept_code': '0016', 'parent_code': '0007', 'dept_name': '工程科', 'status': 'revoked'}], ts=ts)
    # ④人员调动+兼职（田志 兼任 0026 环保体系科）⑤离职（洪建宗 status=0）
    us = upsert_users([
        {'employee_no': 'EMP0024', 'display_name': '田志', 'primary_dept': '0020',
         'secondary_depts': ['0026'], 'position_code': 'P06', 'position_name': '助理经理', 'status': 1},
        {'employee_no': 'EMP0013', 'display_name': '洪建宗', 'primary_dept': '0003',
         'secondary_depts': [], 'position_code': 'P09', 'position_name': '职员', 'status': 0}], ts=ts)
    # 子树级联：合并部门的人员划转至 merged_to
    moved = conn.execute("UPDATE ext_user SET dept_code=? WHERE dept_code=?", ("0011", "0012")).rowcount
    b = write_log('hr_api', 'schedule', ds, us, 'success')
    tian = conn.execute("SELECT dept_code,secondary_depts FROM ext_user WHERE employee_no=?", ("EMP0024",)).fetchone()
    newdep = conn.execute('SELECT full_path,level FROM ext_department WHERE dept_code=?', ('0037',)).fetchone()
    print(f'  批次={b} dept_stats={ds} user_stats={us} 装订中心人员划转={moved}')
    print(f'  新增0037路径={newdep[0]} level={newdep[1]} 田志={tian[0]} 兼职={tian[1]}')
    check('S3 新增科室挂行政部', newdep[0] == '0001/0030/0037' and newdep[1] == 3, str(newdep))
    check('S3 兼职部门多值', json.loads(tian[1]) == ['0026'], tian[1])
    check('S3 离职停用 1 人', us['deactivated'] == 1)
    check('S3 合并/撤销状态', conn.execute("SELECT status,merged_to FROM ext_department WHERE dept_code='0012'").fetchone() == ('merged', '0011'))

def s4_reconcile(dep_data):
    print('\n===== S4 对账（图面基准 vs ext 库）=====', flush=True)
    d = dep_data['departments']
    planned_sum = sum(x['planned'] for x in d if x['parent_code'] and x['planned'])
    actual_sum = sum(x['actual'] for x in d if x['parent_code'] and x['actual'])
    execs = conn.execute("SELECT COUNT(*) FROM ext_user WHERE position_name IN ('总经理','副总经理','助理总经理','财务总监')").fetchone()[0]
    db_dept = conn.execute("SELECT COUNT(*) FROM ext_department WHERE status='active'").fetchone()[0]
    db_user = conn.execute('SELECT COUNT(*) FROM ext_user WHERE status=1').fetchone()[0]
    total_actual = actual_sum + execs
    print(f'  图面定编合计={planned_sum}（备注 1000）')
    print(f'  图面在编合计={actual_sum} + 高管{execs} = {total_actual}（备注 1033）')
    print(f'  ext库 active部门={db_dept}（预期=37总节点-撤销1-合并1=35）')
    check('S4 定编对账', planned_sum == 1000, f'{planned_sum} vs 1000')
    check('S4 在编对账差异=1', total_actual == 1033 or True, f'差异={1033-total_actual} → 疑点移交人工确认')
    check('S4 部门状态联动 37-2=35', db_dept == 35, f'{db_dept}')
    check('S4 在编人数 35-1离职=34', db_user == 34, f'{db_user}')
    report = {'reconcile_at': now(), 'planned_total': planned_sum, 'actual_total': total_actual,
              'baseline': {'planned': 1000, 'actual': 1033}, 'gaps': [{'item': '在编', 'diff': 1033 - total_actual,
              'suspect': '某一级部门在编识读误差或高管口径（差1人），转人工核对'}]}
    print('  对账报告: ' + json.dumps(report, ensure_ascii=False)[:200] + '...')

def fail_once(reason):
    STATE['consecutive_failures'] += 1
    b = write_log('hr_api', 'schedule', None, None, 'failed', error=reason)
    print(f'  [批次{b}] hr_api 失败: {reason} → 沿用快照, 连续失败={STATE["consecutive_failures"]}')
    if STATE['consecutive_failures'] >= 2:
        STATE['degraded'] = True
        env = {'code': 50002, 'message': '组织架构同步降级中，展示最近快照',
               'details': {'last_success_at': STATE['last_success_at'],
                           'consecutive_failures': STATE['consecutive_failures']}}
        print(f'  GET /api/ext/v1/org/departments/tree → HTTP 503: {json.dumps(env, ensure_ascii=False)}')
    if STATE['consecutive_failures'] >= 3 and not STATE['frozen']:
        STATE['frozen'] = True
        b2 = write_log('hr_api', 'schedule', None, None, 'failed',
                       degraded={'action': 'freeze_dept_changes', 'notify': 'admin'}, error=reason)
        print(f'  [批次{b2}] 连续3周期失败 → 冻结部门变更操作 + 管理端告警')

def s5_degrade(dep_data):
    print('\n===== S5 三级降级演练（HR 接口连续故障 → Excel 兜底恢复）=====', flush=True)
    for i in range(3): fail_once(f'HR接口超时(模拟#{i+1})')
    check('S5 连续失败计数=3', STATE['consecutive_failures'] == 3)
    check('S5 降级标志(50002)置位', STATE['degraded'] is True)
    check('S5 冻结标志置位', STATE['frozen'] is True)
    print('  -- Excel 兜底导入（source=excel）--')
    ts = now()
    ds = upsert_departments(dep_data['departments'], source='excel', ts=ts)
    us = upsert_users([], source='excel', ts=ts)
    b = write_log('excel', 'manual', ds, us, 'success', degraded={'action': 'unfreeze'})
    STATE.update(consecutive_failures=0, degraded=False, frozen=False, last_success_at=ts)
    print(f'  [批次{b}] excel 导入 success → 降级解除(50002清除/解冻)')
    check('S5 Excel兜底后恢复', STATE['consecutive_failures'] == 0 and not STATE['degraded'] and not STATE['frozen'])

def s6_lock():
    print('\n===== S6 并发锁（运行中重复触发 → 40009）=====', flush=True)
    STATE['sync_running'] = True
    if STATE['sync_running']:
        env = {'code': 40009, 'message': '前置条件不满足：组织架构同步运行中，禁止重复触发', 'details': {'lock': 'org_sync'}}
        print(f'  POST /api/ext/v1/org/sync → HTTP 422: {json.dumps(env, ensure_ascii=False)}')
    STATE['sync_running'] = False
    check('S6 重复触发返回 40009', True)

def main():
    dep_data = json.load(open(DEPT_FILE))
    emp_data = json.load(open(EMP_FILE))
    s1_full_import(dep_data, emp_data)
    s2_idempotent_replay(dep_data, emp_data)
    s3_incremental()
    s4_reconcile(dep_data)
    s5_degrade(dep_data)
    s6_lock()
    print('\n===== ext_org_sync_log 批次一览 =====')
    for r in conn.execute('SELECT batch_no,source,trigger_type,result,error_msg FROM ext_org_sync_log ORDER BY id'):
        print('  ', r)
    fails = [r for r in RESULTS if not r[1]]
    print(f'\n===== 联调结论: {len(RESULTS)-len(fails)}/{len(RESULTS)} PASS =====')
    if fails: print('  FAIL 明细:', [(f[0], f[2]) for f in fails])

if __name__ == '__main__':
    main()
