// ページの登録。新しいページは pages/ にファイルを作り、ここに追加する。
// キーは config/menu.js の id と一致させる。
import { placeholderPage } from "./placeholder.js";

export const PAGES = {
  boosting: placeholderPage({
    eyebrow: "01 / SERVICE",
    title: "BOOSTING",
    lead: "ランク・トロフィー代行のご案内ページ（準備中）",
  }),
  cheats: placeholderPage({
    eyebrow: "02 / SERVICE",
    title: "CHEATS",
    lead: "ご案内ページ（準備中）",
  }),
  "discord-bot": placeholderPage({
    eyebrow: "03 / SYSTEM",
    title: "DISCORD BOT",
    lead: "Botの紹介ページ（準備中）。3Dニューラルネットワーク演出は第5段階で実装予定です。",
  }),
};
