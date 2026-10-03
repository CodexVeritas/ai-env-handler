from __future__ import annotations

import sys

SERVER_COMMANDS = {"serve", "init", "install", "uninstall"}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in SERVER_COMMANDS:
        command, rest = arguments[0], arguments[1:]
        if command == "serve":
            from envh.server.serve import main as serve_main

            return serve_main(rest)
        if command == "init":
            from envh.server.init_cmd import main as init_main

            return init_main(rest)
        from envh.install.command import main as install_main

        return install_main(command, rest)
    from envh.client.commands import main as client_main

    return client_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
