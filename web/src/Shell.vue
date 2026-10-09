<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NLayout, NLayoutSider, NMenu, NSpace, NText, NModal, NInput, NDrawer, NDrawerContent, NIcon, NDropdown, useMessage,
} from 'naive-ui';
import { NButton } from './ui';
import { setToken, getRole, getUsername, getNickname, setNickname, getAvatar, setAvatar, clearSession, api } from './api';
import { icons } from './icons';
import { TITLES } from './router';

const props = defineProps<{ themeMode?: 'dark' | 'light' }>();
const emit = defineEmits<{ 'update:themeMode': ['dark' | 'light'] }>();

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
const active = computed(() => (route.path.startsWith('/apps') ? '/apps' : route.path));
function ico(name: string, size = 18) {
  return () => h(NIcon, { size }, { default: () => h(icons[name] || icons.SettingsOutline) });
}

function onResize() {
  isMobile.value = window.innerWidth < 900;
  if (!isMobile.value) drawer.value = false;
}
onMounted(() => {
  onResize();
  window.addEventListener('resize', onResize);
});
onBeforeUnmount(() => window.removeEventListener('resize', onResize));

async function renewNow() {
  renewing.value = true;
  try {
    await api.certbotRenew();
    msg.success('certbot 续期检查完成');
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
  '/backups': 'TimeOutline',
  '/logs': 'CodeSlashOutline',
  '/files': 'FolderOpenOutline',
  '/terminal': 'TerminalOutline',
  '/doctor': 'ShieldCheckmarkOutline',
  '/users': 'PeopleOutline',
  '/audit': 'GitNetworkOutline',
  '/settings': 'SettingsOutline',
};

const menuOptions = computed(() => {
  const keys = ['/dashboard', '/apps', '/services', '/db', '/backups', '/logs'];
  if (isAdmin.value) keys.push('/users', '/audit', '/files', '/terminal', '/doctor');
  keys.push('/settings');
  return keys.map((k) => ({ label: TITLES[k], key: k, icon: ico(MENU_ICON[k], 18) }));
});

function go(key: string) {
  drawer.value = false;
  router.push(key);
}

function doLogout() {
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
function toggleTheme() {
  emit('update:themeMode', props.themeMode === 'light' ? 'dark' : 'light');
}
</script>

<template>
  <router-view v-if="isLogin" />

  <NLayout v-else class="cp-shell" has-sider>
    <NLayoutSider
      v-if="!isMobile"
      collapse-mode="width"
      :collapsed-width="64"
      :width="208"
      :collapsed="collapsed"
      bordered
      show-trigger="bar"
      :native-scrollbar="false"
      class="cp-sider"
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="logo" :class="{ mini: collapsed }">
        <div class="logo-mark" aria-hidden="true">C</div>
        <span v-if="!collapsed" class="logo-text">choyeon <b>panel</b></span>
      </div>
      <NMenu
        :indent="18"
        :collapsed-icon-size="19"
        :collapsed="collapsed"
        :collapsed-width="64"
        :value="active"
        :options="menuOptions"
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
        <NMenu :indent="14" :value="active" :options="menuOptions" @update:value="(k: string) => go(k)" />
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

      <div class="cp-scroll">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in"><component :is="Component" /></transition>
        </router-view>
      </div>
    </div>

    <NModal v-model:show="showPwd" preset="card" title="修改密码" style="width: 400px; max-width: 94vw">
      <NSpace vertical :size="12">
        <NInput
          v-model:value="oldPwd"
          type="password"
          placeholder="原密码"
          autocomplete="current-password"
          :input-props="{ 'aria-label': '原密码' }"
          @keyup.enter="doChangePwd"
        />
        <NInput
          v-model:value="newPwd"
          type="password"
          placeholder="新密码（至少 8 位）"
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
      content="退出后需要重新输入账号密码才能进入面板。"
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
.cp-sider { background: var(--cp-bg-1); }
.logo {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-4) var(--space-3);
  min-height: var(--header-h);
}
.logo-mark {
  width: 28px;
  height: 28px;
  border-radius: var(--radius);
  background: var(--gradient-brand);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--fs-md);
  flex-shrink: 0;
  box-shadow: var(--shadow-glow);
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
  flex: 1;
  overflow: auto;
  padding: var(--space-6);
}
@media (max-width: 900px) {
  .cp-header { padding: 0 var(--space-4); }
  .cp-scroll { padding: var(--space-5); }
}
@media (max-width: 640px) {
  .cp-title { font-size: var(--fs-sm); font-weight: 600; }
  .cp-scroll { padding: var(--space-4); }
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
