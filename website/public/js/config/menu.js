// メインメニュー（3D球体）の設定。
// 項目を追加・並べ替えるだけで球体の数と配置が変わります（最大5個まで）。
//   id    : 内部ID（pages/ のページ登録名と一致させる）
//   label : 球体の下に表示する名前
//   route : URL のパス
export const MENU = [
  { id: "boosting", label: "BOOSTING", route: "/boosting" },
  { id: "cheats", label: "CHEATS", route: "/cheats" },
  { id: "discord-bot", label: "DISCORD BOT", route: "/discord-bot" },
];

export const MAX_MENU_ITEMS = 5;
