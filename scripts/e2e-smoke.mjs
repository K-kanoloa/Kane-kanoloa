import process from "node:process";

const apiBase = process.env.E2E_API_BASE_URL ?? "http://127.0.0.1:8000";
const bridgeBase = process.env.E2E_BRIDGE_BASE_URL ?? "http://127.0.0.1:8010";
const webBase = process.env.E2E_WEB_BASE_URL ?? "http://localhost:3000";
const navTimeoutMs = Number(process.env.E2E_NAV_TIMEOUT_MS ?? "60000");

const errorTextPatterns = [
  /API request failed/i,
  /API\s*\u8bf7\u6c42\u5931\u8d25/i,
  /NEXT_PUBLIC_API_BASE_URL/i,
];

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function fetchJson(url, options = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    let json = null;
    try {
      json = JSON.parse(text);
    } catch {
      // Keep json null; caller can still inspect text.
    }
    return { response, text, json };
  } finally {
    clearTimeout(timeout);
  }
}

async function checkEndpoint(label, url) {
  const started = performance.now();
  let result;
  try {
    result = await fetchJson(url);
  } catch (error) {
    throw new Error(
      `${label} is not reachable at ${url}: ${
        error instanceof Error ? error.message : String(error)
      }. Ensure services are running before running E2E smoke.`
    );
  }
  const { response, json, text } = result;
  const elapsedMs = Math.round(performance.now() - started);
  assert(response.ok, `${label} failed: ${response.status} ${text.slice(0, 200)}`);
  assert(json?.status === "ok", `${label} expected status "ok", got ${JSON.stringify(json)}`);
  return { label, url, status: response.status, elapsedMs, statusValue: json?.status ?? null };
}

async function checkWebWorkspace(page) {
  const url = `${webBase}/`;
  const badResponses = [];
  const failedRequests = [];
  const consoleErrors = [];

  const onResponse = (response) => {
    const responseUrl = response.url();
    const status = response.status();
    if ((responseUrl.startsWith(apiBase) || responseUrl.startsWith(webBase)) && status >= 400) {
      badResponses.push({ url: responseUrl, status });
    }
  };
  const onRequestFailed = (request) => {
    const failure = request.failure()?.errorText ?? "request_failed";
    if (!request.url().includes("hot-update") && !failure.includes("ERR_ABORTED")) {
      failedRequests.push({ url: request.url(), failure });
    }
  };
  const onConsole = (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  };
  const onPageError = (error) => {
    consoleErrors.push(error.message);
  };

  page.on("response", onResponse);
  page.on("requestfailed", onRequestFailed);
  page.on("console", onConsole);
  page.on("pageerror", onPageError);

  try {
    const started = performance.now();
    const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: navTimeoutMs });
    const elapsedMs = Math.round(performance.now() - started);
    await page.waitForTimeout(2000);

    assert(response?.ok(), `Web workspace navigation failed: ${response?.status() ?? "no_response"}`);

    const bodyText = await page.locator("body").innerText({ timeout: 10000 });
    await page.locator(".workspace #conversation-main").waitFor({ state: "visible" });
    assert(/Kane/i.test(bodyText), "Web workspace did not render Kane branding");
    assert(!errorTextPatterns.some((pattern) => pattern.test(bodyText)), "Web workspace rendered API error text");
    assert(badResponses.length === 0, `Web workspace had bad HTTP responses (>=400): ${JSON.stringify(badResponses)}`);
    assert(failedRequests.length === 0, `Web workspace had failed requests: ${JSON.stringify(failedRequests)}`);
    assert(consoleErrors.length === 0, `Web workspace had console errors: ${JSON.stringify(consoleErrors.slice(0, 3))}`);

    return { path: "/", status: response.status(), elapsedMs, ok: true };
  } finally {
    page.off("response", onResponse);
    page.off("requestfailed", onRequestFailed);
    page.off("console", onConsole);
    page.off("pageerror", onPageError);
  }
}

async function main() {
  console.log("=== Kane vNext Stack Smoke Test ===");

  // 1. Check API /health
  const apiHealth = await checkEndpoint("api:health", `${apiBase}/health`);
  console.log(`[PASS] API health: ${apiHealth.status} (${apiHealth.elapsedMs}ms)`);

  // 2. Check Bridge /health
  const bridgeHealth = await checkEndpoint("bridge:health", `${bridgeBase}/health`);
  console.log(`[PASS] Bridge health: ${bridgeHealth.status} (${bridgeHealth.elapsedMs}ms)`);

  // Read-only workspace check; agent execution belongs to test:e2e:ui.
  let chromium;
  try {
    const playwright = await import("playwright");
    chromium = playwright.chromium;
  } catch (err) {
    throw new Error(`Failed to import Playwright: ${err.message}`);
  }

  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const webResult = await checkWebWorkspace(page);
    console.log(`[PASS] Web workspace loaded: HTTP ${webResult.status} (${webResult.elapsedMs}ms), 0 console errors, 0 bad responses`);
  } finally {
    await browser.close();
  }

  console.log("=== All stack smoke checks PASSED ===");
}

main().catch((err) => {
  console.error("[FAIL] Stack smoke test failed:", err);
  process.exit(1);
});
