import { ok, repo, spineRepo, sourceRepo, hasPanelKey, preflight } from "./_lib.js";

export default async function handler(req, res) {
  if (preflight(req, res)) return;
  ok(res, {
    ok: true,
    repository: repo(),
    spine_repo: spineRepo(),
    source_repo: sourceRepo(),
    panel_key_required: hasPanelKey(),
    stages: [
      "acquire", "unpack", "detect", "unity", "spine",
      "publish-spine", "publish-source", "selftest",
    ],
  });
}
