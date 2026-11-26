// Smoke test for the VS Code extension without VS Code: a minimal stand-in for
// the `vscode` module, then activate and open the dashboard for a project.
//   node selftest/vscode_smoke.js <project-with-.accepttc>
const Module = require("module");
const path = require("path");
const project = path.resolve(process.argv[2] || ".");
const commands = {};
const state = { status: null, panel: null, messages: [], opened: [] };
const disposable = { dispose() {} };
const watcher = { onDidChange: () => disposable, onDidCreate: () => disposable, onDidDelete: () => disposable, dispose() {} };
const vscode = {
  ViewColumn: { One: 1, Beside: -2 }, StatusBarAlignment: { Left: 1 },
  ThemeColor: class { constructor(id) { this.id = id; } },
  MarkdownString: class { constructor(v) { this.value = v; } },
  Range: class { constructor(a, b, c, d) { this.start = { line: a }; } },
  workspace: {
    findFiles: async () => [{ fsPath: path.join(project, ".accepttc", "tc_gate.py") }],
    getConfiguration: () => ({ get: () => "python3" }),
    createFileSystemWatcher: () => watcher,
    onDidSaveTextDocument: () => disposable,
    openTextDocument: async (p) => { state.opened.push(p); return { uri: { fsPath: p } }; },
  },
  window: {
    activeTextEditor: null,
    createOutputChannel: () => ({ appendLine: (l) => state.messages.push(l), show() {}, dispose() {} }),
    createStatusBarItem: () => (state.status = { show() {}, hide() {}, dispose() {} }),
    createWebviewPanel: () => (state.panel = {
      webview: { html: "", onDidReceiveMessage(fn) { state.onMessage = fn; } },
      onDidDispose() {}, reveal() {} }),
    showWarningMessage: (m) => state.messages.push("WARN " + m),
    showQuickPick: async (items) => items[0],
    showTextDocument: async () => {},
  },
  commands: { registerCommand: (id, fn) => { commands[id] = fn; return disposable; } },
};
const load = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscode : load(request, ...rest));
const ext = require("../vscode-extension/extension.js");

(async () => {
  ext.activate({ subscriptions: [] });
  await commands["accepttc.openDashboard"]();
  await new Promise((r) => setTimeout(r, 1500));
  const html = state.panel.webview.html;
  const check = (name, ok) => { console.log((ok ? "ok   " : "FAIL ") + name); if (!ok) process.exitCode = 1; };
  check("page loaded into the webview (" + html.length + " bytes)", html.includes('id="data"') && !html.includes("__TC_GATE_DATA__"));
  check("status bar text: " + state.status.text, /TC \d+\/\d+/.test(state.status.text || ""));
  await state.onMessage({ open: ".accepttc/config.json", line: 1 });
  await state.onMessage({ open: "../../outside.txt", line: 1 });
  check("opens project files only: " + JSON.stringify(state.opened.map((p) => path.relative(project, p))), state.opened.length === 1);
  if (state.messages.length) console.log(state.messages.join("\n"));
  ext.deactivate();
})();
