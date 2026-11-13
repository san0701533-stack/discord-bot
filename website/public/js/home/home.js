// ホーム画面：オープニング → 巨大球体とRSGYロゴ → 回転で分裂 → 3D球体メニュー
import * as THREE from "three";
import { gsap } from "gsap";
import { ParticleField, fibonacciSphere, offscreenPoint } from "../scene/particles.js";
import { createOrb, createBackGlow } from "../scene/orb.js";
import { createLogo } from "../scene/logo.js";
import { createDust } from "../scene/dust.js";
import { MENU, MAX_MENU_ITEMS } from "../config/menu.js";

const R = 1;                // 巨大球体の半径
const LOGO_Z = -1.7;        // ロゴを置く奥行き（球体の背後）
// ロゴ文字の高さ（ワールド単位）。球体の直径(2)より小さくして球体を主役にする。
// 縦長画面では文字をさらに小さくし、限られた横幅でも球体を大きく見せる。
const logoScale = (aspect) => (aspect >= 1 ? 1.08 : aspect >= 0.75 ? 0.86 : 0.56);
const OVERLAP = 0.8;        // 球体が文字に重なる量（1未満ほど深く重なる）
const SPLIT_ANGLE = 1.25;   // 分裂までに必要な回転量（ラジアン）
const ORB_R = 0.5;          // メニュー球体の半径

export class Home {
  constructor(stage, ui, { onSelect }) {
    this.stage = stage;
    this.ui = ui;
    this.onSelect = onSelect;
    this.items = MENU.slice(0, MAX_MENU_ITEMS);
    this.state = "init";
    this.yaw = 0; this.yawVel = 0; this.pitch = 0; this.pitchTarget = 0;
    this.spun = 0;
    this.root = new THREE.Group();
    stage.scene.add(this.root);
  }

  async build() {
    const { stage } = this;
    const q = stage.quality;

    this.dust = createDust(Math.round(q.particles / 5));
    this.root.add(this.dust.points);

    this.glow = createBackGlow(R);
    this.glow.position.z = -0.6;
    this.glow.material.opacity = 0;
    this.root.add(this.glow);

    this.sphere = createOrb(q, R);
    this.sphere.scale.setScalar(0.0001);
    this.root.add(this.sphere);

    this.logo = await createLogo();
    this.logo.group.position.z = LOGO_Z;
    this.logo._o = 0; // GSAP で不透明度を動かすための値
    this.logo.setOpacity(0);
    this.root.add(this.logo.group);

    this.particles = new ParticleField(q.particles, stage.dpr);
    this.root.add(this.particles.points);

    this.orbs = this.items.map((item) => {
      const orb = createOrb(q, ORB_R);
      orb.scale.setScalar(0.0001);
      orb.visible = false;
      orb.userData.item = item;
      this.root.add(orb);
      return orb;
    });
    this.ui.buildLabels(this.items);

    stage.onResize(() => {
      this.particles.setPixelRatio(stage.dpr);
      this.layoutLogo();
      if (this.state === "menu") this.fitMenu(false);
      else if (this.state !== "init" && this.state !== "opening") this.fitLogo(false);
    });
    stage.onUpdate((dt, t) => this.update(dt, t));
    this.bindPointer();
  }

  // ---------- レイアウト ----------

  // 球体の見かけの直径に合わせて「RS」「GY」の隙間を決める（球体が文字に少し重なる）
  logoGap(camZ) {
    const k = (camZ - LOGO_Z) / camZ; // 奥にあるロゴの面での拡大率
    return (2 * R * k * OVERLAP) / this.logoScale;
  }

  get logoScale() { return logoScale(this.stage.aspect); }

  layoutLogo() {
    if (!this.logo) return;
    this.logo.group.scale.setScalar(this.logoScale);
    this.logo.layout(this.logoGap(this.stage.camera.position.z));
  }

  // RS◯GY 全体が画面に収まるカメラ距離
  logoCameraZ() {
    const { stage } = this;
    const tan = Math.tan(THREE.MathUtils.degToRad(stage.camera.fov / 2));
    let z = 7;
    for (let i = 0; i < 4; i++) {
      const width = this.logo.totalWidth(this.logoGap(z)) * this.logoScale;
      const fill = stage.aspect >= 1 ? 0.86 : 0.92;
      const dLogo = width / (fill * 2 * tan * stage.aspect); // ロゴが横幅に収まる距離
      const zW = dLogo + LOGO_Z; // カメラ〜ロゴの距離から、カメラのz位置へ
      const zH = (2 * R) / (0.6 * 2 * tan); // 球体が縦に収まる距離
      z = Math.max(zW, zH, 5.2);
    }
    return z;
  }

  fitLogo(animate = true) {
    const z = this.logoCameraZ();
    if (animate) gsap.to(this.stage.camera.position, { z, duration: 1.2, ease: "power2.inOut", onUpdate: () => this.layoutLogo() });
    else { this.stage.camera.position.z = z; this.layoutLogo(); }
  }

  // メニュー球体の配置（横長画面は横一列、縦長画面は縦一列。項目数に応じて自動配置）
  menuPositions() {
    const n = this.orbs.length;
    const landscape = this.stage.aspect >= 1;
    const gap = landscape ? 1.9 : 1.75;
    return this.orbs.map((_, i) => {
      const o = i - (n - 1) / 2;
      return landscape
        ? new THREE.Vector3(o * gap, 0.05, -Math.abs(o) * 0.25)
        : new THREE.Vector3(Math.sin(o * 1.3) * 0.25, -o * gap + 0.15, 0);
    });
  }

  menuCameraZ() {
    const pos = this.menuPositions();
    const tan = Math.tan(THREE.MathUtils.degToRad(this.stage.camera.fov / 2));
    const halfW = Math.max(...pos.map((p) => Math.abs(p.x))) + ORB_R * 1.9;
    const halfH = Math.max(...pos.map((p) => Math.abs(p.y))) + ORB_R * 1.9;
    return Math.max(halfW / (tan * this.stage.aspect), halfH / tan, 4.5);
  }

  fitMenu(animate = true) {
    const pos = this.menuPositions();
    this.orbs.forEach((orb, i) => orb.userData.home = pos[i]);
    const z = this.menuCameraZ();
    if (animate) gsap.to(this.stage.camera.position, { z, duration: 1.6, ease: "power3.inOut" });
    else {
      this.stage.camera.position.z = z;
      this.orbs.forEach((orb) => orb.position.copy(orb.userData.home));
    }
  }

  // ---------- 1〜2. オープニング（粒子が集まり、球体とロゴになる） ----------

  playOpening() {
    this.state = "opening";
    const { particles, stage } = this;
    const finalZ = this.logoCameraZ();
    stage.camera.position.z = finalZ * 1.22;
    this.layoutLogo();

    const vis = stage.visibleSizeAt(finalZ);
    const n = particles.count;
    particles.morph(
      (i, v) => offscreenPoint(v, vis.width * 0.6, vis.height * 0.6),
      (i, v) => fibonacciSphere(i, n, R * (1.0 + Math.random() * 0.05), v),
    );
    particles.uniforms.uSwirl.value = 2.4;
    particles.uniforms.uBulge.value = 0;
    particles.uniforms.uSpread.value = 0.45;
    particles.uniforms.uOpacity.value = 1;
    particles.uniforms.uSize.value = 1;

    const mat = this.sphere.userData.mesh.material;
    mat.envMapIntensity = 0;
    const tl = gsap.timeline({ onComplete: () => this.enterLogoState() });
    this.openingTl = tl;
    tl.to(this.dust.material, { opacity: 0.55, duration: 3, ease: "power1.out" }, 0)
      .to(stage.camera.position, { z: finalZ, duration: 6, ease: "power2.inOut", onUpdate: () => this.layoutLogo() }, 0)
      // 画面外から粒子が渦を巻いて中央へ
      .to(particles.uniforms.uProgress, { value: 1, duration: 3.6, ease: "none" }, 0.2)
      // 粒子が融合して鏡面球体に
      .to(this.sphere.scale, { x: 1, y: 1, z: 1, duration: 1.6, ease: "expo.out" }, 3.15)
      .to(mat, { envMapIntensity: 1.25, duration: 1.8, ease: "power2.out" }, 3.15)
      .to(this.glow.material, { opacity: 1, duration: 0.35, ease: "power2.out" }, 3.15)
      .to(this.glow.material, { opacity: 0.22, duration: 1.6, ease: "power2.inOut" }, 3.5)
      .to(particles.uniforms.uOpacity, { value: 0, duration: 1.3, ease: "power2.in" }, 3.3)
      .to(particles.uniforms.uSize, { value: 0.3, duration: 1.3, ease: "power2.in" }, 3.3)
      // 球体の背後から RS / GY がせり出す
      .add(() => this.logoReveal(tl), 4.2)
      .to({}, { duration: 1.8 }, 4.2);
    this.ui.showSkip(() => this.skipOpening());
  }

  logoReveal(parentTl) {
    const { rs, gy } = this.logo;
    const fromRS = rs.userData.finalX + 0.9;
    const fromGY = gy.userData.finalX - 0.9;
    const reveal = gsap.timeline();
    reveal
      .fromTo(rs.position, { x: fromRS }, { x: () => rs.userData.finalX, duration: 1.7, ease: "expo.out" }, 0)
      .fromTo(gy.position, { x: fromGY }, { x: () => gy.userData.finalX, duration: 1.7, ease: "expo.out" }, 0)
      .fromTo(this.logo, { _o: 0 }, { _o: 1, duration: 1.2, ease: "power2.out", onUpdate: () => this.logo.setOpacity(this.logo._o) }, 0);
    this.logoTl = reveal;
    if (parentTl && parentTl.progress() === 1) reveal.progress(1);
  }

  skipOpening() {
    if (this.state !== "opening") return;
    this.openingTl.progress(1);
    if (!this.logoTl) this.logoReveal(null);
    this.logoTl.progress(1);
    this.logo.setOpacity(1);
  }

  enterLogoState() {
    this.state = "logo";
    this.layoutLogo();
    this.logo.setOpacity(1);
    this.particles.points.visible = false;
    this.ui.hideSkip();
    this.ui.showHint(true);
  }

  // ---------- 3. 回転 → 分裂 → メニュー ----------

  split() {
    if (this.state !== "logo") return;
    this.state = "splitting";
    this.ui.showHint(false);
    const { particles } = this;
    const n = particles.count;
    const m = this.orbs.length;
    const pos = this.menuPositions();
    this.orbs.forEach((orb, i) => { orb.userData.home = pos[i]; orb.position.copy(pos[i]); orb.visible = true; });

    // 球体表面（現在の回転を反映）から、各メニュー球体の表面へ
    const rot = new THREE.Euler(this.pitch, this.yaw, 0);
    particles.morph(
      (i, v) => fibonacciSphere(i, n, R * 1.01, v).applyEuler(rot),
      (i, v) => {
        const k = i % m;
        const j = Math.floor(i / m);
        return fibonacciSphere(j, Math.ceil(n / m), ORB_R * 1.04, v).add(pos[k]);
      },
    );
    particles.uniforms.uSwirl.value = 1.1;
    particles.uniforms.uBulge.value = 1.4;
    particles.uniforms.uSpread.value = 0.3;
    particles.uniforms.uOpacity.value = 1;
    particles.uniforms.uSize.value = 1.1;
    particles.points.visible = true;

    const tl = gsap.timeline({ onComplete: () => this.enterMenuState() });
    tl.to(this.logo, { _o: 0, duration: 0.5, ease: "power2.in", onUpdate: () => this.logo.setOpacity(this.logo._o) }, 0)
      .to(this.glow.material, { opacity: 1, duration: 0.2 }, 0)
      .to(this.sphere.scale, { x: 1.05, y: 1.05, z: 1.05, duration: 0.18, ease: "power2.out" }, 0)
      .to(this.sphere.scale, { x: 0.0001, y: 0.0001, z: 0.0001, duration: 0.55, ease: "power3.in" }, 0.18)
      .to(this.glow.material, { opacity: 0, duration: 1.2 }, 0.4)
      .to(particles.uniforms.uProgress, { value: 1, duration: 2.3, ease: "none" }, 0.1)
      .add(() => this.fitMenu(true), 0.3);
    this.orbs.forEach((orb, i) => {
      tl.to(orb.scale, { x: 1, y: 1, z: 1, duration: 1.0, ease: "back.out(1.6)" }, 1.55 + i * 0.08);
    });
    tl.to(particles.uniforms.uOpacity, { value: 0, duration: 0.7, ease: "power2.in" }, 1.9)
      .add(() => this.ui.showLabels(true), 2.0);
  }

  enterMenuState() {
    this.state = "menu";
    this.particles.points.visible = false;
    this.sphere.visible = false;
  }

  // メニュー状態を即座に作る（各ページから戻ったとき・直接ページを開いたとき）
  showMenuImmediately() {
    gsap.killTweensOf(this.stage.camera.position);
    if (this.openingTl) this.openingTl.kill();
    this.ui.hideSkip();
    this.ui.showHint(false);
    this.logo.setOpacity(0);
    this.sphere.visible = false;
    this.particles.points.visible = false;
    this.dust.material.opacity = 0.55;
    this.orbs.forEach((orb) => { orb.visible = true; orb.scale.setScalar(1); });
    this.fitMenu(false);
    this.ui.showLabels(true);
    this.state = "menu";
  }

  // ---------- メニュー選択（ページ遷移の本格演出は第4段階で実装） ----------

  select(orb) {
    if (this.state !== "menu") return;
    this.state = "leaving";
    this.ui.showLabels(false);
    const tl = gsap.timeline({ onComplete: () => this.onSelect(orb.userData.item) });
    tl.to(orb.position, { x: 0, y: 0, z: 0.5, duration: 0.9, ease: "power3.inOut" }, 0)
      .to(orb.scale, { x: 1.6, y: 1.6, z: 1.6, duration: 0.9, ease: "power3.inOut" }, 0);
    this.orbs.filter((o) => o !== orb).forEach((o) => {
      tl.to(o.scale, { x: 0.0001, y: 0.0001, z: 0.0001, duration: 0.5, ease: "power2.in" }, 0);
    });
  }

  // ページから戻ったときにメニューを元の配置へ
  returnToMenu() {
    this.state = "returning";
    const tl = gsap.timeline({ onComplete: () => { this.state = "menu"; this.ui.showLabels(true); } });
    this.orbs.forEach((orb, i) => {
      orb.visible = true;
      tl.to(orb.position, { x: orb.userData.home.x, y: orb.userData.home.y, z: orb.userData.home.z, duration: 1.0, ease: "power3.inOut" }, 0)
        .to(orb.scale, { x: 1, y: 1, z: 1, duration: 1.0, ease: "back.out(1.4)" }, 0.1 + i * 0.06);
    });
  }

  // ---------- 操作 ----------

  bindPointer() {
    const el = this.stage.renderer.domElement;
    const ray = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let down = null;
    let last = null;

    const pick = (e) => {
      const rect = el.getBoundingClientRect();
      ndc.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
      ray.setFromCamera(ndc, this.stage.camera);
      const meshes = this.orbs.filter((o) => o.visible).map((o) => o.userData.mesh);
      const hit = ray.intersectObjects(meshes, false)[0];
      return hit ? this.orbs.find((o) => o.userData.mesh === hit.object) : null;
    };

    el.addEventListener("pointerdown", (e) => {
      down = { x: e.clientX, y: e.clientY, t: performance.now() };
      last = { x: e.clientX, y: e.clientY };
      el.setPointerCapture?.(e.pointerId);
    });
    el.addEventListener("pointermove", (e) => {
      if (down) {
        const dx = e.clientX - last.x;
        const dy = e.clientY - last.y;
        last = { x: e.clientX, y: e.clientY };
        const k = 5.2 / Math.max(320, Math.min(this.stage.width, this.stage.height));
        this.yawVel += dx * k;
        this.pitchTarget = THREE.MathUtils.clamp(this.pitchTarget + dy * k * 0.6, -0.55, 0.55);
        if (this.state === "logo") this.spun += Math.abs(dx * k);
      } else if (this.state === "menu" && e.pointerType === "mouse") {
        this.hovered = pick(e);
        el.style.cursor = this.hovered ? "pointer" : "grab";
      }
    });
    const end = (e) => {
      if (!down) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      const quick = performance.now() - down.t < 500;
      down = null;
      if (moved < 10 && quick && this.state === "menu") {
        const orb = pick(e);
        if (orb) this.select(orb);
      }
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", () => { down = null; });

    // キーボード操作（左右キーで回転、Enter で中央付近を選択はしない：アクセシビリティ用に最低限）
    window.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        const d = e.key === "ArrowLeft" ? -0.35 : 0.35;
        this.yawVel += d;
        if (this.state === "logo") this.spun += Math.abs(d);
      }
    });
  }

  // ---------- 毎フレーム ----------

  update(dt, t) {
    this.particles?.update(t);
    this.dust?.update(dt);
    if (!this.sphere) return;

    // 慣性つきの回転（ゆっくり自転も加える）
    const idle = this.state === "logo" || this.state === "opening" ? 0.12 : 0.18;
    this.yaw += (this.yawVel + idle * dt);
    this.yawVel *= Math.pow(0.04, dt);
    this.pitchTarget *= Math.pow(0.35, dt);
    this.pitch += (this.pitchTarget - this.pitch) * Math.min(1, dt * 6);
    this.sphere.rotation.set(this.pitch, this.yaw, 0.12);

    if (this.state === "logo") {
      // 回すほど文字が消え、一定量回すと分裂
      this.spun = Math.max(0, this.spun - dt * 0.25);
      const fade = THREE.MathUtils.clamp(1 - (this.spun - 0.15) / (SPLIT_ANGLE - 0.15), 0, 1);
      this.logo.setOpacity(fade);
      this.ui.setHintProgress(1 - fade);
      if (this.spun >= SPLIT_ANGLE) this.split();
    }

    // メニュー球体：浮遊・自転・ホバー
    if (this.orbs && this.state !== "init") {
      this.orbs.forEach((orb, i) => {
        if (!orb.visible) return;
        const home = orb.userData.home;
        if (home && (this.state === "menu")) {
          orb.position.y = home.y + Math.sin(t * 0.9 + i * 1.7) * 0.05;
        }
        orb.rotation.y = this.yaw * 0.8 + i * 1.3 + t * 0.15;
        orb.rotation.x = this.pitch * 0.8 + 0.15;
        const target = this.state === "menu" ? (this.hovered === orb ? 1.08 : 1) : null;
        if (target) orb.scale.setScalar(orb.scale.x + (target - orb.scale.x) * Math.min(1, dt * 8));
      });
      if (this.state === "menu" || this.state === "splitting" || this.state === "returning") {
        this.ui.positionLabels(this.orbs, this.stage.camera, ORB_R);
      }
    }
  }
}
