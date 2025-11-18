# GitHub Issue 一括 Re-open ツール（開発者向け）

## 概要

このアプリケーションは、GUI から操作できる **GitHub Issue 一括 re-open ツール**です。

- 複数の close 済み Issue をまとめて再オープン（re-open）
- 再オープン時にラベル、ステータスを一括付与
- 必要に応じて共通コメントを一括投稿
- HTTP/HTTPS プロキシ環境に対応

本 README は **開発者向け** のドキュメントです。  
exe のみを利用する人は `README.user.md` を参照してください。

---

## ファイル構成（例）

    project-root/
      ├─ dist
      |  ├─ github_issue_reopen_with_projects_gui.exe # アプリケーション実行ファイル
      |  └─ README.md # 利用者向け README
      └─ src
          ├─ github_issue_reopen_with_projects_gui.py # メインスクリプト
          └─ README.md # 開発者向け README (本ファイル)

---

## 動作環境

- OS: Windows 10 / 11
- Python: 3.10 〜 3.12 を推奨
- ネットワーク: GitHub への HTTPS アクセスが可能であること
  - 社内プロキシ経由でも可（後述）

---

## 使用技術 / アーキテクチャ概要

### 言語・ライブラリ

- 言語: Python 3
- GUI: `tkinter`（標準ライブラリ）
- HTTP クライアント: `requests`
- パッケージング: `pyinstaller`（開発環境のみ）

### 構造

メインスクリプト `github_issue_reopen_with_projects_gui.py` には、主に以下が含まれます。

- `GitHubIssueReopenApp` クラス
  - GUI 構築（`_build_ui`）
  - ログ出力・入力値パース（`log`, `parse_issue_numbers`, `parse_labels`）
  - プロキシ設定生成（`build_proxies`）
  - メイン処理（`on_run`）
- `main()` 関数
  - `Tk()` 初期化とアプリ起動

### 主な処理フロー

1. GUI から以下を入力:
   - Owner
   - Repository 名
   - API Base URL
   - PAT（Personal Access Token）
   - プロキシ設定（任意）
   - Issue 番号（複数）
   - 追加ラベル
   - 追加コメント
   - ドライランフラグ
2. `requests.Session` を生成し、ヘッダ / プロキシ設定を適用
3. 各 Issue 番号についてループ:
   1. `GET /repos/{owner}/{repo}/issues/{number}` で現在状態と既存ラベル取得
   2. `state == "open"` ならスキップ
   3. 既存ラベルに GUI で指定したラベルをマージ
   4. `PATCH /repos/{owner}/{repo}/issues/{number}` で `state: open` とラベル更新
   5. コメントが指定されていれば `POST /issues/{number}/comments` で追加
4. 結果およびエラーを GUI 下部のログエリアに表示

---

## セキュリティ（PAT の扱い）

- PAT は GUI の入力欄から受け取り、**コードに埋め込まない** 前提です。
- アプリ内でファイル保存は行わず、メモリ上でのみ使用します。
- 開発時・運用時ともに、最低限必要な権限のトークン（Fine-grained Token など）を利用してください。

---

## requirements.txt

開発／実行に必要な Python パッケージは以下です。

    requests>=2.31.0

開発環境で exe ビルドも行う場合は、別途 `pyinstaller` をインストールします。

---

## ローカル開発環境セットアップ

### 1. 仮想環境の作成

    cd project-root
    python -m venv venv
    venv\Scripts\activate

### 2. 依存ライブラリのインストール

    pip install --upgrade pip
    pip install requests

### 3. アプリの起動（開発モード）

    python github_issue_reopen_with_projects_gui.py

GUI ウィンドウが起動すれば OK です。

---

## プロキシ対応

### GUI からの指定

- プロキシ欄に以下の形式で入力します（scheme は未入力でも補完される実装を想定）。
  - 認証なし: `proxy.example.com:8080`
  - 認証あり: `user:password@proxy.example.com:8080`

内部では以下のような `proxies` を構築して `session.proxies` に設定します。

    proxies = {
        "http": "http://proxy.example.com:8080",
        "https": "http://proxy.example.com:8080",
    }
    session.proxies.update(proxies)

### 環境変数による指定

OS の環境変数として設定する場合：

    HTTP_PROXY=http://proxy.example.com:8080
    HTTPS_PROXY=http://proxy.example.com:8080

`requests` はデフォルトでこれらを自動的に読み込みます。  
環境変数を無視したい場合には、コード側で `session.trust_env = False` を設定してください。

### よくあるエラー

- `Not supported proxy scheme None`

  → プロキシ URL のスキームが欠落している場合に発生します。

  確認すべきポイント：
  - GUI プロキシ欄の値が `proxy.example.com:8080` のような形式で、コード側で `http://` を補完しているか？
  - 環境変数 `HTTP_PROXY` / `HTTPS_PROXY` に `proxy.example.com:8080` のようにスキーム無しで設定されていないか？

---

## PyInstaller を使った .exe ビルド

Python が入っていない PC でも利用できるよう、`pyinstaller` でスタンドアロン exe を作成します。

### 1. インストール

    venv\Scripts\activate
    pip install pyinstaller

### 2. ビルド

    pyinstaller --onefile --noconsole github_issue_reopen_with_projects_gui.py

- `--onefile` : 単一の `.exe` にまとめる
- `--noconsole` : コンソールを表示しない（GUI のみ）

ビルド完了後、`dist/` ディレクトリに以下が生成されます。

    dist/
      └─ github_issue_reopen_with_projects_gui.exe

このファイルを配布用として利用してください。
また github に新しいファイルで更新をかける場合は、以下の処理を行ってからプッシュしてください。

- `build/` ディレクトリを削除する
- 生成された `github_issue_reopen_with_projects_gui.exe` で `project-root/dist/github_issue_reopen_with_projects_gui.exe` を置き換える
- `github_issue_reopen_with_projects_gui.spec` ファイルを削除する

---

## 非エンジニア向けドキュメント

exe ファイルを利用するだけのユーザー向けには、  
`project-root/dis/README.md` を参照してもらってください。

`github_issue_reopen_with_projects_gui.exe` と `README.md` をセットで配布する運用を推奨します。

---

## 注意事項

- 本ツールは内部利用／サンプルとしての位置付けです。
- 誤った Issue 番号やトークン設定により、意図しない変更が GitHub 上に行われる可能性があります。
- 本番リポジトリに対して利用する前に、テスト用リポジトリや少数の Issue で動作検証してください。

