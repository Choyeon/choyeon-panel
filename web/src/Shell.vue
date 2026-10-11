<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NLayout, NLayoutSider, NMenu, NSpace, NText, NModal, NInput, NDrawer, NDrawerContent, NIcon, NDropdown, useMessage,
} from 'naive-ui';
import { NButton } from './ui';
import { setToken, getRole, getUsername, getNickname, setNickname, getAvatar, setAvatar, clearSession, api, req } from './api';
import { icon, icons } from './icons';
import { ADMIN_ONLY, TITLES } from './router';
import LogoMark from './components/LogoMark.vue';
import type { ThemeOrigin } from './themeReveal';

const props = defineProps<{ themeMode?: 'dark' | 'light' }>();
const emit = defineEmits<{ 'update:themeMode': [mode: 'dark' | 'light', origin?: ThemeOrigin] }>();

const route = useRoute();
const router = useRouter();
const msg = useMessage();
const collapsed = ref(false);
const showPwd = ref(false);
const showProfile = ref(false);
const showLogout = ref(false);
const oldPwd = ref('');
const newPwd = ref('');
const pwdBusy = ref(false);
const profileBusy = ref(false);
const editNickname = ref('');
const editAvatar = ref('');
const avatarBusy = ref(false);
const renewing = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

// .cp-scroll 是常驻容器，不跟着路由重置的话，新页面会继承上一页的滚动位置，
// 用户看到的是"点进去从半截开始"。
const scrollEl = ref<HTMLElement | null>(null);
watch(
  () => route.fullPath,
  () => nextTick(() => { if (scrollEl.value) scrollEl.value.scrollTop = 0; }),
);

const isMobile = ref(false);
const drawer = ref(false);
// 头像/昵称存在 localStorage（不是响应式源），模态框开关时正好是值可能变了的时刻，
// 用它做依赖让顶栏在保存资料或上传头像后立即刷新，而不是等到下次整页重载。
const profileTick = ref(0);
watch(showProfile, (v) => { if (!v) profileTick.value += 1; });
const isLogin = computed(() => route.path === '/login');
const isAdmin = computed(() => getRole() === 'admin');
const displayName = computed(() => (profileTick.value, getNickname() || getUsername() || '管理员'));
const avatarUrl = computed(() => (profileTick.value, getAvatar()));
const pageTitle = computed(() =>
  route.path.startsWith('/apps/') ? '应用详情' : TITLES[route.path] || 'choyeon panel',
);
// 二级页高亮所属一级菜单：/apps/3 -> 应用，/backups/files -> 备份
const active = computed(() => {
  const top = `/${route.path.split('/')[1] || ''}`;
  return top === '/apps' || top === '/backups' ? top : route.path;
});
const ico = (name: string, size = 18) => icon(name, size);

function onResize() {
  isMobile.value = window.innerWidth < 900;
  if (!isMobile.value) drawer.value = false;
}

// 页脚版本号读 /api/health（公开端点，不需要 token）。取不到就整块不显示，
// 而不是写死一个字符串——那会比"没有版本"更容易骗人。
const panelVersion = ref('');
onMounted(() => {
  onResize();
  window.addEventListener('resize', onResize);
  req('/health').then((d: any) => { if (d?.version) panelVersion.value = String(d.version); }).catch(() => {});
});
onBeforeUnmount(() => window.removeEventListener('resize', onResize));

async function renewNow() {
  renewing.value = true;
  try {
    const r = await api.certbotRenew();
    // 后端按到期日的实际变化给出结论，这里照它说：写死「续期检查完成」会把
    // 「一张都没到期」说成像是续好了。
    msg.success(r.summary || 'certbot 已执行');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    renewing.value = false;
  }
}

const MENU_ICON: Record<string, string> = {
  '/dashboard': 'SpeedometerOutline',
  '/apps': 'ApplicationsOutline',
  '/services': 'ServerOutline',
  '/db': 'CloudOutline',
  '/sites': 'GlobeOutline',
  '/backups': 'TimeOutline',
  '/alerts': 'AlertCircleOutline',
  '/cron': 'SyncOutline',
  '/logs': 'CodeSlashOutline',
  '/logsearch': 'DocumentTextOutline',
  '/files': 'FolderOpenOutline',
  '/terminal': 'TerminalOutline',
  '/doctor': 'ShieldCheckmarkOutline',
  '/users': 'PeopleOutline',
  '/audit': 'GitNetworkOutline',
  '/settings': 'SettingsOutline',
};

/* 侧栏分组照抄 1Panel 的 Sidebar/SubItem.vue 规则：用**嵌套子菜单**（不是
   el-menu 的 type:'group'，1Panel 根本没有分组标题项），并且"只有一个孩子的父项
   直接升级成普通菜单项"（v-if="children.length > 1" ? sub-menu : item）。
   只读账号下多个分组会只剩一项，如果不升级就会多出一层无意义的折叠。 */
const MENU_GROUPS: { label: string; icon: string; items: string[] }[] = [
  { label: '网站与服务', icon: 'GlobeOutline', items: ['/apps', '/sites', '/services', '/db'] },
  { label: '数据与备份', icon: 'TimeOutline', items: ['/backups', '/cron', '/files'] },
  { label: '日志与告警', icon: 'DocumentTextOutline', items: ['/alerts', '/logs', '/logsearch'] },
  { label: '运维工具', icon: 'BuildOutline', items: ['/terminal', '/doctor'] },
  { label: '面板管理', icon: 'SettingsOutline', items: ['/users', '/audit', '/settings'] },
];

function menuItem(k: string) {
  return { label: TITLES[k], key: k, icon: ico(MENU_ICON[k], 18) };
}

const menuOptions = computed(() => {
  const out: any[] = [menuItem('/dashboard')];
  for (const g of MENU_GROUPS) {
    const keys = g.items.filter((k) => !ADMIN_ONLY.includes(k) || isAdmin.value);
    if (!keys.length) continue;
    if (keys.length === 1) out.push(menuItem(keys[0]));
    else out.push({ label: g.label, key: `grp:${g.label}`, icon: ico(g.icon, 18), children: keys.map(menuItem) });
  }
  return out;
});

function go(key: string) {
  if (key.startsWith('grp:')) return; // 父项只做展开，不导航
  drawer.value = false;
  router.push(key);
}

/* element-plus 的 el-menu 会自动展开当前项所在的 sub-menu（default-openeds 由
   default-active 反推）。naive-ui 的 NMenu 不会，所以这里自己把 active 映射成分组 key，
   否则打开面板只看见六个分组名、一个功能项都点不到。 */
const expandedKeys = ref<string[]>([]);
watch(
  active,
  (path) => {
    const g = MENU_GROUPS.find((x) => x.items.includes(path));
    expandedKeys.value = g ? [`grp:${g.label}`] : [];
  },
  { immediate: true },
);

async function doLogout() {
  // 先让服务端吊销 token，再清本地。只清本地的话，那枚签名票据在 TTL（默认 12 小时）内
  // 依然是有效的面板凭证：从开发工具里复制走、共用电脑没退干净，都是这么留下的。
  try {
    await api.logout();
  } catch {
    // 吊销失败不能把人锁在面板里——没有"退出不了"的退出按钮，照样清本地。
  }
  clearSession();
  router.replace('/login');
}

async function doChangePwd() {
  pwdBusy.value = true;
  try {
    const r = await api.changePassword(oldPwd.value, newPwd.value);
    if (r?.token) setToken(r.token);
    oldPwd.value = newPwd.value = '';
    showPwd.value = false;
    msg.success('密码已修改');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    pwdBusy.value = false;
  }
}

function openProfile() {
  editNickname.value = getNickname();
  editAvatar.value = getAvatar();
  showProfile.value = true;
}

async function doSaveProfile() {
  profileBusy.value = true;
  try {
    const r = await api.updateProfile(editNickname.value, editAvatar.value);
    setNickname(r.nickname || '');
    setAvatar(r.avatar || '');
    showProfile.value = false;
    msg.success('个人资料已更新');
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    profileBusy.value = false;
  }
}

// ---------- 头像上传：浏览器端裁剪压缩成 256px 方图，再按原始字节 POST ----------
// 不引第三方图库依赖：中心裁剪 + canvas 缩放足够把 4MB 手机照压到 20-80KB，
// 而直接把原图丢给后端既撑体积又让 GET 回读变慢（头像在每页都渲染）。
const AVA_SIDE = 256;

function cropToSquare(file: File, side: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const w = img.naturalWidth;
      const hh = img.naturalHeight;
      if (!w || !hh) {
        reject(new Error('图片尺寸为 0，无法处理'));
        return;
      }
      const crop = Math.min(w, hh);
      const cv = document.createElement('canvas');
      cv.width = side;
      cv.height = side;
      const ctx = cv.getContext('2d');
      if (!ctx) {
        reject(new Error('当前浏览器不支持图片处理'));
        return;
      }
      // JPEG 没有透明通道：PNG 直接转会让透明区变黑块，先铺一层底色再画
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, side, side);
      ctx.drawImage(img, (w - crop) / 2, (hh - crop) / 2, crop, crop, 0, 0, side, side);
      cv.toBlob(
        (b) => (b ? resolve(b) : reject(new Error('图片压缩失败'))),
        'image/jpeg',
        0.88,
      );
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('图片无法解码，换一张试试'));
    };
    img.src = url;
  });
}

async function onPickAvatar(ev: Event) {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  // 清空 value：否则连续选同一个文件不会触发 change，看起来像"按钮没反应"
  input.value = '';
  if (!file) return;
  if (!/^image\/(png|jpe?g|webp|gif)$/.test(file.type)) {
    msg.error('只支持 PNG / JPEG / WebP / GIF');
    return;
  }
  avatarBusy.value = true;
  try {
    const blob = await cropToSquare(file, AVA_SIDE);
    const r = await api.uploadAvatar(blob);
    editAvatar.value = r.avatar;
    setAvatar(r.avatar);
    profileTick.value += 1;
    msg.success(`头像已更新（${Math.round(blob.size / 1024)}KB）`);
  } catch (e: any) {
    msg.error(e.message);
  } finally {
    avatarBusy.value = false;
  }
}

const userOptions = computed(() => [
  { label: '编辑资料', key: 'profile', icon: () => h(NIcon, { size: 16 }, { default: () => h(icons.PersonCircleOutline) }) },
  { label: '修改密码', key: 'password', icon: () => h(NIcon, { size: 16 }, { default: () => h(icons.KeyOutline) }) },
  { type: 'divider', key: 'd1' },
  { label: '退出登录', key: 'logout', icon: () => h(NIcon, { size: 16 }, { default: () => h(icons.ArrowForwardOutline) }) },
]);

function onUserSelect(key: string) {
  if (key === 'profile') openProfile();
  else if (key === 'password') showPwd.value = true;
  else if (key === 'logout') showLogout.value = true;
}

const themeIcon = computed(() => (props.themeMode === 'light' ? 'MoonOutline' : 'SunnyOutline'));
// 圆形动效的圆心取按钮自身位置，所以要把点击事件里的元素几何一起交给 App.vue
function toggleTheme(ev: MouseEvent) {
  const b = (ev.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  const origin = b ? { x: b.left + b.width / 2, y: b.top + b.height / 2 } : undefined;
  emit('update:themeMode', props.themeMode === 'light' ? 'dark' : 'light', origin);
}
</script>

<template>
  <router-view v-if="isLogin" />

  <NLayout v-else class="cp-shell" has-sider>
    <NLayoutSider
      v-if="!isMobile"
      collapse-mode="width"
      :collapsed-width="75"
      :width="180"
      :collapsed="collapsed"
      bordered
      show-trigger="bar"
      :native-scrollbar="false"
      class="cp-sider"
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="logo" :class="{ mini: collapsed }">
        <LogoMark />
        <span v-if="!collapsed" class="logo-text">choyeon <b>panel</b></span>
      </div>
      <NMenu
        :indent="16"
        :collapsed-icon-size="19"
        :collapsed="collapsed"
        :collapsed-width="75"
        :value="active"
        :options="menuOptions"
        :expanded-keys="expandedKeys"
        @update:expanded-keys="(k: string[]) => (expandedKeys = k)"
        @update:value="(k: string) => go(k)"
      />
    </NLayoutSider>

    <NDrawer v-else v-model:show="drawer" :width="240" placement="left">
      <NDrawerContent :native-scrollbar="false" body-content-style="padding: 0 var(--space-3)">
        <template #header>
          <div class="logo">
            <div class="logo-mark" aria-hidden="true">C</div>
            <span class="logo-text">choyeon <b>panel</b></span>
          </div>
        </template>
        <NMenu
          :indent="14"
          :value="active"
          :options="menuOptions"
          :expanded-keys="expandedKeys"
          @update:expanded-keys="(k: string[]) => (expandedKeys = k)"
          @update:value="(k: string) => go(k)"
        />
      </NDrawerContent>
    </NDrawer>

    <div class="cp-main">
      <header class="cp-header">
        <NSpace align="center" :size="8" :wrap="false">
          <NButton
            v-if="isMobile"
            quaternary
            size="small"
            title="打开导航菜单"
            aria-label="打开导航菜单"
            :icon="ico('MenuOutline')"
            @click="drawer = true"
          />
          <NText depth="3" class="cp-title">{{ pageTitle }}</NText>
        </NSpace>

        <NSpace align="center" :size="10" :wrap="false">
          <NButton v-if="isAdmin && !isMobile" size="small" tertiary :loading="renewing" @click="renewNow">
            续期证书
          </NButton>

          <NButton
            quaternary circle size="small"
            :title="themeIcon === 'MoonOutline' ? '切换深色模式' : '切换浅色模式'"
            :aria-label="themeIcon === 'MoonOutline' ? '切换深色模式' : '切换浅色模式'"
            @click="toggleTheme"
          >
            <template #icon><NIcon :component="icons[themeIcon]" :size="18" /></template>
          </NButton>

          <NDropdown trigger="click" placement="bottom-end" :options="userOptions" :on-select="onUserSelect">
            <button class="user-chip" type="button" :aria-label="`账号菜单，当前：${displayName}`">
              <span class="avatar">
                <img v-if="avatarUrl" :src="avatarUrl" alt="" />
                <NIcon v-else :component="icons.PersonCircleOutline" :size="24" />
              </span>
              <span v-if="!isMobile" class="user-name">{{ displayName }}</span>
              <NIcon v-if="!isMobile" :component="icons.ChevronForwardOutline" :size="12" class="chip-caret" aria-hidden="true" />
            </button>
          </NDropdown>
        </NSpace>
      </header>

      <div ref="scrollEl" class="cp-scroll">
        <router-view v-slot="{ Component }">
          <!-- 不用 mode="out-in"：那会让旧页面先播完再进新页面，切页固定多等一拍（约 200ms 空白）。
               叠层淡出期间旧页面绝对定位，不占高度，新页面立刻接管布局。 -->
          <transition name="page"><component :is="Component" /></transition>
        </router-view>
      </div>

      <!-- 页脚照 1Panel 的 AppFooter：48px 高、上发丝线、左边版权、右边版本 chip -->
      <footer v-if="!isMobile" class="cp-footer">
        <span class="cp-footer-copy">Copyright © 2026 choyeon</span>
        <span v-if="panelVersion" class="cp-footer-ver">choyeon panel {{ panelVersion }}</span>
      </footer>
    </div>

    <NModal v-model:show="showPwd" preset="card" title="修改密码" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput
          v-model:value="oldPwd"
          type="password"
          placeholder="原密码"
          autocomplete="current-password"
          maxlength="1024"
          :input-props="{ 'aria-label': '原密码' }"
          @keyup.enter="doChangePwd"
        />
        <NInput
          v-model:value="newPwd"
          type="password"
          placeholder="新密码（8–128 位）"
          maxlength="128"
          autocomplete="new-password"
          :input-props="{ 'aria-label': '新密码' }"
          @keyup.enter="doChangePwd"
        />
        <NSpace justify="end" :size="8">
          <NButton tertiary @click="showPwd = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="pwdBusy" :disabled="!oldPwd || newPwd.length < 8" @click="doChangePwd">
            确认修改
          </NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal v-model:show="showProfile" preset="card" title="个人资料" style="width: 440px; max-width: 94vw">
      <NSpace vertical :size="16">
        <div class="profile-avatar-preview">
          <span class="avatar-lg">
            <img v-if="editAvatar" :src="editAvatar" alt="" />
            <NIcon v-else :component="icons.PersonCircleOutline" :size="48" color="var(--cp-text-mute)" />
          </span>
        </div>
        <div class="field">
          <label>昵称</label>
          <NInput v-model:value="editNickname" placeholder="显示名称（最多 32 字）" :input-props="{ 'aria-label': '昵称' }" maxlength="32" />
        </div>
        <div class="field">
          <label>头像 URL</label>
          <NSpace :size="8" :wrap="false">
            <NInput v-model:value="editAvatar" placeholder="https://… （留空使用默认头像）" :input-props="{ 'aria-label': '头像 URL' }" maxlength="256" />
            <NButton tertiary :loading="avatarBusy" :icon="ico('CloudUploadOutline', 16)" @click="fileInput?.click()">上传</NButton>
          </NSpace>
          <input ref="fileInput" type="file" accept="image/png,image/jpeg,image/webp,image/gif" class="hidden-file" @change="onPickAvatar" />
          <NText depth="3" class="field-hint">上传自动中心裁剪为 256×256 并压缩到约 50KB，无需图床链接。</NText>
        </div>
        <NSpace justify="end" :size="8">
          <NButton tertiary @click="showProfile = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="profileBusy" @click="doSaveProfile">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>

    <NModal
      v-model:show="showLogout"
      preset="dialog"
      type="warning"
      title="退出登录"
      content="退出会注销该账号的全部登录（包括其他浏览器与设备上的），之后需要重新输入账号密码才能进入面板。"
      positive-text="退出"
      negative-text="取消"
      :positive-button-props="{ type: 'warning' }"
      style="width: 380px; max-width: 94vw"
      @positive-click="doLogout"
    />
  </NLayout>
</template>

<style scoped>
.cp-shell { height: 100vh; }
.cp-sider { background: var(--cp-menu-bg); }
/* 菜单胶囊：1Panel 的每一项都是独立圆角块（42px、4px 圆角、7px 上下间距、
   0 0 4px 外阴影），选中项再加 2px 内描边 + 竖条。naive 的 Menu themeOverrides
   只能给颜色，形状只能在这里补。 */
.cp-sider :deep(.n-menu) { padding: 0 var(--menu-pad-x); }
.cp-sider :deep(.n-menu .n-menu-item-content) {
  margin: 7px 0;
  border-radius: var(--radius-lg);
  /* 常态底色只能画在元素上：naive 的 ::before 常态是 transparent，只有 hover/选中
     才有 background-color，而它的容器色 --n-color（themeOverrides.Menu.color）是整块
     菜单背景——用它当胶囊底色会把 7px 间距一起染掉，实测就是这样糊成一片的。 */
  background: var(--cp-menu-item-bg);
}
/* naive 把状态底色画在 .n-menu-item-content::before 上，且默认 left/right 各缩 8px；
   胶囊是整行的，所以这一层必须撑满——否则 hover/选中会在 166px 的胶囊里只盖住中间
   150px，两边各留 8px 原色，看着像"hover 不全"。颜色交给 theme.ts 的 Menu 变量：
   naive 的 :hover / --hover / 选中四态规则权重不低于这里，平权重时它运行时注入的
   style 排在后面会赢，所以 CSS 只补形状和描边。 */
.cp-sider :deep(.n-menu .n-menu-item-content::before) {
  left: 0;
  right: 0;
  border-radius: var(--radius-lg);
  box-shadow: 0 0 4px var(--cp-menu-shadow-color);
}
.cp-sider :deep(.n-menu .n-menu-item-content--selected::before) {
  box-shadow: 0 0 4px var(--cp-menu-shadow-color), inset 0 0 0 2px var(--cp-brand);
}
/* 选中竖条：图标从 16px 起，所以条放在 7px、宽 3px，中间留 6px 走道；
   原来 12px 起、4px 宽会直接贴在图标左边，看着像多出来的一块色斑。 */
.cp-sider :deep(.n-menu .n-menu-item-content--selected > .n-menu-item-content-header::before) {
  content: '';
  position: absolute;
  left: 7px;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 16px;
  border-radius: 2px;
  background: var(--cp-brand);
}
/* 折叠态下胶囊会挤成一条，去掉左右留白与竖条 */
.cp-sider :deep(.n-menu--collapsed) { padding: 0; }
.cp-sider :deep(.n-menu--collapsed .n-menu-item-content) { margin: 6px 0; }
.cp-sider :deep(.n-menu--collapsed .n-menu-item-content::before) { box-shadow: none; }
.logo {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-4) var(--space-3);
  min-height: var(--header-h);
}
.logo-text {
  font-size: var(--fs-md);
  color: var(--cp-text);
  letter-spacing: 0.01em;
  white-space: nowrap;
}
.logo-text b { color: var(--cp-brand-soft); }

.cp-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}
.cp-header {
  height: var(--header-h);
  min-height: var(--header-h);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--space-5);
  gap: var(--space-3);
  border-bottom: 1px solid var(--cp-border);
  background: var(--cp-bg-1);
}
.cp-scroll {
  /* 内边距抽成变量：淡出中的旧页面绝对定位时，要用同一个值对齐新页面的起点
     （绝对定位的 inset:0 落在 padding box 上，直接写 inset:0 会偏移一个 padding）。
     桌面档对齐 1Panel 的 .app-main { padding: 7px 20px }，与 --page-pad 同一个值，
     表格高度 calc(--table-h) 依赖的就是它。 */
  --pad-scroll: var(--page-pad);
  flex: 1;
  overflow: auto;
  position: relative;
  padding: var(--pad-scroll);
}
.cp-footer {
  flex-shrink: 0;
  min-height: var(--footer-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 10px var(--space-5);
  border-top: 1px solid var(--cp-footer-border);
  background: var(--cp-bg);
  font-size: var(--fs-xs);
  color: var(--cp-text-mute);
  letter-spacing: 0.02em;
}
.cp-footer-ver {
  padding: 2px var(--space-3);
  border-radius: var(--radius-lg);
  border: 1px solid var(--cp-footer-border);
  font-variant-numeric: tabular-nums;
}
/* 切页叠层：旧页面绝对定位后不占高度，新页面立即接管布局，中间没有空窗期。
   动效本身（opacity/transform）定义在 style.css 的 .page-* 全局块里。 */
.page-leave-active {
  position: absolute;
  top: var(--pad-scroll);
  left: var(--pad-scroll);
  right: var(--pad-scroll);
  pointer-events: none;
}
@media (max-width: 900px) {
  .cp-header { padding: 0 var(--space-4); }
  .cp-scroll { --pad-scroll: var(--space-5); }
}
@media (max-width: 640px) {
  .cp-title { font-size: var(--fs-sm); font-weight: 600; }
  .cp-scroll { --pad-scroll: var(--space-4); }
}
.cp-title { font-size: var(--fs-xs); letter-spacing: 0.02em; }

.user-chip {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 4px var(--space-3) 4px 4px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--cp-border);
  background: var(--cp-hover);
  color: var(--cp-text-dim);
  cursor: pointer;
  transition: all var(--dur) var(--ease);
  font-family: inherit;
  font-size: var(--fs-sm);
  line-height: 1;
}
.user-chip:hover {
  background: var(--cp-selected);
  border-color: var(--cp-brand);
  color: var(--cp-brand);
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: var(--cp-hover);
  color: var(--cp-text-mute);
}
.avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.user-name {
  font-size: var(--fs-sm);
  font-weight: 500;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chip-caret {
  transform: rotate(90deg);
  opacity: 0.55;
}
.hidden-file {
  display: none;
}
.field-hint {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  line-height: 1.5;
}

.profile-avatar-preview {
  display: flex;
  justify-content: center;
}
.avatar-lg {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--cp-hover);
  border: 2px solid var(--cp-border);
}
.avatar-lg img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.field label {
  display: block;
  font-size: var(--fs-xs);
  color: var(--cp-text-dim);
  margin-bottom: var(--space-1);
}
</style>
