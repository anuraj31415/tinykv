import sys

from .store import Store

USAGE = "usage: python -m tinykv FILE (get KEY | set KEY VALUE | delete KEY | list)"


def main(argv):
    if len(argv) < 2:
        print(USAGE)
        return 2
    with Store(argv[0]) as s:
        cmd, args = argv[1], argv[2:]
        if cmd == "get" and len(args) == 1:
            v = s.get(args[0])
            if v is None:
                return 1
            print(v if isinstance(v, str) else v.decode("utf-8", "replace"))
        elif cmd == "set" and len(args) == 2:
            s.set(args[0], args[1])
        elif cmd == "delete" and len(args) == 1:
            return 0 if s.delete(args[0]) else 1
        elif cmd == "list" and not args:
            for k in s:
                print(k)
        else:
            print(USAGE)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
