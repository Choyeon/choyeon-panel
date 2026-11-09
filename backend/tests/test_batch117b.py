"""批次117b：真实浏览器逐页验收暴露的三处交互缺陷守卫。

E2E 实测记录（隔离零用户实例 + 真实浏览器）：
1. ConfirmActionDialog 取消后 activeElement 仍是 BODY——焦点归还被 NModal 自己
   的焦点回收覆盖，且 dropdown 触发时打开瞬间就读不到有效 trigger；
2. Shell.vue 的退出确认缺 aria-label，取消后同样丢焦点；
3. /files 页控制台刷 "ResizeObserver loop completed with undelivered
   notifications"——RO 回调里读 rect 又同步写 CSS 变量，把观测目标标脏。
   （/audit 页另有一条同类告警来自 Naive UI 虚拟滚动表格内部，修完本项目这处后仍在，
   不影响功能，未纳入本批修复范围。）
4. 退出 admin 再登录 viewer（不整页刷新）后，顶栏账号菜单仍显示 admin：
   身份字段里只有角色是响应式的，用户名/昵称/头像直读 localStorage，而 Shell 只挂载一次。

这四条都不是"看起来不整齐"，而是键盘用户、控制台与账号状态可复现的行为，钉住修复口径。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestConfirmDialogFocusReturn(unittest.TestCase):
    """共享破坏性确认弹窗：关掉后焦点必须回到触发控件。"""

    def test_focus_is_remembered_and_restored_after_modal_cleanup(self):
        s = _src(WEB / "components" / "ConfirmActionDialog.vue")
        self.assertIn("document.addEventListener('focusin', rememberFocus)", s,
                      "dropdown 触发时 activeElement 会先掉回 body，需要记住最近一次真实焦点")
        self.assertIn("onBeforeUnmount(() => document.removeEventListener('focusin', rememberFocus))", s,
                      "监听器要随组件销毁，不能泄漏")
        self.assertIn("el.isConnected", s, "触发行可能已被删除/重渲染，不能 focus 一个脱离文档的节点")
        self.assertIn("lastFocused", s)
        self.assertIn("{ flush: 'post' }", s,
                      "watch 排在 DOM 更新后，否则 NModal 的焦点回收会覆盖归还动作")
        self.assertIn("nextTick(() => el.focus())", s)
        # 连点闸不能被这次改动碰掉（test_batch29 也守着它）
        self.assertIn("if (!p.value || loading.value) return;", s)


class TestLogoutDialogA11y(unittest.TestCase):
    """退出确认：有可访问名称，取消后焦点回账号菜单；色调与破坏性删除区分。"""

    def test_logout_dialog_label_and_focus_return(self):
        s = _src(WEB / "Shell.vue")
        self.assertIn(":aria-label=\"'退出登录'\"", s)
        self.assertIn("watch(showLogout", s)
        self.assertIn("document.querySelector<HTMLElement>('.user-chip')?.focus()", s)
        self.assertIn("{ flush: 'post' }", s)
        # 退出不是不可逆删除：保留 warning，与 ConfirmActionDialog 的 error 有意区分
        self.assertIn('type="warning"', s)
        self.assertIn(":positive-button-props=\"{ type: 'warning' }\"", s)


class TestIdentityIsReactive(unittest.TestCase):
    """换账号不刷新页面，顶栏也要跟着换名字（E2E 实测：退出 admin → 登录 viewer 仍显示 admin）。"""

    def test_identity_reads_ref_and_every_write_updates_it(self):
        s = _src(WEB / "api.ts")
        self.assertIn("const usernameRef = ref(localStorage.getItem(USER_KEY) || '')", s)
        self.assertIn("const nicknameRef = ref(localStorage.getItem(NICK_KEY) || '')", s)
        self.assertIn("const avatarRef = ref(localStorage.getItem(AVA_KEY) || '')", s)
        self.assertIn("return usernameRef.value", s)
        self.assertIn("return nicknameRef.value", s)
        self.assertIn("return avatarRef.value", s)
        self.assertIn("usernameRef.value = r.username || ''", s)
        self.assertIn("nicknameRef.value = r.nickname || ''", s)
        self.assertIn("avatarRef.value = r.avatar || ''", s)
        # 退出时三个 ref 也要清，否则下次登录前顶栏会残留上一个账号
        clear = s.split("export function clearSession()", 1)[1].split("export const onUnauthorized", 1)[0]
        for name in ("usernameRef", "nicknameRef", "avatarRef"):
            self.assertIn(f"{name}.value = ''", clear)


class TestTableHeightObserverLoop(unittest.TestCase):
    """表格高度测量：RO 回调只负责排下一帧，不在回调里同步写布局。"""

    def test_measure_is_deferred_and_write_is_idempotent(self):
        s = _src(WEB / "composables" / "useTableHeight.ts")
        self.assertIn("new ResizeObserver(scheduleMeasure)", s)
        self.assertIn("requestAnimationFrame(() => {", s)
        self.assertIn("cancelAnimationFrame(rafId)", s, "组件销毁时要撤掉排队的那一帧")
        self.assertIn("if (varHost.style.getPropertyValue(varName) !== value)", s,
                      "同值不写，避免自己把自己标脏")
        self.assertNotIn("new ResizeObserver(measure)", s)


if __name__ == "__main__":
    unittest.main()
