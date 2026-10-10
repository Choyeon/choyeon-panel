import { onScopeDispose } from 'vue';

export interface VisiblePoll {
  /** 打开轮询（周期由 every() 设定）；页面此刻不可见时只记账，不排程。 */
  start(): void;
  /** 调用方不再需要轮询（例如自动刷新选了「关闭」、部署已全部落定）。 */
  stop(): void;
  /** 改周期并立即按当前可见性重排；ms <= 0 等同 stop。 */
  every(ms: number): void;
  /** 定时器此刻真的挂着吗（探针与调试用）。 */
  running(): boolean;
}

/**
 * 只在页面可见时跑的轮询。
 *
 * 浏览器把后台标签页的 setInterval 照原样跑下去，而这个面板的轮询不是纯读缓存：
 * /system/services 每次 fork 一个 systemctl、日志中心每次打 journalctl、Dashboard
 * 一轮要打 5 个端点。挂机一夜就是上万次无意义采样与子进程。
 * 回到前台会立刻补一次 tick，免得切回来还停在切走那一刻的数据上。
 */
export function useVisiblePoll(tick: () => void, ms = 0): VisiblePoll {
  let timer = 0;
  let interval = ms;
  let wanted = false;

  function release() {
    if (timer) {
      // 与 window.setInterval 同一个登记处（浏览器里两者等价，jsdom 里不是：用全局
      // clearInterval 去清 window.setInterval 的句柄会静默失败，隐藏后照跳不误）。
      window.clearInterval(timer);
      timer = 0;
    }
  }
  function acquire() {
    release();
    if (!wanted || interval <= 0 || document.hidden) return;
    timer = window.setInterval(tick, interval);
  }
  function onVisibility() {
    if (document.hidden) {
      release();
      return;
    }
    acquire();
    if (wanted && interval > 0) tick();
  }

  document.addEventListener('visibilitychange', onVisibility);
  onScopeDispose(() => {
    wanted = false;
    release();
    document.removeEventListener('visibilitychange', onVisibility);
  });

  return {
    start() {
      wanted = true;
      acquire();
    },
    stop() {
      wanted = false;
      release();
    },
    every(next: number) {
      interval = next;
      acquire();
    },
    running() {
      return timer !== 0;
    },
  };
}
