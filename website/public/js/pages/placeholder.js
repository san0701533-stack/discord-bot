// 仮ページ。内容・デザインが決まったら、ページごとに専用ファイルへ置き換える。
// ページは { title, mount(container), unmount() } の形をしていれば自由に作れる。
export function placeholderPage({ eyebrow, title, lead }) {
  return {
    title,
    mount(container) {
      container.innerHTML = `
        <section class="page-hero">
          <p class="eyebrow">${eyebrow}</p>
          <h1 class="page-title">${title}</h1>
          <p class="page-lead">${lead}</p>
        </section>
        <section class="page-body">
          <p class="placeholder-note">COMING SOON</p>
        </section>`;
    },
    unmount() {},
  };
}
