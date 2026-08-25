/** Additive Gmail wake bridge for JobOS.
 *
 * The OpenClaw Gmail mapping still performs its original agent/delivery action.
 * This transform sends only a minimal wake signal to a loopback JobOS listener
 * and returns an empty override so OpenClaw preserves the base hook action.
 * Raw email content is intentionally never forwarded to JobOS here.
 */
export async function transformGmail(ctx) {
  const token = (process.env.JOBOS_GMAIL_WAKE_TOKEN || "").trim();
  if (!token) return {};
  const url = (process.env.JOBOS_GMAIL_WAKE_URL || "http://127.0.0.1:8791/gmail").trim();
  const messageId = ctx?.payload?.messages?.[0]?.id ?? null;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);
    try {
      await fetch(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-jobos-wake-token": token,
        },
        body: JSON.stringify({ message_id: messageId }),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  } catch {
    // Wake delivery is acceleration only. The bounded Spam-inclusive fallback
    // watcher preserves correctness if this local bridge is unavailable.
  }
  return {};
}
