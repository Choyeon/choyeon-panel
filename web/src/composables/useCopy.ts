import { useMessage } from 'naive-ui';

/**
 * 复制并如实报告结果。
 * 原来 CopyButton / DescriptionsCard 都是 `catch {}`：面板常用 http://内网IP 访问，
 * 那种上下文里 navigator.clipboard 整个不存在，点复制什么都没发生，
 * 用户却以为已经复制上了，粘出来是空的。
 */
export function useCopy() {
  const msg = useMessage();

  async function copy(text: string, ok = '已复制'): Promise<boolean> {
    if (!text) return false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        msg.success(ok);
        return true;
      } catch {
        /* 权限被拒（未授权 / 页面不可见）时继续走 execCommand 回退 */
      }
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    // 不能 display:none（不参与选择），把它挪到视口外、压到 1px 即可
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;width:1px;height:1px;padding:0;border:0';
    document.body.appendChild(ta);
    ta.select();
    let done = false;
    try {
      done = document.execCommand('copy');
    } catch {
      done = false;
    }
    document.body.removeChild(ta);
    if (done) msg.success(ok);
    else msg.warning('浏览器不允许写入剪贴板，请手动选中复制');
    return done;
  }

  return { copy };
}
