import readline from "node:readline";

const modulePath = String(process.env.BETTERWRIGHT_MODULE || "").trim();
if (!modulePath) throw new Error("BETTERWRIGHT_MODULE is not configured");

const { BetterWright, NetworkPolicy } = await import(modulePath);
const timezone = String(process.env.BETTERWRIGHT_TIMEZONE || "").trim();
const locale = String(process.env.BETTERWRIGHT_LOCALE || "").trim();
const browser = new BetterWright({
  home: "/work",
  headless: true,
  vault: false,
  credentialCapture: false,
  downloadPolicy: "deny",
  publicSearchPolicy: "block",
  liveView: false,
  ...(timezone ? { timezone } : {}),
  ...(locale ? { locale } : {}),
  policy: new NetworkPolicy({
    allowPrivateNetwork: false,
    allowLoopback: false,
  }),
});

const input = readline.createInterface({ input: process.stdin });
process.stdout.write('__BW_READY__{"version":1}\n');

try {
  for await (const line of input) {
    if (!line.trim()) continue;
    let request;
    try {
      request = JSON.parse(line);
      const result = await browser.run(String(request.code || ""), {
        session: String(request.session || "default"),
        timeout: Number(request.timeoutSeconds) || 30,
      });
      process.stdout.write(
        `__BW_RESULT__${JSON.stringify({ id: request.id, result })}\n`,
      );
    } catch (error) {
      process.stdout.write(
        `__BW_RESULT__${JSON.stringify({
          id: request?.id || "",
          result: { ok: false, error: String(error?.message || error) },
        })}\n`,
      );
    }
  }
} finally {
  await browser.close();
}
