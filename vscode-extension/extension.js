// AcceptTC for VS Code.
//
// - Activity Bar view: every test case grouped by approval state, with its
//   specification sources and verification attempts.
// - CodeLens above each test case in the editor: its approval state.
// - Status bar: approved / total.
// - Verification page (the HTML tc_gate.py generates) in an editor tab.  It
//   opens by itself when the AcceptTC sidebar is shown; no click is needed.
// - Install / uninstall AcceptTC in a project from the command palette; the
//   installer travels inside the extension (payload/).
//
// The extension only runs `tc_gate.py status` and `tc_gate.py dashboard`, which
// read files and the Entire session log.  No model is called and no server runs.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const ORDER = ["ESCALATED", "REJECTED", "STALE", "UNVERIFIED", "APPROVED"];
const LABEL = { APPROVED: "승인", REJECTED: "거부", ESCALATED: "사람 결정 필요", STALE: "승인 후 변경됨", UNVERIFIED: "미검증" };
const CODICON = { APPROVED: "pass", REJECTED: "error", ESCALATED: "warning", STALE: "sync", UNVERIFIED: "circle-outline" };
const COLOR = { APPROVED: "testing.iconPassed", REJECTED: "testing.iconFailed", ESCALATED: "editorWarning.foreground",
                STALE: "editorWarning.foreground", UNVERIFIED: "disabledForeground" };

let context = null;
let panel = null;          // the open verification page, if any
let view = null;           // the sidebar tree view
let opening = false;       // the page is being opened by autoOpen
let pendingSelect = null;  // test case to show once the page has loaded
let statusItem = null;
let output = null;
let timer = null;
let model = { root: null, rows: [], error: null };   // what the views show
const treeChanged = new vscode.EventEmitter();
const lensChanged = new vscode.EventEmitter();

// ------------------------------------------------------------------ project
/** Folders that contain an AcceptTC installation (.accepttc/tc_gate.py). */
async function findProjects() {
  const hits = await vscode.workspace.findFiles("**/.accepttc/tc_gate.py", "**/node_modules/**", 20);
  return hits.map((u) => path.dirname(path.dirname(u.fsPath))).sort((a, b) => a.length - b.length);
}

async function pickProject() {
  const roots = await findProjects();
  if (roots.length === 0) {
    const go = "이 프로젝트에 설치";
    const choice = await vscode.window.showWarningMessage("이 작업 폴더에 AcceptTC 가 설치되어 있지 않습니다.", go);
    if (choice === go) vscode.commands.executeCommand("accepttc.install");
    return null;
  }
  if (roots.length === 1) return roots[0];
  if (model.root && roots.includes(model.root)) return model.root;
  return (await vscode.window.showQuickPick(roots, { placeHolder: "AcceptTC 프로젝트를 고르세요" })) || null;
}

/** Run tc_gate.py with arguments; resolves {code, stdout, stderr}. */
function gate(root, args) {
  const python = vscode.workspace.getConfiguration("accepttc").get("python") || "python3";
  return new Promise((resolve) => {
    cp.execFile(python, [path.join(root, ".accepttc", "tc_gate.py"), ...args],
      { cwd: root, timeout: 120000, maxBuffer: 16 * 1024 * 1024 },
      (err, stdout, stderr) => resolve({
        code: err ? (typeof err.code === "number" ? err.code : 1) : 0,
        stdout: String(stdout), stderr: String(stderr || (err && err.message) || "") }));
  });
}

/** Re-read the gate state and repaint the sidebar, the CodeLenses and the status bar. */
async function reload() {
  const roots = await findProjects();
  const root = model.root && roots.includes(model.root) ? model.root : roots[0] || null;
  vscode.commands.executeCommand("setContext", "accepttc.installed", !!root);
  if (!root) {
    model = { root: null, rows: [], error: null };
  } else {
    const r = await gate(root, ["status", "--json"]);
    let rows = null;
    try { rows = JSON.parse(r.stdout); } catch (e) { /* reported below */ }
    model = Array.isArray(rows) && r.code === 0
      ? { root, rows, error: null }
      : { root, rows: [], error: (r.stderr || r.stdout || "상태를 읽지 못했습니다").trim() };
  }
  treeChanged.fire();
  lensChanged.fire();
  paintStatusBar();
}

function counts() {
  const ok = model.rows.filter((x) => x.status === "APPROVED").length;
  return { ok, total: model.rows.length, locked: model.rows.length === 0 || ok < model.rows.length,
           worst: ORDER.find((s) => s !== "APPROVED" && model.rows.some((x) => x.status === s)) };
}

function paintStatusBar() {
  if (!model.root) { statusItem.hide(); return; }
  if (model.error) {
    statusItem.text = "$(question) TC";
    statusItem.tooltip = "AcceptTC: " + model.error;
  } else {
    const c = counts();
    statusItem.text = `$(${c.total === 0 ? "circle-outline" : CODICON[c.worst || "APPROVED"]}) TC ${c.ok}/${c.total}`;
    statusItem.tooltip = `AcceptTC — 테스트 케이스 ${c.total}개 중 ${c.ok}개 승인. ` +
      (c.locked ? "구현 코드 잠김." : "구현 코드 열림.") + " 클릭하면 목록을 엽니다.";
    statusItem.backgroundColor = c.worst === "ESCALATED" || c.worst === "REJECTED"
      ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  }
  statusItem.show();
}

// ------------------------------------------------------------------ sidebar
function item(label, state, props) {
  const it = new vscode.TreeItem(label, state === undefined ? vscode.TreeItemCollapsibleState.None : state);
  return Object.assign(it, props || {});
}
function icon(status) { return new vscode.ThemeIcon(CODICON[status] || "circle-outline", new vscode.ThemeColor(COLOR[status] || "foreground")); }
function openFileCommand(file, line) {
  return { command: "accepttc.openFile", title: "열기", arguments: [file, parseInt(line, 10) || 1] };
}

const tree = {
  onDidChangeTreeData: treeChanged.event,
  getTreeItem: (node) => node,
  getChildren(node) {
    const S = vscode.TreeItemCollapsibleState;
    if (!model.root) return [];
    if (!node) {
      if (model.error) return [item("상태를 읽지 못했습니다", S.None, { description: model.error, iconPath: new vscode.ThemeIcon("warning"), command: { command: "accepttc.doctor", title: "점검" } })];
      const c = counts();
      const head = item(c.locked ? "구현 코드 잠김" : "구현 코드 열림", S.None, {
        description: c.total ? `승인 ${c.ok}/${c.total}` : "테스트 케이스 없음",
        iconPath: new vscode.ThemeIcon(c.locked ? "lock" : "unlock", new vscode.ThemeColor(c.locked ? "testing.iconFailed" : "testing.iconPassed")),
        tooltip: c.locked ? "승인되지 않은 테스트 케이스가 있어 구현 코드를 수정할 수 없습니다." : "모든 테스트 케이스가 승인되어 구현 코드를 수정할 수 있습니다.",
        command: { command: "accepttc.openDashboard", title: "검증 현황 열기" } });
      const groups = ORDER.filter((s) => model.rows.some((r) => r.status === s)).map((s) => {
        const rows = model.rows.filter((r) => r.status === s);
        return item(LABEL[s], s === "APPROVED" && rows.length < model.rows.length ? S.Collapsed : S.Expanded,
          { description: String(rows.length), iconPath: icon(s), contextValue: "group", kind: "group", rows });
      });
      return [head, ...groups];
    }
    if (node.kind === "group") {
      return node.rows.map((r) => item(r.id, S.Collapsed, {
        description: r.test + (r.rejected ? `  · 거부 ${r.rejected}회` : ""), iconPath: icon(r.status),
        tooltip: new vscode.MarkdownString(`**${r.id}** — ${LABEL[r.status]}\n\n\`${r.test}\`\n\n${r.objective || ""}\n\n${r.test_file}:${r.line}`),
        contextValue: "case", kind: "case", row: r,
        command: { command: "accepttc.showCase", title: "검증 과정 보기", arguments: [r.id] } }));
    }
    if (node.kind === "case") {
      const r = node.row;
      const kids = [item("테스트 코드", S.None, { description: `${r.test_file}:${r.line}`, iconPath: new vscode.ThemeIcon("beaker"), command: openFileCommand(r.test_file, r.line) })];
      for (const s of r.sources || []) {
        kids.push(item(`근거 ${s.ref}`, S.None, { description: `${s.file}:${s.lines}`, iconPath: new vscode.ThemeIcon("book"),
          tooltip: "명세의 이 줄을 엽니다", command: openFileCommand(s.file, s.lines) }));
      }
      if (!(r.sources || []).length) kids.push(item("명세 근거 없음", S.None, { iconPath: new vscode.ThemeIcon("warning") }));
      for (const a of r.attempts || []) {
        kids.push(item(`검증 ${a.n}회차: ${LABEL[a.verdict] || a.verdict}`, S.None, {
          description: `${String(a.at || "").replace("T", " ").replace("Z", " UTC")} · ${a.by || ""}`,
          iconPath: icon(a.verdict), tooltip: "판단 근거가 적힌 보고서를 엽니다", command: openFileCommand(a.report, 1) }));
      }
      if (!(r.attempts || []).length) kids.push(item("아직 검증하지 않음", S.None, { iconPath: new vscode.ThemeIcon("circle-outline") }));
      return kids;
    }
    return [];
  },
};

// ------------------------------------------------------------------ CodeLens
const lenses = {
  onDidChangeCodeLenses: lensChanged.event,
  provideCodeLenses(doc) {
    if (!model.root) return [];
    const relp = path.relative(model.root, doc.uri.fsPath).split(path.sep).join("/");
    return model.rows.filter((r) => r.test_file === relp).map((r) => {
      const line = Math.max(0, (r.line || 1) - 1);
      const mark = { APPROVED: "✓", REJECTED: "✕", ESCALATED: "!", STALE: "↻", UNVERIFIED: "○" }[r.status] || "○";
      return new vscode.CodeLens(new vscode.Range(line, 0, line, 0), {
        title: `${mark} ${r.id} · ${LABEL[r.status]}${r.rejected ? ` (거부 ${r.rejected}회)` : ""} — 검증 과정 보기`,
        command: "accepttc.showCase", arguments: [r.id] });
    });
  },
};

// ------------------------------------------------------------------ verification page
function pageFile(root) { return path.join(root, ".accepttc", "verification", "index.html"); }

function showPage(root) {
  if (!panel) return;
  let html;
  try { html = fs.readFileSync(pageFile(root), "utf8"); } catch (e) {
    html = `<!doctype html><html lang="ko"><body style="font:14px system-ui;padding:24px">
      <h2>아직 검증 현황 페이지가 없습니다</h2><p>테스트 케이스가 생기고 검증이 기록되면 여기에 나타납니다.</p></body></html>`;
  }
  panel.webview.html = html;      // the page is self-contained: inline CSS, JS and data
}

async function regenerate(root) {
  const r = await gate(root, ["dashboard"]);
  if (r.code !== 0) output.appendLine("[dashboard] " + (r.stderr || r.stdout).trim());
  return r.code === 0;
}

async function openFile(file, line) {
  if (!model.root || typeof file !== "string") return;
  const target = path.resolve(model.root, file);
  if (!target.startsWith(model.root + path.sep)) return;        // never leave the project
  try {
    const doc = await vscode.workspace.openTextDocument(target);
    const at = Math.max(0, (parseInt(line, 10) || 1) - 1);
    await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn.One, preview: true, selection: new vscode.Range(at, 0, at, 0) });
  } catch (e) {
    vscode.window.showWarningMessage("파일을 열지 못했습니다: " + file);
  }
}

/** Show the verification page as soon as the sidebar is visible, without a click. */
async function autoOpen() {
  if (opening || panel || !view || !view.visible || !model.root || model.error) return;
  opening = true;
  try { await openDashboard(null, true); } finally { opening = false; }
}

async function openDashboard(selectId, keepFocus) {
  const root = await pickProject();
  if (!root) return;
  model.root = root;
  pendingSelect = typeof selectId === "string" ? selectId : null;
  if (!panel) {
    panel = vscode.window.createWebviewPanel("accepttc.dashboard", "AcceptTC 검증 현황",
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: !!keepFocus },
      { enableScripts: true, retainContextWhenHidden: true, enableFindWidget: true });
    panel.onDidDispose(() => { panel = null; });
    panel.webview.onDidReceiveMessage((msg) => {
      if (!msg) return;
      if (msg.ready && pendingSelect && panel) { panel.webview.postMessage({ select: pendingSelect }); pendingSelect = null; }
      if (typeof msg.open === "string") openFile(msg.open, msg.line);
      if (msg.saveConfig && typeof msg.saveConfig === "object") saveConfig(msg.saveConfig);
    });
    await regenerate(root);
    showPage(root);
  } else {
    panel.reveal(undefined, true);
    if (pendingSelect) { panel.webview.postMessage({ select: pendingSelect }); pendingSelect = null; }
  }
}

// ------------------------------------------------------------------ settings
// The settings tab of the verification page sends the configuration the human
// chose.  Agents cannot change .accepttc/config.json (the hook refuses), so this
// write happens here, after the human confirms it.
const CONFIG_LISTS = ["test_globs", "production_globs", "spec_globs", "coverage_files"];

function cleanConfig(cfg) {
  const out = {};
  if (typeof cfg.framework !== "string" || !/^[a-z][a-z0-9_]*$/.test(cfg.framework)) return null;
  out.framework = cfg.framework;
  for (const key of CONFIG_LISTS) {
    if (cfg[key] === undefined) continue;
    if (!Array.isArray(cfg[key]) || !cfg[key].every((x) => typeof x === "string" && x.length < 300 && !x.includes("\n"))) return null;
    out[key] = cfg[key].map((x) => x.trim()).filter(Boolean);
  }
  if (!out.production_globs || !out.production_globs.length) return null;   // nothing locked: not a usable gate
  if (cfg.language !== undefined) {
    if (!["ko", "en", "any"].includes(cfg.language)) return null;
    out.language = cfg.language;
  }
  if (cfg.max_attempts !== undefined) {
    const n = parseInt(cfg.max_attempts, 10);
    if (!(n >= 1 && n <= 9)) return null;
    out.max_attempts = n;
  }
  return out;
}

async function saveConfig(cfg) {
  const root = model.root;
  const reply = (ok, error) => { if (panel) panel.webview.postMessage({ configSaved: ok, error }); };
  if (!root) return reply(false, "AcceptTC 프로젝트를 찾지 못했습니다");
  const clean = cleanConfig(cfg);
  if (!clean) return reply(false, "설정 값이 올바르지 않습니다 (구현 코드 위치는 하나 이상 있어야 합니다)");
  const file = path.join(root, ".accepttc", "config.json");
  let old = {};
  try { old = JSON.parse(fs.readFileSync(file, "utf8")); } catch (e) { /* no usable file yet */ }
  const confirm = "저장";
  const answer = await vscode.window.showWarningMessage(
    `AcceptTC 설정을 저장합니다.\n\n프레임워크: ${old.framework || "(없음)"} → ${clean.framework}\n` +
    `테스트 위치: ${clean.test_globs ? clean.test_globs.join(", ") : "프레임워크 기본값"}\n` +
    `구현 코드 위치: ${clean.production_globs.join(", ")}\n\n` +
    "프레임워크나 위치가 바뀌면 테스트 케이스를 다시 찾습니다.", { modal: true }, confirm);
  if (answer !== confirm) return reply(false, "취소됨");
  const next = Object.assign({}, typeof old === "object" && old ? old : {}, clean);
  if (clean.test_globs === undefined) delete next.test_globs;      // back to the framework's default
  try {
    fs.writeFileSync(file, JSON.stringify(next, null, 2) + "\n", "utf8");
  } catch (e) {
    return reply(false, String(e && e.message || e));
  }
  reply(true);
  await reload();
  await regenerate(root);
  showPage(root);
}

/** Something changed on disk: refresh the views, debounced. */
function scheduleRefresh(regen) {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    await reload();
    if (panel && model.root) {
      if (regen) await regenerate(model.root);
      showPage(model.root);
    }
  }, 600);
}

// ------------------------------------------------------------------ installer
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
async function runInstaller(extraArgs, title, question, confirmLabel) {
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
  if (!extraArgs.includes("--uninstall") && !fs.existsSync(path.join(folder, ".accepttc", "config.json"))) {
    // A new project often has no tests yet, so the framework cannot always be detected.
    const pick = await vscode.window.showQuickPick([
      { label: "GoogleTest", description: "기본값 · C/C++", value: "gtest" },
      { label: "CUnit", description: "C", value: "cunit" },
      { label: "JUnit", description: "Java, Kotlin", value: "junit" },
      { label: "CMocka", description: "C", value: "cmocka" },
      { label: "Unity", description: "C", value: "unity" },
      { label: "pytest", description: "Python (unittest 형식 포함)", value: "pytest" },
      { label: "Jest / Vitest / Mocha", description: "JavaScript, TypeScript", value: "jest" },
      { label: "Go testing", description: "Go", value: "go" },
      { label: "자동 판별", description: "프로젝트의 기존 테스트 파일을 보고 고릅니다 (판별할 파일이 없으면 GoogleTest)", value: "" },
    ], { placeHolder: "이 프로젝트의 테스트 프레임워크 (나중에 검증 현황의 설정 탭에서 바꿀 수 있습니다)" });
    if (!pick) return;
    if (pick.value) extraArgs = [...extraArgs, "--framework", pick.value];
  }
  const answer = await vscode.window.showInformationMessage(question.replace("{folder}", folder), { modal: true }, confirmLabel);
  if (answer !== confirmLabel) return;
  const terminal = vscode.window.createTerminal({ name: "AcceptTC", cwd: folder });
  terminal.show();
  terminal.sendText(["bash", shellQuote(script), ...extraArgs, shellQuote(folder)].join(" "));
}

// ------------------------------------------------------------------ activation
function activate(ctx) {
  context = ctx;
  output = vscode.window.createOutputChannel("AcceptTC");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.command = "accepttc.cases.focus";

  // The verifier's `record` rewrites the page itself; test and trace edits change
  // statuses (STALE), so those regenerate the page first.
  const recorded = vscode.workspace.createFileSystemWatcher("**/.accepttc/{registry.json,verification/index.html,tc_gate.py,config.json}");
  const traces = vscode.workspace.createFileSystemWatcher("**/*.trace.json");
  for (const ev of ["onDidChange", "onDidCreate", "onDidDelete"]) {
    recorded[ev](() => scheduleRefresh(false));
    traces[ev](() => scheduleRefresh(true));
  }
  const saved = vscode.workspace.onDidSaveTextDocument((doc) => {
    if (!doc.uri.fsPath.includes(path.sep + ".accepttc" + path.sep)) scheduleRefresh(true);
  });

  ctx.subscriptions.push(output, statusItem, recorded, traces, saved, treeChanged, lensChanged,
    (view = vscode.window.createTreeView("accepttc.cases", { treeDataProvider: tree })),
    view.onDidChangeVisibility(() => autoOpen()),
    vscode.languages.registerCodeLensProvider({ scheme: "file" }, lenses),
    vscode.commands.registerCommand("accepttc.openDashboard", () => openDashboard()),
    vscode.commands.registerCommand("accepttc.showCase", (id) => openDashboard(id && id.row ? id.row.id : id)),
    vscode.commands.registerCommand("accepttc.openFile", openFile),
    vscode.commands.registerCommand("accepttc.refresh", async () => {
      await reload();
      if (!model.root) return;
      const ok = await regenerate(model.root);
      showPage(model.root);
      if (!ok) { output.show(true); vscode.window.showWarningMessage("검증 현황을 새로 만들지 못했습니다. 출력 창(AcceptTC)을 확인하세요."); }
    }),
    vscode.commands.registerCommand("accepttc.doctor", async () => {
      const root = await pickProject();
      if (!root) return;
      const r = await gate(root, ["doctor"]);
      output.appendLine(r.stdout + r.stderr);
      output.show(true);
    }),
    vscode.commands.registerCommand("accepttc.install", () => runInstaller([],
      "AcceptTC 를 설치할 프로젝트 폴더",
      "{folder}\n\n이 폴더에 AcceptTC 를 설치합니다.\n\n- git, python3, Entire CLI 가 있는지 확인합니다 (없으면 설치 명령을 안내하고 멈춥니다)\n- Entire 를 켭니다 (세션 로그는 git push 때 올리지 않도록 설정)\n- .accepttc/ 와 .claude/ 에 게이트 도구, 검증 에이전트, 스킬, 훅, 권한 규칙을 넣습니다\n\n진행 상황은 터미널에 표시됩니다.",
      "설치")),
    vscode.commands.registerCommand("accepttc.uninstall", () => runInstaller(["--uninstall"],
      "AcceptTC 를 제거할 프로젝트 폴더",
      "{folder}\n\n이 폴더에서 AcceptTC 를 제거합니다. 설정과 승인 기록(.accepttc/)은 남기고, Entire 는 그대로 둡니다.",
      "제거")));
  return reload().then(autoOpen);
}

function deactivate() { clearTimeout(timer); }

module.exports = { activate, deactivate, _test: { tree, lenses, cleanConfig, getModel: () => model } };
