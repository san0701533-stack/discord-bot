// 鏡面シルバーの3D球体。
// 完全な鏡面球は回しても見た目が変わらないため、表面に精密な刻線（緯線・経線・パネル継ぎ目）を
// バンプ／粗さマップとして刻み、回転が目で分かるようにしている。
import * as THREE from "three";

let sharedSurface = null;

function buildSurfaceTextures(size) {
  const w = size, h = size / 2;
  const bump = document.createElement("canvas");
  bump.width = w; bump.height = h;
  const b = bump.getContext("2d");
  b.fillStyle = "#808080";
  b.fillRect(0, 0, w, h);

  const rough = document.createElement("canvas");
  rough.width = w; rough.height = h;
  const r = rough.getContext("2d");
  r.fillStyle = "#1a1a1a"; // 基本はほぼ鏡面
  r.fillRect(0, 0, w, h);

  const line = (ctx, color, width, x1, y1, x2, y2) => {
    ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
  };
  const px = size / 2048;

  // 赤道の太い溝と、細い緯線
  const lat = (deg, width) => {
    const y = h / 2 - (deg / 90) * (h / 2);
    line(b, "#3a3a3a", width * px, 0, y, w, y);
    line(r, "#5a5a5a", width * px * 1.6, 0, y, w, y);
  };
  lat(0, 7);
  for (const d of [12, -12]) lat(d, 2.2);
  for (const d of [38, -38, 62, -62]) lat(d, 1.4);

  // 経線（12分割）と、細かなパネル継ぎ目
  for (let i = 0; i < 12; i++) {
    const x = (i / 12) * w;
    const width = i % 3 === 0 ? 3 : 1.2;
    line(b, "#454545", width * px, x, h * 0.06, x, h * 0.94);
    line(r, "#4a4a4a", width * px * 1.5, x, h * 0.06, x, h * 0.94);
  }
  // 赤道帯の短い目盛り（精密機械のような印象）
  for (let i = 0; i < 180; i++) {
    const x = (i / 180) * w;
    const len = i % 5 === 0 ? 22 : 10;
    line(b, "#5c5c5c", 1 * px, x, h / 2 + 10 * px, x, h / 2 + (10 + len) * px);
  }

  const toTex = (canvas) => {
    const t = new THREE.CanvasTexture(canvas);
    t.wrapS = THREE.RepeatWrapping;
    t.anisotropy = 4;
    return t;
  };
  return { bumpMap: toTex(bump), roughnessMap: toTex(rough) };
}

export function createOrbMaterial(quality) {
  if (!sharedSurface) sharedSurface = buildSurfaceTextures(quality.textureSize);
  return new THREE.MeshPhysicalMaterial({
    color: 0xe6e9ee,
    metalness: 1,
    roughness: 1, // roughnessMap の値をそのまま使う
    roughnessMap: sharedSurface.roughnessMap,
    bumpMap: sharedSurface.bumpMap,
    bumpScale: 1.6,
    clearcoat: 0.6,
    clearcoatRoughness: 0.08,
    envMapIntensity: 1.25,
    transparent: false,
  });
}

export function createOrb(quality, radius = 1) {
  const geo = new THREE.SphereGeometry(radius, quality.sphereSegments, Math.round(quality.sphereSegments / 2));
  const mesh = new THREE.Mesh(geo, createOrbMaterial(quality));
  const group = new THREE.Group();
  group.add(mesh);
  group.userData.mesh = mesh;
  return group;
}

// 球体の背後のごく淡い後光（漆黒の背景で球体の輪郭を浮かび上がらせる）
export function createBackGlow(radius = 1) {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(128, 128, 0, 128, 128, 128);
  grad.addColorStop(0, "rgba(200,210,225,0.32)");
  grad.addColorStop(0.35, "rgba(150,160,180,0.12)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 256);
  const mat = new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.setScalar(radius * 5.5);
  return sprite;
}
