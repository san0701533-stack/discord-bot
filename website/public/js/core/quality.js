// 端末性能に応じた画質プリセット。URL に ?q=low|mid|high を付けると強制指定できる。
const PRESETS = {
  high: { name: "high", particles: 9000, maxDpr: 2, bloom: true, sphereSegments: 160, textureSize: 2048, antialias: true },
  mid: { name: "mid", particles: 5500, maxDpr: 2, bloom: true, sphereSegments: 112, textureSize: 1024, antialias: true },
  low: { name: "low", particles: 2800, maxDpr: 1.5, bloom: false, sphereSegments: 72, textureSize: 1024, antialias: false },
};

export function isTouchDevice() {
  return matchMedia("(pointer: coarse)").matches || navigator.maxTouchPoints > 1;
}

export function detectQuality() {
  const forced = new URLSearchParams(location.search).get("q");
  if (forced && PRESETS[forced]) return { ...PRESETS[forced] };

  const ua = navigator.userAgent;
  const mobile = /Android|iPhone|iPad|iPod/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const cores = navigator.hardwareConcurrency || 4;
  const memory = navigator.deviceMemory || 4;

  let tier;
  if (!mobile) tier = cores >= 6 ? "high" : "mid";
  else if (/iPhone|iPad|Macintosh/.test(ua)) tier = "mid"; // iOS は概ね高性能（deviceMemory 非公開）
  else tier = cores >= 8 && memory >= 6 ? "mid" : "low";
  return { ...PRESETS[tier] };
}

export function prefersReducedMotion() {
  return matchMedia("(prefers-reduced-motion: reduce)").matches;
}
