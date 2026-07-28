import * as vscode from 'vscode';

export interface Variable {
  name: string;
  /** As written: a number, or an "=expr" over the other variables. */
  expression: number | string;
  /** As resolved at the last build. */
  value: number | null;
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
    item.tooltip = isExpression
      ? `${variable.name} = ${String(variable.expression).slice(1)}, currently ${resolved}`
      : `${variable.name} = ${resolved}`;
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

/** Short but honest: no exponent soup for ordinary values, no lost precision. */
function formatNumber(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  const rounded = Number(value.toPrecision(6));
  return String(rounded);
}
