#!/usr/bin/env node
// Upload apk2source Cloudflare Worker via REST API with correct ES-module content-type.
const fs = require("fs");
const path = require("path");

const TOKEN = process.env.CF_TOKEN;
const ACC = process.env.CF_ACCOUNT || "66130f1dbb60b0cf0ddcad52ebe645d1";
const WORKER = process.env.CF_WORKER || "apk2source-api";
const SCRIPT_PATH = path.resolve(process.env.SCRIPT || "backend/worker.js");

if (!TOKEN) { console.error("CF_TOKEN required"); process.exit(2); }
if (!fs.existsSync(SCRIPT_PATH)) { console.error("script not found:", SCRIPT_PATH); process.exit(2); }

const boundary = "----FormBoundary" + Math.random().toString(36).slice(2);
const body = fs.readFileSync(SCRIPT_PATH);
const meta = JSON.stringify({
  main_module: "worker.mjs",
  type: "esm",
  compatibility_date: "2025-02-01",
  compatibility_flags: ["nodejs_compat"],
  workers_dev: true,
});

function buildPart(headers, data) {
  let part = `--${boundary}\r\n`;
  for (const [k, v] of Object.entries(headers)) part += `${k}: ${v}\r\n`;
  part += `\r\n`;
  return Buffer.concat([Buffer.from(part, "utf8"), data, Buffer.from("\r\n")]);
}

const parts = [];
parts.push(buildPart(
  { "Content-Disposition": 'form-data; name="main_module"; filename="worker.mjs"',
    "Content-Type": "text/javascript" },
  body
));
parts.push(buildPart(
  { "Content-Disposition": 'form-data; name="metadata"', "Content-Type": "application/json" },
  Buffer.from(meta, "utf8")
));
parts.push(Buffer.from(`--${boundary}--\r\n`));

const payload = Buffer.concat(parts);

const req = require("https").request(
  {
    hostname: "api.cloudflare.com",
    path: `/client/v4/accounts/${ACC}/workers/scripts/${WORKER}`,
    method: "PUT",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": `multipart/form-data; boundary=${boundary}`,
      "Content-Length": payload.length,
    },
  },
  (res) => {
    let data = "";
    res.on("data", (c) => (data += c));
    res.on("end", () => {
      console.log("HTTP", res.statusCode);
      console.log(data.slice(0, 2000));
      process.exit(res.statusCode >= 200 && res.statusCode < 300 ? 0 : 1);
    });
  }
);
req.on("error", (e) => { console.error(e); process.exit(1); });
req.write(payload);
req.end();
