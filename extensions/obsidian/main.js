const { Plugin, Notice } = require('obsidian');

module.exports = class MaxwellPlugin extends Plugin {
    async onload() {
        this.addCommand({
            id: 'maxwell-submit-task',
            name: 'Ask Maxwell about this note',
            callback: () => {
                new Notice('Maxwell Task Submitted');
            }
        });
        console.log("Vault-as-memory mode initialized.");
    }
    
    onunload() {
    }
}
