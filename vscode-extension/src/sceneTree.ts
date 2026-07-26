import * as vscode from 'vscode';

export interface SceneObject {
  id: string;
  type: string;
  label: string;
  parent: string | null;
}

// Wireframe SVGs in media/icons, one per magpylib class, colored by
// category (magnets red, currents blue, sensors green, misc gray).
const TYPE_ICON_FILES: Record<string, string> = {
  'magnet.Cuboid': 'cuboid',
  'magnet.Cylinder': 'cylinder',
  'magnet.CylinderSegment': 'cylinder-segment',
  'magnet.Sphere': 'sphere',
  'magnet.Tetrahedron': 'tetrahedron',
  'magnet.TriangularMesh': 'mesh',
  'current.Circle': 'loop',
  'current.Polyline': 'polyline',
  'misc.Dipole': 'dipole',
  'misc.CustomSource': 'custom',
  Sensor: 'sensor',
};

function iconFor(
  type: string,
  extensionUri: vscode.Uri,
): vscode.Uri | vscode.ThemeIcon {
  if (type === 'Collection') {
    return new vscode.ThemeIcon('folder');
  }
  const file =
    TYPE_ICON_FILES[type] ?? (type.startsWith('magnet.') ? 'cuboid' : 'custom');
  return vscode.Uri.joinPath(extensionUri, 'media', 'icons', `${file}.svg`);
}

const TREE_MIME = 'application/vnd.code.tree.magpylib-studio.sceneview';

/**
 * Sidebar scene outline. The engine reports a flat list with `parent` ids
 * (depth-first); the root call fetches and caches it, child calls slice it.
 * Drag & drop reparents: onto a Collection = move in, onto a plain object =
 * move next to it, onto empty space = move to the scene root.
 */
export class SceneTreeProvider
  implements
    vscode.TreeDataProvider<SceneObject>,
    vscode.TreeDragAndDropController<SceneObject>
{
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  readonly dragMimeTypes = [TREE_MIME];
  readonly dropMimeTypes = [TREE_MIME];
  private objects: SceneObject[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly listObjects: () => Promise<SceneObject[]>,
    private readonly moveObject: (id: string, parent: string | null) => Promise<void>,
  ) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(obj: SceneObject): vscode.TreeItem {
    const hasChildren = this.objects.some((o) => o.parent === obj.id);
    const item = new vscode.TreeItem(
      obj.label,
      hasChildren
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.None,
    );
    item.id = obj.id;
    item.description = obj.type;
    item.tooltip = `${obj.id} — ${obj.type}`;
    item.contextValue = obj.type === 'Collection' ? 'magpyCollection' : 'magpyObject';
    item.iconPath = iconFor(obj.type, this.extensionUri);
    item.command = {
      command: 'magpylib-studio.selectObject',
      title: 'Select in Studio',
      arguments: [obj.id],
    };
    return item;
  }

  async getChildren(element?: SceneObject): Promise<SceneObject[]> {
    if (!element) {
      this.objects = await this.listObjects();
      return this.objects.filter((o) => o.parent === null);
    }
    return this.objects.filter((o) => o.parent === element.id);
  }

  handleDrag(source: readonly SceneObject[], dataTransfer: vscode.DataTransfer): void {
    dataTransfer.set(TREE_MIME, new vscode.DataTransferItem([...source]));
  }

  async handleDrop(
    target: SceneObject | undefined,
    dataTransfer: vscode.DataTransfer,
  ): Promise<void> {
    const source = dataTransfer.get(TREE_MIME)?.value as SceneObject[] | undefined;
    if (!source?.length) {
      return;
    }
    const parent =
      target === undefined
        ? null
        : target.type === 'Collection'
          ? target.id
          : target.parent;
    for (const obj of source) {
      if (obj.id !== parent && (obj.parent ?? null) !== parent) {
        await this.moveObject(obj.id, parent); // engine rejects cycles cleanly
      }
    }
  }
}
