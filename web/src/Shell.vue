<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  NLayout, NLayoutSider, NMenu, NSpace, NText, NButton,
  NModal, NInput, NDrawer, NDrawerContent, NIcon, useMessage,
} from 'naive-ui';
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
const oldPwd = ref('');
const newPwd = ref('');
const pwdBusy = ref(false);
const profileBusy = ref(false);
const editNickname = ref('');
const editAvatar = ref('');
const renewing = ref(false);

const isMobile = ref(false);
const drawer = ref(false);
const isLogin = computed(() => route.path === '/login');
const isAdmin = computed(() => getRole() === 'admin');
const displayName = computed(() => getNickname() || getUsername() || '管理员');
const avatarUrl = computed(() => getAvatar());
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
  '/files': 'FolderOpenOutline',
  '/terminal': 'TerminalOutline',
  '/doctor': 'ShieldCheckmarkOutline',
  '/settings': 'SettingsOutline',
};

const menuOptions = computed(() => {
  const keys = ['/dashboard', '/apps', '/services', '/db', '/backups'];
  if (isAdmin.value) keys.push('/files', '/terminal', '/doctor');
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

          <button class="user-chip" type="button" :aria-label="`账号菜单，当前：${displayName}`" @click="openProfile">
            <span class="avatar">
              <img v-if="avatarUrl" :src="avatarUrl" alt="" />
              <NIcon v-else :component="icons.PersonCircleOutline" :size="24" />
            </span>
            <span v-if="!isMobile" class="user-name">{{ displayName }}</span>
          </button>

          <NButton size="tiny" quaternary @click="showPwd = true" :aria-label="'修改密码'">
            <template #icon><NIcon :component="icons.KeyOutline" :size="14" /></template>
          </NButton>
          <NButton size="tiny" quaternary @click="doLogout" :aria-label="'退出登录'">
            <template #icon><NIcon :component="icons.ArrowForwardOutline" :size="14" /></template>
          </NButton>
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
          <NInput v-model:value="editAvatar" placeholder="https://… （留空使用默认头像）" :input-props="{ 'aria-label': '头像 URL' }" maxlength="256" />
        </div>
        <NSpace justify="end" :size="8">
          <NButton tertiary @click="showProfile = false">取消</NButton>
          <NButton type="primary" class="cp-press" :loading="profileBusy" @click="doSaveProfile">保存</NButton>
        </NSpace>
      </NSpace>
    </NModal>
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
