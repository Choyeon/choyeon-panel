<script setup lang="ts">
// 品牌标记：与 web/public/favicon.svg、assets/logo.svg 同一套几何（64 网格）。
// 底板用 CSS 渐变而不是 SVG <linearGradient>：同页会渲染多个实例，重复的 gradient id
// 会互相抢引用，而且这样色值仍然只有 style.css 一处。
withDefaults(defineProps<{ size?: number }>(), { size: 28 });
</script>

<template>
  <span class="logo-mark" :style="{ width: `${size}px`, height: `${size}px` }" aria-hidden="true">
    <svg viewBox="0 0 64 64" width="100%" height="100%" focusable="false">
      <rect x="9" y="13" width="46" height="38" rx="9" fill="#062457" fill-opacity=".5" />
      <path d="M9 22a9 9 0 0 1 9-9h28a9 9 0 0 1 9 9v1H9z" fill="#ffffff" fill-opacity=".3" />
      <polyline
        points="17,43 27,31 34,36 48,21"
        fill="none"
        stroke="#ffffff"
        stroke-width="7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <circle cx="48" cy="21" r="6" fill="#8fd460" />
    </svg>
  </span>
</template>

<style scoped>
.logo-mark {
  display: block;
  flex-shrink: 0;
  border-radius: 23%;
  background: var(--gradient-brand);
  box-shadow: var(--shadow-glow);
  overflow: hidden;
}
.logo-mark svg {
  display: block;
}
</style>
