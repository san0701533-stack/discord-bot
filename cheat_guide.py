"""チートの導入手順（チケットの中で1ステップずつ案内する文）。

文を直したいときは、このファイルだけ書き換えればよい。
- PRODUCTS     : チケットのメニューに出す商品
- 各ステップは {"title": 見出し, "body": 本文, "links": [(ボタンの文字, URL), ...]}
- 本文の中の {bundle_id} {password} {search} は自動で置き換わる
"""

SIGN_SITE_URL = "https://sign.ipasign.cc"
CERT_PASSWORD = "1"
BUNDLE_ID = "com.rsgy.app"

# チケットのメニューに出す商品
# shop      : どの手順で買うか（SHOP_STEPS のキー）
# search    : ショップの中で探す商品名
PRODUCTS = {
    "bs": {
        "label": "bs",
        "emoji": "⭐",
        "description": "REGeared で購入",
        "shop": "regear",
        "search": "bsre",
    },
    "bscrack": {
        "label": "bsクラック",
        "emoji": "💥",
        "description": "REGeared で購入",
        "shop": "regear",
        "search": "bsre",  # 未定：bsクラックの商品名が違う場合はここを直す
    },
    "tale": {
        "label": "tale",
        "emoji": "📘",
        "description": "TalePay で購入",
        "shop": "tale",
        "search": "",
    },
}

# 1. 証明書を受け取る（スタッフが用意して、このチケットに送る）
CERT_STEP = {
    "title": "証明書を受け取る",
    "body": (
        "スタッフが証明書を用意しています。少しお待ちください。\n"
        "このチケットに証明書のファイル（**p12** と書かれたものと、もう1つ）が届いたら、"
        "2つともスマホに保存して「できた」を押してください。"
    ),
    "links": [],
}

# 2〜3. 署名サイトで、IPA以外を先に入れておく
SIGN_SETUP_STEPS = [
    {
        "title": "署名サイトを開く",
        "body": (
            "下のボタンから署名サイトを開いて、下にスクロールし、"
            "**Step 1: Select your files** と書かれている所まで進んでください。"
        ),
        "links": [("署名サイトを開く", SIGN_SITE_URL)],
    },
    {
        "title": "証明書を入れる",
        "body": (
            "それぞれの欄に、次のように入れてください。\n"
            "・**P12 Certificate File**：証明書の中の **p12** と書かれている方\n"
            "・**P12 Certificate Password**：`{password}`\n"
            "・**Provisioning Profile**：証明書の中の **p12じゃない方**\n"
            "・**Bundle ID**：`{bundle_id}`\n"
            "・**App Name**：好きな名前\n\n"
            "⚠️ **IPA File** と **Sign it now!** はまだ触らないでください。"
            "この画面のまま、次のステップでチートを買います。"
        ),
        "links": [("署名サイトを開く", SIGN_SITE_URL)],
    },
]

# 4〜. チートを買う（ショップごと）
SHOP_STEPS = {
    "regear": [
        {
            "title": "Telegramを入れる",
            "body": "**Telegram** のアプリをダウンロードして、ログインしてください。",
            "links": [],
        },
        {
            "title": "商品と値段を確認する",
            "body": (
                "下のボタンからリンクを開き、左下の **Open REGeared** をタップします。\n"
                "下のバーの **カタログ** をタップして **{search}** を探し、何スターか値段を確認してください。"
            ),
            "links": [("REGearedを開く", "https://regear.cc/r/84NbBc")],
        },
        {
            "title": "スターを買う",
            "body": (
                "下のバーから **ホーム** を選び、**スター残高** をタップします。\n"
                "お支払い方法は **テレグラムスター** を選び、さっき確認した値段分のスターを買ってください。"
            ),
            "links": [],
        },
        {
            "title": "チートを買う",
            "body": (
                "**{search}** に戻って **今すぐ購入** をタップし、何日分買うか決めて **購入** をタップします。\n"
                "**アドオンなし** を2回タップしたら、**残高から支払う** をタップして購入完了です。\n"
                "購入すると **ipa** という名前の入ったファイルが届くので、スマホに保存してください。"
            ),
            "links": [],
        },
    ],
    "tale": [
        {
            "title": "Telegramを入れる",
            "body": "**Telegram** のアプリをダウンロードして、ログインしてください。",
            "links": [],
        },
        {
            "title": "チートを買う",
            "body": (
                "下のボタンからリンクを開くとチャットに飛びます。\n"
                "左下の **オープン** を押して、「製品」の所の **購入** をタップします。\n"
                "何日分買うか決めたら **残高チャージ** → **テレグラムスター** をタップし、"
                "値段分のスターを選んで **Confirm and Pay** をタップしてください。"
            ),
            "links": [("TalePayを開く", "https://talepay.bot/ref/RDN5JLU")],
        },
        {
            "title": "ファイルを保存する",
            "body": "購入すると **ios** または **ipa** とついたファイルが届くので、スマホにダウンロードしてください。",
            "links": [],
        },
    ],
}

# 最後. アプリ化する
FINAL_STEP = {
    "title": "アプリ化する",
    "body": (
        "署名サイトに戻り、**IPA File** の枠に、さっき保存したipaファイルを入れてください。\n"
        "最後に **Sign it now!** を押せば、アプリ化できます。"
    ),
    "links": [("署名サイトを開く", SIGN_SITE_URL)],
}


def guide_steps(product_key):
    """商品に合わせた手順の一覧を返す。"""
    product = PRODUCTS.get(product_key) or PRODUCTS["bs"]
    steps = [CERT_STEP, *SIGN_SETUP_STEPS, *SHOP_STEPS[product["shop"]], FINAL_STEP]
    filled = []
    for step in steps:
        filled.append({
            "title": step["title"],
            "body": step["body"].format(
                bundle_id=BUNDLE_ID,
                password=CERT_PASSWORD,
                search=product.get("search") or "商品",
            ),
            "links": list(step["links"]),
        })
    return filled


def guide_text_for_ai():
    """AIに渡す手順書（すべての商品の手順をまとめた文）。"""
    parts = []
    for key, product in PRODUCTS.items():
        lines = [f"### {product['label']} の導入手順"]
        for i, step in enumerate(guide_steps(key), 1):
            body = step["body"].replace("**", "")
            links = "".join(f"\n（{label}：{url}）" for label, url in step["links"])
            lines.append(f"{i}. {step['title']}\n{body}{links}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
