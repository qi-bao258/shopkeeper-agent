#!/usr/bin/env python3
"""修复 pnpm 在项目迁移后退化为空目录的符号链接。

背景
----
pnpm 用 junction/symlink 组织 node_modules。项目跨磁盘或跨路径移动后，
这些链接会退化为**空目录**（Windows 复制不保留跨路径链接）。
症状：Vite 报 `MODULE_NOT_FOUND`，涉及 esbuild.exe、rollup 原生模块、
@vitejs/plugin-react、tailwindcss 等。

难点
----
pnpm 对超长目录名做截断：`@babel/plugin-transform-react-jsx-self` 会变成
`@babel+plugin-transform-rea_<hash>`。因此不能只做精确名匹配。

用法
----
    python scripts/repair-node-links.py            # 修复并报告
    python scripts/repair-node-links.py --dry-run  # 只报告不修改
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import _winapi  # Windows
except ImportError:  # pragma: no cover
    _winapi = None

SKIP_DIRS = {".vite-temp", ".vite", ".cache"}


def parse_entity(dirname: str) -> tuple[str, bool]:
    """从 .pnpm 目录名解析 (包名, 是否被截断)。

    目录名格式（pnpm 9/10）::

        <pkg-with-plus-for-slash>@<version>[_<peer-suffix-or-hash>...]

    坑
    --
    **包名本身可能含 `_`**，如 `@types/babel__generator`、`lodash._baseclone`。
    因此绝不能写成 `dirname.split("_")[0]` —— 那会把
    `@types+babel__generator@7.27.0` 解析成 `@types/babel`，
    导致四个不同的 `@types/babel__*` 包全部挤在同一个索引键下，
    既查不到（键名错），又可能互相指错。

    正确做法：**从右往左**找「版本号」的起点，即最后一个满足
    「其后为 `<数字>...` 或纯 hash」的 `_` 分隔符。
    """
    # 1) 先在第一个 '@' 处切开包名与版本段（scoped 包要跳过首个 '@'）
    start = 1 if dirname.startswith("@") else 0
    at = dirname.find("@", start)
    if at == -1:
        # 没有版本段：形如 `@babel+plugin-transform-rea_<hash>` 的截断实体。
        # 末尾的 `_<hex>` 是 pnpm 追加的 hash，必须剥离后才能当包名前缀用。
        pkg_part = _strip_hash_suffix(dirname)
        return pkg_part.replace("+", "/"), True

    pkg_part = dirname[:at]
    rest = dirname[at + 1:]        # 形如 "7.27.0" 或 "6.4.2_@types+node@22.19.17_jiti@1.21.7"
    pkg = pkg_part.replace("+", "/")

    # 2) 版本段的第一个 token 是否是合法 semver 前缀（数字开头）
    first = rest.split("_")[0]
    looks_like_version = bool(first) and first[0].isdigit()

    if looks_like_version:
        # 版本号合法 → 包名完整；若还有 '_' 后缀，说明是 peer 后缀或截断 hash
        truncated = "_" in rest
        return pkg, truncated

    # 3) 版本段不是数字开头 → 这个实体的目录名被截断了：
    #    pnpm 把 `<pkg>@<version>` 整体截断后追加 `_<hash>`，
    #    所以这里 pkg_part 只是真实包名的前缀。
    return pkg, True


# pnpm 截断实体末尾追加的 hash：`_` + 32 位十六进制（偶尔更短）
_HASH_SUFFIX = re.compile(r"_[0-9a-f]{16,}$")


def _strip_hash_suffix(name: str) -> str:
    """剥离 pnpm 追加的 `_<hex-hash>` 后缀。"""
    return _HASH_SUFFIX.sub("", name)


class Resolver:
    def __init__(self, pnpm: Path):
        self.pnpm = pnpm
        self.exact: dict[str, list[str]] = {}
        self.trunc: dict[str, list[str]] = {}
        for d in pnpm.iterdir():
            if not d.is_dir():
                continue
            try:
                pkg, truncated = parse_entity(d.name)
            except Exception:
                continue
            (self.trunc if truncated else self.exact).setdefault(pkg, []).append(d.name)

    def find(self, pkg: str) -> Path | None:
        """定位包 pkg 的实体目录（内含非空内容）。"""
        # 1) 精确名匹配：两个索引都查。
        #    注意：带 peer 后缀的实体（如 vite@6.4.2_jiti@1.21.7）会被归入 trunc，
        #    但它的包名其实完全等于目标名，必须能在这一步命中。
        for idx in (self.exact, self.trunc):
            for name in idx.get(pkg, []):
                t = self.pnpm / name / "node_modules" / pkg
                if t.is_dir() and _nonempty(t):
                    return t

        # 2) 截断实体前缀匹配：实体包名必须是目标包名的「真前缀」。
        #    必须严格比对 scope，否则 @tailwindcss/vite 会误配到 vite 的实体。
        scope, _, short = pkg.rpartition("/")
        want_scope, want_name = scope, (short if scope else pkg)
        for epkg, names in self.trunc.items():
            if epkg == pkg:
                continue  # 已在第 1 步查过
            e_scope, _, e_short = epkg.rpartition("/")
            e_scope = e_scope or ""
            e_name = e_short if e_scope else epkg
            if e_scope != want_scope:
                continue
            # 长度门槛：无 scope 的包名放宽到 3（如 vite / fdir 本身就很短），
            # 有 scope 的保持 6，避免误配。
            min_len = 3 if not want_scope else 6
            if len(e_name) < min_len:
                continue
            if not want_name.startswith(e_name):
                continue
            for name in names:
                t = self.pnpm / name / "node_modules" / pkg
                if t.is_dir() and _nonempty(t):
                    return t
        return None


def _nonempty(p: Path) -> bool:
    try:
        return any(p.iterdir())
    except OSError:
        return False


def make_junction(target: Path, link: Path) -> str:
    if _winapi is not None:
        _winapi.CreateJunction(str(target), str(link))
        return "junction"
    os.symlink(str(target), str(link), target_is_directory=True)
    return "symlink"


def repair(frontend: Path, dry_run: bool = False) -> dict:
    nm = frontend / "node_modules"
    pnpm = nm / ".pnpm"
    if not pnpm.is_dir():
        raise SystemExit(f"未找到 pnpm 仓库目录：{pnpm}")

    resolver = Resolver(pnpm)
    report: dict = {"fixed": [], "skipped": [], "failed": [], "unresolved": []}

    empties: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(pnpm):
        d = Path(dirpath)
        if d == pnpm:
            dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
            continue
        if not dirnames and not filenames:
            empties.append(d)

    # 深度大的优先，避免父目录被当作空目录处理
    empties.sort(key=lambda p: len(p.parts), reverse=True)

    for d in empties:
        rel = d.relative_to(pnpm)
        parts = rel.parts

        # 实体自身的包目录：.pnpm/<entity>/node_modules/<entity-pkg>
        #   例：.pnpm/vite@6.4.2_jiti@1.21.7/node_modules/vite   ← 不可动
        # 但同一层也可能是嵌套依赖链接：
        #   例：.pnpm/vite@6.4.2_jiti@1.21.7/node_modules/picomatch  ← 需要修
        # 因此必须比对「目录名是否等于实体自身的包名」，不能只看层级。
        if len(parts) == 3 and parts[1] == "node_modules":
            entity_dir = parts[0]
            try:
                entity_pkg, _ = parse_entity(entity_dir)
            except Exception:
                entity_pkg = None
            if entity_pkg is not None and parts[2] == entity_pkg:
                report["skipped"].append({"path": str(rel), "reason": "entity-itself"})
                continue
            # scoped 实体的自身目录是两层：@scope/name
        if len(parts) == 4 and parts[1] == "node_modules":
            entity_dir = parts[0]
            try:
                entity_pkg, _ = parse_entity(entity_dir)
            except Exception:
                entity_pkg = None
            if entity_pkg is not None and f"{parts[2]}/{parts[3]}" == entity_pkg:
                report["skipped"].append({"path": str(rel), "reason": "entity-itself"})
                continue

        pkg = None
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] == "node_modules" and i + 1 < len(parts):
                pkg = parts[i + 1]
                if pkg.startswith("@") and i + 2 < len(parts):
                    pkg = pkg + "/" + parts[i + 2]
                break
        if not pkg:
            report["skipped"].append({"path": str(rel), "reason": "no-pkg"})
            continue

        target = resolver.find(pkg)
        if target is None:
            report["unresolved"].append({"path": str(rel), "pkg": pkg})
            continue
        if target == d:
            report["skipped"].append({"path": str(rel), "reason": "self"})
            continue
        if dry_run:
            report["fixed"].append({"path": str(rel), "pkg": pkg, "dry_run": True})
            continue
        try:
            os.rmdir(d)  # 只删单个空目录，避免触发批量删除保护
            d.parent.mkdir(parents=True, exist_ok=True)
            how = make_junction(target, d)
            report["fixed"].append({"path": str(rel), "pkg": pkg, "how": how})
        except Exception as e:
            report["failed"].append({"path": str(rel), "pkg": pkg, "error": repr(e)})

    # 顶层 scope 内的空链接
    for scope in sorted(p for p in nm.iterdir() if p.is_dir() and p.name.startswith("@")):
        for child in sorted(scope.iterdir()):
            if child.is_dir() and not _nonempty(child):
                full = f"{scope.name}/{child.name}"
                target = resolver.find(full)
                if target is None:
                    report["unresolved"].append({"path": full, "pkg": full})
                    continue
                if dry_run:
                    report["fixed"].append({"path": full, "pkg": full, "dry_run": True})
                    continue
                try:
                    os.rmdir(child)
                    child.parent.mkdir(parents=True, exist_ok=True)
                    how = make_junction(target, child)
                    report["fixed"].append({"path": full, "pkg": full, "how": how})
                except Exception as e:
                    report["failed"].append({"path": full, "pkg": full, "error": repr(e)})

    # 顶层无 scope 的直接依赖：pnpm 把 <pkg> 放在 node_modules 根下，
    # 迁移后同样会退化为空目录（如 vite / react / tailwindcss）。
    for entry in sorted(nm.iterdir()):
        if not entry.is_dir() or entry.name.startswith("@"):
            continue
        if entry.name == "node_modules" or entry.name.startswith("."):
            continue
        if _nonempty(entry):
            continue
        pkg = entry.name
        target = resolver.find(pkg)
        if target is None:
            report["unresolved"].append({"path": pkg, "pkg": pkg})
            continue
        if dry_run:
            report["fixed"].append({"path": pkg, "pkg": pkg, "dry_run": True})
            continue
        try:
            os.rmdir(entry)
            how = make_junction(target, entry)
            report["fixed"].append({"path": pkg, "pkg": pkg, "how": how})
        except Exception as e:
            report["failed"].append({"path": pkg, "pkg": pkg, "error": repr(e)})

    return report


def verify(frontend: Path) -> dict:
    """校验关键链路是否真的可用。

    注意：不要硬编码版本号。不同项目的 esbuild / rollup / vite 版本不同，
    因此这里一律按「通配 + 取任一有效实体」的方式检查。
    """
    nm = frontend / "node_modules"
    pnpm = nm / ".pnpm"
    out = {}

    # vite 入口（顶层链接）
    out["vite/bin/vite.js"] = (nm / "vite" / "bin" / "vite.js").is_file()

    # esbuild 平台二进制：任意版本
    eb_found = None
    for d in pnpm.glob("@esbuild+win32-x64@*"):
        exe = d / "node_modules" / "@esbuild" / "win32-x64" / "esbuild.exe"
        if exe.is_file():
            eb_found = d.name
            break
    out["esbuild.exe"] = eb_found is not None

    # rollup 原生模块：任意版本
    for d in pnpm.glob("rollup@*"):
        n = d / "node_modules" / "@rollup"
        if n.is_dir():
            for sub in n.iterdir():
                if sub.name.startswith("rollup-win32"):
                    out[f"rollup_native:{sub.name}"] = _nonempty(sub)

    # 顶层直接依赖：读 package.json 核对 name 是否与目录名一致（防指错实体）
    pj_file = frontend / "package.json"
    if pj_file.is_file():
        try:
            pj = json.loads(pj_file.read_text(encoding="utf-8"))
        except Exception:
            pj = {}
        deps = list(pj.get("dependencies", {})) + list(pj.get("devDependencies", {}))
        bad = []
        for dep in sorted(deps):
            link = nm / dep
            pj2 = link / "package.json"
            if not pj2.is_file():
                bad.append(f"{dep}(missing)")
                continue
            try:
                actual = json.loads(pj2.read_text(encoding="utf-8")).get("name")
            except Exception:
                bad.append(f"{dep}(bad-json)")
                continue
            if actual != dep:
                bad.append(f"{dep}->{actual}")
        out["direct_deps_ok"] = len(bad) == 0
        if bad:
            out["direct_deps_bad"] = bad
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 pnpm 空目录链接")
    ap.add_argument("--frontend", default=None,
                    help="前端目录（默认：本脚本上一级的 frontend）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不修改")
    ap.add_argument("--json", default=None, help="把报告写入指定 JSON 文件")
    args = ap.parse_args()

    frontend = Path(args.frontend).resolve() if args.frontend else (
        Path(__file__).resolve().parent.parent / "frontend"
    )
    if not frontend.is_dir():
        print(f"[ERR] 前端目录不存在：{frontend}", file=sys.stderr)
        return 2

    print(f"前端目录：{frontend}")
    report = repair(frontend, dry_run=args.dry_run)
    print(f"  已修复 : {len(report['fixed'])}")
    print(f"  已跳过 : {len(report['skipped'])}")
    print(f"  无法解析: {len(report['unresolved'])}")
    print(f"  失败   : {len(report['failed'])}")

    v = verify(frontend)
    print("  校验：")
    for k, ok in v.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {k}")

    if report["failed"]:
        print("\n失败明细：", file=sys.stderr)
        for f in report["failed"][:20]:
            print(f"  {f}", file=sys.stderr)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"report": report, "verify": v}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
