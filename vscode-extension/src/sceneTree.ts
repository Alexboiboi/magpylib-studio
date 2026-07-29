import * as vscode from 'vscode';

export interface SceneObject {
  id: string;
  type: string;
  label: string;
  parent: string | null;
  visible: boolean;
  /** Set on a copy made by a duplicate event, naming the object it came
   *  from. Generated objects have no document entry to edit, so the tree
   *  shows them and stops there. */
  derived?: string;
}

/** One step of the construction, shown under the object it happened to. */
export interface SceneOperation {
  kind: 'operation';
  index: number;
  id: string;
  target: string;
  op: string;
  /** What it did, in words: "orbit 36° about z". */
  label: string;
  /** The call that did it, for the tooltip. */
  source: string;
  /** Recorded, but after the point the history is rolled back to. */
  pending?: boolean;
  /** The last rebuild could not apply it. */
  error?: string;
}

export type SceneNode = SceneObject | SceneOperation;

export function isOperation(node: SceneNode): node is SceneOperation {
  return (node as SceneOperation).kind === 'operation';
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

/** A glyph per kind of step, so the shape of a history reads at a glance. */
const OPERATION_ICONS: Record<string, string> = {
  create: 'add',
  remove: 'trash',
  reparent: 'type-hierarchy',
  move: 'move',
  position: 'pin',
  orientation: 'compass',
  rotate_from_angax: 'sync',
  rotate_from_rotvec: 'sync',
  duplicate_around: 'circuit-board',
};

/**
 * Sidebar scene outline. The engine reports a flat list with `parent` ids
 * (depth-first); the root call fetches and caches it, child calls slice it.
 * Drag & drop reparents: onto a Collection = move in, onto a plain object =
 * move next to it, onto empty space = move to the scene root.
 */
export class SceneTreeProvider
  implements
    vscode.TreeDataProvider<SceneNode>,
    vscode.TreeDragAndDropController<SceneNode>
{
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;
  readonly dragMimeTypes = [TREE_MIME];
  readonly dropMimeTypes = [TREE_MIME];
  private objects: SceneObject[] = [];
  private operations: SceneOperation[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly listObjects: () => Promise<SceneObject[]>,
    private readonly moveObject: (id: string, parent: string | null) => Promise<void>,
    private readonly listOperations: () => Promise<SceneOperation[]>,
  ) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(node: SceneNode): vscode.TreeItem {
    return isOperation(node) ? this.operationItem(node) : this.objectItem(node);
  }

  /**
   * One step of the construction. Named for what it did rather than for the
   * call that did it - the call is in the tooltip, and in the script tab.
   */
  private operationItem(operation: SceneOperation): vscode.TreeItem {
    const item = new vscode.TreeItem(
      operation.label,
      vscode.TreeItemCollapsibleState.None,
    );
    item.id = `op-${operation.id}`;
    item.tooltip = new vscode.MarkdownString(
      '`' + operation.source + '`' +
        (operation.error ? `\n\n$(error) ${operation.error}` : '') +
        (operation.pending ? '\n\nAfter the step the history is rolled back to.' : '') +
        '\n\n$(edit) Edit Step… shows its values in the Inspector.',
    );
    item.tooltip.supportThemeIcons = true;
    item.contextValue = 'magpyOperation' + (operation.op === 'create' ? 'Create' : '');
    item.iconPath = operation.error
      ? new vscode.ThemeIcon('error', new vscode.ThemeColor('errorForeground'))
      : new vscode.ThemeIcon(
          OPERATION_ICONS[operation.op] ?? 'circle-small-filled',
          operation.pending
            ? new vscode.ThemeColor('disabledForeground')
            : undefined,
        );
    if (operation.pending) {
      item.description = 'not applied';
    }
    item.command = {
      command: 'magpylib-studio.selectOperation',
      title: 'Show this step',
      arguments: [operation],
    };
    return item;
  }

  private objectItem(obj: SceneObject): vscode.TreeItem {
    const hasChildren = this.objects.some((o) => o.parent === obj.id);
    const item = new vscode.TreeItem(
      obj.label,
      hasChildren
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.None,
    );
    item.id = obj.id;
    if (obj.derived) {
      // A generated copy: real geometry, real field source, but no spec —
      // every edit command would fail on it, so it gets no context value
      // (nothing in the menus matches), no selection, and a dimmed icon.
      item.description = `${obj.type} · copy of ${obj.derived}`;
      item.tooltip =
        `${obj.id} — generated by the duplicate event on ${obj.derived}. ` +
        `Edit the event or its variables, not the copy.`;
      // deliberately outside the magpy* namespace: every scene-view menu
      // entry is gated on /^magpy/, so this matches none of them
      item.contextValue = 'derivedCopy';
      item.iconPath = new vscode.ThemeIcon(
        'circle-small-filled',
        new vscode.ThemeColor('disabledForeground'),
      );
      return item;
    }
    item.description = obj.visible ? obj.type : `${obj.type} · hidden`;
    item.tooltip = `${obj.id} — ${obj.type}${obj.visible ? '' : ' (hidden)'}`;
    // visibility is part of contextValue so the inline eye can flip its icon
    item.contextValue =
      (obj.type === 'Collection' ? 'magpyCollection' : 'magpyObject') +
      (obj.visible ? 'Visible' : 'Hidden');
    item.iconPath = obj.visible
      ? iconFor(obj.type, this.extensionUri)
      : new vscode.ThemeIcon('eye-closed', new vscode.ThemeColor('disabledForeground'));
    item.command = {
      command: 'magpylib-studio.selectObject',
      title: 'Select in Studio',
      arguments: [obj.id],
    };
    return item;
  }

  async getChildren(element?: SceneNode): Promise<SceneNode[]> {
    if (!element) {
      [this.objects, this.operations] = await Promise.all([
        this.listObjects(),
        this.listOperations(),
      ]);
      return this.objects.filter((o) => o.parent === null);
    }
    if (isOperation(element)) {
      return [];
    }
    // An object's own steps first, then whatever it contains: how this came
    // to be, before what is inside it.
    return [
      ...this.operations.filter((op) => op.target === element.id),
      ...this.objects.filter((o) => o.parent === element.id),
    ];
  }

  handleDrag(source: readonly SceneNode[], dataTransfer: vscode.DataTransfer): void {
    // Generated copies cannot be reparented: they are not in the document.
    // Steps are not dragged either - they are reordered from their own menu,
    // where "earlier"/"later" says what moving one actually means.
    const movable = source.filter(
      (o): o is SceneObject => !isOperation(o) && !o.derived,
    );
    if (movable.length) {
      dataTransfer.set(TREE_MIME, new vscode.DataTransferItem(movable));
    }
  }

  async handleDrop(
    target: SceneNode | undefined,
    dataTransfer: vscode.DataTransfer,
  ): Promise<void> {
    const source = dataTransfer.get(TREE_MIME)?.value as SceneObject[] | undefined;
    if (!source?.length || (target && isOperation(target))) {
      return;
    }
    const parent =
      target === undefined
        ? null
        : target.type === 'Collection' && !target.derived
          ? target.id
          : target.parent;
    for (const obj of source) {
      if (obj.id !== parent && (obj.parent ?? null) !== parent) {
        await this.moveObject(obj.id, parent); // engine rejects cycles cleanly
      }
    }
  }
}
