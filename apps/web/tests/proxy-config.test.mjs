import assert from "node:assert/strict";
import { test } from "node:test";

test("proxy uses the server build origin, independently of the browser API URL", async (t) => {
  const originalInternal = process.env.INTERNAL_API_URL;
  const originalPublic = process.env.NEXT_PUBLIC_API_URL;
  try {
    for (const [internal, expected] of [
      [undefined, "http://localhost:8000"],
      ["http://api:8000", "http://api:8000"],
      ["http://127.0.0.1:8100/", "http://127.0.0.1:8100"],
    ]) {
      await t.test(expected, async () => {
        if (internal === undefined) delete process.env.INTERNAL_API_URL;
        else process.env.INTERNAL_API_URL = internal;
        process.env.NEXT_PUBLIC_API_URL = "https://public-api.example";
        const { default: config } = await import(`../next.config.mjs?target=${expected}`);
        assert.deepEqual(await config.rewrites(), [
          { source: "/api/:path*", destination: `${expected}/api/:path*` },
          { source: "/health/:path*", destination: `${expected}/health/:path*` },
        ]);
      });
    }
  } finally {
    if (originalInternal === undefined) delete process.env.INTERNAL_API_URL;
    else process.env.INTERNAL_API_URL = originalInternal;
    if (originalPublic === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = originalPublic;
  }
});
