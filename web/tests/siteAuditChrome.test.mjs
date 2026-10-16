// 「站点链路对账」卡片（网站页）的源码级守卫：`npm run test:unit`（node --test）。
//
// 钉的是批次㊼B 立下的五条口径，全都是"改坏了照样能构建、上线才在 375px 上露馅"的那类：
// 1) 反代目标一格只有三种答案：有人听 / 没人听 / 判不了。「判不了」绝不能折进「没人听」，
//    也不能折进「有人听」——监听表读不到那一轮，把 5 个目标都标绿等于宣称全站都在服务。
// 2) 三条判据（nginx 配置 / 监听表 / 证书清单）任何一条读不到，必须显出来，而且原因用
//    后端那一句。页面自己编一句"读不到"就掩盖了是 ss 装了没用还是证书目录权限不对。
// 3) 「知道就好」那一档（异地目标、面板外 vhost）默认收起，且那颗切换按钮真的绑上了。
//    批次㊻B 的 375px 走查抓到过一次 `@click` 忘了写：构建、类型检查全绿，点了没反应。
// 4) 处置列只把后端给的只读命令原样交给用户复制。面板不替你 reload nginx、不改 vhost、
//    不签证书——这三件事改错了是全站域名一起不可用，不该由一个看板按钮触发。
// 5) 判词、端口数、覆盖关系都在后端算（site_audit_service 是唯一出处）。页面抄一份 nginx
//    解析或证书剩余天数算法，就是第二个出处，阈值一改两边分叉。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Sites.vue');
const src = readFileSync(SRC, 'utf8');
const start = src.indexOf('站点链路对账 */');
const end = src.indexOf('</script>');
assert.ok(start > 0 && end > start, '站点链路对账那一段必须还在 script 里');
const audit = src.slice(start, end);

test('反代目标三种答案各走各的分支，判不了不折成有人听也不折成没人听', () => {
  const t = /if \(t\.listening === true\)[\s\S]*?if \(t\.listening === false\)[\s\S]*?sa-mark mute/.exec(audit);
  assert.ok(t, 'true / false / 其余 三档顺序判定必须还在，且 fallthrough 落到"判不了"');
  assert.match(audit, /sa-mark ok mono' \}, label\)/, '在听只显示 host:port，不加判词');
  assert.match(audit, /sa-mark err mono' \}, `\$\{label\} 没人听`\)/);
  assert.match(audit, /`\$\{label\} 未确认`\)/, '判不了必须写"未确认"，不能复用"没人听"');
  assert.match(audit, /t\.local \? '监听表这一轮读不到[\s\S]*?不在本机/,
    '两种"判不了"来路不同（本机读不到 vs 目标不在这台机器），tooltip 要分开');
});

test('判据读不到时逐条说破，原因取后端那一句', () => {
  assert.match(audit, /const auditBlind = computed\(\(\) => auditFacts\.value\.filter\(\(f: any\) => !f\.ok\)\)/);
  for (const p of ['ng.reason ||', 'li.reason ||', 'ce.reason ||']) {
    assert.ok(audit.includes(p), `${p} 少了就变成页面自己编读不到的原因`);
  }
  assert.match(src, /v-for="f in auditBlind"[^>]*class="sa-blind-line"/, '读不到的判据要一条条列出来，不是一句"部分不可用"');
  assert.match(audit, /这一轮反代目标全部标未确认，不等于都在听/);
  assert.match(audit, /这一轮覆盖关系判不了/);
  assert.match(src, /相关结论只能标「未确认」而不是「正常」/, '要说破"空/读不到"与"正常"是两回事');
});

test('「知道就好」默认收起，且展开按钮真的绑上', () => {
  assert.match(audit, /const showInfo = ref\(false\)/, '默认收起：一屏黄条会让人把真的 502 当背景噪音');
  assert.match(audit, /showInfo\.value \? auditFindings\.value : auditFindings\.value\.filter\(\(f: any\) => f\.severity !== 'info'\)/);
  assert.match(src, /:data="shownFindings"/, '判词表吃的是过滤后的那份，不是 auditFindings');
  assert.match(src, /@click="showInfo = !showInfo"/, '只写了按钮没写处理函数，构建照样绿');
  assert.match(src, /showInfo \? '收起' : '展开'/);
  assert.match(src, /要点上方那颗按钮才看得见/, '只剩 info 档时，空表要指路而不是说"没有问题"');
});

test('只读：面板不替你 reload nginx、不改 vhost、不签证书', () => {
  assert.match(src, /audit: \(refresh: boolean\) => req\('\/site-audit' \+ \(refresh \? '\?refresh=1' : ''\), \{ timeout: 120000 \}\)/,
    'GET 轮询 + refresh 显式重探；超时必须给到 120s，比后端探测先断会假装成对账失败');
  const apiLine = src.split('\n').find((l) => l.includes("'/site-audit'")) || '';
  assert.ok(!/method:/.test(apiLine), '对账只读：这一行出现 method 就是往生产配置写');
  for (const banned of ['nginx -s reload', 'systemctl reload', 'systemctl restart']) {
    assert.ok(!src.includes(banned), `处置列只能给命令文本，出现了 ${banned} 就是面板替用户动了生产配置`);
  }
  const after = src.slice(src.indexOf('站点链路对账', end));
  const card = after.slice(0, after.indexOf('</NCard>') + 8);
  assert.ok(card.includes('</NCard>'), '卡片区块切片失败，守卫要看的就是这一段');
  const clicks = [...card.matchAll(/@click="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(clicks.length >= 2, '重新对账 + 展开/收起两颗按钮要在');
  for (const c of clicks) {
    assert.ok(/^loadAudit\(.*\)$|^showInfo = !showInfo$/.test(c), `卡片里多了别的点击动作：${c}——这一卡只读，不写`);
  }
  assert.match(src, /h\(CopyButton, \{ text: String\(r\.fix\)/, '复制的内容必须是后端给的 fix 原文，不改写不拼接');
  assert.match(src, /r\.fix \? h\(CopyButton/, '没有 fix 的判词不给复制按钮，避免复制出 undefined');
});

test('事实只有一个出处：页面不解析 nginx、不数端口、不算证书天数', () => {
  for (const banned of ['proxy_pass', 'split(', 'Math.', 'Date.parse', 'new Date', 'ss -ltn']) {
    assert.ok(!audit.includes(banned), `对账段里出现了 ${banned}——判据要在 site_audit_service 里算，页面抄一份就是第二个出处`);
  }
  assert.match(audit, /\$\{li\.ports\} 个端口在听/, '端口数直接用后端给的计数');
  assert.match(audit, /覆盖 \$\{ce\.domains\} 个域名（到期天数以下方证书表为准）/, '天数归证书表，这里不复算');
  assert.match(audit, /const bad = r\.uncovered \|\| \[\]/, '哪些域名不在证书里由后端点名');
  assert.match(audit, /\(r\.domains \|\| \[\]\)\.join/, '域名列表用后端给的那份，页面不 grep nginx');
  assert.match(audit, /const l: number\[\] = r\.listen \|\| \[\]/, '本块监听端口同样来自后端，不是页面去 ss 数');
});

test('进页面读缓存那一轮，手动重探才花探测的钱', () => {
  assert.match(src, /function refresh\(\) \{[\s\S]*?loadAudit\(\);[\s\S]*?\}/, '挂载即读缓存，不在首屏做 120s 探测');
  assert.match(src, /@click="loadAudit\(true\)"/, '只有"重新对账"这颗按钮带 refresh');
  assert.match(src, /:loading="loadingAudit && !auditData"/, '后台刷新不清空已有结论，避免读成"链路没问题"');
  assert.match(audit, /catch \(e: any\) \{\s*auditErr\.value = e\.message/, '失败要留下原因，不许吞掉');
  assert.match(audit, /if \(!auditData\.value\) return \{[^}]*对账失败/, '没有数据时色调不能假装成成功');
});
