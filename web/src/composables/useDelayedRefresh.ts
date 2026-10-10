import { getCurrentScope, onScopeDispose, ref } from 'vue';

/**
 * 「操作完之后等一会儿再刷新」的唯一种子实现（原来 Services / Apps / AppDetail 各抄了一遍
 * `const timers: number[]` + `later()`）。
 *
 * 为什么必须等再刷新：systemd 的 active 状态、备份产物落盘都有延迟，
 * 立刻拉一次拿到的是旧状态，用户会以为按钮没生效。
 * 为什么必须清理：卸载后那次请求是一次没人看的请求，失败还会把错误 toast 打到新页面上。
 *
 * 多次调用合并成一次刷新：连点四个服务只需要一回列表请求。
 */
export function useDelayedRefresh() {
  const pending = ref(false);
  let timer = 0;
  // 卸载时刻必须单独记住，不能只靠 cancel()：调用方普遍写成
  // `await api.xxx(); later(reload, 400)`，请求期间页面被切走时 dispose 先跑，
  // 那一刻定时器还没建（timer=0，cancel 无事可做），接口 resolve 之后照样挂表，
  // 于是在别人的页面上多发一次列表请求，失败还会把不相干的 toast 打过去。
  let disposed = false;

  function cancel() {
    if (timer) window.clearTimeout(timer);
    timer = 0;
    pending.value = false;
  }

  /** ms 后执行 fn；组件卸载自动取消。返回 false 表示已被卸载/取消挡下 */
  function later(fn: () => void | Promise<void>, ms = 400): boolean {
    if (disposed) return false;
    if (timer) window.clearTimeout(timer);
    pending.value = true;
    timer = window.setTimeout(() => {
      timer = 0;
      pending.value = false;
      if (disposed) return;
      void fn();
    }, ms);
    return true;
  }

  /** promise 版：await 之后再刷新，用于「等接口真的返回」的场景 */
  function after(p: Promise<unknown>, fn: () => void | Promise<void>, ms = 400) {
    void p.catch(() => undefined).then(() => later(fn, ms));
  }

  if (getCurrentScope()) onScopeDispose(() => {
    disposed = true;
    cancel();
  });

  /** 探针用：是否还挂着定时器（卸载后必须是 false） */
  return { pending, later, after, cancel, hasPendingTimer: () => timer !== 0 };
}
