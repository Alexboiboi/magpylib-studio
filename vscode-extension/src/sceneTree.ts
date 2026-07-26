import * as vscode from 'vscode';

export interface SceneObject {
  id: string;
  type: string;
  label: string;
}

// One codicon per magpylib class, colored by category
// (magnets red, currents blue, sensors green).
const TYPE_ICONS: Record<string, string> = {
  'magnet.Cuboid': 'primitive-square',
  'magnet.Cylinder': 'database',
  'magnet.CylinderSegment': 'pie-chart',
  'magnet.Sphere': 'circle-large-filled',
  'magnet.Tetrahedron': 'triangle-up',
  'magnet.TriangularMesh': 'type-hierarchy-sub',
  'current.Circle': 'circle-large',
  'current.Polyline': 'pulse',
  'misc.Dipole': 'compass',
  'misc.CustomSource': 'tools',
  Sensor: 'circuit-board',
  Collection: 'folder',
};

function iconFor(type: string): vscode.ThemeIcon {
  const category = type.startsWith('magnet.')
    ? new vscode.ThemeColor('charts.red')
    : type.startsWith('current.')
      ? new vscode.ThemeColor('charts.blue')
      : type === 'Sensor'
        ? new vscode.ThemeColor('charts.green')
        : undefined;
  const icon =
    TYPE_ICONS[type] ?? (type.startsWith('magnet.') ? 'magnet' : 'symbol-object');
  return new vscode.ThemeIcon(icon, category);
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
