"use strict";

const http = require("http");
const https = require("https");
const vscode = require("vscode");

function config() {
  return vscode.workspace.getConfiguration("maxwellConductor");
}

function daemonUrl() {
  return String(config().get("daemonUrl") || "http://127.0.0.1:8080").replace(/\/+$/, "");
}

function authHeaders() {
  const token = String(config().get("token") || "").trim();
  return token ? { authorization: `Bearer ${token}` } : {};
}

function requestJson(method, path, body) {
  return new Promise((resolve, reject) => {
    const base = daemonUrl();
    const url = new URL(path, `${base}/`);
    const payload = body === undefined ? undefined : JSON.stringify(body);
    const client = url.protocol === "https:" ? https : http;
    const req = client.request(
      url,
      {
        method,
        headers: {
          accept: "application/json",
          ...(payload ? { "content-type": "application/json", "content-length": Buffer.byteLength(payload) } : {}),
          ...authHeaders(),
        },
      },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => {
          raw += chunk;
        });
        res.on("end", () => {
          if (res.statusCode < 200 || res.statusCode >= 300) {
            reject(new Error(`${method} ${path} failed: ${res.statusCode} ${raw.slice(0, 200)}`));
            return;
          }
          if (!raw) {
            resolve(null);
            return;
          }
          try {
            resolve(JSON.parse(raw));
          } catch (error) {
            reject(error);
          }
        });
      }
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

class MaxwellTreeItem extends vscode.TreeItem {
  constructor(label, collapsibleState, options = {}) {
    super(label, collapsibleState);
    this.description = options.description;
    this.tooltip = options.tooltip || label;
    this.contextValue = options.contextValue;
    this.command = options.command;
  }
}

class AgentTreeProvider {
  constructor() {
    this._onDidChangeTreeData = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._onDidChangeTreeData.event;
    this.snapshot = { backends: [], tasks: [], repos: [], error: null };
  }

  refresh() {
    this._onDidChangeTreeData.fire();
  }

  async load() {
    try {
      const [backends, tasks, fleet] = await Promise.all([
        requestJson("GET", "/api/v1/backends"),
        requestJson("GET", "/api/v1/tasks?limit=50"),
        requestJson("GET", "/api/v1/fleet").catch(() => ({ repos: [] })),
      ]);
      this.snapshot = {
        backends: backends.backends || [],
        tasks: Array.isArray(tasks) ? tasks : [],
        repos: fleet.repos || [],
        error: null,
      };
    } catch (error) {
      this.snapshot = { backends: [], tasks: [], repos: [], error };
    }
  }

  getTreeItem(element) {
    return element;
  }

  async getChildren(element) {
    if (!element) {
      await this.load();
      if (this.snapshot.error) {
        return [
          new MaxwellTreeItem("Daemon unavailable", vscode.TreeItemCollapsibleState.None, {
            description: this.snapshot.error.message,
          }),
        ];
      }
      return [
        new MaxwellTreeItem("Active agents", vscode.TreeItemCollapsibleState.Expanded, { contextValue: "group:agents" }),
        new MaxwellTreeItem("Fleet repositories", vscode.TreeItemCollapsibleState.Collapsed, { contextValue: "group:fleet" }),
        new MaxwellTreeItem("Recent tasks", vscode.TreeItemCollapsibleState.Collapsed, { contextValue: "group:tasks" }),
      ];
    }

    if (element.contextValue === "group:agents") {
      return this.snapshot.backends.map(
        (name) => new MaxwellTreeItem(name, vscode.TreeItemCollapsibleState.None, { description: "available" })
      );
    }

    if (element.contextValue === "group:fleet") {
      return this.snapshot.repos.map(
        (repo) =>
          new MaxwellTreeItem(repo.name, vscode.TreeItemCollapsibleState.None, {
            description: `${repo.active_tasks || 0} active`,
            tooltip: `${repo.org}/${repo.name}`,
          })
      );
    }

    if (element.contextValue === "group:tasks") {
      return this.snapshot.tasks.map((task) => {
        const target = task.issue_repo ? `${task.issue_repo}#${task.issue_number}` : task.prompt;
        return new MaxwellTreeItem(target || task.id, vscode.TreeItemCollapsibleState.None, {
          description: task.status,
          tooltip: task.pr_url || task.result || task.error || task.id,
          command: task.pr_url
            ? {
                command: "maxwellConductor.openPrDiff",
                title: "Open PR Diff",
                arguments: [task.pr_url],
              }
            : undefined,
        });
      });
    }

    return [];
  }
}

function parseIssueRef(raw) {
  const value = String(raw || "").trim();
  const urlMatch = value.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);
  if (urlMatch) return { repo: urlMatch[1], number: Number(urlMatch[2]) };
  const short = value.match(/^([A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$/);
  if (short) return { repo: short[1], number: Number(short[2]) };
  return null;
}

async function dispatchIssue(provider) {
  const raw = await vscode.window.showInputBox({
    title: "Dispatch GitHub Issue",
    prompt: "Enter a GitHub issue URL or owner/repo#number.",
    placeHolder: "D-sorganization/Maxwell-Daemon#12",
  });
  if (!raw) return;

  const ref = parseIssueRef(raw);
  if (!ref) {
    vscode.window.showErrorMessage("Could not parse that issue reference.");
    return;
  }

  const mode = String(config().get("defaultMode") || "implement");
  const task = await requestJson("POST", "/api/v1/issues/dispatch", {
    repo: ref.repo,
    number: ref.number,
    mode,
  });
  vscode.window.showInformationMessage(`Dispatched ${ref.repo}#${ref.number} as task ${task.id}.`);
  provider.refresh();
}

async function configureDaemon() {
  const current = daemonUrl();
  const next = await vscode.window.showInputBox({
    title: "Configure Maxwell-Daemon URL",
    value: current,
    prompt: "Set the Maxwell-Daemon API base URL.",
  });
  if (!next) return;
  await config().update("daemonUrl", next, vscode.ConfigurationTarget.Global);
}

function openPrDiff(rawUrl) {
  const value = rawUrl || "";
  const url = value.endsWith("/files") ? value : `${value.replace(/\/+$/, "")}/files`;
  if (!url || !/^https:\/\/github\.com\/.+\/pull\/\d+\/files$/.test(url)) {
    vscode.window.showErrorMessage("Open PR Diff needs a GitHub pull request URL.");
    return;
  }
  vscode.env.openExternal(vscode.Uri.parse(url));
}

function streamLogs() {
  const writeEmitter = new vscode.EventEmitter();
  let timer = null;
  const pty = {
    onDidWrite: writeEmitter.event,
    open: () => {
      writeEmitter.fire(`Maxwell-Daemon task stream from ${daemonUrl()}\r\n`);
      timer = setInterval(async () => {
        try {
          const tasks = await requestJson("GET", "/api/v1/tasks?limit=20");
          const now = new Date().toLocaleTimeString();
          writeEmitter.fire(`\r\n[${now}] ${tasks.length} tasks\r\n`);
          for (const task of tasks) {
            const target = task.issue_repo ? `${task.issue_repo}#${task.issue_number}` : task.prompt;
            writeEmitter.fire(`  ${task.status.padEnd(10)} ${String(target || task.id).slice(0, 90)}\r\n`);
          }
        } catch (error) {
          writeEmitter.fire(`\r\nstream error: ${error.message}\r\n`);
        }
      }, 3000);
    },
    close: () => {
      if (timer) clearInterval(timer);
    },
  };
  vscode.window.createTerminal({ name: "Maxwell Logs", pty }).show();
}

function activate(context) {
  const provider = new AgentTreeProvider();
  vscode.window.registerTreeDataProvider("maxwellConductor.agents", provider);

  context.subscriptions.push(
    vscode.commands.registerCommand("maxwellConductor.refresh", () => provider.refresh()),
    vscode.commands.registerCommand("maxwellConductor.dispatchIssue", () => dispatchIssue(provider)),
    vscode.commands.registerCommand("maxwellConductor.openPrDiff", openPrDiff),
    vscode.commands.registerCommand("maxwellConductor.streamLogs", streamLogs),
    vscode.commands.registerCommand("maxwellConductor.configureDaemon", configureDaemon)
  );
}

function deactivate() {}

module.exports = { activate, deactivate, parseIssueRef };
