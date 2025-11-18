import os
import json
import re
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

import requests


# =======================
# GitHub API Wrapper
# =======================

class GitHubAPI:
    def __init__(
        self,
        token: str,
        rest_base: str = "https://api.github.com",
        graphql_endpoint: str = "https://api.github.com/graphql",
        proxies: Optional[Dict[str, str]] = None,
    ) -> None:
        self.token = token
        self.rest_base = rest_base.rstrip("/")
        self.graphql_endpoint = graphql_endpoint
        self.session = requests.Session()
        self.session.headers.update(
            {
                # REST / GraphQL 両方で使えるよう Bearer で統一
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if proxies:
            self.session.proxies.update(proxies)

    # ---------- REST (Issue) ----------

    def get_issue(self, owner: str, repo: str, number: int) -> dict:
        url = f"{self.rest_base}/repos/{owner}/{repo}/issues/{number}"
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def reopen_issue_with_labels(
        self,
        owner: str,
        repo: str,
        number: int,
        labels_to_add: List[str],
    ) -> Tuple[str, bool, str, List[str], List[str]]:
        """
        指定 Issue を open にし、必要であればラベルを追加する。

        Returns:
            (state_before, reopened, node_id, labels_before, labels_after)
        """
        issue = self.get_issue(owner, repo, number)
        state_before = issue.get("state")
        node_id = issue.get("node_id")  # GraphQL 用 ID
        existing_labels = [lbl["name"] for lbl in issue.get("labels", [])]

        patch_payload = {}
        # すでに open の場合は state 更新不要だが、ラベル追加は行う
        if state_before != "open":
            patch_payload["state"] = "open"

        labels_after = existing_labels
        if labels_to_add:
            labels_after = sorted(set(existing_labels + labels_to_add))
            patch_payload["labels"] = labels_after

        reopened = False
        if patch_payload:
            url = f"{self.rest_base}/repos/{owner}/{repo}/issues/{number}"
            resp = self.session.patch(url, json=patch_payload, timeout=15)
            resp.raise_for_status()
            updated = resp.json()
            state_after = updated.get("state")
            labels_after = [lbl["name"] for lbl in updated.get("labels", [])]
            if state_before != "open" and state_after == "open":
                reopened = True

        return state_before, reopened, node_id, existing_labels, labels_after

    # ---------- GraphQL 共通 ----------

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        # headers は Authorization を上書きしないよう最小限
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }

        resp = self.session.post(
            self.graphql_endpoint, json=payload, headers=headers, timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data.get("data", {})

    # ---------- Project / Status 取得 ----------

    def get_project_id(
        self, owner_login: str, project_number: int, owner_type: str
    ) -> Tuple[str, str]:
        """
        owner_type: "user" or "org"
        戻り値: (project_id, project_title)
        """
        if owner_type == "org":
            query = """
            query($owner: String!, $number: Int!) {
              organization(login: $owner) {
                projectV2(number: $number) {
                  id
                  title
                }
              }
            }
            """
            data = self.graphql(query, {"owner": owner_login, "number": project_number})
            org = data.get("organization")
            if not org or not org.get("projectV2"):
                raise RuntimeError("organization/projectV2 が見つかりませんでした。")
            proj = org["projectV2"]
        else:
            query = """
            query($owner: String!, $number: Int!) {
              user(login: $owner) {
                projectV2(number: $number) {
                  id
                  title
                }
              }
            }
            """
            data = self.graphql(query, {"owner": owner_login, "number": project_number})
            user = data.get("user")
            if not user or not user.get("projectV2"):
                raise RuntimeError("user/projectV2 が見つかりませんでした。")
            proj = user["projectV2"]

        return proj["id"], proj.get("title", "")

    def get_status_field_options(
        self, project_id: str, field_name: str = "Status"
    ) -> Tuple[str, Dict[str, str]]:
        """
        Project の単一選択フィールド Status の Field ID と
        option 名→option ID の dict を返す。
        """
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options {
                      id
                      name
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"projectId": project_id})
        node = data.get("node")
        if not node or not node.get("fields"):
            raise RuntimeError("Project の fields が取得できませんでした。")

        field_nodes = node["fields"]["nodes"]
        for f in field_nodes:
            if not f:
                continue
            name = f.get("name")
            if name == field_name:
                field_id = f["id"]
                options_list = f.get("options", [])
                if not options_list:
                    raise RuntimeError(f"Status フィールド '{field_name}' に options がありません。")
                options_map = {opt["name"]: opt["id"] for opt in options_list}
                return field_id, options_map

        raise RuntimeError(f"単一選択フィールド '{field_name}' が見つかりませんでした。")

    # ---------- Project item 追加 & Status 更新 ----------

    def add_issue_to_project_and_get_item_id(
        self, project_id: str, issue_node_id: str
    ) -> str:
        """
        addProjectV2ItemById により Issue を Project に紐付け、
        その ProjectV2Item の ID を返す。
        既に存在する場合も既存 ID が返る。
        """
        query = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(
            input: { projectId: $projectId, contentId: $contentId }
          ) {
            item {
              id
            }
          }
        }
        """
        data = self.graphql(
            query, {"projectId": project_id, "contentId": issue_node_id}
        )
        item = data.get("addProjectV2ItemById", {}).get("item")
        if not item or not item.get("id"):
            raise RuntimeError("addProjectV2ItemById の結果から item.id を取得できませんでした。")
        return item["id"]

    def update_project_item_status(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
    ) -> None:
        """
        updateProjectV2ItemFieldValue で Status を更新。
        """
        query = """
        mutation(
          $projectId: ID!,
          $itemId: ID!,
          $fieldId: ID!,
          $optionId: String!
        ) {
          updateProjectV2ItemFieldValue(
            input: {
              projectId: $projectId,
              itemId: $itemId,
              fieldId: $fieldId,
              value: { singleSelectOptionId: $optionId }
            }
          ) {
            projectV2Item {
              id
            }
          }
        }
        """
        variables = {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "optionId": option_id,
        }
        data = self.graphql(query, variables)
        if not data.get("updateProjectV2ItemFieldValue", {}).get("projectV2Item"):
            raise RuntimeError("updateProjectV2ItemFieldValue が期待通り成功しませんでした。")


# =======================
# GUI アプリ
# =======================

class GitHubIssueReopenApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("GitHub Issue 一括 Re-open + Project Status 更新ツール")

        # 入力値を保持する変数
        self.owner_var = tk.StringVar()
        self.repo_var = tk.StringVar()
        self.rest_base_var = tk.StringVar(value="https://api.github.com")
        self.graphql_endpoint_var = tk.StringVar(
            value="https://api.github.com/graphql"
        )

        self.token_var = tk.StringVar()
        self.use_env_token_var = tk.BooleanVar(value=True)

        self.use_proxy_var = tk.BooleanVar(value=False)
        self.proxy_var = tk.StringVar()

        self.project_owner_type_var = tk.StringVar(value="org")  # "user" or "org"
        self.project_number_var = tk.StringVar()

        self.status_choice_var = tk.StringVar()
        self.status_manual_var = tk.StringVar()

        self.labels_var = tk.StringVar()

        # Project / Status 関連キャッシュ
        self.project_id: Optional[str] = None
        self.project_title: Optional[str] = None
        self.status_field_id: Optional[str] = None
        self.status_options: Dict[str, str] = {}

        self._build_ui()

    # ---------- UI 構築 ----------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # 列伸縮
        main.columnconfigure(1, weight=1)

        row = 0

        # --- リポジトリ設定 ---
        ttk.Label(main, text="■ GitHub リポジトリ設定").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(main, text="Owner (ユーザ/組織)").grid(row=row, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.owner_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        ttk.Label(main, text="Repository 名").grid(row=row, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.repo_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        ttk.Label(main, text="REST API Base URL").grid(row=row, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.rest_base_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        ttk.Label(main, text="GraphQL Endpoint").grid(row=row, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.graphql_endpoint_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        # --- 認証 & プロキシ ---
        ttk.Label(main, text="■ 認証 & プロキシ設定").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        row += 1

        ttk.Label(main, text="Personal Access Token").grid(
            row=row, column=0, sticky="w"
        )
        ttk.Entry(main, textvariable=self.token_var, show="*").grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        ttk.Checkbutton(
            main,
            text="GITHUB_TOKEN 環境変数を優先して使用（上の入力はなければ使う）",
            variable=self.use_env_token_var,
        ).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        ttk.Checkbutton(
            main,
            text="プロキシを使う",
            variable=self.use_proxy_var,
        ).grid(row=row, column=0, sticky="w")
        ttk.Entry(main, textvariable=self.proxy_var).grid(
            row=row, column=1, sticky="ew"
        )
        ttk.Label(main, text="例: proxy.example.com:8080 または user:pass@host:8080").grid(
            row=row + 1, column=1, sticky="w"
        )
        row += 2

        # --- Project / Status ---
        ttk.Label(main, text="■ Project / Status 設定（任意）").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        row += 1

        # Owner type
        owner_type_frame = ttk.Frame(main)
        owner_type_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Label(owner_type_frame, text="Project Owner Type:").pack(side="left")
        ttk.Radiobutton(
            owner_type_frame,
            text="Organization",
            value="org",
            variable=self.project_owner_type_var,
        ).pack(side="left", padx=(4, 0))
        ttk.Radiobutton(
            owner_type_frame,
            text="User",
            value="user",
            variable=self.project_owner_type_var,
        ).pack(side="left", padx=(4, 0))
        row += 1

        ttk.Label(main, text="Project Number（URL の /projects/数字）").grid(
            row=row, column=0, sticky="w"
        )
        ttk.Entry(main, textvariable=self.project_number_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        # Status 取得ボタン
        status_btn_frame = ttk.Frame(main)
        status_btn_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Button(
            status_btn_frame,
            text="Status 候補を取得してプルダウンに反映",
            command=self.on_fetch_status_options,
        ).pack(side="left")
        row += 1

        ttk.Label(main, text="Status（プルダウン）").grid(row=row, column=0, sticky="w")
        self.status_combo = ttk.Combobox(
            main,
            textvariable=self.status_choice_var,
            state="disabled",  # 取得後に "readonly" に変更
        )
        self.status_combo.grid(row=row, column=1, sticky="ew")
        row += 1

        ttk.Label(main, text="Status 名を手入力（自動取得不可のとき用）").grid(
            row=row, column=0, sticky="w"
        )
        ttk.Entry(main, textvariable=self.status_manual_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        # --- Issue / ラベル / コメント ---
        ttk.Label(main, text="■ Issue / ラベル / コメント").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        row += 1

        ttk.Label(
            main, text="Issue 番号（複数可: 改行 / カンマ / スペース区切り）"
        ).grid(row=row, column=0, sticky="w")
        row += 1
        self.issue_text = tk.Text(main, height=4)
        self.issue_text.grid(row=row, column=0, columnspan=2, sticky="nsew")
        main.rowconfigure(row, weight=1)
        row += 1

        ttk.Label(main, text="追加ラベル（任意, カンマ区切り）").grid(
            row=row, column=0, sticky="w"
        )
        ttk.Entry(main, textvariable=self.labels_var).grid(
            row=row, column=1, sticky="ew"
        )
        row += 1

        ttk.Label(main, text="追加コメント（任意, 全 Issue 共通）").grid(
            row=row, column=0, sticky="w"
        )
        row += 1
        self.comment_text = tk.Text(main, height=5)
        self.comment_text.grid(row=row, column=0, columnspan=2, sticky="nsew")
        main.rowconfigure(row, weight=1)
        row += 1

        # --- 実行ボタン ---
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=(4, 4), sticky="ew")
        ttk.Button(btn_frame, text="実行", command=self.on_run).pack(side="left")
        ttk.Button(btn_frame, text="ログクリア", command=self.clear_log).pack(
            side="left", padx=(4, 0)
        )
        row += 1

        # --- ログ ---
        ttk.Label(main, text="■ ログ").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1
        self.log_text = tk.Text(main, height=10)
        self.log_text.grid(row=row, column=0, columnspan=2, sticky="nsew")
        main.rowconfigure(row, weight=2)

    # ---------- ユーティリティ ----------

    def log(self, msg: str):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def parse_issue_numbers(self, raw: str) -> List[int]:
        tokens = re.split(r"[,\s]+", raw.strip())
        numbers: List[int] = []
        for t in tokens:
            if not t:
                continue
            if not t.isdigit():
                raise ValueError(f"Issue 番号として不正な値があります: '{t}'")
            numbers.append(int(t))
        if not numbers:
            raise ValueError("Issue 番号が入力されていません。")
        return numbers

    def parse_labels(self, raw: str) -> List[str]:
        labels: List[str] = []
        for part in raw.split(","):
            name = part.strip()
            if name:
                labels.append(name)
        return labels

    def build_proxies(self) -> Optional[Dict[str, str]]:
        if not self.use_proxy_var.get():
            return None
        raw = self.proxy_var.get().strip()
        if not raw:
            return None
        if not raw.startswith("http://") and not raw.startswith("https://"):
            raw = "http://" + raw
        return {"http": raw, "https": raw}

    def resolve_token(self) -> str:
        token = self.token_var.get().strip()
        if self.use_env_token_var.get():
            env_token = os.environ.get("GITHUB_TOKEN", "").strip()
            if env_token:
                return env_token
        if not token:
            raise ValueError(
                "Personal Access Token が入力されていないか、GITHUB_TOKEN 環境変数が設定されていません。"
            )
        return token

    # ---------- Status 候補取得ボタン ----------

    def on_fetch_status_options(self):
        self.clear_log()
        try:
            owner = self.owner_var.get().strip()
            repo = self.repo_var.get().strip()
            rest_base = self.rest_base_var.get().strip() or "https://api.github.com"
            graphql_endpoint = (
                self.graphql_endpoint_var.get().strip()
                or "https://api.github.com/graphql"
            )
            token = self.resolve_token()
            project_number_str = self.project_number_var.get().strip()
            if not owner or not repo:
                raise ValueError("Owner / Repository 名は必須です。")
            if not project_number_str:
                raise ValueError("Project Number を入力してください。")
            project_number = int(project_number_str)
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        proxies = self.build_proxies()
        self.log("=== Status 候補取得 ===")
        self.log(f"REST Base: {rest_base}")
        self.log(f"GraphQL Endpoint: {graphql_endpoint}")
        self.log(f"Project Owner Type: {self.project_owner_type_var.get()}")
        self.log(f"Project Number: {project_number_str}")
        self.log(f"プロキシ: {proxies if proxies else 'なし（環境変数 HTTP_PROXY/HTTPS_PROXY は requests によって自動利用）'}")

        try:
            api = GitHubAPI(
                token=token,
                rest_base=rest_base,
                graphql_endpoint=graphql_endpoint,
                proxies=proxies,
            )
            # Project ID を取得
            project_id, title = api.get_project_id(
                owner_login=owner,
                project_number=project_number,
                owner_type=self.project_owner_type_var.get(),
            )
            self.project_id = project_id
            self.project_title = title
            self.log(f"Project 取得成功: id={project_id}, title='{title}'")

            # Status フィールドの options を取得
            field_id, options = api.get_status_field_options(project_id, field_name="Status")
            self.status_field_id = field_id
            self.status_options = options

            names = list(options.keys())
            self.status_combo["values"] = names
            self.status_combo.configure(state="readonly")

            self.log(f"Status フィールド ID: {field_id}")
            self.log(f"Status options: {names}")
            messagebox.showinfo(
                "Status 候補取得完了",
                "Status 候補の取得に成功しました。プルダウンから選択できます。",
            )
        except Exception as e:
            self.log(f"[ERROR] Status 候補の取得に失敗しました: {e}")
            messagebox.showerror("Status 候補取得エラー", str(e))

    # ---------- 実行ボタン ----------

    def on_run(self):
        self.clear_log()
        # 入力値のバリデーションとパース
        try:
            owner = self.owner_var.get().strip()
            repo = self.repo_var.get().strip()
            rest_base = self.rest_base_var.get().strip() or "https://api.github.com"
            graphql_endpoint = (
                self.graphql_endpoint_var.get().strip()
                or "https://api.github.com/graphql"
            )
            token = self.resolve_token()

            if not owner or not repo:
                raise ValueError("Owner / Repository 名は必須です。")

            issue_raw = self.issue_text.get("1.0", tk.END)
            issue_numbers = self.parse_issue_numbers(issue_raw)

            labels_to_add = self.parse_labels(self.labels_var.get())
            comment = self.comment_text.get("1.0", tk.END).strip()

            project_number_str = self.project_number_var.get().strip()
            project_number: Optional[int] = None
            if project_number_str:
                project_number = int(project_number_str)

        except ValueError as e:
            messagebox.showerror("入力エラー", str(e))
            return

        proxies = self.build_proxies()

        self.log("=== 設定内容 ===")
        self.log(f"Repository: {owner}/{repo}")
        self.log(f"REST Base: {rest_base}")
        self.log(f"GraphQL Endpoint: {graphql_endpoint}")
        self.log(f"Issue 数: {len(issue_numbers)}")
        self.log(f"追加ラベル: {labels_to_add if labels_to_add else 'なし'}")
        self.log(f"コメント: {'あり' if comment else 'なし'}")
        self.log(
            f"Project: number={project_number if project_number is not None else '未指定'} "
            f"(owner type={self.project_owner_type_var.get()})"
        )
        self.log(f"プロキシ: {proxies if proxies else 'なし（環境変数利用の可能性あり）'}")
        self.log("=================")

        # GitHubAPI クライアントを作成
        try:
            api = GitHubAPI(
                token=token,
                rest_base=rest_base,
                graphql_endpoint=graphql_endpoint,
                proxies=proxies,
            )
        except Exception as e:
            messagebox.showerror("初期化エラー", f"GitHub API クライアントの初期化に失敗しました: {e}")
            return

        # Project / Status 更新を行うかどうかの判定
        status_target_name = self.status_choice_var.get().strip()
        if not status_target_name:
            status_target_name = self.status_manual_var.get().strip()

        use_project_status = (
            project_number is not None and bool(status_target_name)
        )

        project_id = self.project_id
        status_field_id = self.status_field_id
        option_id: Optional[str] = None

        if use_project_status:
            self.log("Project / Status 更新を有効化します。")
            try:
                # Project ID が未取得ならここで取得
                if not project_id:
                    project_id, title = api.get_project_id(
                        owner_login=owner,
                        project_number=project_number,
                        owner_type=self.project_owner_type_var.get(),
                    )
                    self.project_id = project_id
                    self.project_title = title
                    self.log(f"[Project] id={project_id}, title='{title}' を取得しました。")

                # Status フィールド ID / options が未取得ならここで取得
                if not status_field_id or not self.status_options:
                    field_id, options = api.get_status_field_options(
                        project_id, field_name="Status"
                    )
                    self.status_field_id = field_id
                    self.status_options = options
                    self.log(f"[Status Field] id={field_id}, options={list(options.keys())}")

                # 指定された Status 名から option ID を解決
                option_id = self.status_options.get(status_target_name)
                if not option_id:
                    raise RuntimeError(
                        f"指定された Status 名 '{status_target_name}' は、"
                        f"Project の Status options に存在しません。"
                    )

                status_field_id = self.status_field_id
                self.log(
                    f"Status 更新対象: name='{status_target_name}', option_id={option_id}, field_id={status_field_id}"
                )
            except Exception as e:
                self.log(f"[ERROR] Project / Status 更新準備に失敗しました: {e}")
                self.log("→ Issue の re-open / ラベル更新のみ実行します。")
                use_project_status = False

        # Issue ごとの処理ループ
        for num in issue_numbers:
            self.log(f"\n--- Issue #{num} の処理 ---")
            try:
                state_before, reopened, node_id, labels_before, labels_after = (
                    api.reopen_issue_with_labels(
                        owner=owner,
                        repo=repo,
                        number=num,
                        labels_to_add=labels_to_add,
                    )
                )
                self.log(
                    f"Issue #{num}: state={state_before} -> "
                    f"{'open (変更)' if reopened else state_before} / "
                    f"labels: {labels_before} -> {labels_after}"
                )

                # コメント追加
                if comment:
                    url = f"{api.rest_base}/repos/{owner}/{repo}/issues/{num}/comments"
                    resp = api.session.post(
                        url, json={"body": comment}, timeout=15
                    )
                    if resp.status_code not in (200, 201):
                        self.log(
                            f"[ERROR] Issue #{num} へのコメント追加に失敗: "
                            f"status={resp.status_code}, body={resp.text}"
                        )
                    else:
                        self.log(f"Issue #{num} にコメントを追加しました。")

                # Project Status 更新
                if use_project_status and node_id and project_id and status_field_id and option_id:
                    try:
                        item_id = api.add_issue_to_project_and_get_item_id(
                            project_id=project_id,
                            issue_node_id=node_id,
                        )
                        self.log(
                            f"Issue #{num} の Project item id={item_id} を取得 / 作成しました。"
                        )
                        api.update_project_item_status(
                            project_id=project_id,
                            item_id=item_id,
                            field_id=status_field_id,
                            option_id=option_id,
                        )
                        self.log(
                            f"Issue #{num} の Project Status を '{status_target_name}' に更新しました。"
                        )
                    except Exception as e:
                        self.log(
                            f"[ERROR] Issue #{num} の Project Status 更新に失敗: {e}"
                        )

            except requests.HTTPError as e:
                self.log(
                    f"[ERROR] Issue #{num} の処理中に HTTP エラーが発生しました: {e.response.status_code} {e.response.text}"
                )
            except Exception as e:
                self.log(f"[ERROR] Issue #{num} の処理中に例外: {e}")

        self.log("\n=== 全 Issue の処理が完了しました（エラーはログを確認してください） ===")
        messagebox.showinfo("完了", "全 Issue の処理が完了しました。ログを確認してください。")


def main():
    root = tk.Tk()
    # 多少広めに
    root.geometry("900x700")
    app = GitHubIssueReopenApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
