// Drives the Flights tab in Chromium inside the real dashboard. Started by harness.py.
import { createRequire } from 'module';
if (!process.env.PLAYWRIGHT_ROOT) { console.error('Set PLAYWRIGHT_ROOT to a folder with node_modules/playwright'); process.exit(2); }
const require = createRequire(process.env.PLAYWRIGHT_ROOT + '/');
const { chromium } = require('playwright');
const { HUB, ADMIN, VIEWER, SHOTS } = process.env;
let passed = 0;
function check(name, cond, detail) {
  if (!cond) { console.error('FAIL:', name, detail === undefined ? '' : detail); process.exit(1); }
  passed++; console.log('ok  ', name);
}
const browser = await chromium.launch();
const errors = [];

async function open(token, theme, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: viewport.width < 500 ? 2 : 1 });
  await ctx.addInitScript(([t, th]) => { try { localStorage.setItem('pi_hub_token', t); localStorage.setItem('pihub-theme', th); } catch (e) { /* frame */ } }, [token, theme]);
  const page = await ctx.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/401|Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(HUB + '/');
  await page.waitForTimeout(800);
  await page.evaluate(() => { const n = [...document.querySelectorAll('.nav-item')].find(n => n.textContent.trim() === 'Flights'); n.click(); });
  const frame = page.frameLocator('iframe.plg-frame');
  await frame.locator('tbody tr').nth(5).waitFor({ timeout: 15000 });
  return { ctx, page, frame };
}
const frameEval = (page, fn) => page.frames().find(f => f.url().includes('/frame/') || f !== page.mainFrame()).evaluate(fn);

// Admin, desktop, dark
const a = await open(ADMIN, 'dark', { width: 1366, height: 900 });
const pill = a.page.locator('[data-slot="header.pill"] .ph-badge');
await pill.waitFor({ timeout: 10000 });
check('header pill shows the count', /^✈︎ \d+$/.test(await pill.innerText()), await pill.innerText());
check('pill title', /^\d+ aircraft \(\d+ with position\)$/.test(await pill.getAttribute('title')));
const ph = await pill.evaluate(e => e.getBoundingClientRect().height);
const hp = await a.page.locator('.hpill').first().evaluate(e => e.getBoundingClientRect().height).catch(() => 26);
check('pill as high as core pills', Math.abs(ph - hp) < 1.5, ph + ' vs ' + hp);
check('six stat tiles', await a.frame.locator('.tile').count() === 6);
check('aircraft now = 33', (await a.frame.locator('.tile .value').first().innerText()).trim() === '33');
check('33 rows', await a.frame.locator('tbody tr').count() === 33);
await a.frame.locator('.attrib', { hasText: 'Test tiles' }).waitFor({ timeout: 10000 });
check('attribution shown', true);
await a.page.waitForTimeout(1500);
const painted = await frameEval(a.page, () => {
  const c = document.querySelector('.mapwrap canvas'), x = c.getContext('2d');
  const d = x.getImageData(0, 0, c.width, c.height).data; let n = 0;
  for (let i = 0; i < d.length; i += 4 * 97) if (d[i] !== d[0] || d[i + 1] !== d[1]) n++;
  return n;
});
check('map canvas painted', painted > 50, painted);
// sort by altitude
await a.frame.locator('th', { hasText: 'Altitude' }).click();
const firstAlt = await a.frame.locator('tbody tr').first().locator('td').nth(4).innerText();
check('sorted by altitude (highest first)', Number(firstAlt.replace(/,/g, '')) >= 38000, firstAlt);
// click a row: highlight + tooltip
await a.frame.locator('tbody tr').nth(2).click();
await a.frame.locator('tbody tr.sel').waitFor();
await a.frame.locator('.tip:not([hidden])').waitFor({ timeout: 5000 });
check('row click shows the tooltip', (await a.frame.locator('.tip').innerText()).includes('Altitude'));
// zoom buttons
await a.frame.locator('button[aria-label="Zoom in"]').click();
await a.frame.locator('button[aria-label="Zoom out"]').click();
await a.frame.locator('button', { hasText: 'Center on receiver' }).click();
check('map controls work', true);
// charts + links + records
check('two charts', await a.frame.locator('.chart canvas').count() === 2);
await a.frame.locator('.link').first().waitFor({ timeout: 8000 });
check('three links', await a.frame.locator('.link').count() === 3);
await a.frame.locator('.link[data-state="online"]').waitFor({ timeout: 12000 });
check('probe: online', true);
await a.frame.locator('.link[data-state="offline"]').waitFor({ timeout: 12000 });
check('probe: offline', true);
check('records shown', (await a.frame.locator('.card', { hasText: 'Records' }).innerText()).includes('TST070'));
check('receiver signal shown', (await a.frame.locator('.card').filter({ has: a.frame.getByRole('heading', { name: 'Receiver', exact: true }) }).innerText()).includes('-12.3 dBFS'));
await a.frame.locator('.chip', { hasText: '7 days' }).click();
await a.frame.locator('.chip', { hasText: '24 h' }).click();
if (SHOTS) {
  await a.frame.locator('tbody tr.sel').click();      // deselect for a clean shot
  await a.page.waitForTimeout(600);
  await a.page.screenshot({ path: SHOTS + '/flights-desktop-dark.png', fullPage: true });
}
// Links in the frame open through ph.open (8.3.1): a new page, or the
// dashboard's "Open link" dialog when the browser blocks the popup.
const linkBtn = a.frame.locator('button.link', { hasText: 'tar1090' });
await linkBtn.waitFor({ timeout: 8000 });
const popup = a.ctx.waitForEvent('page', { timeout: 6000 }).catch(() => null);
await linkBtn.click();
let newPage = await popup;
if (!newPage) {
  const anchor = a.page.locator('a[target="_blank"]', { hasText: /open/i }).last();
  await anchor.waitFor({ timeout: 4000 });
  const p2 = a.ctx.waitForEvent('page', { timeout: 6000 });
  await anchor.click();
  newPage = await p2;
}
await newPage.waitForLoadState('domcontentloaded');
check('frame link opens a new page', newPage.url().includes('/tar1090/'), newPage.url());
await newPage.close();
await a.frame.locator('.card p', { hasText: 'Opening tar1090' }).waitFor({ timeout: 5000 }).catch(() => {});

// The pill opens the Flights tab
await a.page.evaluate(() => document.querySelector('.nav-item[data-go="hosts"]').click());
await a.page.waitForTimeout(400);
await a.page.locator('[data-slot="header.pill"] .ph-badge').click();
await a.page.waitForTimeout(600);
const current = await a.page.evaluate(() => (document.querySelector('.nav-item[aria-current="page"]') || {}).textContent || '');
check('clicking the pill opens Flights', current.trim() === 'Flights', current);
check('pill is a link', (await a.page.locator('[data-slot="header.pill"] [role="link"]').count()) >= 1);

// The declarative tab: links open natively
await a.page.evaluate(() => { const n = [...document.querySelectorAll('.nav-item')].find(n => n.textContent.trim() === 'ADS-B receiver'); n.click(); });
const link = a.page.locator('a.ph-link', { hasText: 'tar1090' });
await link.waitFor({ timeout: 8000 });
check('receiver tab link opens in a new tab', await link.getAttribute('target') === '_blank');
if (SHOTS) await a.page.screenshot({ path: SHOTS + '/receiver-tab-dark.png' });
await a.ctx.close();

// Light
const l = await open(ADMIN, 'light', { width: 1366, height: 900 });
await l.frame.locator('.attrib', { hasText: 'Test tiles' }).waitFor({ timeout: 10000 });
await l.page.waitForTimeout(1500);
const bg = await frameEval(l.page, () => getComputedStyle(document.querySelector('.tile')).backgroundColor);
check('light theme reaches the frame', bg !== 'rgba(0, 0, 0, 0)', bg);
if (SHOTS) await l.page.screenshot({ path: SHOTS + '/flights-desktop-light.png', fullPage: true });
await l.ctx.close();

// Phone, viewer
const v = await open(VIEWER, 'dark', { width: 390, height: 844 });
await v.page.waitForTimeout(1500);
const overflow = await frameEval(v.page, () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
check('no horizontal overflow at 390 px', overflow <= 0, overflow);
check('viewer sees the data', await v.frame.locator('tbody tr').count() === 33);
if (SHOTS) {
  await v.page.screenshot({ path: SHOTS + '/flights-390-dark.png', fullPage: true });
  await v.page.locator('iframe.plg-frame').screenshot({ path: SHOTS + '/flights-390-frame.png' });
}
await v.ctx.close();

await browser.close();
check('no page errors', errors.length === 0, errors.join('\n'));
console.log(passed + ' checks passed');
