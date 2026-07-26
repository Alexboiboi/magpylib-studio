"""Entry point: `python -m magpylib_studio` starts the JSON-RPC stdio server."""

from magpylib_studio.rpc import serve

if __name__ == "__main__":
    serve()
