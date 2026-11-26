// Smoke test for the VS Code extension without VS Code: a minimal stand-in for
// the `vscode` module, then activate and drive the views for a project.
//   node selftest/vscode_smoke.js <project-with-.accepttc>
const Module = require("module");
const path = require("path");
const project = path.resolve(process.argv[2] || ".");
const commands = {};
const state = { status: null, panel: null, messages: [], opened: [], sent: [], posted: [], context: {} };
const disposable = { dispose() {} };
const watcher = { onDidChange: () => disposable, onDidCreate: () => disposable, onDidDelete: () => disposable, dispose() {} };
class Emitter { constructor() { this.fired = 0; this.event = () => disposable; } fire() { this.fired++; } dispose() {} }
const vscode = {
  ViewColumn: { One: 1, Beside: -2 }, StatusBarAlignment: { Left: 1 },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  EventEmitter: Emitter,
  TreeItem: class { constructor(label, s) { this.label = label; this.collapsibleState = s; } },
  ThemeIcon: class { constructor(id, color) { this.id = id; this.color = color; } },
  ThemeColor: class { constructor(id) { this.id = id; } },
  MarkdownString: class { constructor(v) { this.value = v; } },
  Range: class { constructor(a) { this.start = { line: a }; } },
  CodeLens: class { constructor(range, command) { this.range = range; this.command = command; } },
  workspace: {
    workspaceFolders: [{ uri: { fsPath: project } }],
    findFiles: async () => [{ fsPath: path.join(project, ".accepttc", "tc_gate.py") }],
    getConfiguration: () => ({ get: () => "python3" }),
    createFileSystemWatcher: () => watcher,
    onDidSaveTextDocument: () => disposable,
    openTextDocument: async (p) => { state.opened.push(p); return { uri: { fsPath: p } }; },
  },
  languages: { registerCodeLensProvider: () => disposable },
  window: {
    createOutputChannel: () => ({ appendLine: (l) => state.messages.push(l), show() {}, dispose() {} }),
    createStatusBarItem: () => (state.status = { show() {}, hide() {}, dispose() {} }),
    registerTreeDataProvider: () => disposable,
    createWebviewPanel: () => (state.panel = {
      webview: { html: "", onDidReceiveMessage(fn) { state.onMessage = fn; }, postMessage: (m) => state.posted.push(m) },
      onDidDispose() {}, reveal() {} }),
    showWarningMessage: (m) => state.messages.push("WARN " + m),
    showErrorMessage: (m) => state.messages.push("ERROR " + m),
    showInformationMessage: async (m, opts, label) => label,
    showQuickPick: async (items) => items[0],
    showTextDocument: async () => {},
    createTerminal: () => ({ show() {}, sendText: (t) => state.sent.push(t) }),
  },
  commands: {
    registerCommand: (id, fn) => { commands[id] = fn; return disposable; },
    executeCommand: (id, key, value) => { if (id === "setContext") state.context[key] = value; },
  },
};
const load = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscode : load(request, ...rest));
const ext = require("../vscode-extension/extension.js");
const check = (name, ok) => { console.log((ok ? "ok   " : "FAIL ") + name); if (!ok) process.exitCode = 1; };

(async () => {
  const extensionPath = path.join(__dirname, "..", "vscode-extension");
  await ext.activate({ subscriptions: [], extensionPath });
  const { tree, lenses, getModel } = ext._test;
  const rows = getModel().rows;
  check("status read: " + rows.length + " test case(s), status bar '" + state.status.text + "'", rows.length > 0 && /TC \d+\/\d+/.test(state.status.text));
  check("installed context key set", state.context["accepttc.installed"] === true);

  // sidebar: lock line, status groups, test cases, details
  const top = tree.getChildren();
  check("sidebar top: " + top.map((n) => n.label + (n.description ? " (" + n.description + ")" : "")).join(" | "), top.length >= 2 && /구현 코드/.test(top[0].label));
  const group = top[1], cases = tree.getChildren(group);
  check("group '" + group.label + "' lists " + cases.length + " case(s), first: " + cases[0].label + " " + cases[0].description, cases.length > 0 && /^TC-|^\(no-id/.test(cases[0].label));
  const detail = tree.getChildren(cases[0]);
  check("case details: " + detail.map((n) => n.label).join(", "), detail[0].label === "테스트 코드" && detail.length >= 2);

  // CodeLens above the test case
  const testFile = rows[0].test_file;
  const lens = lenses.provideCodeLenses({ uri: { fsPath: path.join(project, testFile) } });
  check("CodeLens in " + testFile + ": " + (lens[0] && lens[0].command.title), lens.length === rows.filter((r) => r.test_file === testFile).length && lens.length > 0);

  // clicking a case opens the page on that case
  await commands["accepttc.showCase"](rows[0].id);
  const html = state.panel.webview.html;
  check("page loaded into the webview (" + html.length + " bytes)", html.includes('id="data"') && !html.includes("__TC_GATE_DATA__"));
  state.onMessage({ ready: true });
  check("page asked to show " + rows[0].id, state.posted.some((m) => m.select === rows[0].id));

  // files open inside the project only
  await commands[detail[0].command.command](...detail[0].command.arguments);
  await commands["accepttc.openFile"]("../../outside.txt", 1);
  check("opens project files only: " + JSON.stringify(state.opened.map((p) => path.relative(project, p))), state.opened.length === 1);

  // install / uninstall run the bundled installer in a terminal
  await commands["accepttc.install"]();
  const expected = "bash '" + path.join(extensionPath, "payload", "install.sh") + "' '" + project + "'";
  check("install runs the bundled installer: " + state.sent[0], state.sent[0] === expected);
  await commands["accepttc.uninstall"]();
  check("uninstall passes --uninstall", / --uninstall '/.test(state.sent[1] || ""));
  if (state.messages.length) console.log(state.messages.join("\n"));
  ext.deactivate();
})();
