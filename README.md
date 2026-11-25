# TokenScope

Claude Code が使ったトークンを、1回の実行の合計と、1往復ごとに送った文脈の大きさに分けて記録し、ブラウザの管理画面で比べるツールです。
自分のプロンプトを測り、「毎回乗る文脈」と「往復の回数」のどちらで増えているかを見てから、設定や指示を直せます。

trends（CodeCamp）の記事で作ったツールです。作り方と計測の結果は、次の2本で解説しています。

- 前編：[【前編】Claude Code・Codexのトークン使用量を確認するツールを作ってみた](https://trends.codecamp.jp/blogs/media/python-tokenscope-fastapi-otlp-dashboard-output)
- 後編：[【後編】Claude Codeのトークン消費を減らす方法をTokenScopeで実測してみた](https://trends.codecamp.jp/blogs/media/python-tokenscope-token-reduction-output)

## 入っているファイル

| ファイル | 役割 |
| --- | --- |
| `tokenscope.py` | 管理画面。届いた記録から、往復ごとの文脈を折れ線と表にする |
| `measure_prompt.py` | プロンプトを Claude Code（`claude -p`）で実行し、往復ごとの文脈を TokenScope へ送る |
| `prompt.txt` | 測るプロンプトの例。普段 Claude Code に送っている指示に書き換えて使う |
| `prompt_v2.txt` | `prompt.txt` に読む場所（ファイルと行の範囲）を書き足した例 |
| `examples/settings.json` | 使わないツールを止める設定の例（`permissions.deny`） |
| `requirements.txt` | 管理画面に要るパッケージ |

## 動かす環境

- Windows 11 の PowerShell と Python 3.13 で動作を確かめています。
- Claude Code（`claude` コマンド）を入れて、ログインしておいてください。
- 計測は `claude -p` を実際に動かすため、測るたびにそのぶんの利用枠を使います。

## 1. 取得して準備する

ホームフォルダーの外（例：`C:\TokenScope`）に置いてください。
ホームフォルダーの下だと、上の階層のフォルダーに置いた `CLAUDE.md` も読み込まれ、計測に混ざります。自分用の指示（`.claude\CLAUDE.md`）は、どのフォルダーで実行しても読み込まれます。

```powershell
git clone https://github.com/kk8800819/tokenscope.git C:\TokenScope
cd C:\TokenScope
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 管理画面を起動する（ターミナル1）

```powershell
cd C:\TokenScope
.\.venv\Scripts\python.exe -m uvicorn tokenscope:app --host 127.0.0.1 --port 8877
```

ブラウザで `http://127.0.0.1:8877` を開きます。
記録はメモリーにだけ持つため、管理画面を止めると消えます。
計測の前に画面だけ見たいときは、画面上の「JSON＋Protobuf fixtureを投入」ボタンで見本の記録を入れられます。

## 3. 自分のプロンプトを測る（ターミナル2）

`prompt.txt` に普段の指示を書き、Claude Code に作業させるフォルダーを `--cwd` に指定します。
下の例では `C:\TokenScope\myapp` を作業フォルダーにしています。自分のプロジェクトで測るときは、そのフォルダーに置き換えてください。

```powershell
cd C:\TokenScope
.\.venv\Scripts\python.exe .\measure_prompt.py claude-code .\prompt.txt --cwd .\myapp --tools default --allowed-tools Read Grep Glob --scenario my-prompt --variant before
```

| 指定 | 意味 |
| --- | --- |
| `--tools default` | 普段と同じツールを Claude Code に渡す。付けないとツールなしで動き、ファイルを読む作業は測れない |
| `--allowed-tools` | 確認なしで使ってよいツール。読むだけのプロンプトなら `Read Grep Glob` で足りる |
| `--scenario` | 測るプロンプトの名前。管理画面では名前ごとに枠が分かれる |
| `--variant` | 条件の名前。管理画面では条件ごとに線が分かれる。差は `before` と比べて出すので、直す前は `before` にする |
| `--repeat 3` | 同じ条件で続けて3回測る。1回ごとにぶれるので、中央値で比べるのがおすすめ |

## 4. 見るところと直し方

管理画面の「往復ごとの文脈(中央値)」の折れ線を見ます。1往復目の高さが毎回乗る文脈で、点の数が往復の回数です。
その上の表で、条件ごとの合計と `before` との差、API料金換算を確かめてください。

| 画面で見えたこと | 直すところ | 測り直すときの条件名 |
| --- | --- | --- |
| 1往復目が大きい | 使わないツールを `settings.json` の `permissions.deny` で止める | `deny` |
| 往復が多い | 読むファイルと行の範囲（または関数名）を指示に書く | `deny-specific` |
| 1回ごとに数字がぶれる | `--repeat 3` を付けて数回ずつ測り、中央値で比べる | 変えない |

使わないツールを止めるときは、`examples/settings.json` を作業フォルダーの `.claude\settings.json` に置きます。
ツールの名前は Claude Code の版と使う機能で変わるため、作業フォルダーで `claude -p "/context"` を実行して確かめ、自分の環境に合わせて書き換えてください。

```powershell
New-Item -ItemType Directory -Force .\myapp\.claude
Copy-Item .\examples\settings.json .\myapp\.claude\settings.json
.\.venv\Scripts\python.exe .\measure_prompt.py claude-code .\prompt.txt --cwd .\myapp --tools default --allowed-tools Read Grep Glob --scenario my-prompt --variant deny
.\.venv\Scripts\python.exe .\measure_prompt.py claude-code .\prompt_v2.txt --cwd .\myapp --tools default --allowed-tools Read Grep Glob --scenario my-prompt --variant deny-specific
```

## 記事の動画と同じ材料で試す

`prompt.txt` と `prompt_v2.txt` の例は、`myapp\fastapi` に FastAPI 0.141.1 の本体ソースを置いた状態を前提にしています（行の範囲もこの版のものです）。
FastAPI のソースはこのリポジトリに入れていないため、PyPI から取得して置いてください。
最後の行では、配布物に同梱されている `.agents` フォルダー（説明の Markdown）を消し、記事の計測と同じ中身にしています。

```powershell
cd C:\TokenScope
.\.venv\Scripts\python.exe -m pip download fastapi==0.141.1 --no-deps -d .\_fastapi
Copy-Item .\_fastapi\fastapi-0.141.1-py3-none-any.whl .\_fastapi\fastapi.zip
Expand-Archive .\_fastapi\fastapi.zip .\_fastapi\src
New-Item -ItemType Directory -Force .\myapp
Copy-Item .\_fastapi\src\fastapi .\myapp\fastapi -Recurse
Remove-Item .\myapp\fastapi\.agents -Recurse -Force
```

## ライセンス

[MIT License](LICENSE)
