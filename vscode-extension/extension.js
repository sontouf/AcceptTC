// AcceptTC for VS Code.
//
// Shows the verification page that tc_gate.py generates inside an editor tab,
// keeps it up to date while the agents work, and puts the gate state in the
// status bar.  It runs `tc_gate.py status` and `tc_gate.py dashboard`, which
// read files and the Entire session log; no model is called and no server runs.
//
// The extension also carries the AcceptTC installer (payload/), so a project can
// be set up from the command palette without downloading anything else.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const ICON = { APPROVED: "$(pass)", REJECTED: "$(error)", ESCALATED: "$(warning)", STALE: "$(sync)", UNVERIFIED: "$(circle-outline)" };

let panel = null;        // the open webview, if any
let panelRoot = null;    // project the panel shows
let statusItem = null;
let output = null;
let timer = null;

/** Folders that contain an AcceptTC installation (.accepttc/tc_gate.py). */
async function findProjects() {
  const hits = await vscode.workspace.findFiles("**/.accepttc/tc_gate.py", "**/node_modules/**", 20);
  return hits.map((u) => path.dirname(path.dirname(u.fsPath))).sort((a, b) => a.length - b.length);
}

async function pickProject() {
  const roots = await findProjects();
  if (roots.length === 0) {
    const go = "이 프로젝트에 설치";
    const choice = await vscode.window.showWarningMessage(
      "이 작업 폴더에 AcceptTC 가 설치되어 있지 않습니다.", go);
    if (choice === go) vscode.commands.executeCommand("accepttc.install");
    return null;
  }
  if (roots.length === 1) return roots[0];
  const active = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri.fsPath;
  const owner = active && roots.filter((r) => active.startsWith(r + path.sep)).pop();
  if (owner) return owner;
  return (await vscode.window.showQuickPick(roots, { placeHolder: "AcceptTC 프로젝트를 고르세요" })) || null;
}

/** Run tc_gate.py with arguments; resolves {code, stdout, stderr}. */
function gate(root, args) {
  const python = vscode.workspace.getConfiguration("accepttc").get("python") || "python3";
  return new Promise((resolve) => {
    cp.execFile(python, [path.join(root, ".accepttc", "tc_gate.py"), ...args],
      { cwd: root, timeout: 120000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr || (err && err.message) || "") }));
  });
}

async function updateStatusBar() {
  const roots = await findProjects();
  if (roots.length === 0) { statusItem.hide(); return; }
  const root = panelRoot && roots.includes(panelRoot) ? panelRoot : roots[0];
  const r = await gate(root, ["status", "--json"]);
  let rows = [];
  try { rows = JSON.parse(r.stdout); } catch (e) { /* shown below as unknown */ }
  if (r.code !== 0 || !Array.isArray(rows)) {
    statusItem.text = "$(question) TC";
    statusItem.tooltip = "AcceptTC: 상태를 읽지 못했습니다. " + r.stderr.trim();
  } else {
    const ok = rows.filter((x) => x.status === "APPROVED").length;
    const worst = ["ESCALATED", "REJECTED", "STALE", "UNVERIFIED"].find((s) => rows.some((x) => x.status === s));
    const locked = rows.length === 0 || ok < rows.length;
    statusItem.text = `${rows.length === 0 ? "$(circle-outline)" : ICON[worst || "APPROVED"]} TC ${ok}/${rows.length}`;
    statusItem.tooltip = new vscode.MarkdownString(
      `**AcceptTC** — 테스트 케이스 ${rows.length}개 중 ${ok}개 승인\n\n` +
      (locked ? "구현 코드 잠김: 승인 안 된 테스트 케이스가 있습니다." : "구현 코드 열림: 모든 테스트 케이스가 승인되었습니다.") +
      "\n\n" + rows.filter((x) => x.status !== "APPROVED").slice(0, 12).map((x) => `- ${x.status} \`${x.id}\` ${x.test}`).join("\n") +
      "\n\n클릭하면 검증 현황을 엽니다.");
    statusItem.backgroundColor = worst === "ESCALATED" || worst === "REJECTED"
      ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  }
  statusItem.show();
}

function pageFile(root) { return path.join(root, ".accepttc", "verification", "index.html"); }

function showPage(root) {
  if (!panel) return;
  let html;
  try { html = fs.readFileSync(pageFile(root), "utf8"); } catch (e) {
    html = `<!doctype html><html lang="ko"><body style="font:14px system-ui;padding:24px">
      <h2>아직 검증 현황 페이지가 없습니다</h2><p>테스트 케이스가 생기고 검증이 기록되면 여기에 나타납니다.</p></body></html>`;
  }
  panel.title = "AcceptTC 검증 현황";
  panel.webview.html = html;      // the page is self-contained: inline CSS, JS and data
}

async function regenerate(root) {
  const r = await gate(root, ["dashboard"]);
  if (r.code !== 0) output.appendLine("[dashboard] " + (r.stderr || r.stdout).trim());
  return r.code === 0;
}

async function openDashboard() {
  const root = await pickProject();
  if (!root) return;
  panelRoot = root;
  if (!panel) {
    panel = vscode.window.createWebviewPanel("accepttc.dashboard", "AcceptTC 검증 현황", vscode.ViewColumn.Beside,
      { enableScripts: true, retainContextWhenHidden: true, enableFindWidget: true });
    panel.onDidDispose(() => { panel = null; });
    panel.webview.onDidReceiveMessage(async (msg) => {
      if (!msg || typeof msg.open !== "string" || !panelRoot) return;
      const target = path.resolve(panelRoot, msg.open);
      if (!target.startsWith(panelRoot + path.sep)) return;      // never leave the project
      try {
        const doc = await vscode.workspace.openTextDocument(target);
        const line = Math.max(0, (parseInt(msg.line, 10) || 1) - 1);
        await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One, selection: new vscode.Range(line, 0, line, 0) });
      } catch (e) {
        vscode.window.showWarningMessage("파일을 열지 못했습니다: " + msg.open);
      }
    });
  } else {
    panel.reveal(undefined, true);
  }
  await regenerate(root);
  showPage(root);
  updateStatusBar();
}

function shellQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }

/** Folder to install into: the workspace folder, or one the user picks. */
async function pickFolder(title) {
  const folders = (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
  const other = "다른 폴더 고르기...";
  let choice = folders.length === 1 ? folders[0] : null;
  if (!choice) {
    choice = await vscode.window.showQuickPick([...folders, other], { placeHolder: title });
    if (!choice) return null;
  }
  if (choice === other) {
    const picked = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: "이 폴더 선택" });
    return picked && picked[0] ? picked[0].fsPath : null;
  }
  return choice;
}

/** Run the bundled install.sh in a terminal so its checks and guidance are visible. */
async function runInstaller(context, extraArgs, title, question, confirmLabel) {
  const script = path.join(context.extensionPath, "payload", "install.sh");
  if (!fs.existsSync(script)) {
    vscode.window.showErrorMessage("확장에 설치 파일이 들어 있지 않습니다 (payload/install.sh). 확장을 다시 설치하세요.");
    return;
  }
  if (process.platform === "win32") {
    vscode.window.showErrorMessage("AcceptTC 설치는 Linux(WSL 포함)에서만 지원합니다. WSL 에 연결된 VS Code 창에서 실행하세요.");
    return;
  }
  const folder = await pickFolder(title);
  if (!folder) return;
  const answer = await vscode.window.showInformationMessage(question.replace("{folder}", folder), { modal: true }, confirmLabel);
  if (answer !== confirmLabel) return;
  const terminal = vscode.window.createTerminal({ name: "AcceptTC", cwd: folder });
  terminal.show();
  terminal.sendText(["bash", shellQuote(script), ...extraArgs, shellQuote(folder)].join(" "));
}

/** Something under .accepttc or a test/trace file changed: refresh, debounced. */
function scheduleRefresh(regen) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (panel && panelRoot) {
      if (regen) await regenerate(panelRoot);
      showPage(panelRoot);
    }
    updateStatusBar();
  }, 600);
}

function activate(context) {
  output = vscode.window.createOutputChannel("AcceptTC");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.command = "accepttc.openDashboard";

  // The verifier's `record` rewrites the page itself; test and trace edits change
  // statuses (STALE), so those regenerate the page first.
  const recorded = vscode.workspace.createFileSystemWatcher("**/.accepttc/{registry.json,verification/index.html}");
  const traces = vscode.workspace.createFileSystemWatcher("**/*.trace.json");
  const installed = vscode.workspace.createFileSystemWatcher("**/.accepttc/tc_gate.py");
  for (const ev of ["onDidChange", "onDidCreate", "onDidDelete"]) {
    recorded[ev](() => scheduleRefresh(false));
    traces[ev](() => scheduleRefresh(true));
    installed[ev](() => scheduleRefresh(false));
  }
  const saved = vscode.workspace.onDidSaveTextDocument((doc) => {
    if (!doc.uri.fsPath.includes(path.sep + ".accepttc" + path.sep)) scheduleRefresh(true);
  });

  context.subscriptions.push(output, statusItem, recorded, traces, installed, saved,
    vscode.commands.registerCommand("accepttc.install", () => runInstaller(context, [],
      "AcceptTC 를 설치할 프로젝트 폴더",
      "{folder}\n\n이 폴더에 AcceptTC 를 설치합니다.\n\n- git, python3, Entire CLI 가 있는지 확인합니다 (없으면 설치 명령을 안내하고 멈춥니다)\n- Entire 를 켭니다 (세션 로그는 git push 때 올리지 않도록 설정)\n- .accepttc/ 와 .claude/ 에 게이트 도구, 검증 에이전트, 스킬, 훅, 권한 규칙을 넣습니다\n\n진행 상황은 터미널에 표시됩니다.",
      "설치")),
    vscode.commands.registerCommand("accepttc.uninstall", () => runInstaller(context, ["--uninstall"],
      "AcceptTC 를 제거할 프로젝트 폴더",
      "{folder}\n\n이 폴더에서 AcceptTC 를 제거합니다. 설정과 승인 기록(.accepttc/)은 남기고, Entire 는 그대로 둡니다.",
      "제거")),
    vscode.commands.registerCommand("accepttc.openDashboard", openDashboard),
    vscode.commands.registerCommand("accepttc.refresh", async () => {
      const root = panelRoot || (await pickProject());
      if (!root) return;
      panelRoot = root;
      const ok = await regenerate(root);
      showPage(root);
      updateStatusBar();
      if (!ok) { output.show(true); vscode.window.showWarningMessage("검증 현황을 새로 만들지 못했습니다. 출력 창(AcceptTC)을 확인하세요."); }
    }),
    vscode.commands.registerCommand("accepttc.doctor", async () => {
      const root = await pickProject();
      if (!root) return;
      const r = await gate(root, ["doctor"]);
      output.appendLine(r.stdout + r.stderr);
      output.show(true);
    }));
  updateStatusBar();
}

function deactivate() { clearTimeout(timer); }

module.exports = { activate, deactivate };
