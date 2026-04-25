import * as vscode from 'vscode';

export function activate(context: vscode.ExtensionContext) {
    const commands = [
        vscode.commands.registerCommand('maxwell.submitTask', () => {
            vscode.window.showInformationMessage('Maxwell: Task Submitted');
        }),
        vscode.commands.registerCommand('maxwell.askAboutSelection', () => {
            vscode.window.showInformationMessage('Maxwell: Asking About Selection');
        }),
        vscode.commands.registerCommand('maxwell.fixThisFile', () => {
            vscode.window.showInformationMessage('Maxwell: Fixing This File');
        }),
        vscode.commands.registerCommand('maxwell.generateTests', () => {
            vscode.window.showInformationMessage('Maxwell: Generating Tests');
        }),
        vscode.commands.registerCommand('maxwell.reviewDiff', () => {
            vscode.window.showInformationMessage('Maxwell: Reviewing Diff');
        }),
        vscode.commands.registerCommand('maxwell.showCost', () => {
            vscode.window.showInformationMessage('Maxwell: Showing Cost');
        })
    ];

    context.subscriptions.push(...commands);
}

export function deactivate() {}
