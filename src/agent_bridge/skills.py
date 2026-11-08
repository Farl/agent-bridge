"""skill 的內容只有一份，其餘都是指向它的連結。抄自 ctd 的 agentskills（#130）。

內容住在 repo 的 `.claude/skills/`——那同時是 Claude Code 的專案 skill 路徑。
`agent-bridge skills link` 把它連到 Claude Code 掃的 `~/.claude/skills` 與 Codex
掃的 `~/.agents/skills`，於是兩邊在任何專案裡都看得到同一份。

**連結不進版控**：commit 進 git 的 symlink 在 Windows checkout 上會壞（ctd 實測過），
所以連結在安裝時才建。
"""

from __future__ import annotations

import os
from pathlib import Path

SOURCE_REL = Path(".claude") / "skills"


def source_dir(repo: Path) -> Path:
    return Path(repo) / SOURCE_REL


def names(repo: Path) -> list[str]:
    """這個 repo 有哪些 skill。**名單就是目錄本身**，不在別處抄一份。

    要有 `SKILL.md` 才算——半個目錄（例如 rename 做到一半）不該被連出去，
    連出去之後 agent 只會拿到一個掃不到東西的路徑。
    """
    src = source_dir(repo)
    if not src.is_dir():
        return []
    return sorted(d.name for d in src.iterdir() if (d / "SKILL.md").is_file())


def roots(repo: Path, home: Path | None = None,
          main: Path | None = None) -> tuple[tuple[Path, Path], ...]:
    """(連結放哪, 內容來自哪一棵樹)。

    Claude Code 掃 `~/.claude/skills`，Codex 掃 `~/.agents/skills`；第三個是
    repo 自己的 `.agents/skills`，給「在這個 repo 裡開的 Codex」——它看不到
    `.claude/skills/`，而那是本專案最常見的 worker 情境之一。

    **家目錄那兩個一律指主工作目錄，不是 worktree。** 一條分支一個 worktree，
    而 worktree 在分支合併後就消失；從 worktree 跑一次就會讓全域連結指向一個
    即將不存在的路徑。這個坑 `main_repo_root` 的註解已經記過一次（launchd 的
    job 差點被指到 worktree），這裡是第二個會踩到它的地方。

    repo 內的那條相反：它就該指這棵樹自己的內容，所以在 worktree 裡改 skill
    時，同一個 worktree 裡的 Codex 讀得到改過的版本。
    """
    home = Path(home) if home is not None else Path.home()
    main = Path(main) if main is not None else Path(repo)
    return ((home / ".claude" / "skills", main),
            (home / ".agents" / "skills", main),
            (Path(repo) / ".agents" / "skills", Path(repo)))


def _target(skill: Path, root: Path, tree: Path) -> str:
    """連結要寫進去的字串。住在那棵樹裡面的用相對路徑，其餘用絕對路徑。

    相對的那條在 repo 搬家之後仍然成立，而且指的是自己這一棵樹。
    """
    return (os.path.relpath(skill, root)
            if root.is_relative_to(tree) else str(skill))


def plan(repo: Path, home: Path | None = None,
         main: Path | None = None) -> list[tuple[Path, str]]:
    """每一條連結該在哪、該指向什麼。link 與 dangling 都從這一份推導。"""
    repo = Path(repo).resolve()
    main = Path(main).resolve() if main is not None else repo
    return [(root / name, _target(source_dir(tree) / name, root, tree))
            for root, tree in roots(repo, home, main)
            for name in names(tree)]


def dangling(repo: Path, home: Path | None = None,
             main: Path | None = None) -> list[Path]:
    """**連過、現在解不開**的那些連結。

    只算「已經存在的 symlink」：沒連過的機器不該被唸（原則五），而連過卻壞掉
    的機器需要被告知——升級時 `skills/` 從 repo 裡消失，舊連結會全部懸空而
    merge 一個字都不會說。
    """
    return [path for path, _ in plan(repo, home, main)
            if path.is_symlink() and not (path / "SKILL.md").is_file()]


def link(repo: Path, home: Path | None = None,
         main: Path | None = None) -> list[str]:
    """建立／修復所有連結。冪等；回傳它**動過**的事，沒動就是空的。

    `dest` 是真的目錄（不是連結）時**跳過並說一句**，不刪。那可能是使用者自己
    放的東西，而刪掉它是不可逆的——不可逆的動作不會在這種順手的地方做。
    """
    done: list[str] = []
    for dest, target in plan(repo, home, main):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            if os.readlink(dest) == target:
                continue
            # 先記再動：`unlink` 之後才 append 的話，中間拋例外就永久失去
            # 「那條連結本來指著哪裡」——而那正是出事時唯一想知道的事。
            done.append(f"重指 {dest}（原本 {os.readlink(dest)}）-> {target}")
            dest.unlink()
        elif dest.exists():
            done.append(f"跳過 {dest}：那裡是真的檔案或目錄，不是連結。"
                        "agent-bridge 不刪你放的東西，要換的話自己移開再跑一次")
            continue
        else:
            done.append(f"建立 {dest} -> {target}")
        os.symlink(target, dest, target_is_directory=True)
    return done
