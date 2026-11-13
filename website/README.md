# RSGY Website

Discordサーバー「RSGY」の宣伝用Webサイト。Three.js（WebGL）と GSAP による3D演出が中心です。

## 起動

```bash
cd website
npm install
npm start          # http://localhost:3000 （PORT 環境変数で変更可）
```

ビルド工程はありません。`server.js`（Node.js 標準機能のみ）が `public/` と、`node_modules` 内の three / gsap / フォントを配信します。

### MWS（Node.js サーバー）での運用
- 起動コマンド：`npm start`
- ポート：環境変数 `PORT`（未設定なら 3000）
- `npm install` 済みの `node_modules` が必要です（three / gsap / フォント）。

## 制作段階

| 段階 | 内容 | 状態 |
|---|---|---|
| 1 | 銀粒子が集まるオープニング | ✅ |
| 2 | 巨大な鏡面球体と「RS◯GY」ロゴ | ✅ |
| 3 | 球体の回転・分裂・3D球体メニュー | ✅ |
| 4 | PARTICLE EXPLOSION によるページ移動 | 未着手（現在は簡易な遷移） |
| 5 | 各ページの内容・スクロール粒子・Botページのニューラルネットワーク | 未着手（仮ページ） |
| 6 | 細部調整・パフォーマンス最適化 | 未着手 |

## ファイル構成

```
website/
├── server.js                 静的配信サーバー（SPA 対応・gzip・パス制限）
├── package.json
└── public/
    ├── index.html
    ├── css/style.css
    └── js/
        ├── main.js           起動・ページ切り替え（URL: /boosting など）
        ├── config/menu.js    ★メニュー（球体）の設定。最大5個
        ├── core/
        │   ├── stage.js      レンダラー・カメラ・環境マップ・ブルーム・自動画質調整
        │   ├── quality.js    端末性能の判定（粒子数・解像度など）
        │   └── ui.js         スキップ・操作ヒント・メニュー名などの HTML
        ├── scene/
        │   ├── particles.js  銀粒子（GPU で集合・分裂を計算）
        │   ├── orb.js        鏡面シルバー球体
        │   ├── logo.js       RSGY ロゴ
        │   └── dust.js       背景の銀の塵
        ├── home/home.js      ホームの演出（オープニング→ロゴ→回転→分裂→メニュー）
        └── pages/
            ├── index.js      ★ページの登録
            └── placeholder.js 仮ページ
```

## メニュー・ページの追加

1. `public/js/config/menu.js` に項目を追加（例：`{ id: "about", label: "ABOUT", route: "/about" }`）。球体の数と配置は自動で変わります（最大5個）。
2. `public/js/pages/` にページを作り、`pages/index.js` の `PAGES` に同じ `id` で登録します。ページは `{ title, mount(container), unmount() }` の形です。

## 画質

- 端末の性能から自動で「high / mid / low」を選び、粒子数・解像度・ブルームを調整します。
- 実際の描画が重いと、解像度→ブルームの順に自動で下げます。
- 動作確認用：URL に `?q=low`、`?q=mid`、`?q=high` を付けると画質を固定、`&adapt=0` で自動調整を止めます。
- 端末の「視差効果を減らす」設定が有効な場合は、オープニングを省略します。
