import { chromium } from "playwright";
const dir = "/tmp/claude-1000/-projects-sentinel-rag/2ab9a06f-e9ec-41be-bea3-5dc3a185e693/scratchpad";
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
await page.goto("http://localhost:3000", { waitUntil: "networkidle", timeout: 30000 });

const btn = page.getByRole("button", { name: /Platform status/i }).first();
await btn.waitFor({ state: "visible", timeout: 10000 });
await btn.click();
await page.waitForTimeout(1500);
try { await page.locator('button:near(:text("Failed to connect to LangGraph"))').first().click({ timeout: 1000 }); } catch {}
await page.screenshot({ path: `${dir}/60-panel-width.png` });

const panelWidth = await page.evaluate(() => {
  const el = document.querySelector('.grid > div:nth-child(2)');
  return el ? el.getBoundingClientRect().width : null;
});
console.log("viewport 1400, platform panel width:", panelWidth, "=", panelWidth ? (panelWidth / 1400 * 100).toFixed(1) + "%" : "?");

await page.locator('svg.lucide-x').last().click().catch(() => {});
await page.waitForTimeout(500);

// open thread history via the panel toggle button (top-left)
await page.locator('button:has(svg.lucide-panel-right-close), button:has(svg.lucide-panel-right-open)').first().click();
await page.waitForTimeout(600);
const historyWidth = await page.evaluate(() => {
  const el = document.querySelector('.absolute.z-20.h-full');
  return el ? el.getBoundingClientRect().width : null;
});
console.log("thread history width:", historyWidth);
await page.screenshot({ path: `${dir}/61-history-width.png` });

await browser.close();
