// 球体の背後に出る「RSGY」ロゴ。「RS」と「GY」を別々の板にして間に球体を置き、
// 球体が文字の内側に少し重なることで「RS◯GY」に見える構図を作る。
import * as THREE from "three";

const FONT = '700 220px "Syncopate", "Michroma", "Helvetica Neue", Arial, sans-serif';

function drawSilverText(text) {
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = FONT;
  const m = measure.measureText(text);
  const pad = 40;
  const w = Math.ceil(m.width + pad * 2);
  const ascent = m.actualBoundingBoxAscent || 170;
  const descent = m.actualBoundingBoxDescent || 10;
  const h = Math.ceil(ascent + descent + pad * 2);

  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d");
  g.font = FONT;
  g.textBaseline = "alphabetic";
  const baseline = pad + ascent;

  // 鏡面シルバーのグラデーション（上が明るく、中央で一度落ちて、下で再び反射）
  const grad = g.createLinearGradient(0, pad, 0, pad + ascent);
  grad.addColorStop(0.0, "#f4f6f9");
  grad.addColorStop(0.32, "#d9dde3");
  grad.addColorStop(0.5, "#7b818a");
  grad.addColorStop(0.56, "#4a4f57");
  grad.addColorStop(0.7, "#aeb4bc");
  grad.addColorStop(1.0, "#e4e8ee");
  g.fillStyle = grad;
  g.fillText(text, pad, baseline);

  // 上辺の細いハイライト
  g.globalCompositeOperation = "source-atop";
  const sheen = g.createLinearGradient(0, 0, w, 0);
  sheen.addColorStop(0, "rgba(255,255,255,0)");
  sheen.addColorStop(0.5, "rgba(255,255,255,0.35)");
  sheen.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = sheen;
  g.fillRect(0, pad, w, ascent * 0.18);
  g.globalCompositeOperation = "source-over";

  return { canvas: c, width: w, height: h, textLeft: pad, textRight: pad + m.width, ascent, baselineFromTop: baseline };
}

function makePlane(info, worldPerPx) {
  const tex = new THREE.CanvasTexture(info.canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false, opacity: 0 });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(info.width * worldPerPx, info.height * worldPerPx), mat);
  return mesh;
}

export async function createLogo() {
  try {
    await Promise.race([document.fonts.load(FONT, "RSGY"), new Promise((r) => setTimeout(r, 2500))]);
  } catch { /* フォント読み込み失敗時は代替フォントで描画 */ }

  const left = drawSilverText("RS");
  const right = drawSilverText("GY");
  const group = new THREE.Group();
  // 文字の高さ（キャップハイト）をワールド単位で 1.0 にそろえる
  const worldPerPx = 1.0 / left.ascent;
  const rs = makePlane(left, worldPerPx);
  const gy = makePlane(right, worldPerPx);
  group.add(rs, gy);

  // 縦位置：キャップハイトの中心を y=0 に合わせる
  const centerOffset = (info) => (info.height / 2 - (info.baselineFromTop - info.ascent / 2)) * worldPerPx;
  rs.position.y = centerOffset(left);
  gy.position.y = centerOffset(right);

  const halfText = {
    rs: (left.textRight - left.width / 2) * worldPerPx, // 板の中心から「S」の右端まで
  };
  const textWidth = {
    rs: (left.textRight - left.textLeft) * worldPerPx,
    gy: (right.textRight - right.textLeft) * worldPerPx,
  };

  return {
    group,
    rs,
    gy,
    // gap: 「S」の右端と「G」の左端の距離（ワールド単位）。球体がこの隙間に重なる。
    layout(gap) {
      rs.userData.finalX = -gap / 2 - halfText.rs;
      gy.userData.finalX = gap / 2 + (right.width / 2 - right.textLeft) * worldPerPx;
      rs.position.x = rs.userData.finalX;
      gy.position.x = gy.userData.finalX;
    },
    // ロゴ全体の横幅（ワールド単位）
    totalWidth(gap) { return textWidth.rs + gap + textWidth.gy; },
    setOpacity(v) { rs.material.opacity = v; gy.material.opacity = v; group.visible = v > 0.001; },
  };
}
