<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { NInput, NSpace, NIcon, NAlert, useMessage } from 'naive-ui';
import { NButton } from '../ui';
import { api, saveLoginData } from '../api';
import { icons } from '../icons';
import LogoMark from '../components/LogoMark.vue';

const router = useRouter();
// 登录/初始化的错误已经有就地 NAlert 说明错在哪；成功原来只靠跳转体现，
// 补一句 toast 让「进去了」这件事有一个明确的回执（provider 在 App.vue，跨路由不丢）。
const msg = useMessage();
const needsSetup = ref(false);
const username = ref('');
const password = ref('');
const password2 = ref('');
const err = ref('');
const loading = ref(false);
// 两步验证的输入框只在后端**要**的时候才出现：登录页不预先查"这个账号开没开 2FA"，
// 因为 /auth/login 是唯一免认证入口，任何提前分流的响应都会变成账号名单探测面。
const code = ref('');
const codeVisible = ref(false);
const codeInput = ref<any>(null);
// 位数与步长来自响应的 digits/period（唯一出处是 backend/app/totp.py），这里不写死数字。
const digits = ref(0);
const period = ref(0);

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
  // 已经要过一次了就别再空着提交：这一轮无论如何都会被拒，白等一个 scrypt 的往返。
  if (codeVisible.value && !code.value) return '请输入验证器里的动态口令';
  return '';
}

function acceptTotpError(e: any): boolean {
  if (e?.code !== 'totp_required' && e?.code !== 'totp_invalid') return false;
  const p = e.payload || {};
  if (p.digits) digits.value = Number(p.digits);
  if (p.period) period.value = Number(p.period);
  codeVisible.value = true;
  // 口令错一次就清空：重放同一枚会被服务端按"已用过"拒掉，留着只让人对着同一个框发懵。
  if (e.code === 'totp_invalid') code.value = '';
  err.value = e.message || '请输入验证器里的动态口令';
  // 用户名和口令保持原样（它们已经对了），只把焦点交给新出现的这一格。
  nextTick(() => codeInput.value?.focus?.());
  return true;
}

async function submit() {
  // 回车走的是 form 的 submit，不受按钮 disabled 约束：连按两次回车会打两次 /login，
  // 第二次带着同样的凭据撞进登录限速，把一次成功的登录判成"尝试过多"。
  if (loading.value) return;
  err.value = validate();
  if (err.value) return;
  loading.value = true;
  try {
    const r = needsSetup.value
      ? await api.setup(username.value, password.value)
      : await api.login(username.value, password.value, code.value);
    saveLoginData(r);
    try {
      localStorage.setItem(LAST_USER, username.value);
    } catch {
      /* 存不下不影响登录 */
    }
    msg.success(needsSetup.value ? '管理员已创建，已自动登录' : '登录成功，欢迎回来');
    router.replace('/dashboard');
  } catch (e: any) {
    if (!acceptTotpError(e)) err.value = e.message || '请求失败';
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
        <LogoMark :size="34" />
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
          <NSpace vertical :size="16">
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
            <div v-if="codeVisible" class="field">
              <label for="cp-code">动态口令<template v-if="period">（{{ period }} 秒换一枚）</template></label>
              <!-- maxlength 只能挂在 NInput 上：写成 :input-props="{ maxlength }" 时它既没变成 attribute
                   也没变成 maxLength 属性（线上探针实测 el.maxLength === -1），位数上限会静默失效。 -->
              <NInput
                ref="codeInput"
                v-model:value="code"
                size="large"
                :maxlength="digits || undefined"
                :placeholder="digits ? `验证器里的 ${digits} 位数字` : '验证器里的数字'"
                :input-props="{
                  id: 'cp-code',
                  name: 'otp-code',
                  autocomplete: 'one-time-code',
                  inputmode: 'numeric',
                }"
              />
              <p class="hint">这个账号开启了两步验证：口令已经对了，再填验证器 App 里当前那一枚。</p>
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
  min-height: var(--viewport-h);
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
  /* 两团光斑慢慢游动：登录页是面板的第一眼，纯静的渐变底看着像没加载完。
     只动 transform（blur 结果在合成层里缓存，不重算滤镜），22s 一轮慢到
     不被察觉成"动画"，只留下"这页是活的"。系统开了减少动效时由全局规则停掉。 */
  animation: cp-glow-drift 22s var(--ease) infinite;
}
@keyframes cp-glow-drift {
  0%,
  100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(4%, -5%, 0) scale(1.08); }
}
.g1 { width: 520px; height: 520px; background: var(--cp-brand); top: -140px; left: -120px; }
/* 反向相位：两团同起同落会像整屏在呼吸，错开半轮才是"各自漂" */
.g2 { width: 460px; height: 460px; background: var(--cp-brand-accent); bottom: -160px; right: -100px; animation-delay: -11s; }
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
.hint { margin: var(--space-2) 0 0; font-size: var(--fs-xs); color: var(--cp-text-mute); line-height: 1.6; }
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
