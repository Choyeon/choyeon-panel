// useCachedFetch 的行为回归测试：`npm run test:unit`（node --test）。
//
// 这组用例钉的是"写完之后界面到底更不更新"。批次㊸在线上浏览器里撞到：备份页新建异地目标，
// 接口 200 返回了那一行，卡片却还写着「还没有异地目标」，点刷新也没用——组件被绑在一条
// 已经 dead 的缓存条目上，响应回来了既不写 store 也不画画面。六个视图都是
// 「invalidateCache(自己的 key) → refresh()」这套写法，所以这一条必须用真代码测住，
// 不能只靠构建通过。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { effectScope, ref } from 'vue';
import { useCachedFetch, invalidateCache, cachedKeys, clearCache } from '../src/composables/useCachedFetch.ts';

/** 在活动作用域里挂一个订阅者，返回它的响应式出口与调用次数 */
function mount(loader, keyGetter, extra = {}) {
  const scope = effectScope();
  let api = null;
  let calls = 0;
  scope.run(() => {
    api = useCachedFetch({
      key: keyGetter,
      loader: async (k) => { calls++; return loader(k); },
      maxAge: extra.maxAge ?? 60000,
      // 默认不由挂载自动取数：用例要自己控制第 1 次与第 2 次请求，否则"打了几个接口"
      // 取决于 immediate 那一次有没有先落地，测的是时序而不是行为。
      immediate: extra.immediate ?? false,
    });
  });
  return {
    scope,
    get calls() { return calls; },
    get data() { return api.data.value; },
    get error() { return api.error.value; },
    get loading() { return api.loading.value; },
    get refreshing() { return api.refreshing.value; },
    refresh: api.refresh,
    load: api.load,
    invalidate: api.invalidate,
  };
}

test('invalidate 自己的 key 之后，refresh() 必须真的把新数据画上来', async () => {
  clearCache();
  const key = 't:stale-after-write';
  let payload = ['row-a'];
  const v = mount(async () => payload, () => key);
  await v.refresh();
  assert.deepEqual(v.data, ['row-a'], '首帧要有数据');

  payload = ['row-a', 'row-b'];
  invalidateCache(key);
  await v.refresh();
  assert.deepEqual(v.data, ['row-a', 'row-b'], '写完操作后界面必须出现新行');

  // 第二次写同样要走：只修一次的话，第二条起又会绑回死条目
  payload = ['row-a', 'row-b', 'row-c'];
  invalidateCache(key);
  await v.refresh();
  assert.deepEqual(v.data, ['row-a', 'row-b', 'row-c'], '连续两次写都要更新');
  v.scope.stop();
});

test('跨页失效（前缀命中）之后同样能回源', async () => {
  clearCache();
  const key = 'files:/root/www';
  let payload = { entries: ['a.txt'] };
  const v = mount(async () => payload, () => key);
  await v.refresh();
  payload = { entries: ['a.txt', 'b.txt'] };
  assert.ok(invalidateCache('files:') >= 1, '前缀失效必须真删到条目');
  await v.refresh();
  assert.deepEqual(v.data.entries, ['a.txt', 'b.txt']);
  v.scope.stop();
});

test('maxAge 之内不重新打接口，且立刻用缓存画画面', async () => {
  clearCache();
  const key = 't:swr-fresh';
  let payload = 1;
  const v = mount(async () => payload, () => key, { maxAge: 60000 });
  await v.refresh();
  assert.equal(v.calls, 1);
  payload = 2;
  const again = await v.load();
  assert.equal(again, 1, '没过期就沿用缓存值');
  assert.equal(v.calls, 1, '过期之前一次都不许多打接口');
  assert.equal(cachedKeys().includes(key), true);
  v.scope.stop();
});

test('key 变了以后，慢的旧响应不许盖回新画面', async () => {
  clearCache();
  // 必须是真响应式来源：key 写成普通 let 的话 computed 追不到，watch 根本不会触发，
  // 测出来的"通过"是假的（第一版就是这么写，结果 A 覆盖了 B 却没人报错）。
  const key = ref('t:slow-a');
  const delay = (ms, v) => new Promise((r) => setTimeout(() => r(v), ms));
  const scope = effectScope();
  let api = null;
  scope.run(() => {
    api = useCachedFetch({
      key: () => key.value,
      // A 慢、B 快：旧 key 的响应一定会后到，这正是"画面停在先点的那一页"的现场
      loader: async (k) => (k === 't:slow-a' ? delay(60, 'A') : delay(5, 'B')),
      maxAge: 60000,
      immediate: false,
    });
  });
  const first = api.load();
  await new Promise((r) => setTimeout(r, 0));
  key.value = 't:slow-b';
  await new Promise((r) => setTimeout(r, 0));   // currentKey 的 watch 重新 bind + load
  assert.equal(await api.load(), 'B');
  await first;
  await new Promise((r) => setTimeout(r, 40));  // 让 A 的 then 链彻底跑完
  assert.equal(api.data.value, 'B', '旧 key 的后到响应不能覆盖当前画面');
  scope.stop();
});

test('失败先报错，恢复之后强制刷新要把画面救回来', async () => {
  clearCache();
  const key = 't:err';
  let boom = true;
  const v = mount(async () => { if (boom) throw new Error('500'); return 'ok'; }, () => key, { maxAge: 60000, immediate: false });
  await assert.rejects(v.refresh());
  assert.match(String(v.error), /500/);
  assert.equal(v.loading, false, '失败之后不能一直转圈');
  boom = false;
  await v.refresh();
  assert.equal(v.data, 'ok', '强制刷新成功后画面恢复');
  assert.equal(v.error, null, '成功了就不该留着旧错误');
  v.scope.stop();
});
