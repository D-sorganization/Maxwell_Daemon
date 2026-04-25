import * as vscode from 'vscode';

export function activate(context: vscode.ExtensionContext) {
    let disposable = vscode.commands.registerCommand('maxwell.submitTask', () => {
        vscode.window.showInformationMessage('Maxwell Task Submitted');
    });

    context.subscriptions.push(disposable);
}

export function deactivate() {}
