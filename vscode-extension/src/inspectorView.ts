import * as vscode from 'vscode';

import { mediaUri, nonce as webviewNonce } from './webview';

const MUTATING = new Set([
  'apply_edit',
  'reset_style',
  'move',
  'rotate',
  'set_transform',
  'clear_path',
  'set_param',
  'edit_event',
]);

/**
 * Sidebar style inspector: schema-driven widgets for the selected object.
 * Widgets are generated in the webview from `get_schema` (enum -> dropdown,
 * format:color -> picker, bounded number -> slider, boolean -> tri-state) and
 * filled from `get_values` (resolved values shown; explicitly set paths get a
 * reset button). All edits go through the shared engine RPC router.
 */
export class InspectorViewProvider implements vscode.WebviewViewProvider {
  static readonly viewId = 'magpylib-studio.inspectorView';

  private view: vscode.WebviewView | undefined;
  private ready = false;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly request: (method: string, params?: Record<string, unknown>) => Promise<unknown>,
    private readonly onMutation: () => void,
    private readonly getSelection: () => string | undefined,
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
        this.select(this.getSelection());
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

  select(objectId: string | undefined): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'select', objectId });
    }
  }

  /** Show one construction step's own values above the object it acted on. */
  showOperation(eventId: string | undefined): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'operation', eventId });
    }
  }

  /** External change (chat tool, tree action): re-pull values. */
  refresh(): void {
    if (this.view && this.ready) {
      this.view.webview.postMessage({ type: 'refresh' });
    }
  }

  private html(webview: vscode.Webview): string {
    const nonce = webviewNonce();
    const styleUri = mediaUri(webview, this.extensionUri, 'inspector.css');
    const scriptUri = mediaUri(webview, this.extensionUri, 'inspector.js');
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';" />
  <link rel="stylesheet" href="${styleUri}" />
</head>
<body>
  <div id="header"></div>
  <div id="step"></div>
  <div id="params"></div>
  <div id="transform"></div>
  <!-- the filter sits with what it filters: the style list, which is the
       only long one and the only one it touches -->
  <input id="filter" type="text" placeholder="Filter style properties…" hidden />
  <div id="props"></div>
  <div id="empty">Select an object in the Scene view.</div>
  <div id="status"></div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
