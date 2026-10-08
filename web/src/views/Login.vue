<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NInput, NButton, NSpace, NIcon, NAlert } from 'naive-ui';
import { api, setToken, setRole } from '../api';
import { icons } from '../icons';

const router = useRouter();
const needsSetup = ref(false);
const username = ref('');
const password = ref('');
const password2 = ref('');
const err = ref('');
const loading = ref(false);

// 用户名由本机记忆提供：/auth/status 不再回显账号名，避免未登录就泄露管理员用户名
const LAST_USER = 'cp_last_user';

onMounted(async () => {
  try {
    const s = await api.status();
    needsSetup.value = s.needsSetup;
  } catch {
    err.value = '无法连接面板后端，请确认服务已启动';
  }
  if (!needsSetup.value) {
    try {
      username.value = localStorage.getItem(LAST_USER) || '';
    } catch {
      /* 隐私模式下 localStorage 不可用，留空即可 */
    }
  }
});

const FEATS = [
  'Node / Python 项目一键部署与回滚日志',
  'nginx 反代 + Let’s Encrypt 全自动',
  'PostgreSQL / Redis 管理与定时备份',
  '实时监控 · 网页终端 · 多用户权限',
];

function validate(): string {
  if (!username.value) return '请输入用户名';
  if (password.value.length < 8) return '密码至少 8 位';
  if (needsSetup.value && password.value !== password2.value) return '两次密码不一致';
  return '';
}

async function submit() {
  err.value = validate();
  if (err.value) return;
  loading.value = true;
  try {
    const r = needsSetup.value
      ? await api.setup(username.value, password.value)
      : await api.login(username.value, password.value);
    setToken(r.token);
    setRole(r.role || 'admin');
    try {
      localStorage.setItem(LAST_USER, username.value);
    } catch {
      /* 存不下不影响登录 */
    }
    router.replace('/dashboard');
  } catch (e: any) {
    err.value = e.message || '请求失败';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="wrap">
    <div class="bg-glow g1" aria-hidden="true"></div>
    <div class="bg-glow g2" aria-hidden="true"></div>

    <div class="left cp-rise" style="--i: 0">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">C</div>
        <span>choyeon <b>panel</b></span>
      </div>
      <h1>服务器，<br />尽在掌握。</h1>
      <p class="tagline">应用部署 · systemd 服务 · 数据库 · 备份 · 监控告警 —— 一台机器，一个面板。</p>
      <ul class="feats">
        <li v-for="(f, i) in FEATS" :key="f" class="cp-rise" :style="`--i:${i + 2}`">
          <NIcon :component="icons.CheckmarkCircleOutline" :size="16" :color="'var(--cp-ok)'" />
          <span>{{ f }}</span>
        </li>
      </ul>
    </div>

    <div class="right cp-rise" style="--i: 1">
      <div class="card">
        <h2>{{ needsSetup ? '初始化面板' : '欢迎回来' }}</h2>
        <p class="sub">{{ needsSetup ? '创建管理员账号开始使用' : '登录以管理 choyeon 服务器' }}</p>

        <NAlert
          v-if="err"
          type="error"
          :bordered="false"
          size="small"
          class="err-alert"
          role="alert"
          aria-live="polite"
          tabindex="0"
        >
          {{ err }}
        </NAlert>

        <form @submit.prevent="submit">
          <NSpace vertical :size="14">
            <div class="field">
              <label for="cp-username">用户名</label>
              <NInput
                v-model:value="username"
                :placeholder="needsSetup ? '3-32 位字母、数字、下划线' : '请输入用户名'"
                size="large"
                :autofocus="true"
                :input-props="{ id: 'cp-username', name: 'username', autocomplete: 'username' }"
              />
            </div>
            <div class="field">
              <label for="cp-password">密码</label>
              <NInput
                v-model:value="password"
                type="password"
                size="large"
                show-password-on="click"
                placeholder="至少 8 位"
                :input-props="{
                  id: 'cp-password',
                  name: 'password',
                  autocomplete: needsSetup ? 'new-password' : 'current-password',
                }"
              />
            </div>
            <div v-if="needsSetup" class="field">
              <label for="cp-password2">确认密码</label>
              <NInput
                v-model:value="password2"
                type="password"
                size="large"
                placeholder="再输入一次"
                :input-props="{
                  id: 'cp-password2',
                  name: 'password2',
                  autocomplete: 'new-password',
                }"
              />
            </div>
            <NButton
              type="primary"
              size="large"
              block
              attr-type="submit"
              class="cp-press"
              :loading="loading"
              :disabled="loading"
            >
              {{ needsSetup ? '创建并进入' : '登 录' }}
              <template #icon><NIcon :component="icons.ArrowForwardOutline" /></template>
            </NButton>
          </NSpace>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6vw;
  background: var(--cp-bg);
  position: relative;
  overflow: hidden;
  padding: var(--space-5);
  box-sizing: border-box;
}
.bg-glow {
  position: absolute;
  border-radius: 50%;
  filter: blur(120px);
  opacity: 0.22;
  pointer-events: none;
}
.g1 { width: 520px; height: 520px; background: var(--cp-brand); top: -140px; left: -120px; }
.g2 { width: 460px; height: 460px; background: var(--cp-brand-accent); bottom: -160px; right: -100px; }
.left { max-width: 460px; min-width: 0; }
.right { width: 360px; max-width: 100%; }
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-6);
  color: var(--cp-text);
  font-size: var(--fs-lg);
}
.brand b { color: var(--cp-brand-soft); }
.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: var(--radius-lg);
  background: var(--gradient-brand);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: var(--shadow-glow);
}
h1 {
  font-size: var(--fs-3xl);
  line-height: 1.3;
  margin: 0 0 var(--space-4);
  color: var(--cp-text-strong);
  letter-spacing: -0.01em;
}
.tagline { color: var(--cp-text-dim); font-size: var(--fs-md); margin: 0 0 var(--space-5); }
.feats {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  color: var(--cp-text-dim);
  font-size: var(--fs-sm);
}
.feats li { display: flex; align-items: center; gap: var(--space-2); }
.card {
  background: var(--cp-elevated);
  border: 1px solid var(--cp-border);
  border-radius: var(--radius-xl);
  padding: var(--space-6) var(--space-5);
  box-shadow: var(--shadow-pop);
}
html[data-theme='dark'] .card { background: color-mix(in srgb, var(--cp-elevated) 86%, transparent); backdrop-filter: blur(8px); }
.card h2 { margin: 0 0 var(--space-1); font-size: var(--fs-xl); color: var(--cp-text-strong); }
.err-alert { margin-bottom: var(--space-3); }
.sub { margin: 0 0 var(--space-5); color: var(--cp-text-mute); font-size: var(--fs-sm); }
.field label { display: block; font-size: var(--fs-xs); color: var(--cp-text-dim); margin-bottom: var(--space-2); }
@media (max-width: 900px) {
  .wrap { gap: 0; }
  .left { display: none; }
  .right { width: 100%; max-width: 420px; }
}
@media (max-width: 640px) {
  .wrap { padding: var(--space-4); }
  .card { padding: var(--space-5); }
  .card h2 { font-size: var(--fs-lg); }
  .sub { margin-bottom: var(--space-4); }
}
</style>
