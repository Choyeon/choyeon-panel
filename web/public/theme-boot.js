// 首屏防闪：在样式与框架加载前先落地主题，避免暗/亮切换时的白屏闪烁。
// 这段原来写在 index.html 的内联 <script> 里，而 CSP 因此不得不放行
// script-src 'unsafe-inline'——那等于把"任何一处 HTML 注入直接升级成拿 token"
// 这一层唯一的兜底拿掉了。挪成同源静态文件后 CSP 收紧到 'self'。
(function () {
  try {
    var m = localStorage.getItem('cp_theme') || 'auto';
    var dark = m === 'auto' ? window.matchMedia('(prefers-color-scheme: dark)').matches : m === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#2e313d' : '#f4f4f4');
  } catch (e) {
    /* 隐私模式下 localStorage 不可用时忽略 */
  }
})();
