// 批次112 契约守卫：网站页页头那三个数是「读到几个」还是「这一轮没读到」，必须是两句话。
//
// 原来页头直接写模板串 `共 ${sites.length} 个域名 · ${claimedCount} 个已纳管 · ${certs.length} 张证书`：
// 接口失败时 certs / sites 都被清成空数组，页头于是画「共 0 个域名 · 0 个已纳管 · 0 张证书」——
// 和「这台机器一个站点一张证书也没有」同形，而同页面下方（批次98）已经用 confirmable 区分过这两件事。
// 归属那张标签同样：清单读不到时数组为空，走到 `!sites.value.length` 那档写「没有站点」。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'views', 'Sites.vue'), 'utf8');

test('页头的三个数走同一条三态判据，不再裸拼模板串', () => {
  assert.match(SRC, /:sub="headerSub"/, '页头读 computed，不在模板里重推');
  assert.ok(!SRC.includes('${certs.length} 张证书。'), '模板里不再有裸的证书计数');
  assert.ok(!SRC.includes('${sites.length} 个域名'), '模板里不再有裸的域名计数');
  assert.match(SRC, /loadingSites\.value \|\| sitesReason\.value/, '域名侧：读不到与没读都算没读到');
  assert.match(SRC, /certsInfo\.value\.confirmable !== true/, '证书侧沿用批次98 的 confirmable 判据');
  assert.ok(!SRC.includes("certsInfo.source === 'none'"), '不许在页面里按 source 重推一遍');
});

test('读不到的那一句带上接口给的原因', () => {
  assert.match(SRC, /unknownNote\('域名清单', sitesReason\.value \?\? undefined\)/);
  assert.match(SRC, /unknownNote\('证书数量', certsInfo\.value\.reason \?\? undefined\)/);
  assert.match(SRC, /import \{ NButton, unknownNote \} from '\.\.\/ui'/, '原因那句话用共享的 unknownNote');
  assert.ok(!SRC.includes('未读到'), '页面不另写一份第三态短名');
});

test('归属标签：清单读不到不许说成「没有站点」', () => {
  const tone = SRC.slice(SRC.indexOf('const claimTone = computed'), SRC.indexOf('const siteCountText'));
  assert.ok(
    tone.indexOf('sitesReason.value') < tone.indexOf("'没有站点'"),
    '失败分支必须排在「没有站点」之前，否则读不到仍是绿灯空表');
  assert.ok(tone.indexOf('loadingSites.value') < tone.indexOf("'没有站点'"), '加载中也不许先报「没有站点」');
  assert.match(tone, /unknownNote\('归属清单', sitesReason\.value\)/);
});
