// RSGY Webサイト用の軽量 Node.js サーバー（外部依存なし）
// - public/ を静的配信
// - /vendor/three, /vendor/gsap を node_modules から配信（ビルド不要）
// - 拡張子のないパス（/boosting など）は index.html を返す（ページ遷移はフロント側で処理）
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(ROOT, "public");
const VENDOR = {
  "/vendor/three/": path.join(ROOT, "node_modules", "three"),
  "/vendor/gsap/": path.join(ROOT, "node_modules", "gsap"),
  "/vendor/fonts/": path.join(ROOT, "node_modules", "@fontsource"),
};
const PORT = Number(process.env.PORT) || 3000;
const HOST = process.env.HOST || "0.0.0.0";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".txt": "text/plain; charset=utf-8",
};
const COMPRESSIBLE = new Set([".html", ".js", ".mjs", ".css", ".json", ".svg", ".txt"]);

// ベースディレクトリの外へ出るパスを拒否する
function safeJoin(base, rel) {
  const target = path.normalize(path.join(base, rel));
  return target === base || target.startsWith(base + path.sep) ? target : null;
}

function resolveFile(urlPath) {
  for (const [prefix, dir] of Object.entries(VENDOR)) {
    if (urlPath.startsWith(prefix)) {
      return { file: safeJoin(dir, urlPath.slice(prefix.length)), longCache: true };
    }
  }
  if (urlPath === "/" || !path.extname(urlPath)) {
    return { file: path.join(PUBLIC_DIR, "index.html"), longCache: false };
  }
  return { file: safeJoin(PUBLIC_DIR, urlPath), longCache: false };
}

const server = http.createServer((req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" }).end();
    return;
  }
  let urlPath;
  try {
    urlPath = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
  } catch {
    res.writeHead(400).end("Bad Request");
    return;
  }

  const { file, longCache } = resolveFile(urlPath);
  if (!file) {
    res.writeHead(403).end("Forbidden");
    return;
  }

  fs.stat(file, (err, stat) => {
    if (err || !stat.isFile()) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not Found");
      return;
    }
    const ext = path.extname(file).toLowerCase();
    const headers = {
      "Content-Type": MIME[ext] || "application/octet-stream",
      "Cache-Control": longCache ? "public, max-age=604800" : "no-cache",
      "X-Content-Type-Options": "nosniff",
    };
    const acceptsGzip = /\bgzip\b/.test(req.headers["accept-encoding"] || "");
    if (COMPRESSIBLE.has(ext) && acceptsGzip) {
      headers["Content-Encoding"] = "gzip";
      headers["Vary"] = "Accept-Encoding";
      res.writeHead(200, headers);
      if (req.method === "HEAD") return res.end();
      fs.createReadStream(file).pipe(zlib.createGzip()).pipe(res);
    } else {
      headers["Content-Length"] = stat.size;
      res.writeHead(200, headers);
      if (req.method === "HEAD") return res.end();
      fs.createReadStream(file).pipe(res);
    }
  });
});

server.listen(PORT, HOST, () => {
  console.log(`RSGY website: http://localhost:${PORT}`);
});
