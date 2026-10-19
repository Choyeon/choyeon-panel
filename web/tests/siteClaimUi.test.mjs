// 「站点纳管」这一段（网站页）的源码级守卫：`npm run test:unit`（node --test）。
//
// 批次57B 把归属这件事从「按端口猜，猜不出就一句『未纳管的手工配置』」换成
// 「登记表说了算，判定只是建议，判不出的写明理由」。钉住的都是改坏了照样能构建、
// 上线才露馅的那类：
// 1) 三个纳管动词只碰登记表。页面里出现了 nginx 写命令，就等于把「纳管」和「套用」
//    混成同一件事——前者不影响流量，后者会 reload。
// 2) 「已登记」「建议」「判不出」是三种不同的话，必须各有出处：分别读 claim_note、
//    suggest_note、suggest_reason。折成一句空话，用户就没法逐行质疑归属。
// 3) 判不出的行不给「纳管」按钮，只给「指定应用」。批量「全部纳管」也不带 app_id——
//    面板不许替管理员猜一个应用。
// 4) 写操作只在 admin 下出现；每个动作先抢自己那一类锁再发请求，且锁的键带域名。
//    锁按对象分两把：nginx 写类（套用/移除/续期）与登记表类（纳管系）不碰同一个东西，
//    合成一把时一次 certbot renew（620s）会把纳管按钮锁死十分钟。
// 5) 端口差额、标签组合都由后端给，页面不做减法、不数标签。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Sites.vue');
const src = readFileSync(SRC, 'utf8');
const start = src.indexOf('站点纳管 */');
const end = src.indexOf('function chips(r: any)');
assert.ok(start > 0 && end > start, '站点纳管那一段必须还在 script 里');
const claim = src.slice(start, end);

test('纳管三个动词只写登记表，页面上没有 nginx 的写路径', () => {
  assert.match(src, /req\('\/sites\/claim', \{ body: \{ domain, app_id/);
  assert.match(src, /req\('\/sites\/unclaim'/);
  assert.match(src, /req\('\/sites\/claim-all'/);
  for (const banned of ['nginx -t', 'nginx -s reload', 'systemctl reload', 'render_vhost', 'certbot']) {
    assert.ok(!claim.includes(banned), `纳管这一段出现了 ${banned}——它只该写登记表，碰 nginx 的是「套用」「移除」`);
  }
  assert.match(src, /不写 nginx、不 reload/, '要有一句明说纳管不动线上配置');
});

test('批量纳管不带 app_id：判不出的一个都不猜', () => {
  const line = src.split('\n').find((l) => l.includes("'/sites/claim-all'")) || '';
  assert.ok(line.includes('body: {}'), `claim-all 必须不带任何指定应用：${line}`);
  assert.match(claim, /const res = await api\.claimAll\(\);[\s\S]*?res\.skipped/, '判不出的数量要如实进提示');
  assert.match(claim, /面板不替你猜应用/);
  assert.match(claim, /if \(!got && !miss\) msg\.info\(/, '既没纳管也没跳过时说"都已经登记过"，不说成功纳管 0 个');
});

test('归属的三种来路各说各的话，不折成一句空话', () => {
  assert.match(claim, /if \(r\.claimed\) return r\.claim_note/, '已纳管那行给登记表里的说明');
  assert.match(claim, /if \(r\.suggest_note\) return r\.suggest_note/, '未纳管但判得出的给判定依据');
  assert.match(claim, /return r\.suggest_reason/, '判不出的把"要怎么做才认得出"读出来');
  assert.match(claim, /已登记归属（没有留说明）/, '登记了却没留说明时如实说没有说明，不编一句依据');
  assert.match(claim, /nginx 里查不到这个域名的归属线索/, '三份事实都空时也不能是空字符串');
  assert.match(claim, /if \(r\.claimed\) return r\.app \|\| '登记的应用已不存在'/,
    '登记指向的应用没了就照实说，不替它编一个名字');
  assert.match(claim, /return r\.app \? `建议 \$\{r\.app\}` : '未登记'/, '建议与已登记不许长成同一个样子');
});

test('判不出的行只给「指定应用」，判得出的才给「纳管」', () => {
  assert.match(src, /isAdmin\.value && !r\.claimed && r\.suggest_kind/, '有判定才谈得上"按判定纳管"');
  assert.match(src, /isAdmin\.value && !r\.claimed && !r\.suggest_kind/);
  assert.match(src, /onClick: \(\) => openAssign\(r\)/);
  assert.match(src, /default: \(\) => '指定应用'/);
  assert.match(claim, /async function claimOne\(r: any, appId\?: number \| null\)/);
  assert.match(claim, /assignId\.value \|\| regBusyAny\(\)\) return/, '没选应用不许提交');
  assert.match(claim, /if \(await claimOne\(assignRow\.value, assignId\.value\)\) showAssign\.value = false/,
    '失败时浮层留着让人改选择，成功才关');
});

test('写操作锁 admin，且一次只有一个在飞、键带域名', () => {
  const triggers = [
    /isAdmin\.value && !r\.claimed && r\.suggest_kind/,
    /isAdmin\.value && !r\.claimed && !r\.suggest_kind/,
    /isAdmin\.value && r\.claimed/,
    /<NPopconfirm v-if="isAdmin" @positive-click="claimAllSites">/,
  ];
  for (const t of triggers) assert.match(src, t, '少一个 isAdmin 判断就是让 viewer 角色看见写按钮');
  const bodyOf = (name) => {
    const i = src.indexOf(`async function ${name}`);
    assert.ok(i > 0, `${name} 要在`);
    return src.slice(i, src.indexOf('\n}', i) + 2);
  };
  for (const fn of ['claimOne', 'unclaimOne', 'claimAllSites']) {
    const body = bodyOf(fn);
    assert.ok(body.includes('regBusyAny()'), `${fn} 没先抢登记表那把锁：连点两次会往同一行登记写两次`);
    assert.ok(body.includes('regBusy.value = { key:'), `${fn} 没有 regBusy 认领`);
    assert.ok(body.includes('regBusy.value = null;'), 'finally 里要释放，否则整页按钮永久 disabled');
    assert.ok(body.includes('msg.error(e.message)'), `${fn} 失败要说出来，不能静默`);
    if (fn !== 'claimAllSites') {
      assert.ok(body.includes('${r.domain}'), `${fn} 的锁键必须带域名，不然 A 行的浮层会锁住 B 行`);
    }
  }
  assert.match(claim, /await loadSites\(\)/, '写完就重读列表：登记表是唯一出处，页面不自己改内存里那一行');
});

test('纳管与 nginx 写类分持两把锁：一次十分钟的续期不许锁死登记', () => {
  // 这条锁的就是"合成一把 busy"的那个退化：renew 走服务端 _nginx_section()，前端超时给到 620s，
  // 如果纳管也挤在同一格里，那十分钟里登记表改不动，页面表现成"点了没反应"。
  const slots = /const busy = ref<\{ key: string; verb: string \} \| null>\(null\);\nconst regBusy = ref<\{ key: string; verb: string \} \| null>\(null\);/.exec(src);
  assert.ok(slots, 'busy 与 regBusy 必须是两个槽，不是一个');
  const claimSeg = src.slice(src.indexOf('站点纳管 */'), src.indexOf('function chips(r: any)'));
  assert.ok(!/busy\.value = \{/.test(claimSeg.replace(/regBusy\.value = \{/g, '')),
    '纳管那一段不许往 nginx 写类那一格里放锁');
  for (const verb of ['apply', 'remove', 'renew']) {
    assert.ok(src.includes(`verb: '${verb}'`), `nginx 写类动作 ${verb} 要在 busy 槽里`);
  }
  assert.ok(!src.includes("regBusy.value = { key: `cert-"), '续期占的是 nginx 那一格，不是登记那一格');
});

test('取消纳管不许把「本就没有登记」粉饰成成功', () => {
  assert.match(claim, /msg\[res\.removed \? 'success' : 'warning'\]/);
  assert.match(claim, /本就没有登记/);
  assert.match(src, /只删除 \$\{r\.domain\} 这一行登记/, '要说破"nginx 一个字节都不动"');
});

test('端口差额与标签组合由后端给，页面不做减法', () => {
  assert.match(claim, /if \(r\.port_mismatch\)/, '不等这件事由 site_claim_service 判，页面只翻译');
  assert.match(claim, /`端口不符 \$\{r\.port\}≠登记 \$\{r\.app_port\}`/);
  assert.ok(!/r\.port !== r\.app_port/.test(claim), '页面自己比端口就是第二个出处');
  assert.match(claim, /r\.claim_source === 'manual'/);
  assert.match(claim, /r\.claim_stale/);
});

test('窄屏契约：归属列走 minWidth 弹性、标签整项换行', () => {
  const col = /title: '归属',[\s\S]*?\n  \},/.exec(src)?.[0] || '';
  assert.ok(col.includes('minWidth'), '归属列写死 width 会在 375px 上把正文挤成竖排');
  assert.ok(!/wrap:\s*false/.test(col), '标签组合数不固定，nowrap 会把最先被压扁的是尾部标签');
  assert.match(col, /h\(NSpace, \{ size: 4 \}, \(\) => claimTags\(r\)\)/);
  assert.match(src, /h\('div', \{ class: 'cell-sub cp-prose', title: why \}, why\)/,
    '依据这一长句必须折行可见，不能只挂在 hover 上');
});

test('「全部纳管」要确认、要禁用，图标在共享清单里', () => {
  assert.match(src, /:disabled="regBusyAny\(\) \|\| !unclaimedCount"/, '没有待纳管域名时不给可点的批量按钮');
  assert.match(src, /:loading="onKey\('claim-all', 'claim-all'\)"/);
  const at = src.indexOf('全部纳管</NButton>');
  assert.ok(at > 0, '全部纳管按钮要在');
  const name = /:icon="ico\('(\w+)'\)"/.exec(src.slice(at - 500, at));
  assert.ok(name, '按钮要带图标');
  const iconSet = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'icons.ts'), 'utf8');
  assert.ok(iconSet.includes(`${name[1]},`), `图标 ${name[1]} 不在 icons.ts 的共享清单里，构建绿但格子是空的`);
});
