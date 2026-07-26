import * as vscode from 'vscode';

export interface HistoryEntry {
  index: number;
  label: string;
}

interface History {
  entries: HistoryEntry[];
  current: number;
}

/**
 * Session timeline: every scene change as a clickable checkpoint. Entry 0 is
 * the initial state; clicking any entry jumps the scene there (undoing or
 * redoing as needed), so the list stays put and you can move both ways.
 */
export class HistoryTreeProvider implements vscode.TreeDataProvider<HistoryEntry> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  private current = 0;

  constructor(private readonly getHistory: () => Promise<History>) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(entry: HistoryEntry): vscode.TreeItem {
    const item = new vscode.TreeItem(entry.label, vscode.TreeItemCollapsibleState.None);
    item.id = `history-${entry.index}`;
    const isCurrent = entry.index === this.current;
    const isFuture = entry.index > this.current;
    item.iconPath = new vscode.ThemeIcon(
      isCurrent ? 'debug-stackframe' : isFuture ? 'circle-outline' : 'circle-filled',
      isFuture ? new vscode.ThemeColor('disabledForeground') : undefined,
    );
    if (isCurrent) {
      item.description = 'current';
    }
    item.tooltip = isCurrent
      ? `${entry.label} — the scene is here`
      : `Jump the scene to: ${entry.label}`;
    item.contextValue = 'magpyHistoryEntry';
    item.command = {
      command: 'magpylib-studio.gotoHistory',
      title: 'Jump to this point',
      arguments: [entry],
    };
    return item;
  }

  async getChildren(element?: HistoryEntry): Promise<HistoryEntry[]> {
    if (element) {
      return [];
    }
    const history = await this.getHistory();
    this.current = history.current;
    return [...history.entries].reverse(); // newest on top, like a chat log
  }
}
