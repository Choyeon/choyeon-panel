// 「远端清单看得见、拉得回」（批次56B）的守卫：`npm run test:unit`（node --test）。
//
// 56A 把两个接口做完了（`GET /api/backups/{bid}/cloud-files?target=`、
// `POST /api/backups/{bid}/cloud-pull`），CLI 也接上了，界面却停在连通性测试那句
// 「这个前缀下有 N 个对象」上：机器重装后本机目录是空的，看得见计数而一份都摸不到，
// 恢复又只读本机的文件——看得见才谈得上拉回。这一段钉的都是构建绿、上线才露馅的那类：
// 1) 三种读法各有出处：读不到（接口失败）、前缀下确实没对象、有对象但不属于这条任务。
//    折成一句「没有产物」就是界面在替远端的健康状况说谎（与批次⑰A、㊷B 同一口径）。
// 2) 迟到的一轮列举不许盖进另一条任务的抽屉，忙位只由最新那一轮放下（批次61B 的恢复弹窗）。
// 3) 拉回是写盘操作：一次只允许一份在飞、锁按产物名释放、成败都回源重读，页面不自己插行。
// 4) 界面只读远端、只往本机写：不存在"删桶里对象"的入口，也不覆盖本机同名产物。
// 5) `remote_total` 是"翻到的对象数"而不是"桶里的总数"，翻页触顶必须显式说；
//    读不到修改时间就写未知，不拿"现在"补一个看起来正常的日期。
// 6) 前端超时 ≥ 服务端上限，否则本地 abort 而远端还在写，第二次点就是重复请求。
// 7) 「本机有 / 仅异地」这套词与 CLI 同一条判据，两屏读出来必须是一个名字。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const src = readFileSync(join(HERE, '..', 'src', 'views', 'Backups.vue'), 'utf8');
const apiSrc = readFileSync(join(HERE, '..', 'src', 'api.ts'), 'utf8');
const cliSrc = readFileSync(join(ROOT, 'backend', 'cli.py'), 'utf8');
const svcSrc = readFileSync(join(ROOT, 'backend', 'app', 'services', 'cloud_backup.py'), 'utf8');
const cfgSrc = readFileSync(join(ROOT, 'backend', 'app', 'config.py'), 'utf8');

const start = src.indexOf('---- 远端清单');
const end = src.indexOf('function openTargetCreate()');
assert.ok(start > 0 && end > start, '远端清单那一段必须还在 script 里');
const manifest = src.slice(start, end);
const dStart = src.indexOf('<NDrawer v-if="isAdmin" v-model:show="cfShow"');
const drawer = src.slice(dStart, src.indexOf('</NDrawer>', dStart));
assert.ok(dStart > 0, '远端清单抽屉的模板要在');

function fnBody(name) {
  const i = manifest.indexOf(`async function ${name}`);
  assert.ok(i > 0, `${name} 要在`);
  return manifest.slice(i, manifest.indexOf('\n}\n', i) + 3);
}

test('「读不到」那一支不渲染表格，也不说成没有产物', () => {
  assert.match(manifest, /if \(seq !== cfSeq\) return;[\s\S]{0,200}?cfData\.value = null;\s*cfErr\.value = /,
    '列举失败要把清单清空并留下错误原文');
  assert.match(drawer, /<NAlert v-if="cfErr" type="error"/);
  const dataBranch = drawer.indexOf('<template v-else-if="cfData">');
  const table = drawer.indexOf('<NDataTable');
  assert.ok(dataBranch > 0 && table > dataBranch, '表格只能长在真读到数据的那一支里');
  assert.ok(!drawer.slice(0, dataBranch).includes('<NDataTable'), '错误分支里不许出现表格');
  assert.match(manifest, /「读不到」不是「远端没有」/);
  assert.match(drawer, /这是「读不到」，不是「桶里没有副本」/);
});

test('空清单按前缀下有没有对象分两种说法', () => {
  const body = manifest.slice(manifest.indexOf('const cfEmptyText'), manifest.indexOf('const cfColumns'));
  assert.match(body, /if \(!d\.remote_total\)/, '一个对象都没读到 与 有对象但不归本任务 是两件事');
  assert.match(body, /没有任何对象/);
  assert.match(body, /没有一条属于本任务/);
  assert.match(body, /远端清单还没读到/, '还没读到过的空态也不能是一句空话');
});

test('翻页触顶如实说：读到的是样本，不是桶的总数', () => {
  assert.match(manifest, /已达翻页上限/);
  assert.match(drawer, /v-if="cfData\.truncated"/);
  assert.match(drawer, /看不到某一份产物，不等于远端没有那一份/);
  assert.match(manifest, /const cfLocalGone/, '「远端有、本机一份都没有」要单独点名');
  assert.match(drawer, /远端有 \{\{ cfData\.files\.length \}\} 份，本机一份都没有/);
});

test('迟到的一轮列举不许盖进另一条任务的抽屉', () => {
  assert.match(manifest, /let cfSeq = 0;/);
  assert.match(manifest, /const seq = \+\+cfSeq;/);
  assert.match(manifest, /if \(seq !== cfSeq\) return;/);
  assert.match(manifest, /if \(seq === cfSeq\) cfLoading\.value = false;/,
    '忙位只由最新那一轮放下：旧响应把转圈灯关掉，界面就在说"读完了"而数据还没到');
  // 关抽屉即作废这一轮：不推进 cfSeq，下一次打开会盖上旧清单；不放下两把灯，下一条任务一进来就是灰的
  assert.match(manifest, /cfSeq \+= 1;\s*cfPulling\.value = '';\s*cfLoading\.value = false;/);
  assert.match(manifest, /cfErr\.value = '';\s*try \{/, '重试开始时先放下上一轮的"读不到"');
});

test('拉回：一次只在飞一份，锁按产物名释放，成败都回源', () => {
  const body = fnBody('pullCloud');
  assert.match(body, /if \(cfPulling\.value !== ''\) return;/, '并发拉回会抢同一份落盘预算');
  assert.match(body, /cfPulling\.value = f\.name;/);
  assert.match(body, /if \(cfPulling\.value === f\.name\) cfPulling\.value = '';/,
    '只放自己那一把：迟到响应不许把别人的锁松开');
  assert.match(body, /msg\.error\(`拉回失败：\$\{e\.message\}`\)/, '没拉回来绝不能静默');
  assert.match(body, /void reload\(\)\.catch\(\(\) => undefined\);/, '写完回源：任务行与目标表一起重读');
  assert.ok(!/cfData\.value\.files\.(push|unshift)/.test(manifest),
    '页面不自己往清单里插一行：本机列表与远端列举才是出处');
  assert.match(body, /cfStillMine\(job, target\)/, '提示只在该抽屉还是这条任务时说');
  // restoreable 由服务端按本机列表那同一个判据给，页面不许自己再按扩展名判一次
  assert.match(body, /res\.restoreable/);
  assert.ok(!/endsWith|\.split\('\.'\)/.test(body), '页面自己按扩展名判"能不能恢复"就是第二个口径');
});

test('界面只读远端、只往本机写：没有删桶里对象的入口', () => {
  const both = manifest + drawer;
  for (const bad of ['deleteCloudTarget', 'deleteBackupFile', "method: 'DELETE'", 'AbortController']) {
    assert.ok(!both.includes(bad), `这一段出现了 ${bad}：清单页只负责看见和拉回`);
  }
  assert.match(drawer, /不删桶里的对象/);
  assert.match(drawer, /也不覆盖本机已有的同名产物/);
});

test('远端入口只给 admin，且关联的目标必须还在', () => {
  assert.match(src, /\.\.\.\(isAdmin && r\.cloud_target_id && tgById\.value\[r\.cloud_target_id\]\s*\n\s*\? \[\{ label: '远端清单'/,
    '目标已删除那一行不给"必然列不出东西"的按钮');
  assert.ok(src.includes("label: '远端清单'"), '入口按钮要在操作列里');
  assert.ok(src.includes('<NDrawer v-if="isAdmin" v-model:show="cfShow"'), 'viewer 不该看见这个抽屉');
  assert.match(manifest, /const t = tgById\.value\[r\.cloud_target_id\];\s*if \(!t\) return;/,
    'openCloudFiles 自己也要挡一次，不靠按钮隐藏');
});

test('停用中的目标照样能列能拉，界面把"只影响推送"说清楚', () => {
  assert.match(manifest, /已停用：只影响推送，不影响拉回/);
  assert.match(manifest, /t && !t\.enabled/);
});

test('「本机有 / 仅异地」这套词与 CLI 同一个判据', () => {
  assert.match(cliSrc, /'本机有' if f\['local'\] else '仅异地'/, '终端那侧的叫法是唯一出处');
  assert.match(manifest, /'本机有'/);
  assert.match(manifest, /'仅异地'/);
  // 归属判据在服务端（name_owner），页面不自己比前缀
  assert.match(svcSrc, /if name_owner\(name\) != int\(bid\):/);
  assert.ok(!/startsWith\(`bk/.test(manifest), '页面自己判产物归属就是第二个出处');
});

test('读不到修改时间写「未知」，不拿现在补一个看起来正常的日期', () => {
  const col = /title: '远端时间',[\s\S]*?\n  \},/.exec(manifest)?.[0] || '';
  assert.ok(col.includes("f.modified ? fmtDateTime(f.modified) : '未知'"), col.slice(0, 200));
  assert.ok(!/Date\.now\(\)|new Date\(\)/.test(col), '补一个当前时间就是把"读不到"粉饰成"刚刚"');
});

test('前端超时不低于服务端上限，target 一路必填', () => {
  const pages = Number(/LIST_PAGES = (\d+)/.exec(svcSrc)[1]);
  const probe = Number(/CP_CLOUD_PROBE_TIMEOUT_SEC", (\d+)/.exec(cfgSrc)[1]);
  const push = Number(/CP_CLOUD_PUSH_TIMEOUT_SEC", (\d+)/.exec(cfgSrc)[1]);
  const cfMs = Number(/cloudFiles:[\s\S]*?timeout: (\d+)/.exec(apiSrc)[1]);
  const pullMs = Number(/cloudPull:[\s\S]*?timeout: (\d+)/.exec(apiSrc)[1]);
  assert.ok(cfMs >= pages * probe * 1000, `列举超时 ${cfMs}ms 短于服务端最坏 ${pages * probe}s：本地先 abort，界面留下"读不到"而远端其实好着`);
  assert.ok(pullMs >= push * 1000, `拉回超时 ${pullMs}ms 短于服务端 deadline ${push}s：本地 abort 后盘上可能已经多一份`);
  assert.match(apiSrc, /cloud-files\?target=\$\{targetId\}/, '看哪个桶必须由点进来的人说清楚，不缺省');
  assert.match(apiSrc, /cloud-pull.*body: \{ target: targetId, name \}/);
  assert.match(manifest, /const target = cfTargetId\.value;/);
  assert.match(manifest, /api\.cloudFiles\(job, target\)/);
});

test('窄屏契约：清单里的等宽短标签都带折行触发器（装不下时不许吃掉尾数）', () => {
  const cols = src.slice(src.indexOf('const cfColumns'), src.indexOf('const cfLocalGone'));
  const nowrapLines = cols.split('\n').filter((l) => /class:\s*'[^']*(mono-dim|cell-sub)/.test(l));
  assert.ok(nowrapLines.length >= 5, `清单列的 nowrap 格子少于一处说明这一节被重写，钉子要跟着重做：${nowrapLines.length}`);
  for (const l of nowrapLines) {
    assert.match(
      l,
      /cp-break|cp-prose|title/,
      `这一格只有 nowrap 没有第二个入口：375px 抽屉里 "1023.9 MiB" 这类长档会被 ellipsis 吃掉尾数（触屏没有 hover 读全文）\n${l.trim()}`,
    );
  }
});

// 56B 上线第一天就踩到的：对端 LIST 没带 <Size>（或带了非数字）时，链路上一旦有人
// 写 `int(x or 0)`，"我们没读到大小"就变成了「0 B」——读的人以为桶里躺着一份空对象，
// 而真实情况是这份产物完好、只是头部信息读不到。两种情况处置完全不同，所以每一段都得钉。
test('大小读不到是「未知」而不是「0 B」：抽屉那一格与后端两处读者同源', () => {
  const col = /title: '大小',[\s\S]*?\n  \},/.exec(manifest)?.[0] || '';
  assert.ok(col.includes("f.size === null ? '未知' : fmtBytes(f.size)"), col.slice(0, 240));
  assert.ok(col.includes("'对端未返回大小'"), '未知这一格要说清为什么未知（触屏没有 hover 之外的入口）');
  assert.ok(!/\|\|\s*0/.test(col), '把 null 折成 0 就写成「0 B」，界面在替远端的健康状况说谎');
  // 服务端：LIST 与 HEAD 各一个读者，None 一路留到底，不许中途 int() 成 0
  assert.ok(svcSrc.includes('"size": _size_or_none(c.findtext'), '列举里的大小要经同一个读者');
  assert.ok(svcSrc.includes('"size": _size_or_none(headers.get("content-length"))'),
    'HEAD 读不到 Content-Length 时是 None，不是 0');
  assert.ok(svcSrc.includes('"size": r["size"],'), 'list_for_job 直接透传，不再折一次 0');
  assert.ok(!svcSrc.includes('int(r["size"] or 0)'),
    '这里再折一次 0，前面留的 None 就白留了');
  // CLI 同一个事实同一个说法：`{f['size']:>12}` 会直接让 cloud-list 崩在这份产物上
  const cl = cliSrc.slice(cliSrc.indexOf('cli:backup:cloud-list'),
                          cliSrc.indexOf('cli:backup:cloud-pull'));
  assert.ok(cl.length > 100, 'cloud-list 那一段要在');
  assert.ok(!/\{f\['size'\]:>/.test(cl), '终端那侧不许把 None 直接格式化');
  assert.ok(cl.includes("f['size'] is not None"), 'CLI 的未知要走同一个判据');
});

test('连通性测试的合计：少一条大小就整体报未知，并点名几条没给', () => {
  const probe = src.slice(src.indexOf('async function testTarget'), src.indexOf('probeShow.value = true'));
  assert.ok(probe.includes('res.bytes_under_prefix === null'), '合计有未知这一支');
  assert.ok(probe.includes('个对象对端没给大小'), '要说清是几条读不到，而不是笼统一句未知');
  assert.ok(!/\|\|\s*0/.test(probe), '少一条大小就报一个偏小的合计，读的人以为桶里就这么多');
  // 服务端同一判据：任一对象没有大小 → 整体 None，并单独给个数
  assert.ok(svcSrc.includes('unknown_sizes = sum(1 for r in rows if r["size"] is None)'));
  assert.ok(svcSrc.includes('"bytes_under_prefix": None if unknown_sizes else sum('),
    '合计不许用 0 兜底');
  assert.ok(svcSrc.includes('"size_unknown_objects": unknown_sizes'));
  // CLI 同一个事实同一个说法
  assert.ok(cliSrc.includes('个对象对端没给大小'), '终端那侧也不能把未知报成一个数');
});

test('HEAD 不肯确认字节数时，推送按失败处理而不是记一笔假账', () => {
  const verify = svcSrc.slice(svcSrc.indexOf('if got["size"] != size:'), svcSrc.indexOf('if got["size"] != size:') + 700);
  const noneAt = verify.indexOf('if got["size"] is None:');
  const diffAt = verify.indexOf('不一致');
  assert.ok(noneAt > 0, '「读不到」与「对不上」分成两支：合成一句会让运维以为对端在撒谎');
  assert.ok(diffAt > noneAt, 'None 那一支必须先返回，否则走到"远端大小 None 与本地 N 不一致"这句假话上');
  assert.ok(verify.includes('上传后 HEAD 没给大小'));
});

test('HEAD 静默时余量预检交给 GET，闸门不许整道消失', () => {
  const pull = svcSrc.slice(svcSrc.indexOf('size = head["size"]'));
  const pullBody = pull.slice(0, pull.indexOf('return {"ok": True, "name": name'));
  assert.ok(pullBody.includes('if size is not None:'),
    '拿 0 去预检等于把这道闸假装检过了：读不到时这里判不了');
  assert.ok(pullBody.includes('budget_dir=config.BACKUP_DIR'),
    '判不了不等于不判：把闸门交给 get_object 在第一个字节落盘前补上');
  assert.ok(pullBody.includes('if size is not None and size > MAX_SINGLE_GET'),
    '上限那一道同样不许用 0 去比');
  // get_object 那一侧的补判
  assert.ok(svcSrc.includes('if budget_dir and declared:'), 'GET 里的余量闸要真存在');
  assert.ok(svcSrc.includes('assert_write_budget(budget_dir, declared'), '用的是对端声明的那个数');
});
