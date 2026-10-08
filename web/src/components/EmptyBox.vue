<script setup lang="ts">
defineProps<{ text?: string }>();
</script>

<template>
  <div class="eb">
    <svg width="44" height="44" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" opacity="0.35" aria-hidden="true">
      <rect x="6" y="10" width="36" height="28" rx="3" />
      <path d="M6 18h36M14 27h10" />
    </svg>
    <p>{{ text || '暂无数据' }}</p>
    <slot />
  </div>
</template>

<style scoped>
.eb {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-6) var(--space-4);
  color: var(--cp-text-mute);
  font-size: var(--fs-sm);
  text-align: center;
}
.eb p {
  margin: 0;
  max-width: 42em;
  line-height: 1.6;
}
</style>
