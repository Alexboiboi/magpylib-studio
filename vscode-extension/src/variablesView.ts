import * as vscode from 'vscode';

export interface VariableBounds {
  /** Hard limits: the engine rejects a value outside them. */
  min?: number;
  max?: number;
  /** Soft limits: the range worth dragging through; outside stays legal. */
  soft_min?: number;
  soft_max?: number;
}

export interface Variable {
  name: string;
  /** As written: a number, or an "=expr" over the other variables. */
  expression: number | string;
  /** As resolved at the last build. */
  value: number | null;
  bounds?: VariableBounds;
}

/** The range a slider should span: soft limits if given, else the hard ones. */
export function sliderRange(bounds?: VariableBounds): [number, number] | undefined {
  if (!bounds) {
    return undefined;
  }
  const low = bounds.soft_min ?? bounds.min;
  const high = bounds.soft_max ?? bounds.max;
  return low === undefined || high === undefined || low >= high
    ? undefined
    : [low, high];
}

/**
 * The document's variables, one row each. Clicking a row edits it — which is
 * the whole point of a variable: change one number and everything defined in
 * terms of it follows on the next rebuild. Expressions show what they were
 * written as *and* what they came out to, since that is exactly what you want
 * to check when a scene lands somewhere unexpected.
 */
export class VariablesTreeProvider implements vscode.TreeDataProvider<Variable> {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(private readonly getVariables: () => Promise<{ variables: Variable[] }>) {}

  refresh(): void {
    this.emitter.fire();
  }

  getTreeItem(variable: Variable): vscode.TreeItem {
    const item = new vscode.TreeItem(variable.name, vscode.TreeItemCollapsibleState.None);
    item.id = `variable-${variable.name}`;
    const isExpression = typeof variable.expression === 'string';
    const resolved = variable.value === null ? '?' : formatNumber(variable.value);
    item.description = isExpression
      ? `${String(variable.expression).slice(1)} = ${resolved}`
      : resolved;
    const b = variable.bounds;
    item.tooltip = isExpression
      ? `${variable.name} = ${String(variable.expression).slice(1)}, currently ${resolved}`
      : `${variable.name} = ${resolved}`;
    if (b) {
      const hard = describeRange(b.min, b.max);
      const soft = describeRange(b.soft_min, b.soft_max);
      item.description += `   ${hard || soft}`;
      item.tooltip +=
        (hard ? `\nallowed ${hard}` : '') + (soft ? `\nslider ${soft}` : '');
    }
    item.iconPath = new vscode.ThemeIcon(isExpression ? 'symbol-operator' : 'symbol-number');
    item.contextValue = 'magpyVariable';
    item.command = {
      command: 'magpylib-studio.editVariable',
      title: 'Edit Variable',
      arguments: [variable],
    };
    return item;
  }

  async getChildren(element?: Variable): Promise<Variable[]> {
    return element ? [] : (await this.getVariables()).variables;
  }
}

/** "0..10", "≥ 0", "≤ 10", or nothing when neither end is set. */
function describeRange(low?: number, high?: number): string {
  if (low !== undefined && high !== undefined) {
    return `${formatNumber(low)}..${formatNumber(high)}`;
  }
  if (low !== undefined) {
    return `≥ ${formatNumber(low)}`;
  }
  return high === undefined ? '' : `≤ ${formatNumber(high)}`;
}

/** Short but honest: no exponent soup for ordinary values, no lost precision. */
function formatNumber(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  const rounded = Number(value.toPrecision(6));
  return String(rounded);
}
