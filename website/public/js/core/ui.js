// 3Dの上に重ねる HTML（スキップボタン・操作ヒント・メニュー名・ページ）
import * as THREE from "three";
import { gsap } from "gsap";

export class UI {
  constructor() {
    this.skip = document.getElementById("skip");
    this.hint = document.getElementById("hint");
    this.hintBar = document.querySelector("#hint .hint-bar i");
    this.labelsEl = document.getElementById("menu-labels");
    this.labels = [];
    this.v = new THREE.Vector3();
  }

  showSkip(onSkip) {
    this.skip.onclick = onSkip;
    gsap.to(this.skip, { autoAlpha: 1, duration: 0.8, delay: 0.6, overwrite: true });
  }
  hideSkip() { gsap.to(this.skip, { autoAlpha: 0, duration: 0.4, overwrite: true }); }

  showHint(show) {
    gsap.to(this.hint, { autoAlpha: show ? 1 : 0, duration: show ? 1.2 : 0.4, delay: show ? 0.4 : 0, overwrite: true });
  }
  setHintProgress(p) { this.hintBar.style.transform = `scaleX(${p.toFixed(3)})`; }

  buildLabels(items) {
    this.labelsEl.innerHTML = "";
    this.labels = items.map((item, i) => {
      const el = document.createElement("div");
      el.className = "orb-label";
      el.innerHTML = `<span class="idx">0${i + 1}</span><span class="name">${item.label}</span>`;
      this.labelsEl.appendChild(el);
      return el;
    });
  }

  showLabels(show) {
    gsap.to(this.labels, {
      autoAlpha: show ? 1 : 0, // 位置は positionLabels が transform で制御するため不透明度のみ
      duration: show ? 0.9 : 0.3,
      stagger: show ? 0.08 : 0,
      overwrite: true,
      ease: "power2.out",
    });
  }

  // 3D球体の真下にメニュー名を追従させる
  positionLabels(orbs, camera, radius) {
    const w = window.innerWidth, h = window.innerHeight;
    orbs.forEach((orb, i) => {
      const el = this.labels[i];
      if (!el) return;
      this.v.copy(orb.position);
      this.v.y -= radius * orb.scale.y * 1.32;
      this.v.project(camera);
      el.style.transform = `translate(-50%, 0) translate(${((this.v.x + 1) / 2) * w}px, ${((1 - this.v.y) / 2) * h}px)`;
    });
  }
}
