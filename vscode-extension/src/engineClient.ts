import { ChildProcessWithoutNullStreams, spawn } from 'child_process';

export interface RpcError {
  type: string;
  message: string;
}

interface RpcResponse {
  id: number | null;
  result?: unknown;
  error?: RpcError;
}

export class EngineError extends Error {
  constructor(public readonly rpcError: RpcError) {
    super(`${rpcError.type}: ${rpcError.message}`);
  }
}

/**
 * Promise-based client for the magpylib-studio engine: spawns
 * `python -m magpylib_studio` and speaks newline-delimited JSON-RPC on stdio.
 * Owns the request-id space, so callers never juggle ids themselves.
 */
export class EngineClient {
  private proc: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void }>();
  private stdoutRemainder = '';
  private exited = false;

  onStderr: ((text: string) => void) | undefined;
  onExit: ((code: number | null) => void) | undefined;

  constructor(pythonPath: string, cwd: string) {
    this.proc = spawn(pythonPath, ['-m', 'magpylib_studio'], { cwd });

    this.proc.stdout.on('data', (chunk: Buffer) => {
      this.stdoutRemainder += chunk.toString();
      const lines = this.stdoutRemainder.split('\n');
      this.stdoutRemainder = lines.pop() ?? '';
      for (const line of lines) {
        if (line.trim()) {
          this.handleLine(line);
        }
      }
    });

    this.proc.stderr.on('data', (chunk: Buffer) => {
      this.onStderr?.(chunk.toString());
    });

    this.proc.on('error', (err) => {
      this.failAll(new Error(`engine failed to start: ${err.message}`));
    });

    this.proc.on('exit', (code) => {
      this.exited = true;
      this.failAll(new Error(`engine exited with code ${code}`));
      this.onExit?.(code);
    });
  }

  get isRunning(): boolean {
    return !this.exited;
  }

  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (this.exited) {
      return Promise.reject(new Error('engine is not running'));
    }
    const id = this.nextId++;
    const promise = new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
    });
    this.proc.stdin.write(JSON.stringify({ id, method, params: params ?? {} }) + '\n');
    return promise;
  }

  dispose(): void {
    if (!this.exited) {
      this.proc.kill();
    }
  }

  private handleLine(line: string): void {
    let response: RpcResponse;
    try {
      response = JSON.parse(line);
    } catch {
      this.onStderr?.(`invalid JSON from engine: ${line}\n`);
      return;
    }
    if (response.id === null || response.id === undefined) {
      // Engine-level parse error with no id to route to; surface it.
      this.onStderr?.(`engine error: ${response.error?.message ?? line}\n`);
      return;
    }
    const entry = this.pending.get(response.id);
    if (!entry) {
      return;
    }
    this.pending.delete(response.id);
    if (response.error) {
      entry.reject(new EngineError(response.error));
    } else {
      entry.resolve(response.result);
    }
  }

  private failAll(err: Error): void {
    for (const { reject } of this.pending.values()) {
      reject(err);
    }
    this.pending.clear();
  }
}
