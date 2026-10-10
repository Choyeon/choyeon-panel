/**
 * 全站进度条：顶部 NLoadingBar 由「在飞请求数 + 懒加载 chunk 数」这一个内部计数器驱动。
 *
 * 为什么放在 api 层而不是每个页面：页面各自 start/finish 会在并发请求时提前结束，
 * 或者两个请求各 start 一次导致条来回闪。计数器只在 0→1 时 start、1→0 时 finish。
 *
 * 为什么还要给路由 chunk 留接口：`卡一下` 的主因是懒加载页面 JS 的下拉时间，
 * 这段时间没有任何请求，只有 chunk 计数能看到。router 由另一位同学维护，
 * 这里只暴露 beginChunk/endChunk/chunkFailed 三个函数。
 */

export type BarHandle = { start: () => void; finish: () => void; error: () => void };

let bar: BarHandle | null = null;
let requests = 0;
let chunks = 0;
let running = false;

function sync() {
  if (!bar) return;
  const busy = requests + chunks > 0;
  if (busy && !running) {
    running = true;
    bar.start();
  } else if (!busy && running) {
    running = false;
    bar.finish();
  }
}

/** App.vue 在 NLoadingBarProvider 内部注册；provider 卸载时传 null 复位计数 */
export function bindLoadingBar(handle: BarHandle | null) {
  bar = handle;
  if (!handle) {
    requests = 0;
    chunks = 0;
    running = false;
  }
}

export function beginRequest() {
  requests++;
  sync();
}

/** ok=false 走 error 状态（网络错误/超时/非 2xx），随后照常收尾，条不会停在那儿 */
export function endRequest(ok = true) {
  if (requests > 0) requests--;
  if (!ok && bar && requests + chunks === 0) {
    running = false;
    bar.error();
    bar.finish();
    return;
  }
  sync();
}

export function beginChunk() {
  chunks++;
  sync();
}

export function endChunk() {
  if (chunks > 0) chunks--;
  sync();
}

export function chunkFailed() {
  if (chunks > 0) chunks--;
  // 不能复用 endRequest(false)：那会再减一次 requests，把另一个仍在飞的请求的
  // 名额吃掉，条子提前收尾，真请求返回时既不亮红也不再更新。
  if (bar && requests + chunks === 0) {
    running = false;
    bar.error();
    bar.finish();
    return;
  }
  sync();
}

/** 探针 / 调试用快照 */
export function progressState() {
  return { requests, chunks, running };
}

export const appProgress = { bindLoadingBar, beginRequest, endRequest, beginChunk, endChunk, chunkFailed, progressState };
