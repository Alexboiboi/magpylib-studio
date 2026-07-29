import * as vscode from 'vscode';

/**
 * The two things every webview here needs to load its code from `media/`.
 *
 * The scripts live in real files rather than inside template literals so the
 * compiler, the linter and the editor all read them. They used not to, and
 * three separate defects hid in that blind spot: an escape that TypeScript
 * resolved into a line break — a syntax error the panel could not report,
 * since a script that does not parse cannot tell you so — and two message
 * types one end sent and the other never handled.
 */

/** One-use token that lets exactly the tags we emit run under the CSP. */
export function nonce(): string {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from({ length: 32 }, () =>
    alphabet.charAt(Math.floor(Math.random() * alphabet.length)),
  ).join('');
}

/** A file under `media/`, as the opaque URI a webview may actually load. */
export function mediaUri(
  webview: vscode.Webview,
  extensionUri: vscode.Uri,
  ...file: string[]
): vscode.Uri {
  return webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, 'media', ...file));
}
