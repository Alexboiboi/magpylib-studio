import * as vscode from 'vscode';

export interface SceneObject {
  id: string;
  type: string;
  label: string;
}

function iconFor(type: string): vscode.ThemeIcon {
  if (type.startsWith('magnet.')) {
    return new vscode.ThemeIcon('magnet');
  }
  if (type.startsWith('current.')) {
    return new vscode.ThemeIcon('issue-reopened');
  }
  if (type === 'Sensor') {
    return new vscode.ThemeIcon('radio-tower');
  }
  return new vscode.ThemeIcon('symbol-object');
}

/**
 * Sidebar scene outline. Flat for now (the engine's document is a single
 * collection); getChildren is already shaped for nested collections later.
 */
export class SceneTreeProvider implements vscode.TreeDataProvider<SceneObject> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(private readonly listObjects: () => Promise<SceneObject[]>) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(obj: SceneObject): vscode.TreeItem {
    const item = new vscode.TreeItem(obj.label, vscode.TreeItemCollapsibleState.None);
    item.id = obj.id;
    item.description = obj.type;
    item.tooltip = `${obj.id} — ${obj.type}`;
    item.contextValue = 'magpyObject';
    item.iconPath = iconFor(obj.type);
    item.command = {
      command: 'magpylib-studio.selectObject',
      title: 'Select in Studio',
      arguments: [obj.id],
    };
    return item;
  }

  getChildren(element?: SceneObject): Promise<SceneObject[]> | SceneObject[] {
    if (element) {
      return []; // flat scene for now; nested collections will hang off here
    }
    return this.listObjects();
  }
}
