// レンダラー・カメラ・環境光・ポストエフェクト・描画ループをまとめた土台。
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

// 鏡面シルバーを映えさせるための「漆黒のスタジオ」環境マップ。
// 黒い空間にソフトボックスとストリップライトを置き、その映り込みで金属感を出す。
function buildStudioEnvironment(renderer) {
  const env = new THREE.Scene();
  env.background = new THREE.Color(0x000000);
  const panel = (w, h, intensity, pos, rot = [0, 0, 0]) => {
    const mat = new THREE.MeshBasicMaterial({ color: new THREE.Color(intensity, intensity, intensity * 1.04), side: THREE.DoubleSide });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat);
    mesh.position.set(...pos);
    mesh.rotation.set(...rot);
    env.add(mesh);
  };
  // 天井の大きなソフトボックス（上半分に銀色のグラデーションを作る）
  panel(22, 12, 1.9, [0, 9, -1], [Math.PI / 2, 0, 0]);
  // 背面のごく淡い大パネル（真っ黒になりすぎず、銀の地色を残す）
  panel(40, 14, 0.16, [0, 3, -14]);
  // 左右の細いストリップライト（縦のハイライト）
  panel(1.4, 16, 4.0, [-8, 1, 2], [0, Math.PI / 2.4, 0]);
  panel(0.6, 14, 3.2, [8, 1, -1], [0, -Math.PI / 2.2, 0]);
  // 背面の水平ライン（地平線のような横のハイライト）
  panel(26, 0.35, 2.6, [0, -0.6, -10]);
  // 手前下の弱い反射板（下側の輪郭を出す）
  panel(18, 6, 0.35, [0, -7, 4], [-Math.PI / 2.4, 0, 0]);
  // 斜め上の小さなキーライト（鋭いスペキュラー）
  panel(2.2, 2.2, 7.0, [5, 5, 7], [0, -0.6, 0]);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const texture = pmrem.fromScene(env, 0.02).texture;
  pmrem.dispose();
  return texture;
}

export class Stage {
  constructor(canvas, quality) {
    this.quality = quality;
    this.renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: quality.antialias,
      alpha: false,
      powerPreference: "high-performance",
    });
    this.renderer.setClearColor(0x000000, 1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.scene.environment = buildStudioEnvironment(this.renderer);
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 200);
    this.camera.position.set(0, 0, 7);

    this.dpr = Math.min(window.devicePixelRatio || 1, quality.maxDpr);
    this.composer = null;
    if (quality.bloom) {
      this.composer = new EffectComposer(this.renderer);
      this.composer.addPass(new RenderPass(this.scene, this.camera));
      this.bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.32, 0.35, 0.93);
      this.composer.addPass(this.bloom);
      this.composer.addPass(new OutputPass());
    }

    this.noAdapt = new URLSearchParams(location.search).get("adapt") === "0"; // 動作確認用
    this.updaters = new Set();
    this.resizeHandlers = new Set();
    this.clock = new THREE.Clock();
    this.frameTimes = [];
    this.lastAdapt = 0;

    this.resize = this.resize.bind(this);
    window.addEventListener("resize", this.resize);
    this.resize();
  }

  get width() { return this.renderer.domElement.clientWidth; }
  get height() { return this.renderer.domElement.clientHeight; }
  get aspect() { return this.width / Math.max(1, this.height); }

  onUpdate(fn) { this.updaters.add(fn); return () => this.updaters.delete(fn); }
  onResize(fn) { this.resizeHandlers.add(fn); fn(); return () => this.resizeHandlers.delete(fn); }

  resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.renderer.setPixelRatio(this.dpr);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (this.composer) {
      this.composer.setPixelRatio(this.dpr);
      this.composer.setSize(w, h);
      // ブルームは半分の解像度で十分（負荷軽減）
      this.bloom.resolution.set((w * this.dpr) / 2, (h * this.dpr) / 2);
    }
    for (const fn of this.resizeHandlers) fn();
  }

  // ある深さ（カメラからの距離）で見える横幅・縦幅（ワールド単位）
  visibleSizeAt(distance) {
    const h = 2 * distance * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2));
    return { width: h * this.camera.aspect, height: h };
  }

  // 実際の描画速度を見て解像度を自動で下げる（重い端末でも滑らかさを優先）
  adapt(dt, now) {
    if (this.noAdapt) return;
    this.frameTimes.push(dt);
    if (this.frameTimes.length > 90) this.frameTimes.shift();
    if (now - this.lastAdapt < 2 || this.frameTimes.length < 60) return;
    const avg = this.frameTimes.reduce((a, b) => a + b, 0) / this.frameTimes.length;
    if (avg > 1 / 42 && this.dpr > 1) {
      this.dpr = Math.max(1, this.dpr - 0.25);
      this.lastAdapt = now;
      this.frameTimes.length = 0;
      this.resize();
    } else if (avg > 1 / 30 && this.composer) {
      this.composer = null; // それでも重ければブルームを切る
      this.lastAdapt = now;
      this.frameTimes.length = 0;
    }
  }

  start() {
    const loop = () => {
      const dt = Math.min(this.clock.getDelta(), 0.05);
      const t = this.clock.elapsedTime;
      for (const fn of this.updaters) fn(dt, t);
      if (this.composer) this.composer.render(dt);
      else this.renderer.render(this.scene, this.camera);
      this.adapt(dt, t);
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }
}
