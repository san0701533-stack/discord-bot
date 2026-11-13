// 背景を漂うごく淡い銀の塵（奥行き感と空気感を出すための常時演出）
import * as THREE from "three";

function dotTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.3, "rgba(220,226,235,0.5)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

export function createDust(count) {
  const pos = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    pos[i * 3] = (Math.random() - 0.5) * 40;
    pos[i * 3 + 1] = (Math.random() - 0.5) * 26;
    pos[i * 3 + 2] = -4 - Math.random() * 30;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  const mat = new THREE.PointsMaterial({
    size: 0.06,
    map: dotTexture(),
    transparent: true,
    opacity: 0,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    color: 0xc9d0da,
  });
  const points = new THREE.Points(geo, mat);
  points.frustumCulled = false;
  return {
    points,
    material: mat,
    update(dt) {
      points.rotation.y += dt * 0.008;
      points.rotation.x = Math.sin(performance.now() * 0.00005) * 0.05;
    },
  };
}
