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
  /** Set on a row the tree makes up for an object the built scene does not
   *  have, and why it does not: the log still records everything that
   *  happened to it, and steps you cannot see are steps you cannot undo. */
  absent?: AbsentReason;
}

/** Why an object with a history is missing from the scene: it was deleted,
 *  the history is rolled back to before it existed, or the log no longer
 *  builds it (its `create` was dropped or reordered away). */
export type AbsentReason = 'removed' | 'not applied' | 'not built';

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

/** The glyph for a magpylib class, shared with the "Add Object…" menu so a
 *  cylinder is picked by the same picture the tree will show it as. */
export function iconFor(
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

/** What a headstone row says it is, in the terms that make it fixable. */
const ABSENT_TOOLTIP: Record<AbsentReason, (id: string) => string> = {
  removed: (id) =>
    `${id} was deleted. Deleting is recorded, not erased, so its steps are ` +
    `still here — drop the "removed" step to have it back.`,
  'not applied': (id) =>
    `${id} is created after the step the history is rolled back to, so the ` +
    `scene does not have it yet. Clear the rollback to build it.`,
  'not built': (id) =>
    `${id} is never created by the log as it now stands — its "created" ` +
    `step was dropped or moved after the steps that need it. Undo, or move ` +
    `a "created" step back to the front.`,
};

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
  duplicate_along: 'symbol-array',
  mirror: 'split-horizontal',
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
  private absent: SceneObject[] = [];

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
    // The `Create` suffix earns a row of its own: on the step that brought an
    // object into being, "remove" has two readings — drop the step, so it was
    // never created, or delete the object, which is a step in its own right —
    // and only that row can offer both.
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
    // Both halves of what getChildren returns: an object with no children of
    // its own still has the steps that built it, and a row with no chevron is
    // a row those steps cannot be reached from. Only what contains objects
    // opens on its own — steps wait to be asked for, so a ring of twelve does
    // not arrive as twenty-four rows.
    const contains = this.objects.some((o) => o.parent === obj.id);
    const steps = this.operations.some((op) => op.target === obj.id);
    const item = new vscode.TreeItem(
      obj.label,
      contains
        ? vscode.TreeItemCollapsibleState.Expanded
        : steps
          ? vscode.TreeItemCollapsibleState.Collapsed
          : vscode.TreeItemCollapsibleState.None,
    );
    item.id = obj.id;
    if (obj.absent) {
      // A headstone: the object is not in the scene, but its steps are still
      // in the log, and every one of them can be selected, edited, reordered
      // or dropped from here. Deleting an object is recorded rather than
      // erased, so without this row the step that deleted it — and the whole
      // story before it — could be neither seen nor undone.
      item.description = obj.absent;
      item.tooltip = ABSENT_TOOLTIP[obj.absent](obj.id);
      // outside the magpy* namespace, like a generated copy: the object
      // commands act on something the scene has, and this row is the
      // absence of one. Its steps carry their own menus.
      item.contextValue = 'absentObject';
      item.iconPath = new vscode.ThemeIcon(
        obj.absent === 'removed' ? 'circle-slash' : 'circle-outline',
        new vscode.ThemeColor('disabledForeground'),
      );
      return item;
    }
    if (obj.derived) {
      // A generated copy: real geometry, real field source, but no spec, so
      // no edit command can act on it — hence no context value (every menu
      // entry is gated on /^magpy/) and a dimmed icon. It *is* selectable
      // though: a row you can see and click that does nothing at all is
      // worse than one that opens read-only and says where to edit instead.
      item.description = `${obj.type} · copy of ${obj.derived}`;
      item.tooltip =
        `${obj.id} — generated by the duplicate event on ${obj.derived}. ` +
        `Edit the event or its variables, not the copy.`;
      item.command = {
        command: 'magpylib-studio.selectObject',
        title: 'Select in Studio',
        arguments: [obj.id],
      };
      // deliberately outside the magpy* namespace: every scene-view menu
      // entry is gated on /^magpy/, so this matches none of them
      item.contextValue = 'derivedCopy';
      // Same glyph as anything else of its type. A copy of a cuboid is a
      // cuboid, and a ring of twelve reading as twelve anonymous dots hides
      // the one thing the tree is for — what the scene is made of. That it
      // is a copy is said in words, in the row it is said about.
      item.iconPath = iconFor(obj.type, this.extensionUri);
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

  /**
   * Rows for the objects the log has a history for and the scene does not:
   * deleted, rolled back past, or no longer built. In log order, after the
   * scene proper — they are what the scene used to have, or does not have
   * yet, and either way not part of reading what is in front of you.
   */
  private absentObjects(): SceneObject[] {
    const live = new Set(this.objects.map((o) => o.id));
    const steps = new Map<string, SceneOperation[]>();
    for (const op of this.operations) {
      if (!live.has(op.target)) {
        const own = steps.get(op.target) ?? [];
        own.push(op);
        steps.set(op.target, own); // insertion order is log order
      }
    }
    return [...steps].map(([id, own]) => ({
      id,
      type: '',
      label: id,
      parent: null,
      visible: false,
      // A `remove` that has itself been applied is the one that says why.
      // Everything else is a scene that has not been built that far: either
      // on purpose (rolled back) or not (the create no longer applies).
      absent: own.some((op) => op.op === 'remove' && !op.pending)
        ? ('removed' as const)
        : own.every((op) => op.pending)
          ? ('not applied' as const)
          : ('not built' as const),
    }));
  }

  async getChildren(element?: SceneNode): Promise<SceneNode[]> {
    if (!element) {
      [this.objects, this.operations] = await Promise.all([
        this.listObjects(),
        this.listOperations(),
      ]);
      this.absent = this.absentObjects();
      return [...this.objects.filter((o) => o.parent === null), ...this.absent];
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
    // Nor can headstones: there is nothing in the scene to move. Steps are
    // not dragged either - they are reordered from their own menu, where
    // "earlier"/"later" says what moving one actually means.
    const movable = source.filter(
      (o): o is SceneObject => !isOperation(o) && !o.derived && !o.absent,
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
    // Dropping *onto* a headstone would read as "put it where that used to
    // be", which is not a place: the scene has no such parent to join.
    if (!source?.length || (target && (isOperation(target) || target.absent))) {
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
