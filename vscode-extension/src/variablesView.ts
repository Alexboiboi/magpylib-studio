import * as vscode from 'vscode';

import { mediaUri, nonce as webviewNonce } from './webview';

/** Calls from this panel that change the scene, not just what it displays. */
const MUTATING = new Set(['set_variable', 'set_variable_bounds', 'remove_variable']);

export interface VariableBounds {
  /** Hard limits: the engine rejects a value outside them. */
  min?: number;
  max?: number;
  /** Soft limits: the range worth dragging through; outside stays legal. */
  soft_min?: number;
  soft_max?: number;
  /** This variable counts things, so only whole values are legal. */
  integer?: boolean;
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
 * The scene's variables, as a webview rather than a tree.
 *
 * A TreeItem can hold a label and an icon and nothing else, and the point of
 * bounding a variable is to be able to *drag* it — so the panel that shows the
 * variables has to be one that can hold a slider. The cost is that per-row
 * actions are buttons in the row instead of a context menu; the view's title
 * bar still carries New Variable… and Sweep…
 */
export class VariablesViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'magpylib-studio.variablesView';

  private view: vscode.WebviewView | undefined;
  private ready = false;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly request: (
      method: string,
      params?: Record<string, unknown>,
    ) => Promise<unknown>,
    private readonly onAction: (action: string, name: string) => void,
    private readonly onMutation: () => void,
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.view = webviewView;
    this.ready = false;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };
    webviewView.webview.html = this.html(webviewView.webview);

    webviewView.webview.onDidReceiveMessage(async (message) => {
      if (message.type === 'ready') {
        this.ready = true;
        this.refresh();
        webviewView.webview.postMessage({ type: 'help' });
        return;
      }
      if (message.type === 'action') {
        this.onAction(message.action, message.name);
        return;
      }
      if (message.type !== 'rpcRequest') {
        return;
      }
      const { reqId, method, params } = message;
      try {
        const result = await this.request(method, params);
        webviewView.webview.postMessage({ type: 'rpcResult', reqId, method, result });
        if (MUTATING.has(method)) {
          // Dragging a variable moves everything written in terms of it —
          // which is the whole point, and none of it is on this panel.
          this.onMutation();
        }
      } catch (err) {
        webviewView.webview.postMessage({
          type: 'rpcError',
          reqId,
          method,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    });

    webviewView.onDidDispose(() => {
      this.view = undefined;
      this.ready = false;
    });
  }

  refresh(): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'refresh' });
    }
  }

  private html(webview: vscode.Webview): string {
    const nonce = webviewNonce();
    const styleUri = mediaUri(webview, this.extensionUri, 'variables.css');
    const scriptUri = mediaUri(webview, this.extensionUri, 'variables.js');
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';" />
  <link rel="stylesheet" href="${styleUri}" />
</head>
<body>
  <div id="list"></div>
  <div id="empty" hidden>
    No variables yet. A named number any position, dimension or angle can be
    written in terms of — change it once and the scene follows.
  </div>
  <details id="help">
    <summary>what can go in a value</summary>
    <div id="helpBody"></div>
  </details>
  <div id="status"></div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
