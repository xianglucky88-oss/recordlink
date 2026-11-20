"""命令行入口：默认运行离线流水线；支持 undo / explain / review 子命令。"""
import json
import sys

from . import explain as _explain
from . import pipeline, rollback, store


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(json.dumps(pipeline.run(), ensure_ascii=False))
        return 0
    cmd, rest = args[0], args[1:]
    con = store.connect()
    try:
        if cmd == "undo" and rest:
            print(json.dumps(rollback.undo(con, rest[0]), ensure_ascii=False))
        elif cmd == "explain" and rest:
            print(json.dumps(_explain.explain_entity(con, rest[0]),
                             ensure_ascii=False, default=str))
        elif cmd == "review":
            print(json.dumps(store.open_pendings(con), ensure_ascii=False))
        else:
            print("用法: python -m recordlink.cli [undo OP_ID | explain ENTITY_ID | review]",
                  file=sys.stderr)
            return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
