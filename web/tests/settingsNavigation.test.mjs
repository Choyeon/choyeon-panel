import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
const read = (path) => readFileSync(join(SRC, path), 'utf8');

test('settings no longer duplicates standalone routes as page tabs', () => {
  const settings = read('views/Settings.vue');
  assert.doesNotMatch(settings, /PageTabBar|navTabs|useRoute|useRouter/);
  for (const route of ['/users', '/audit', '/logs']) assert.ok(!settings.includes(route));
  assert.match(settings, /activeSection === 'security'/);
  assert.match(settings, /activeSection === 'alerts'/);
  assert.match(settings, /activeSection === 'access'/);
  assert.match(settings, /activeSection === 'certificates'/);
});

test('settings section switcher is admin-only, while account security remains available to viewers', () => {
  const settings = read('views/Settings.vue');
  assert.match(settings, /<div v-if="isAdmin" class="settings-nav/);
  assert.match(settings, /<section v-if="activeSection === 'security'">/);
  assert.match(settings, /if \(!admin\) activeSection\.value = 'security'/);
});

test('top-level apps and backups pages do not duplicate sidebar destinations', () => {
  const apps = read('views/Apps.vue');
  const backups = read('views/Backups.vue');
  assert.doesNotMatch(apps, /router\.push\('\/sites'\)/);
  assert.doesNotMatch(backups, /router\.push\('\/backups\/files'\)/);
  assert.match(backups, /router\.push\(\{ path: '\/backups\/files'/);
});
