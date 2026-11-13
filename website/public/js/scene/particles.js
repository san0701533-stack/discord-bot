// 数千個の銀粒子。位置の補間（集合・分裂・爆発）は GPU のシェーダーで計算する。
// morph(from, to, options) で「どこから → どこへ」を指定し、progress を 0→1 に動かすだけで動く。
import * as THREE from "three";

const vertexShader = /* glsl */ `
  attribute vec3 aFrom;
  attribute vec3 aTo;
  attribute float aDelay;
  attribute float aSize;
  attribute float aSeed;
  uniform float uProgress;
  uniform float uTime;
  uniform float uSize;
  uniform float uPixelRatio;
  uniform float uSwirl;
  uniform float uBulge;
  uniform float uSpread;
  varying float vSpark;
  varying float vFade;

  float easeInOut(float t) { return t < 0.5 ? 4.0 * t * t * t : 1.0 - pow(-2.0 * t + 2.0, 3.0) / 2.0; }

  vec3 rotateY(vec3 p, float a) {
    float c = cos(a), s = sin(a);
    return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z);
  }

  void main() {
    float t = clamp((uProgress - aDelay * uSpread) / max(0.0001, 1.0 - uSpread), 0.0, 1.0);
    float e = easeInOut(t);
    vec3 p = mix(aFrom, aTo, e);
    // 渦を巻きながら集まる
    p = rotateY(p, (1.0 - e) * uSwirl * (0.6 + aSeed));
    // 移動中は外側へふくらむ（分裂・爆発の弧）
    float arc = sin(e * 3.14159);
    p += normalize(p + vec3(0.0001)) * arc * uBulge * (0.4 + aSeed);
    // 微細な揺らぎ
    p += vec3(sin(uTime * 1.3 + aSeed * 40.0), cos(uTime * 1.1 + aSeed * 31.0), sin(uTime * 0.9 + aSeed * 17.0)) * 0.006;

    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    // 移動中は少し大きく光る
    float boost = 1.0 + arc * 0.6;
    gl_PointSize = aSize * uSize * boost * uPixelRatio * (12.0 / -mv.z);
    vSpark = 0.5 + 0.5 * sin(uTime * 3.0 + aSeed * 60.0);
    vFade = smoothstep(0.0, 0.15, t + (1.0 - step(0.0001, uSpread)));
  }
`;

const fragmentShader = /* glsl */ `
  uniform float uOpacity;
  varying float vSpark;
  varying float vFade;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c);
    if (d > 0.5) discard;
    float core = smoothstep(0.5, 0.0, d);
    float glow = pow(core, 2.4);
    vec3 silver = mix(vec3(0.62, 0.66, 0.72), vec3(1.0, 1.0, 1.02), glow);
    silver += vSpark * glow * 0.25;
    gl_FragColor = vec4(silver, core * uOpacity * mix(0.55, 1.0, vFade));
  }
`;

export class ParticleField {
  constructor(count, pixelRatio) {
    this.count = count;
    const geo = new THREE.BufferGeometry();
    this.from = new Float32Array(count * 3);
    this.to = new Float32Array(count * 3);
    const delay = new Float32Array(count);
    const size = new Float32Array(count);
    const seed = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      seed[i] = Math.random();
      size[i] = 0.35 + Math.pow(Math.random(), 3) * 1.4; // ほとんどは小粒、ときどき大粒
      delay[i] = Math.random();
    }
    geo.setAttribute("position", new THREE.BufferAttribute(this.to, 3)); // バウンディング用
    geo.setAttribute("aFrom", new THREE.BufferAttribute(this.from, 3));
    geo.setAttribute("aTo", new THREE.BufferAttribute(this.to, 3));
    geo.setAttribute("aDelay", new THREE.BufferAttribute(delay, 1));
    geo.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
    geo.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
    this.delay = delay;
    this.geometry = geo;

    this.uniforms = {
      uProgress: { value: 0 },
      uTime: { value: 0 },
      uSize: { value: 1 },
      uPixelRatio: { value: pixelRatio },
      uSwirl: { value: 0 },
      uBulge: { value: 0 },
      uSpread: { value: 0.35 },
      uOpacity: { value: 1 },
    };
    this.material = new THREE.ShaderMaterial({
      vertexShader,
      fragmentShader,
      uniforms: this.uniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    this.points = new THREE.Points(geo, this.material);
    this.points.frustumCulled = false;
  }

  // from/to: (index, out:Vector3) => void で各粒子の始点・終点を決める
  morph(fromFn, toFn, { delayFn } = {}) {
    const v = new THREE.Vector3();
    for (let i = 0; i < this.count; i++) {
      fromFn(i, v); this.from[i * 3] = v.x; this.from[i * 3 + 1] = v.y; this.from[i * 3 + 2] = v.z;
      toFn(i, v); this.to[i * 3] = v.x; this.to[i * 3 + 1] = v.y; this.to[i * 3 + 2] = v.z;
      if (delayFn) this.delay[i] = delayFn(i);
    }
    const a = this.geometry.attributes;
    a.aFrom.needsUpdate = true;
    a.aTo.needsUpdate = true;
    a.position.needsUpdate = true;
    if (delayFn) a.aDelay.needsUpdate = true;
    this.uniforms.uProgress.value = 0;
  }

  // 現在の見かけの位置（progress=1 の終点）を返す。次の morph の始点に使う。
  currentTo(i, out) {
    return out.set(this.to[i * 3], this.to[i * 3 + 1], this.to[i * 3 + 2]);
  }

  update(t) { this.uniforms.uTime.value = t; }
  setPixelRatio(dpr) { this.uniforms.uPixelRatio.value = dpr; }
}

// 球面上にほぼ均等に点を置く（フィボナッチ球）
export function fibonacciSphere(i, n, radius, out) {
  const y = 1 - (i / (n - 1)) * 2;
  const r = Math.sqrt(1 - y * y);
  const theta = i * 2.399963229728653; // 黄金角
  return out.set(Math.cos(theta) * r * radius, y * radius, Math.sin(theta) * r * radius);
}

// 画面の外側（カメラから見て遠く・広い殻）のランダムな点
export function offscreenPoint(out, spreadX, spreadY) {
  const angle = Math.random() * Math.PI * 2;
  const k = 1.15 + Math.random() * 0.9;
  return out.set(Math.cos(angle) * spreadX * k, Math.sin(angle) * spreadY * k, (Math.random() - 0.5) * 8);
}
