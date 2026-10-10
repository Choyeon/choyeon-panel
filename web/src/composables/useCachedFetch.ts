import {
  computed, getCurrentScope, onScopeDispose, ref, shallowRef, toValue, watch,
  type ComputedRef, type MaybeRefOrGetter, type Ref,
} from 'vue';

/**
 * 带 stale-while-revalidate 的模块级缓存。
 * 目的：切走再切回同一页时先用上一次的结果**立刻**渲染（不再出现空白帧），
 * 只有超过 maxAge 才在后台重新拉；请求期间旧数据一直保持在屏幕上。
 *
 * 三条硬约束：
 * - 内存有界：LRU 上限 MAX_ENTRIES，超出即淘汰最久未用的 key；
 * - 同 key 并发去重：一次飞行中的请求被所有调用者共享；
 * - 失效可控：mutation 后用 invalidate(精确 key 或前缀)，别的数据源订阅者能感知。
 */

type CacheEntry = {
  key: string;
  value: unknown;
  at: number;
  err: unknown;
  inflight: Promise<unknown> | null;
  /** 被 invalidate（或 clearCache）后置死：飞行中的响应不能再写回 store。 */
  dead: boolean;
  /** 正在显示这一条的组件数；有订阅者时 LRU 不淘汰它，避免脚下被抽走。 */
  readers: number;
};

const MAX_ENTRIES = 32;
const DEFAULT_MAX_AGE = 20000;
const store = new Map<string, CacheEntry>();

/** Map 的插入顺序就是 LRU 顺序：命中后 delete+set 挪到队尾，队首即最久未用。 */
function touch(e: CacheEntry) {
  store.delete(e.key);
  store.set(e.key, e);
}

function drop(key: string) {
  const e = store.get(key);
  if (!e) return;
  e.dead = true;
  e.inflight?.catch(() => undefined);
  store.delete(key);
}

function evict() {
  let guard = store.size;
  while (store.size > MAX_ENTRIES && guard-- > 0) {
    const oldest = store.keys().next().value as string | undefined;
    if (oldest === undefined) break;
    const e = store.get(oldest);
    if (!e) break;
    if (e.readers > 0) {
      // 仍有页面在用：挪到队尾，下一轮淘汰别人；全部都有读者时停止（上限软约束）
      touch(e);
      const next = store.keys().next().value as string | undefined;
      if (next === undefined || next === oldest) break;
      continue;
    }
    drop(oldest);
  }
}

function getEntry(key: string): CacheEntry {
  const e = store.get(key);
  if (e && !e.dead) {
    touch(e);
    return e;
  }
  const fresh: CacheEntry = { key, value: undefined, at: 0, err: null, inflight: null, dead: false, readers: 0 };
  store.set(key, fresh);
  evict();
  return fresh;
}

/**
 * 精确 key 命中就整条删掉，否则按前缀批量删（`audit:` 一次清掉所有条数档位）。
 * 返回删掉的条数，调用方可据此判断确实清到了。
 */
export function invalidateCache(keyOrPrefix: string): number {
  const exact = store.get(keyOrPrefix);
  if (exact && !exact.dead) {
    drop(keyOrPrefix);
    return 1;
  }
  let n = 0;
  for (const k of [...store.keys()]) {
    if (k.startsWith(keyOrPrefix)) {
      drop(k);
      n++;
    }
  }
  return n;
}

/** 登出 / 401 必须调用：缓存里是上一个账号（可能是管理员）拉到的数据，不能留给下一个会话。 */
export function clearCache() {
  for (const k of [...store.keys()]) drop(k);
}

export function cachedKeys(): string[] {
  return [...store.keys()];
}

/** 探针用：验证 LRU 真的有上界。生产代码不要依赖。 */
export function cacheSize(): number {
  return store.size;
}

async function run<T>(e: CacheEntry, loader: (key: string) => Promise<T>): Promise<T> {
  if (e.inflight) return e.inflight as Promise<T>;
  const p: Promise<T> = loader(e.key).then(
    (v) => {
      if (!e.dead) {
        e.value = v;
        e.err = null;
        e.at = Date.now();
      }
      return v;
    },
    (err) => {
      if (!e.dead) {
        e.err = err;
        // 失败也盖时间戳：否则每次按键过滤都会重打同一个坏接口
        e.at = Date.now();
      }
      throw err;
    },
  ).finally(() => {
    if (e.inflight === p) e.inflight = null;
  });
  e.inflight = p;
  return p;
}

export type CachedFetchOptions<T> = {
  /** 缓存键，可传 ref / getter；变化时自动按 SWR 语义装载 */
  key: MaybeRefOrGetter<string>;
  loader: (key: string) => Promise<T>;
  /** 多久算过期（过期依然先立刻显示，只是后台再验证），默认 20s */
  maxAge?: number;
  /** 默认挂载时装载；设 false 由调用方自己触发 */
  immediate?: boolean;
};

export function useCachedFetch<T = unknown>(opts: CachedFetchOptions<T>) {
  const maxAge = opts.maxAge ?? DEFAULT_MAX_AGE;
  const data = shallowRef<T | undefined>() as Ref<T | undefined>;
  const error = shallowRef<unknown>(null);
  // loading：屏幕上还什么都没有时的首屏加载（表格用它盖遮罩、显示空态）
  const loading = ref(false);
  // refreshing：已有画面在背后的再验证（按钮用它转圈，表格不闪）
  const refreshing = ref(false);
  const busy = computed(() => loading.value || refreshing.value);
  const stale = computed(() => data.value !== undefined && Date.now() - enteredAt() > maxAge);

  const currentKey = computed(() => toValue(opts.key));
  let boundKey = '';
  let boundEntry: CacheEntry | null = null;

  function enteredAt() {
    return boundEntry?.at ?? 0;
  }

  /** 订阅当前 key：LRU 淘汰绕开有人在读的条目 */
  function bind(key: string) {
    if (key === boundKey) return;
    if (boundEntry) {
      boundEntry.readers = Math.max(0, boundEntry.readers - 1);
      boundEntry = null;
    }
    boundKey = key;
    boundEntry = getEntry(key);
    boundEntry.readers++;
  }

  async function load(force = false): Promise<T | undefined> {
    bind(currentKey.value);
    const e = boundEntry as CacheEntry;
    // 先把能画的画上：命中缓存时这一句就是"秒开"的全部机制
    if (!force && e.value !== undefined) {
      data.value = e.value as T;
      error.value = null;
    } else {
      error.value = e.err;
    }
    const fresh = e.at > 0 && Date.now() - e.at < maxAge;
    if (!force && e.value !== undefined && fresh && !e.inflight) {
      loading.value = false;
      refreshing.value = false;
      return e.value as T;
    }
    const painting = e.value !== undefined || data.value !== undefined;
    loading.value = !painting;
    refreshing.value = painting || !!e.inflight;
    try {
      const v = await run(e, opts.loader);
      // 两种情况都不能把这一次的结果写进画面：
      // ①期间被 invalidate（e.dead）；②期间 key 变了（翻页/切目录/改条数），
      //   此时 e 还活着（旧条目没人 invalidate 它），但画面已经属于新 key。
      // 只判 dead 的话，慢的旧响应会后到并把上一个目录/上一页的数据盖回 data，
      // 列表停在先点的那一页，而新请求其实还在飞（下面的 finally 还会提前把
      // loading/refreshing 关掉，转圈消失却没有数据 —— 实测最难复现的就是这一条）。
      if (!e.dead && boundEntry === e) {
        data.value = v;
        error.value = null;
      }
      return v;
    } catch (err) {
      if (boundEntry === e) error.value = err;
      throw err;
    } finally {
      // 只有当前仍在这一次的 key 上，才由它收尾进度标志
      if (boundEntry === e) {
        loading.value = false;
        refreshing.value = false;
      }
    }
  }

  /** 强制回源：用户点刷新、或本页刚做完写操作 */
  function refresh() {
    return load(true);
  }

  /** 只失效本页这一条；跨页失效用 invalidateCache(前缀) */
  function invalidate() {
    const key = boundKey;
    boundKey = '';
    if (boundEntry) {
      boundEntry.readers = Math.max(0, boundEntry.readers - 1);
      boundEntry = null;
    }
    return invalidateCache(key);
  }

  const stop = watch(currentKey, (k) => {
    bind(k);
    // 错误经 error ref 交给调用方 toast；这里必须吞掉 rejection，否则是 unhandled rejection
    void load().catch(() => undefined);
  });

  if (getCurrentScope()) onScopeDispose(() => {
    stop();
    if (boundEntry) {
      boundEntry.readers = Math.max(0, boundEntry.readers - 1);
      boundEntry = null;
      boundKey = '';
    }
  });

  if (opts.immediate !== false) {
    // 没有活动作用域（探针/直接调用）时由调用方自己 load()
    if (getCurrentScope()) void load().catch(() => undefined);
  }

  return { data, loading, refreshing, busy, error, stale, hasData: computed(() => data.value !== undefined), load, refresh, invalidate };
}

export type CachedFetch<T> = {
  data: Ref<T | undefined>;
  loading: Ref<boolean>;
  refreshing: Ref<boolean>;
  busy: ComputedRef<boolean>;
  error: Ref<unknown>;
  stale: ComputedRef<boolean>;
  hasData: ComputedRef<boolean>;
  load: (force?: boolean) => Promise<T | undefined>;
  refresh: () => Promise<T | undefined>;
  invalidate: () => number;
};
